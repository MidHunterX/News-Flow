import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SOURCES = {
    "kaumudi": "https://keralakaumudi.com/latest",
    "mangalam": "https://www.mangalam.com/category/latest-news",
    # It is ridden with pro articles, no tags etc.
    # "madhyamam": "https://www.madhyamam.com/latest-news",
}

TIMEOUT = httpx.Timeout(20.0)

# Maximum number of articles to keep per source in the database.
MAX_ARTICLES_PER_SOURCE = 10
