"""
strategies/weather_forecast_arb.py
-----------------------------------
Weather forecast arbitrage strategy.

Compares NOAA/NWS (~85-94% accurate) and Open-Meteo forecast data against
Polymarket weather market prices to find mispricings.

Core insight: NOAA runs $6.5B in supercomputers while retail bettors price
weather contracts off vibes.  When federal science says 92% but Polymarket
says 50%, that's a pure edge.

Strategy loop (runs every scan cycle):
  1. Identify weather-related markets (temperature, precipitation, snow, etc.)
  2. Extract location + metric + date from the market question.
  3. Fetch NOAA/NWS point forecast and Open-Meteo 7-day data.
  4. Map the forecast to the correct Polymarket outcome bucket.
  5. Compare forecast-implied probability to market price.
  6. Buy when market price << forecast probability (edge > threshold).
  7. Sell at exit threshold or hold to resolution.

Data sources (all free, no API keys):
  - api.weather.gov (NOAA/NWS) — US locations, gold-standard accuracy
  - Open-Meteo — global, free, no key, good for non-US markets

Inspired by:
  - @xmayeth: Agent bought at 11¢, sold at 44¢ using NOAA vs Polymarket
  - @RoundtableSpace: Agent-01 pulls NOAA every 10 min, finds 92% vs 50% gaps
  - DevGenius: Weather bots making $24K with entry ≤15¢, exit ≥45¢
"""

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from constants import CITY_COORDS
from http_client import get_session
from strategies.base import BaseStrategy, TradeSignal
from market_scanner import MarketInfo, TokenInfo

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

# Entry: only buy shares priced at or below this (deep mispricings only)
MAX_ENTRY_PRICE = 0.40      # 40¢ — NOAA-backed bets should be cheap
MIN_ENTRY_PRICE = 0.02      # 2¢ floor — avoid dust/dead markets

# Exit: sell when price reaches this level (don't always wait for resolution)
EXIT_THRESHOLD = 0.55       # Sell at 55¢+ for solid profit on <40¢ entries

# Minimum edge: forecast probability minus market price must exceed this
MIN_EDGE = 0.20             # 20 percentage points minimum edge

# Minimum forecast confidence to act on
MIN_FORECAST_CONFIDENCE = 0.60  # Only trade when forecast says ≥60% likely

# Maximum signals per scan cycle
MAX_SIGNALS_PER_CYCLE = 3

# Market cooldown (don't re-enter same market within this window)
MARKET_COOLDOWN = 600       # 10 minutes

# Forecast cache TTL (seconds) — NOAA updates every ~1 hour
FORECAST_CACHE_TTL = 300    # 5 minutes

# City coordinates imported from constants.py (41 cities)

# Patterns identifying weather markets
WEATHER_PATTERNS = [
    r"\b(temperature|high|low|degrees?)\b.*\b(above|below|over|under|between|reach)\b",
    r"\b(above|below|over|under|at least|exceed)\s+\d+\s*[°]?\s*[FfCc]\b",
    r"\b\d+\s*[°]?\s*[FfCc]\b",
    r"\b(rain|snow|precipitation|inches?|mm)\b.*\b(above|below|more|less|over|under)\b",
    r"\b(hurricane|tornado|tropical storm|cyclone)\b",
    r"\b(record high|record low|heat wave|cold snap|freeze|frost)\b",
    r"\bhigh temperature\b",
    r"\blow temperature\b",
    r"\bweather\b.*\b(market|bet|predict)\b",
]

# Patterns for extracting temperature thresholds from questions
TEMP_EXTRACT_PATTERNS = [
    r"(above|over|exceed|at least|reach|higher than)\s+(\d+)\s*[°]?\s*([FfCc])",
    r"(below|under|lower than|less than|drop below)\s+(\d+)\s*[°]?\s*([FfCc])",
    r"(between|from)\s+(\d+)\s*[°]?\s*([FfCc])\s*(?:and|to|-)\s*(\d+)\s*[°]?\s*([FfCc])",
    r"(\d+)\s*[°]?\s*([FfCc])\s*(or higher|or above|\+)",
    r"(\d+)\s*[°]?\s*([FfCc])\s*(or lower|or below|-)",
    r"high.*?(\d+)\s*[°]?\s*([FfCc])",
    r"low.*?(\d+)\s*[°]?\s*([FfCc])",
]


