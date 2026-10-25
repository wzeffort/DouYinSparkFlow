"""GitHub star/fork counts with a short in-process cache.

Unauthenticated GitHub API allows 60 requests/hour per IP; a 10-minute
cache keeps page traffic well under it while feeling live to visitors.
Fetch failures never break the page - callers hide the chip when None.
"""
import time

import httpx

_REPO_API = "https://api.github.com/repos/2061360308/DouYinSparkFlow"
_REPO_URL = "https://github.com/2061360308/DouYinSparkFlow"
_CACHE_TTL_SECONDS = 600
_cache = {"at": 0.0, "stars": None, "forks": None}


def repo_url() -> str:
    return _REPO_URL


def get_github_stats() -> dict:
    now = time.monotonic()
    if now - _cache["at"] < _CACHE_TTL_SECONDS:
        return {
            "stars": _cache["stars"],
            "forks": _cache["forks"],
            "url": _REPO_URL,
        }
    stars = forks = None
    try:
        response = httpx.get(
            _REPO_API,
            timeout=4,
            follow_redirects=False,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "spark-console",
            },
        )
        if response.status_code == 200:
            body = response.json()
            stars = body.get("stargazers_count")
            forks = body.get("forks_count")
    except Exception:
        pass
    if stars is not None:
        _cache.update({"at": now, "stars": stars, "forks": forks})
    return {"stars": stars, "forks": forks, "url": _REPO_URL}
