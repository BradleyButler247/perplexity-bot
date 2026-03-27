"""
utils.py
--------
Shared utilities for the Polymarket trading bot.

Centralises constants and helpers that were previously duplicated
across wallet_discovery.py, strategies/copy_trading.py,
news_aggregator.py, and strategies/weather_forecast_arb.py.
"""

from typing import Optional
import requests

# ── API constants ─────────────────────────────────────────────────────────────

DATA_API = "https://data-api.polymarket.com"

# Polymarket fee on winnings (2%)
POLYMARKET_FEE = 0.02

# ── City coordinates (merged from news_aggregator.py and weather_forecast_arb.py) ──

CITY_COORDS = {
    "new york": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "la": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "philadelphia": (39.9526, -75.1652),
    "san antonio": (29.4241, -98.4936),
    "san diego": (32.7157, -117.1611),
    "dallas": (32.7767, -96.7970),
    "miami": (25.7617, -80.1918),
    "atlanta": (33.7490, -84.3880),
    "boston": (42.3601, -71.0589),
    "seattle": (47.6062, -122.3321),
    "denver": (39.7392, -104.9903),
    "washington": (38.9072, -77.0369),
    "dc": (38.9072, -77.0369),
    "san francisco": (37.7749, -122.4194),
    "sf": (37.7749, -122.4194),
    "las vegas": (36.1699, -115.1398),
    "portland": (45.5152, -122.6784),
    "detroit": (42.3314, -83.0458),
    "minneapolis": (44.9778, -93.2650),
    "tampa": (27.9506, -82.4572),
    "orlando": (28.5383, -81.3792),
    "nashville": (36.1627, -86.7816),
    "austin": (30.2672, -97.7431),
    "columbus": (39.9612, -82.9988),
    "charlotte": (35.2271, -80.8431),
    "london": (51.5074, -0.1278),
    "paris": (48.8566, 2.3522),
    "tokyo": (35.6762, 139.6503),
    "berlin": (52.5200, 13.4050),
    "sydney": (-33.8688, 151.2093),
    "toronto": (43.6532, -79.3832),
    # Additional cities from news_aggregator.py city list
    "mumbai": (19.0760, 72.8777),
    "dubai": (25.2048, 55.2708),
    "singapore": (1.3521, 103.8198),
    "hong kong": (22.3193, 114.1694),
    "seoul": (37.5665, 126.9780),
}

# ── Category keywords (merged from copy_trading.py and news_aggregator.py) ────
# Uses plain keyword lists (copy_trading.py style).
# news_aggregator.py uses regex patterns separately — kept there.

CATEGORY_KEYWORDS = {
    "politics": [
        "president", "election", "congress", "senate", "vote", "poll",
        "democrat", "republican", "governor", "trump", "biden", "party",
        "legislation", "bill", "executive order", "impeach",
        "desantis", "harris", "newsom", "supreme court", "mayor",
    ],
    "sports": [
        "nba", "nfl", "mlb", "nhl", "mls", "premier league", "champions league",
        "win", "championship", "playoffs", "finals", "game", "match",
        "basketball", "football", "soccer", "baseball", "hockey", "tennis",
        "golf", "boxing", "mma", "ufc", "team", "player", "coach",
        "serie a", "la liga", "tournament", "score", "mvp", "draft",
    ],
    "crypto": [
        "bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "blockchain",
        "defi", "nft", "token", "halving", "etf", "binance", "coinbase",
        "sol", "sec", "all-time high", "ath",
    ],
    "finance": [
        "fed", "federal reserve", "interest rate", "inflation", "gdp",
        "recession", "stock", "s&p", "nasdaq", "dow", "earnings", "ipo",
        "treasury", "bond", "yield", "forex", "dollar", "market cap",
    ],
    "geopolitics": [
        "war", "conflict", "sanctions", "treaty", "nato", "united nations",
        "russia", "ukraine", "china", "taiwan", "iran", "israel",
        "diplomacy", "ceasefire", "invasion", "military", "tariff",
        "gaza", "north korea", "g7", "g20", "trade war",
    ],
    "culture": [
        "oscar", "grammy", "emmy", "award", "movie", "film", "album",
        "tv show", "netflix", "disney", "spotify", "twitter", "instagram",
        "tiktok", "youtube", "celebrity", "tony", "golden globe",
        "kardashian", "musk", "taylor swift", "drake",
    ],
    "weather": [
        "temperature", "weather", "rain", "snow", "hurricane", "tornado",
        "storm", "flood", "heat", "cold", "freeze", "drought", "forecast",
        "celsius", "fahrenheit", "wind", "hail", "blizzard", "wildfire",
        "noaa", "nws", "record high", "record low",
    ],
    "esports": [
        "league of legends", "lol", "cs2", "csgo", "dota", "valorant",
        "overwatch", "esports", "e-sports", "gaming", "twitch", "worlds",
    ],
}


def parse_timestamp(trade: dict) -> float:
    """
    Extract a Unix timestamp (seconds) from an activity record.

    Handles multiple common field names and both seconds and milliseconds.

    Args:
        trade: A dict representing a Polymarket trade/activity record.

    Returns:
        Unix timestamp in seconds, or 0.0 if not found.
    """
    for key in ("timestamp", "createdAt", "created_at", "time", "ts"):
        val = trade.get(key)
        if val:
            try:
                ts = float(val)
                # Milliseconds if value is larger than year 2100 in seconds
                if ts > 4_102_444_800:
                    ts /= 1000.0
                return ts
            except (TypeError, ValueError):
                continue
    return 0.0


def detect_market_category(question: str) -> str:
    """
    Detect the market category of a question by scoring it against CATEGORY_KEYWORDS.

    Args:
        question: The market question text.

    Returns:
        The best-matching category string, or "unknown" if no match.
    """
    q = question.lower()
    best_cat = "unknown"
    best_score = 0

    for category, keywords in CATEGORY_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in q)
        if score > best_score:
            best_score = score
            best_cat = category

    return best_cat


def create_http_session(user_agent: str = "PolymarketBot/1.0") -> requests.Session:
    """
    Create a requests.Session pre-configured with standard headers.

    Args:
        user_agent: The User-Agent header value.

    Returns:
        Configured requests.Session instance.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent,
        "Accept": "application/json",
    })
    return session
