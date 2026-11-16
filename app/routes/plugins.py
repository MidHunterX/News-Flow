"""Routes for browsing, previewing, and downloading the ``plugins/`` folder.

The plugins directory holds the tooling that lives outside the scraping
pipeline (the InDesign ExtendScript, mostly). This router exposes it
read-only, under ``/plugins``:

* ``GET /plugins?path=<folder>`` — the directory listing (and the page's
  entry point);
* ``GET /plugins/view?path=<file>`` — a text preview of one file;
* ``GET /plugins/download?path=<file|folder>`` — the file as an
  attachment, or a whole folder zipped.

Every request path is resolved against ``PLUGINS_DIR`` before anything
is read or served, so ``..``, absolute paths and symlinks pointing out of
the folder all 404 instead of escaping it. Nothing here writes to disk:
this is a viewer, not a file manager.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
)
from fastapi.templating import Jinja2Templates

from app.routes.scrape import _toggle_context

router = APIRouter()
templates = Jinja2Templates(directory="templates")

# The project's plugins folder. Resolved from this file (not the working
# directory) because it doubles as the containment boundary for requests:
# anything that resolves outside it is refused.
PLUGINS_DIR = Path(__file__).resolve().parents[2] / "plugins"

# Files bigger than this are download-only: pulling them into the preview
# page would build a useless (and huge) HTML document.
MAX_PREVIEW_BYTES = 512 * 1024


def _root() -> Path:
    """The plugins folder, resolved — also the containment boundary."""
    return PLUGINS_DIR.resolve()


def _resolve(rel: str | None) -> Path:
    """Map a request's relative ``path`` onto a file inside plugins/.

    Resolution happens *before* the containment check, so ``..`` segments,
    absolute paths and outward-pointing symlinks all land outside the root
    and are refused with a 404 rather than served.
    """
    root = _root()
    try:
        target = (root / (rel or "").lstrip("/")).resolve()
    except (OSError, RuntimeError) as exc:  # symlink loop, vanished dir
        raise HTTPException(404, "Not found") from exc
    if not target.is_relative_to(root):
        raise HTTPException(404, "Not found")
    return target


def _existing(rel: str | None) -> Path:
    """:func:`_resolve` for a path that must already exist."""
    target = _resolve(rel)
    if not target.exists():
        raise HTTPException(404, "Not found")
    return target


def _href(base: str, rel: str) -> str:
    """URL for *base* plus a ``path`` query (omitted at the plugins root)."""
    return base if not rel else f"{base}?path={quote(rel)}"


def _human_size(size: int) -> str:
    """Byte count as the short string the listing shows."""
    if size < 1024:
        return f"{size} B"
    value = size / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _stamp(timestamp: float) -> str:
    """Format a POSIX timestamp the way every listing column expects."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _entries(directory: Path) -> list[dict]:
    """Listing rows for *directory*: folders first, then files, A→Z.

    Each row carries the hrefs the template links to, so the page never
    builds a query string itself. Rows that can't be stat'ed (a file
    vanishing mid-request, a broken symlink) are skipped instead of
    failing the whole listing.
    """
    root = _root()
    rows = []
    for child in directory.iterdir():
        try:
            stat = child.stat()
        except OSError:
            continue
        is_dir = child.is_dir()
        rel = child.relative_to(root).as_posix()
        rows.append(
            {
                "name": child.name,
                "is_dir": is_dir,
                "size": "" if is_dir else _human_size(stat.st_size),
                "modified": _stamp(stat.st_mtime),
                # Folders navigate; files open their preview.
                "href": _href("/plugins" if is_dir else "/plugins/view", rel),
                "download_href": _href("/plugins/download", rel),
            }
        )
    rows.sort(key=lambda row: (not row["is_dir"], row["name"].lower()))
    return rows


def _crumbs(rel: str) -> list[dict]:
    """Breadcrumb trail from the plugins root down to *rel*."""
    crumbs = [{"label": "plugins", "href": "/plugins"}]
    parts = [part for part in rel.split("/") if part]
    for index, part in enumerate(parts):
        crumbs.append(
            {"label": part, "href": _href("/plugins", "/".join(parts[: index + 1]))}
        )
    return crumbs