@dataclass
class ForecastData:
    """Weather forecast data for a specific location and date."""
    location: str
    date: str                       # YYYY-MM-DD or "today"/"tomorrow"
    high_f: Optional[float] = None  # Forecast high in °F
    low_f: Optional[float] = None   # Forecast low in °F
    precip_prob: Optional[float] = None  # Precipitation probability 0-100
    precip_inches: Optional[float] = None
    snow_inches: Optional[float] = None
    wind_mph: Optional[float] = None
    source: str = "unknown"
    detail: str = ""


class WeatherForecastArbStrategy(BaseStrategy):
    """
    Weather forecast arbitrage strategy.

    Buys Polymarket weather outcome tokens that are deeply mispriced
    relative to NOAA/Open-Meteo forecasts.
    """

    def name(self) -> str:
        return "weather_forecast_arb"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._session = get_session()
        self._market_cooldown: Dict[str, float] = {}
        # Cache: location -> (ForecastData, fetch_time)
        self._forecast_cache: Dict[str, Tuple[ForecastData, float]] = {}

    def scan(self) -> List[TradeSignal]:
        """Scan for weather forecast arbitrage opportunities."""
        signals: List[TradeSignal] = []
        markets = self.market_scanner.get_markets()

        # Filter to weather markets only
        weather_markets = [m for m in markets if self._is_weather_market(m)]

        if not weather_markets:
            self.log.debug("No active weather markets found.")
            return []

        self.log.debug(
            "Found %d weather market(s) to evaluate.", len(weather_markets)
        )

        for market in weather_markets:
            try:
                market_signals = self._evaluate_weather_market(market)
                signals.extend(market_signals)
                if len(signals) >= MAX_SIGNALS_PER_CYCLE:
                    break
            except Exception as exc:
                self.log.debug(
                    "Error evaluating weather market %s: %s",
                    market.market_id[:16], exc,
                )

        if signals:
            self.log.info(
                "Weather forecast arb: %d signal(s) from %d market(s).",
                len(signals), len(weather_markets),
            )

        return signals[:MAX_SIGNALS_PER_CYCLE]

    # ─────────────────────────────────────────────────────────────────────────
    # Market identification
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _is_weather_market(market: MarketInfo) -> bool:
        """Check if a market is a weather/temperature market."""
        q = market.question.lower()

        # Quick keyword pre-filter
        weather_keywords = [
            "temperature", "degrees", "°f", "°c", "fahrenheit", "celsius",
            "rain", "snow", "precipitation", "weather", "high temp",
            "low temp", "heat", "cold", "freeze", "frost", "hurricane",
            "tornado", "storm", "wind", "drought", "flood",
        ]
        if not any(kw in q for kw in weather_keywords):
            return False

        # Confirm with regex patterns
        for pattern in WEATHER_PATTERNS:
            if re.search(pattern, q, re.IGNORECASE):
                return True

        return False

    # ─────────────────────────────────────────────────────────────────────────
    # Market evaluation
    # ─────────────────────────────────────────────────────────────────────────

    def _evaluate_weather_market(self, market: MarketInfo) -> List[TradeSignal]:
        """Evaluate a weather market against forecast data."""
        signals = []

        # Cooldown check
        last_entry = self._market_cooldown.get(market.market_id, 0)
        if time.time() - last_entry < MARKET_COOLDOWN:
            return []

        question = market.question

        # Extract location from the question
        location = self._extract_location(question)
        if not location:
            self.log.debug("No location found in: %s", question[:80])
            return []

        # Get forecast data (cached)
        forecast = self._get_forecast(location)
        if not forecast:
            self.log.debug("No forecast data for location: %s", location)
            return []

        # Parse what the market is asking about
        market_type, threshold, direction = self._parse_market_question(question)
        if not market_type:
            self.log.debug("Could not parse market question: %s", question[:80])
            return []

        # Calculate forecast-implied probability for each outcome
        for token in market.tokens:
            signal = self._evaluate_token_vs_forecast(
                token, market, forecast, market_type, threshold, direction
            )
            if signal:
                signals.append(signal)
                self._market_cooldown[market.market_id] = time.time()

        return signals

    def _evaluate_token_vs_forecast(
        self,
        token: TokenInfo,
        market: MarketInfo,
        forecast: ForecastData,
        market_type: str,
        threshold: float,
        direction: str,
    ) -> Optional[TradeSignal]:
        """
        Compare a single outcome token's price to forecast-implied probability.

        This is where the edge calculation happens:
          edge = forecast_probability - market_price

        If edge > MIN_EDGE, generate a BUY signal.
        """
        price = token.mid_price or token.best_ask
        if price <= 0 or price < MIN_ENTRY_PRICE or price > MAX_ENTRY_PRICE:
            return None

        # Calculate what the forecast says the probability should be
        forecast_prob = self._calculate_forecast_probability(
            forecast, market_type, threshold, direction, token.outcome
        )

        if forecast_prob is None or forecast_prob < MIN_FORECAST_CONFIDENCE:
            return None

        # The edge: how much the market underprices this outcome
        edge = forecast_prob - price

        if edge < MIN_EDGE:
            return None

        # ── Confidence score ────────────────────────────────────────────────
        confidence = 0.0

        # Edge magnitude (bigger edge = higher confidence)
        confidence += min(edge * 1.5, 0.45)  # Up to 0.45 from edge

        # Forecast source quality
        if forecast.source == "noaa":
            confidence += 0.20  # NOAA is gold-standard
        elif forecast.source == "openmeteo":
            confidence += 0.10  # Open-Meteo is good but not NOAA-tier

        # Time proximity (closer events = more accurate forecasts)
        if forecast.date == "today" or forecast.date == time.strftime("%Y-%m-%d"):
            confidence += 0.15  # Same-day forecast ~90%+ accurate
        elif forecast.date == "tomorrow":
            confidence += 0.10  # Next-day forecast ~85%+ accurate
        else:
            confidence += 0.05  # Multi-day forecast still useful

        # Price attractiveness (lower price = higher potential return)
        if price <= 0.10:
            confidence += 0.10  # Extreme mispricing
        elif price <= 0.20:
            confidence += 0.07
        elif price <= 0.30:
            confidence += 0.04

        confidence = max(0.0, min(confidence, 1.0))

        # Minimum confidence gate
        if confidence < 0.40:
            return None

        # ── Build signal ────────────────────────────────────────────────────
        budget = self.cfg.MAX_POSITION_SIZE * confidence * 0.5
        size = budget / token.best_ask if token.best_ask > 0 else 0
        size = max(round(size, 2), 5.0)

        # Expected payoff if it resolves at $1
        expected_payoff = (1.0 - price) / price

        reason = (
            f"Weather Arb [{forecast.source.upper()}] {token.outcome} @ {price:.2f} | "
            f"forecast_prob={forecast_prob:.0%} | edge={edge:.0%} | "
            f"payoff={expected_payoff:.0%} | {location} | "
            f"{market.question[:60]}"
        )

        signal = TradeSignal(
            strategy=self.name(),
            market_id=market.market_id,
            token_id=token.token_id,
            side="BUY",
            price=round(token.best_ask, 4),
            size=size,
            confidence=confidence,
            reason=reason,
            order_type="GTC",
        )

        self._log_signal(signal)
        return signal

    # ─────────────────────────────────────────────────────────────────────────
    # Forecast probability calculation
    # ─────────────────────────────────────────────────────────────────────────

    def _calculate_forecast_probability(
        self,
        forecast: ForecastData,
        market_type: str,
        threshold: float,
        direction: str,
        outcome: str,
    ) -> Optional[float]:
        """
        Calculate the forecast-implied probability that an outcome is correct.

        For temperature markets:
          - If NOAA says high = 45°F and market asks "above 40°F?",
            forecast implies Yes is very likely (~85-95%).
          - If the token is "Yes" → return high probability
          - If the token is "No" → return 1 - probability

        Uses the forecast point value plus a standard error margin
        to estimate the probability distribution.
        """
        is_yes = outcome.lower() in ("yes", "over", "above", "higher")

        prob = None

        if market_type == "high_temp":
            if forecast.high_f is None:
                return None
            # Estimate probability that actual high exceeds threshold
            # NOAA forecast standard error: ~3°F for 1-day, ~5°F for 2-day
            std_err = 3.0 if forecast.source == "noaa" else 4.0
            diff = forecast.high_f - threshold

            if direction == "above":
                # P(actual_high > threshold)
                prob = self._normal_prob_above(diff, std_err)
            elif direction == "below":
                # P(actual_high < threshold)
                prob = 1.0 - self._normal_prob_above(diff, std_err)
            elif direction == "between":
                # For bucket markets (e.g., 35-40°F)
                # threshold is the bucket center; use wider range
                prob = self._bucket_probability(forecast.high_f, threshold, std_err)

        elif market_type == "low_temp":
            if forecast.low_f is None:
                return None
            std_err = 3.0 if forecast.source == "noaa" else 4.0
            diff = forecast.low_f - threshold

            if direction == "above":
                prob = self._normal_prob_above(diff, std_err)
            elif direction == "below":
                prob = 1.0 - self._normal_prob_above(diff, std_err)
            elif direction == "between":
                prob = self._bucket_probability(forecast.low_f, threshold, std_err)

        elif market_type == "precip":
            if forecast.precip_prob is not None:
                # Direct probability from forecast
                prob = forecast.precip_prob / 100.0
                if direction == "below":
                    prob = 1.0 - prob

        elif market_type == "snow":
            if forecast.snow_inches is not None:
                # Simple threshold comparison with uncertainty
                diff = forecast.snow_inches - threshold
                prob = self._normal_prob_above(diff, 1.0)
                if direction == "below":
                    prob = 1.0 - prob

        if prob is None:
            return None

        # Invert for "No" tokens
        if not is_yes:
            prob = 1.0 - prob

        return prob

    @staticmethod
    def _normal_prob_above(diff: float, std_err: float) -> float:
        """
        Approximate P(X > 0) given X ~ N(diff, std_err²).

        Uses a simple logistic approximation to the normal CDF.
        When diff >> 0, probability approaches 1.
        When diff << 0, probability approaches 0.
        """
        # Logistic approximation: 1 / (1 + exp(-k * diff / std_err))
        # k ≈ 1.7 gives a good fit to the normal CDF
        z = 1.7 * diff / max(std_err, 0.5)
        # Clamp to avoid overflow
        z = max(-10, min(10, z))
        return 1.0 / (1.0 + math.exp(-z))

    @staticmethod
    def _bucket_probability(
        forecast_value: float, bucket_center: float, std_err: float
    ) -> float:
        """
        Estimate probability that actual value falls within a bucket.

        Assumes buckets are typically 5°F wide (e.g., 35-40°F).
        bucket_center is the midpoint of the range.
        """
        bucket_half = 2.5  # Half of 5°F bucket width
        # P(center - 2.5 < X < center + 2.5) where X ~ N(forecast, std_err²)
        z_low = 1.7 * (forecast_value - (bucket_center - bucket_half)) / max(std_err, 0.5)
        z_high = 1.7 * (forecast_value - (bucket_center + bucket_half)) / max(std_err, 0.5)
        z_low = max(-10, min(10, z_low))
        z_high = max(-10, min(10, z_high))
        p_above_low = 1.0 / (1.0 + math.exp(-z_low))
        p_above_high = 1.0 / (1.0 + math.exp(-z_high))
        return p_above_low - p_above_high

    # ─────────────────────────────────────────────────────────────────────────
    # Market question parsing
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_market_question(
        question: str,
    ) -> Tuple[Optional[str], float, str]:
        """
        Parse a weather market question to extract:
          - market_type: "high_temp", "low_temp", "precip", "snow"
          - threshold: numeric threshold value (e.g., 40 for "above 40°F")
          - direction: "above", "below", or "between"

        Returns (None, 0, "") if parsing fails.
        """
        q = question.lower()

        # Determine market type
        market_type = None
        if any(kw in q for kw in ["high temp", "high of", "daily high", "maximum"]):
            market_type = "high_temp"
        elif any(kw in q for kw in ["low temp", "low of", "daily low", "minimum", "overnight"]):
            market_type = "low_temp"
        elif any(kw in q for kw in ["rain", "precipitation", "precip"]):
            market_type = "precip"
        elif "snow" in q:
            market_type = "snow"
        elif any(kw in q for kw in ["temperature", "degrees", "°f", "°c"]):
            # Default to high temp if just "temperature" mentioned
            market_type = "high_temp"

        if not market_type:
            return None, 0.0, ""

        # Extract threshold and direction
        threshold = 0.0
        direction = "above"  # default

        # Try "above/over X°F" patterns
        m = re.search(
            r"(above|over|exceed|at least|reach|higher than|warmer than)\s+(\d+)",
            q,
        )
        if m:
            direction = "above"
            threshold = float(m.group(2))
            return market_type, threshold, direction

        # Try "below/under X°F" patterns
        m = re.search(
            r"(below|under|lower than|less than|drop below|colder than|cooler than)\s+(\d+)",
            q,
        )
        if m:
            direction = "below"
            threshold = float(m.group(2))
            return market_type, threshold, direction

        # Try "between X and Y" patterns (bucket markets)
        m = re.search(r"between\s+(\d+)\s*(?:°[FfCc])?\s*(?:and|to|-)\s*(\d+)", q)
        if m:
            direction = "between"
            low_val = float(m.group(1))
            high_val = float(m.group(2))
            threshold = (low_val + high_val) / 2.0  # Bucket center
            return market_type, threshold, direction

        # Try bare "X°F" with context
        m = re.search(r"(\d+)\s*[°]?\s*[FfCc]", q)
        if m:
            threshold = float(m.group(1))
            # Infer direction from context
            if any(kw in q for kw in ["above", "over", "exceed", "higher", "warmer", "or more"]):
                direction = "above"
            elif any(kw in q for kw in ["below", "under", "lower", "cooler", "or less"]):
                direction = "below"
            else:
                direction = "above"  # Default assumption
            return market_type, threshold, direction

        return None, 0.0, ""

    # ─────────────────────────────────────────────────────────────────────────
    # Location extraction
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_location(question: str) -> Optional[str]:
        """Extract a city/location from a market question."""
        q = question.lower()

        # Check known cities
        for city in CITY_COORDS:
            if city in q:
                return city

        # Try "in <Location>" pattern
        match = re.search(r"\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", question)
        if match:
            return match.group(1).lower()

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Forecast data fetching
    # ─────────────────────────────────────────────────────────────────────────

    def _get_forecast(self, location: str) -> Optional[ForecastData]:
        """Get weather forecast for a location (with caching)."""
        cache_key = location.lower()

        # Check cache
        if cache_key in self._forecast_cache:
            cached, fetch_time = self._forecast_cache[cache_key]
            if time.time() - fetch_time < FORECAST_CACHE_TTL:
                return cached

        # Try NOAA first (US, most accurate)
        forecast = self._fetch_noaa(location)

        # Fallback to Open-Meteo (global)
        if not forecast:
            forecast = self._fetch_openmeteo(location)

        if forecast:
            self._forecast_cache[cache_key] = (forecast, time.time())

        return forecast

    def _fetch_noaa(self, location: str) -> Optional[ForecastData]:
        """Fetch forecast from NOAA/NWS weather.gov API (US only, free)."""
        try:
            loc_lower = location.lower()
            lat, lon = None, None
            for city, coords in CITY_COORDS.items():
                if city in loc_lower:
                    lat, lon = coords
                    break

            if lat is None:
                return None

            # Step 1: Get forecast grid endpoint
            headers = {"User-Agent": "PolymarketBot/1.0 (trading@bot.com)"}
            resp = self._session.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers=headers, timeout=8,
            )
            if not resp.ok:
                return None

            point_data = resp.json()
            forecast_url = point_data.get("properties", {}).get("forecast", "")
            if not forecast_url:
                return None

            # Step 2: Get the detailed forecast
            resp = self._session.get(forecast_url, headers=headers, timeout=8)
            if not resp.ok:
                return None

            forecast_data = resp.json()
            periods = forecast_data.get("properties", {}).get("periods", [])
            if not periods:
                return None

            # Extract today's/tonight's forecast
            # Day periods have isDaytime=True and contain the high
            # Night periods have isDaytime=False and contain the low
            high_f = None
            low_f = None
            precip_prob = None
            wind_mph = None
            detail = ""
            date_label = "today"

            for period in periods[:4]:  # First 2 day/night pairs
                temp = period.get("temperature")
                is_day = period.get("isDaytime", True)
                precip = period.get("probabilityOfPrecipitation", {})
                precip_val = precip.get("value") if precip else None
                wind_str = period.get("windSpeed", "")
                det = period.get("detailedForecast", "")

                if is_day and temp is not None and high_f is None:
                    high_f = float(temp)
                    detail = det
                    # Extract wind speed
                    wm = re.search(r"(\d+)", wind_str)
                    if wm:
                        wind_mph = float(wm.group(1))
                    if precip_val is not None:
                        precip_prob = float(precip_val)
                elif not is_day and temp is not None and low_f is None:
                    low_f = float(temp)
                    if precip_val is not None and precip_prob is None:
                        precip_prob = float(precip_val)

            if high_f is None and low_f is None:
                return None

            self.log.debug(
                "NOAA forecast for %s: high=%.0f°F low=%s°F precip=%s%%",
                location,
                high_f or 0,
                f"{low_f:.0f}" if low_f else "?",
                f"{precip_prob:.0f}" if precip_prob else "?",
            )

            return ForecastData(
                location=location,
                date=date_label,
                high_f=high_f,
                low_f=low_f,
                precip_prob=precip_prob,
                wind_mph=wind_mph,
                source="noaa",
                detail=detail[:200],
            )

        except Exception as exc:
            self.log.debug("NOAA fetch failed for %s: %s", location, exc)
            return None

    def _fetch_openmeteo(self, location: str) -> Optional[ForecastData]:
        """Fetch forecast from Open-Meteo API (global, free, no key)."""
        try:
            # Geocode location
            geo_resp = self._session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1},
                timeout=5,
            )
            if not geo_resp.ok:
                return None
            results = geo_resp.json().get("results", [])
            if not results:
                return None

            lat = results[0]["latitude"]
            lon = results[0]["longitude"]

            # Get forecast
            weather_resp = self._session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,"
                             "precipitation_sum,snowfall_sum,"
                             "wind_speed_10m_max,"
                             "precipitation_probability_max",
                    "temperature_unit": "fahrenheit",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
                timeout=5,
            )
            if not weather_resp.ok:
                return None

            data = weather_resp.json().get("daily", {})
            highs = data.get("temperature_2m_max", [])
            lows = data.get("temperature_2m_min", [])
            precip_probs = data.get("precipitation_probability_max", [])
            snow = data.get("snowfall_sum", [])
            wind = data.get("wind_speed_10m_max", [])

            if not highs:
                return None

            # Use today's (first) forecast
            high_f = float(highs[0]) if highs else None
            low_f = float(lows[0]) if lows else None
            precip_prob = float(precip_probs[0]) if precip_probs else None
            snow_inches = float(snow[0]) / 2.54 if snow and snow[0] else None  # cm -> inches
            wind_mph = float(wind[0]) * 0.621371 if wind and wind[0] else None  # km/h -> mph

            self.log.debug(
                "Open-Meteo forecast for %s: high=%.0f°F low=%s°F",
                location,
                high_f or 0,
                f"{low_f:.0f}" if low_f else "?",
            )

            return ForecastData(
                location=location,
                date="today",
                high_f=high_f,
                low_f=low_f,
                precip_prob=precip_prob,
                snow_inches=snow_inches,
                wind_mph=wind_mph,
                source="openmeteo",
            )

        except Exception as exc:
            self.log.debug("Open-Meteo fetch failed for %s: %s", location, exc)
            return None
