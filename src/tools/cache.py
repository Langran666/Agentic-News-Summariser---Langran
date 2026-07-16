"""Generic disk-based caching layer for API-backed tool functions.

Used to stay under the free-tier rate limits of external APIs (e.g. SerpAPI's
100 searches/month, AlphaVantage's 25 requests/day) during development.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"

F = TypeVar("F", bound=Callable[..., Any])


def _make_cache_key(func_name: str, args: tuple, kwargs: dict) -> str:
    """Build a stable cache key from the function name and its arguments."""
    try:
        payload = json.dumps(
            {"args": args, "kwargs": kwargs}, sort_keys=True, default=str
        )
    except TypeError:
        payload = repr((args, kwargs))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{func_name}_{digest}"


def _cache_path(cache_key: str) -> Path:
    return CACHE_DIR / f"{cache_key}.json"


def disk_cache(ttl_hours: float) -> Callable[[F], F]:
    """Cache a function's JSON-serialisable return value to disk.

    The cache key is derived from the wrapped function's name plus a hash of
    its call arguments, so different arguments produce different cache
    entries. Cached entries expire after `ttl_hours` and are then refetched
    from the underlying (real) function.

    Results that are a dict containing an "error" key are treated as failed
    calls and are not cached, so a transient API failure doesn't get "stuck"
    for the full TTL.
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_key = _make_cache_key(func.__name__, args, kwargs)
            path = _cache_path(cache_key)

            if path.exists():
                try:
                    with path.open("r", encoding="utf-8") as f:
                        entry = json.load(f)
                    age_seconds = time.time() - entry["cached_at"]
                    if age_seconds < ttl_hours * 3600:
                        logger.info(
                            "Cache hit for %s (age=%.1fs, ttl=%.1fh)",
                            func.__name__,
                            age_seconds,
                            ttl_hours,
                        )
                        return entry["result"]
                    logger.info("Cache expired for %s", func.__name__)
                except (json.JSONDecodeError, KeyError, OSError) as exc:
                    logger.warning(
                        "Failed to read cache file %s (%s); refetching", path, exc
                    )

            logger.info("Cache miss for %s; calling underlying function", func.__name__)
            result = func(*args, **kwargs)

            is_error = isinstance(result, dict) and "error" in result
            if not is_error:
                entry = {"cached_at": time.time(), "ttl_hours": ttl_hours, "result": result}
                try:
                    with path.open("w", encoding="utf-8") as f:
                        json.dump(entry, f)
                except (TypeError, OSError) as exc:
                    logger.warning(
                        "Failed to write cache file %s (%s); result not cached", path, exc
                    )
            else:
                logger.info(
                    "Not caching error result for %s: %s", func.__name__, result.get("error")
                )

            return result

        return wrapper  # type: ignore[return-value]

    return decorator
