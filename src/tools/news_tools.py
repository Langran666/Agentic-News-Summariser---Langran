"""News-fetching tools: RSS feeds and SerpAPI Google News search."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import feedparser
import requests
from dotenv import load_dotenv
from langchain_core.tools import tool

from .cache import disk_cache

load_dotenv()

logger = logging.getLogger(__name__)

DEFAULT_RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
]

SERPAPI_ENDPOINT = "https://serpapi.com/search"


def _entry_published_at(entry: Any) -> Optional[datetime]:
    time_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if time_struct is None:
        return None
    return datetime(*time_struct[:6], tzinfo=timezone.utc)


def _serpapi_published_at(raw_date: Optional[str]) -> Optional[str]:
    if not raw_date:
        return None
    try:
        from dateutil import parser as date_parser

        return date_parser.parse(raw_date, fuzzy=True).astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, ImportError):
        return None


@tool
@disk_cache(ttl_hours=6)
def fetch_rss_news(
    feeds: Optional[list[str]] = None, hours_back: int = 24
) -> dict[str, Any]:
    """Fetch recent crypto news articles from RSS feeds.

    Use this tool to pull recent articles from crypto news RSS feeds (e.g.
    CoinDesk, Decrypt). If no feeds are given, it defaults to CoinDesk and
    Decrypt's main RSS feeds. Only articles published within `hours_back`
    hours are returned. This is a free, low-cost way to sweep recent
    headlines - prefer it over `search_crypto_news` for general "what's
    new" queries, and reserve `search_crypto_news` for targeted searches
    about a specific topic, coin, or event.

    Args:
        feeds: List of RSS feed URLs to fetch. Defaults to CoinDesk and
            Decrypt feeds if not provided.
        hours_back: Only include articles published within this many hours
            of now. Defaults to 24.

    Returns:
        A dict with:
        - "articles": list of dicts, each with "title", "url",
          "published_at" (ISO 8601 string or null if unavailable),
          "source", and "summary_raw".
        - "count": number of articles returned.
        On failure (e.g. all feeds unreachable or malformed), returns a
        dict with an "error" key describing the problem instead of
        raising.
    """
    feed_urls = feeds if feeds else list(DEFAULT_RSS_FEEDS)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)

    articles: list[dict[str, Any]] = []
    errors: list[str] = []

    for feed_url in feed_urls:
        try:
            parsed = feedparser.parse(feed_url)
            if parsed.bozo and not parsed.entries:
                errors.append(f"{feed_url}: {parsed.bozo_exception}")
                continue
            source = parsed.feed.get("title", feed_url)
            for entry in parsed.entries:
                published_at = _entry_published_at(entry)
                if published_at is not None and published_at < cutoff:
                    continue
                articles.append(
                    {
                        "title": entry.get("title", ""),
                        "url": entry.get("link", ""),
                        "published_at": published_at.isoformat() if published_at else None,
                        "source": source,
                        "summary_raw": entry.get("summary", ""),
                    }
                )
        except Exception as exc:
            logger.warning("Failed to fetch/parse feed %s: %s", feed_url, exc)
            errors.append(f"{feed_url}: {exc}")

    if not articles and errors:
        return {"error": f"Failed to fetch any RSS articles: {'; '.join(errors)}"}

    return {"articles": articles, "count": len(articles)}


@tool
@disk_cache(ttl_hours=6)
def search_crypto_news(query: str, num_results: int = 10) -> dict[str, Any]:
    """Search recent news articles via Google News (through SerpAPI).

    Use this tool for targeted searches about a specific coin, protocol,
    person, or event (e.g. "SEC ETF approval", "Ethereum Dencun upgrade")
    that may not be covered by the default RSS feeds. Requires the
    SERPAPI_KEY environment variable to be set. SerpAPI's free tier is
    limited to 100 searches/month, so use this tool sparingly and prefer
    `fetch_rss_news` for broad, general sweeps of recent headlines.

    Args:
        query: The search query string, e.g. "Bitcoin ETF approval".
        num_results: Maximum number of articles to return. Defaults to 10.

    Returns:
        A dict with:
        - "articles": list of dicts, each with "title", "url",
          "published_at" (ISO 8601 string, or null if it could not be
          parsed), "source", "summary_raw".
        - "count": number of articles returned.
        On failure (missing/invalid API key, network error, rate limit, or
        no results), returns a dict with an "error" key instead of
        raising.
    """
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        return {"error": "SERPAPI_KEY environment variable is not set."}

    params = {
        "engine": "google_news",
        "q": query,
        "api_key": api_key,
    }

    try:
        response = requests.get(SERPAPI_ENDPOINT, params=params, timeout=15)
    except requests.RequestException as exc:
        return {"error": f"Network error calling SerpAPI: {exc}"}

    if response.status_code in (401, 403):
        logger.error(
            "SerpAPI authentication failed (status %s). Check your SERPAPI_KEY "
            "- not retrying, to avoid burning quota against a dead key.",
            response.status_code,
        )
        return {
            "error": (
                f"SerpAPI authentication failed (status {response.status_code}). "
                "Check your SERPAPI_KEY."
            )
        }

    if response.status_code == 429:
        return {"error": "SerpAPI rate limit exceeded (status 429)."}

    if response.status_code != 200:
        return {
            "error": f"SerpAPI returned unexpected status {response.status_code}: {response.text[:200]}"
        }

    try:
        data = response.json()
    except ValueError as exc:
        return {"error": f"Failed to parse SerpAPI response as JSON: {exc}"}

    news_results = data.get("news_results", [])
    if not news_results:
        return {"error": f"No news results found for query: {query!r}"}

    articles = [
        {
            "title": item.get("title", ""),
            "url": item.get("link", ""),
            "published_at": _serpapi_published_at(item.get("date")),
            "source": (item.get("source") or {}).get("name", ""),
            "summary_raw": item.get("snippet", ""),
        }
        for item in news_results[:num_results]
    ]

    return {"articles": articles, "count": len(articles)}
