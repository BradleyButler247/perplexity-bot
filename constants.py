"""
constants.py
------------
Shared constants and utilities used across multiple modules.

Centralises values that were previously duplicated in 4+ files.
"""

from typing import Dict, Optional, Tuple

# ── API endpoints ────────────────────────────────────────────────────────────

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# ── Fee constant ─────────────────────────────────────────────────────────────

POLYMARKET_FEE = 0.02  # 2% on winnings

# ── City coordinates (used by news_aggregator + weather_forecast_arb) ────────

CITY_COORDS: Dict[str, Tuple[float, float]] = {
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
    "mumbai": (19.0760, 72.8777),
    "dubai": (25.2048, 55.2708),
    "singapore": (1.3521, 103.8198),
    "hong kong": (22.3193, 114.1694),
    "seoul": (37.5665, 126.9780),
}


def parse_timestamp(trade: dict) -> float:
    """
    Extract a Unix timestamp (seconds) from a trade/activity record.

    Handles multiple common field names and both seconds and milliseconds.
    Returns 0.0 if no timestamp can be extracted.
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
