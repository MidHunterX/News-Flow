import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from fastapi import HTTPException

WORKSPACE_DIR = Path("workspace")
COVERS_DIR = WORKSPACE_DIR / "covers"


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


async def download_image(url: str, filename: str | None = None) -> str | None:
    """Download an image and save it to workspace/covers/. Returns the file path.

    If *filename* is given the image is saved under that name (preserving the
    original extension).  Otherwise the basename of the URL path is used.
    """
    from app.client import get_client

    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = os.path.basename(urlparse(url).path)
    if not filename:
        return None
    # Preserve the original extension when a custom filename is supplied.
    if "." not in filename:
        orig_ext = os.path.splitext(urlparse(url).path)[1]
        if orig_ext:
            filename += orig_ext
    save_path = COVERS_DIR / filename
    client = await get_client()
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        save_path.write_bytes(resp.content)
        return str(save_path)
    except httpx.HTTPError:
        return None