def _file_context(target: Path) -> dict:
    """Preview data for one file: metadata plus text, when safe to show.

    Undecodable bytes and files past ``MAX_PREVIEW_BYTES`` come back with
    ``content`` None and a ``note`` instead — the page still offers the
    download link.
    """
    stat = target.stat()
    content: str | None = None
    note: str | None = None
    if stat.st_size > MAX_PREVIEW_BYTES:
        note = "Too large to preview inline — download the file instead."
    else:
        try:
            raw = target.read_bytes()
        except OSError:
            note = "The file could not be read."
        else:
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                note = "Binary file — download it instead."
            else:
                if "\x00" in content:
                    content, note = None, "Binary file — download it instead."
    return {
        "name": target.name,
        "size": _human_size(stat.st_size),
        "modified": _stamp(stat.st_mtime),
        "content": content,
        "note": note,
    }


def _attachment(filename: str) -> str:
    """A Content-Disposition value that survives non-ASCII names."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


def _zip_directory(target: Path) -> bytes:
    """Zip *target* (folder included) and return the archive bytes.

    Plugins are scripts, so building the archive in memory is fine. File
    symlinks are skipped: requests only ever serve paths that resolve
    inside the plugins folder, and the zip must not leak anything a link
    might point at. An unreadable file is skipped rather than failing the
    whole download.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for child in sorted(target.rglob("*")):
            if child.is_symlink() or not child.is_file():
                continue
            try:
                archive.write(child, child.relative_to(target.parent).as_posix())
            except OSError:
                continue
    return buffer.getvalue()


@router.get("/plugins", response_class=HTMLResponse)
async def plugins_view(
    request: Request,
    path: str | None = Query(None, description="Folder inside plugins/ to list"),
):
    """List one folder of the plugins directory.

    The folder's entries come back as dirs-first, A→Z rows (name, size,
    modified, links to open/preview and download). ``path`` is relative to
    the plugins root; a path that names a file is redirected to its
    preview, so a hand-typed ``/plugins?path=<file>`` still works. Every
    folder row links to its own zip download via ``download_href``.
    """
    target = _resolve(path)
    if not target.exists():
        raise HTTPException(404, "Not found")
    root = _root()
    rel = "" if target == root else target.relative_to(root).as_posix()

    if target.is_file():
        return RedirectResponse(_href("/plugins/view", rel), status_code=307)
    if not target.is_dir():
        raise HTTPException(404, "Not found")

    entries = _entries(target)
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return templates.TemplateResponse(
        request=request,
        name="plugins.html",
        context={
            "request": request,
            "active_nav": "plugins",
            "file": None,
            "current_path": rel,
            "crumbs": _crumbs(rel),
            "entries": entries,
            "count": len(entries),
            "download_href": _href("/plugins/download", rel),
            "parent_href": _href("/plugins", parent) if rel else None,
            **await _toggle_context(),
        },
    )


@router.get("/plugins/view", response_class=HTMLResponse)
async def plugin_file_view(
    request: Request,
    path: str = Query(..., description="File inside plugins/ to preview"),
):
    """Preview one plugins file: its metadata and, when it is text, body.

    Binary files and files beyond ``MAX_PREVIEW_BYTES`` render a note in
    place of their content — the download button next to the page still
    serves the real bytes.
    """
    target = _existing(path)
    if not target.is_file():
        raise HTTPException(404, "Not found")

    rel = target.relative_to(_root()).as_posix()
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    return templates.TemplateResponse(
        request=request,
        name="plugins.html",
        context={
            "request": request,
            "active_nav": "plugins",
            "file": _file_context(target),
            "current_path": rel,
            "crumbs": [*_crumbs(parent), {"label": target.name, "href": None}],
            "entries": [],
            "count": 0,
            "download_href": _href("/plugins/download", rel),
            "parent_href": _href("/plugins", parent),
            **await _toggle_context(),
        },
    )


@router.get("/plugins/download")
async def plugin_download(
    path: str | None = Query(
        None, description="File or folder inside plugins/ to download"
    ),
):
    """Download a plugins file, or a folder as a zip archive.

    A folder is archived under its own name (``plugins/…`` when the root
    is requested), so the zip extracts into a sensibly named directory.
    Files are served as attachments, with their original name.
    """
    target = _existing(path)
    if target.is_dir():
        return Response(
            content=_zip_directory(target),
            media_type="application/zip",
            headers={"Content-Disposition": _attachment(f"{target.name}.zip")},
        )
    if not target.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(
        target, filename=target.name, content_disposition_type="attachment"
    )
