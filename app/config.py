import os
from pathlib import Path

import httpx


def _load_dotenv() -> None:
    """Seed os.environ from a .env file at the project root.

    Keys already present in the real environment win, so deployments can
    override the file. Values may be wrapped in single or double quotes;
    full-line comments and blank lines are ignored (no inline comments).
    """
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value[1:-1] if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'" else value
        os.environ.setdefault(key, value)


_load_dotenv()


def _env_str(name: str) -> str:
    return os.environ.get(name, "").strip()


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

# --- WordPress publishing (see .env.example) --------------------------------
# Completed articles are published to the WordPress REST API when these are
# set; otherwise completion happens without publishing.
WORDPRESS_URL = _env_str("WORDPRESS_URL").rstrip("/")
WORDPRESS_USERNAME = _env_str("WORDPRESS_USERNAME")
WORDPRESS_APP_PASSWORD = _env_str("WORDPRESS_APP_PASSWORD")
# Optional category ID applied to every published post (empty = site default).
WORDPRESS_CATEGORY_ID = _env_str("WORDPRESS_CATEGORY_ID")

# --- Google Gemini (auto-categorization) -------------------------------------
# When an API key is set, accepted articles are sent to Gemini along with the
# site's categories/tags and the response marks the categories on the article
# before publishing. Get a key at https://aistudio.google.com/apikey
GEMINI_API_KEY = _env_str("GEMINI_API_KEY")
GEMINI_MODEL = _env_str("GEMINI_MODEL") or "gemini-2.5-flash"
