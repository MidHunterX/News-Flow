import httpx
from app.config import TIMEOUT, USER_AGENT


# Singleton HTTP client
class HttpClient:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=TIMEOUT,
                follow_redirects=True,
            )
        return cls._instance


async def get_client():
    return HttpClient()
