import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SOURCES = {
    "kaumudi": "https://keralakaumudi.com/latest",
    "mangalam": "https://www.mangalam.com/category/latest-news",
    "madhyamam": "https://www.madhyamam.com/latest-news",
}

TIMEOUT = httpx.Timeout(20.0)
