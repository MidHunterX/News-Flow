from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
from fastapi import HTTPException


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
