"""Market-data tools: CoinGecko (crypto) and AlphaVantage (equities)."""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from dotenv import load_dotenv
from langchain_core.tools import tool

from .cache import disk_cache

load_dotenv()

logger = logging.getLogger(__name__)

COINGECKO_ENDPOINT = "https://api.coingecko.com/api/v3/coins/markets"
ALPHAVANTAGE_ENDPOINT = "https://www.alphavantage.co/query"


@tool
@disk_cache(ttl_hours=1)
def get_coin_market_snapshot(coin_ids: list[str]) -> dict[str, Any]:
    """Get a current market snapshot for one or more cryptocurrencies.

    Use this tool to fetch live price and volume data for cryptocurrencies
    from CoinGecko's public API (free, no API key required). Coin ids must
    be CoinGecko's own id strings, not ticker symbols (e.g. "bitcoin" not
    "BTC", "ethereum" not "ETH"). Use this for crypto assets only; for
    equities/stocks (e.g. "COIN", "MSTR") use `get_stock_quote` instead.

    Args:
        coin_ids: List of CoinGecko coin ids, e.g. ["bitcoin", "ethereum"].

    Returns:
        A dict with:
        - "coins": list of dicts, each with "id", "symbol", "name",
          "current_price" (USD), "price_change_percentage_24h", and
          "total_volume" (USD).
        - "count": number of coins returned.
        On failure (invalid ids, network error, rate limit, or empty
        response), returns a dict with an "error" key instead of raising.
    """
    if not coin_ids:
        return {"error": "coin_ids must be a non-empty list of CoinGecko coin ids."}

    params = {
        "vs_currency": "usd",
        "ids": ",".join(coin_ids),
        "price_change_percentage": "24h",
    }

    try:
        response = requests.get(COINGECKO_ENDPOINT, params=params, timeout=15)
    except requests.RequestException as exc:
        return {"error": f"Network error calling CoinGecko: {exc}"}

    if response.status_code == 429:
        return {"error": "CoinGecko rate limit exceeded (status 429)."}

    if response.status_code != 200:
        return {
            "error": f"CoinGecko returned unexpected status {response.status_code}: {response.text[:200]}"
        }

    try:
        data = response.json()
    except ValueError as exc:
        return {"error": f"Failed to parse CoinGecko response as JSON: {exc}"}

    if not data:
        return {
            "error": (
                f"No market data found for coin_ids: {coin_ids!r}. "
                "Check that the ids are valid CoinGecko ids."
            )
        }

    coins = [
        {
            "id": item.get("id", ""),
            "symbol": item.get("symbol", ""),
            "name": item.get("name", ""),
            "current_price": item.get("current_price"),
            "price_change_percentage_24h": item.get("price_change_percentage_24h"),
            "total_volume": item.get("total_volume"),
        }
        for item in data
    ]

    return {"coins": coins, "count": len(coins)}


@tool
@disk_cache(ttl_hours=24)
def get_stock_quote(symbol: str) -> dict[str, Any]:
    """Get the latest quote for a publicly traded equity (stock).

    Use this tool for stocks and equities only (e.g. "COIN" for Coinbase
    Global, "MSTR" for MicroStrategy) - NOT for cryptocurrencies themselves;
    use `get_coin_market_snapshot` for crypto assets like BTC or ETH.
    Backed by AlphaVantage's GLOBAL_QUOTE endpoint, which is limited to 25
    requests/day on the free tier, so results are cached for 24 hours.
    Requires the ALPHAVANTAGE_KEY environment variable to be set.

    Args:
        symbol: The equity ticker symbol, e.g. "COIN", "MSTR", "AAPL".

    Returns:
        A dict with "symbol", "price", "change", "change_percent", and
        "latest_trading_day". On failure (missing/invalid API key, invalid
        symbol, rate limit, or network error), returns a dict with an
        "error" key instead of raising.
    """
    api_key = os.environ.get("ALPHAVANTAGE_KEY")
    if not api_key:
        return {"error": "ALPHAVANTAGE_KEY environment variable is not set."}

    params = {
        "function": "GLOBAL_QUOTE",
        "symbol": symbol,
        "apikey": api_key,
    }

    try:
        response = requests.get(ALPHAVANTAGE_ENDPOINT, params=params, timeout=15)
    except requests.RequestException as exc:
        return {"error": f"Network error calling AlphaVantage: {exc}"}

    if response.status_code != 200:
        return {
            "error": f"AlphaVantage returned unexpected status {response.status_code}: {response.text[:200]}"
        }

    try:
        data = response.json()
    except ValueError as exc:
        return {"error": f"Failed to parse AlphaVantage response as JSON: {exc}"}

    # AlphaVantage always returns HTTP 200, even for auth failures and rate
    # limits - it signals these via "Information"/"Note"/"Error Message"
    # keys in the JSON body instead of the HTTP status code.
    info_message = data.get("Information") or data.get("Note") or data.get("Error Message")
    if info_message:
        if "apikey" in info_message.lower() or "api key" in info_message.lower():
            logger.error(
                "AlphaVantage authentication failed - check your ALPHAVANTAGE_KEY. "
                "Response: %s",
                info_message,
            )
            return {
                "error": (
                    "AlphaVantage authentication failed. Check your "
                    f"ALPHAVANTAGE_KEY. Details: {info_message}"
                )
            }
        return {"error": f"AlphaVantage error: {info_message}"}

    quote = data.get("Global Quote")
    if not quote:
        return {"error": f"No quote data found for symbol: {symbol!r}"}

    try:
        return {
            "symbol": quote.get("01. symbol", symbol),
            "price": float(quote["05. price"]),
            "change": float(quote["09. change"]),
            "change_percent": quote.get("10. change percent", ""),
            "latest_trading_day": quote.get("07. latest trading day", ""),
        }
    except (KeyError, ValueError) as exc:
        return {"error": f"Unexpected AlphaVantage response format: {exc}"}
