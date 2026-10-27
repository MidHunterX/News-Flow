import os
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

PUBLIC_DIR = Path("public")
ARTICLES_DIR = PUBLIC_DIR / "articles"
COVERS_DIR = PUBLIC_DIR / "covers"
# Publication workspace: the daily reset mirrors the curation workspace here
# (previous day's scraped-content .txt files + cover images) instead of just
# deleting it, so /publication can show yesterday's data.
PUBLICATION_DIR = PUBLIC_DIR / "publication"
PUBLICATION_ARTICLES_DIR = PUBLICATION_DIR / "articles"
PUBLICATION_COVERS_DIR = PUBLICATION_DIR / "covers"


def clean_text(value: str | None) -> str:
    return " ".join((value or "").split())


def resolve_image_url(src: str | None, base_url: str) -> str | None:
    if not src:
        return None
    if src.startswith("/_next/image"):
        query = urlparse(src).query
        encoded = parse_qs(query).get("url")
        if encoded:
            src = unquote(encoded[0])
    base_parsed = urlparse(base_url)
    origin = f"{base_parsed.scheme}://{base_parsed.netloc}"
    return urljoin(origin, src)


async def fetch_html(url: str) -> str:
    from app.client import get_client

    client = await get_client()
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Failed to fetch {url}: {exc}") from exc


def _convert_to_jpeg(data: bytes) -> bytes | None:
    """Re-encode *data* as JPEG bytes, or return it unchanged if it already is.

    Sources serve covers as png/webp/gif as often as jpg; storing them under
    one format (jpg) keeps every consumer — the article desk's clipboard
    copy, the WordPress publisher — on a format they all handle.

    Already-JPEG bytes come back untouched (only the extension changes);
    anything else is re-encoded at quality 85, with transparency composited
    onto white so the flat jpg background looks like the source rather than
    black.

    Returns None when Pillow can't decode the bytes at all — the caller then
    keeps the original file instead of losing the image.
    """
    try:
        with Image.open(BytesIO(data)) as img:
            if img.format == "JPEG":
                return data
            if img.mode != "RGB":
                # RGBA/LA/P (and anything else with an alpha channel) needs a
                # solid backdrop: JPEG has no transparency of its own.
                img = img.convert("RGBA")
                background = Image.new("RGB", img.size, (255, 255, 255))
                background.paste(img, mask=img.getchannel("A"))
                img = background
            out = BytesIO()
            img.save(out, format="JPEG", quality=85)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError):
        return None

def _unique_path(path: Path) -> Path:
    """Return *path* if it doesn't exist, else append ``_2``, ``_3``, …

    ``cover.jpg`` -> ``cover_2.jpg`` -> ``cover_3.jpg``. The suffix is
    inserted before the extension so the file stays a ``.jpg``.
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def _jpeg_path(directory: Path, filename: str) -> Path:
    """Target path for a cover: always ``.jpg``, whatever the source named it."""
    path = directory / filename
    return path.with_suffix(".jpg") if path.suffix else Path(f"{path}.jpg")


async def download_image(url: str, filename: str | None = None,
                          dest_dir: Path | None = None) -> str | None:
    """Download an image, convert it to jpg, and save it to workspace/covers/.

    Returns the saved file path — its name always ends in ``.jpg``, even when
    the source URL (or *filename*) said otherwise, because non-jpg bytes are
    re-encoded through Pillow (see :func:`_convert_to_jpeg`). If the bytes
    can't be decoded at all they are written as-is under the requested name
    rather than dropped.

    If *filename* is given the image is saved under that name (its extension
    is rewritten to ``.jpg``). Otherwise the basename of the URL path is used.
    *dest_dir* overrides the target directory — the publication workspace
    mirror downloads there instead of ``public/covers/``.
    """
    from app.client import get_client

    target_dir = dest_dir or COVERS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = os.path.basename(urlparse(url).path)
    if not filename:
        return None
    # Preserve the original extension when a custom filename is supplied.
    if "." not in filename:
        orig_ext = os.path.splitext(urlparse(url).path)[1]
        if orig_ext:
            filename += orig_ext
    client = await get_client()
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    data = resp.content
    save_path = target_dir / filename
    jpeg = _convert_to_jpeg(data)
    if jpeg is not None:
        # Decodable: store the jpeg and correct the name to match. Otherwise
        # keep the original bytes and name instead of dropping the image
        # (the HTTP fetch itself succeeded).
        data, save_path = jpeg, _jpeg_path(target_dir, filename)
    save_path = _unique_path(save_path)
    save_path.write_bytes(data)
    return str(save_path)
