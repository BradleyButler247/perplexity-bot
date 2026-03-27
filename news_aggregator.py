"""
news_aggregator.py
------------------
Aggregates real-world data from free public sources to provide context
for AI-powered market probability estimation.

Data sources (all free, no API keys required):
  - Google News RSS: Headlines for any topic
  - ESPN/sports APIs: Live scores, standings, odds
  - Associated Press RSS: Breaking news and politics
  - CoinGecko: Crypto prices and market data
  - Wikipedia Current Events: Daily notable events

The aggregator categorizes each Polymarket market by topic, then fetches
relevant real-world data to feed into Claude for probability estimation.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from xml.etree import ElementTree

from constants import CITY_COORDS
from http_client import get_session

logger = logging.getLogger("bot.news")

# Cache duration for news data (seconds)
NEWS_CACHE_TTL = 300  # 5 minutes

# ── Market category detection ─────────────────────────────────────────────────

CATEGORY_PATTERNS = {
    "sports": [
        r"\b(nba|nfl|mlb|nhl|mls|premier league|champions league|serie a|la liga)\b",
        r"\b(win|beat|defeat|championship|playoffs|finals|match|game|tournament)\b",
        r"\b(team|player|coach|season|score|mvp|draft)\b",
        r"\b(basketball|football|soccer|baseball|hockey|tennis|golf|boxing|mma|ufc)\b",
    ],
    "esports": [
        r"\b(league of legends|lol|cs2|csgo|dota|valorant|overwatch)\b",
        r"\b(esports?|e-sports?|gaming|twitch|worlds|major|lan)\b",
    ],
    "politics": [
        r"\b(president|congress|senate|house|election|vote|poll|bill|law|legislation)\b",
        r"\b(democrat|republican|gop|liberal|conservative|party)\b",
        r"\b(governor|mayor|supreme court|executive order|impeach)\b",
        r"\b(trump|biden|desantis|harris|newsom)\b",
    ],
    "geopolitics": [
        r"\b(war|conflict|sanctions|treaty|nato|united nations|un|g7|g20)\b",
        r"\b(russia|ukraine|china|taiwan|iran|israel|gaza|north korea)\b",
        r"\b(diplomacy|ceasefire|invasion|military|tariff|trade war)\b",
    ],
    "crypto": [
        r"\b(bitcoin|btc|ethereum|eth|solana|sol|crypto|blockchain)\b",
        r"\b(defi|nft|token|halving|etf|sec|binance|coinbase)\b",
        r"\b(up or down|price target|all.time.high|ath)\b",
    ],
    "finance": [
        r"\b(fed|federal reserve|interest rate|inflation|gdp|recession)\b",
        r"\b(stock|s&p|nasdaq|dow|earnings|ipo|market cap)\b",
        r"\b(treasury|bond|yield|forex|dollar)\b",
    ],
    "weather": [
        r"\b(weather|temperature|rain|snow|hurricane|tornado|storm|flood)\b",
        r"\b(heat|cold|freeze|drought|wildfire|celsius|fahrenheit)\b",
        r"\b(noaa|nws|forecast|climate|wind|hail|blizzard)\b",
        r"\b(record high|record low|above normal|below normal)\b",
    ],
    "culture": [
        r"\b(oscar|grammy|emmy|tony|golden globe|award|nomination)\b",
        r"\b(movie|film|album|song|tv show|netflix|disney|spotify)\b",
        r"\b(twitter|x\.com|instagram|tiktok|youtube|follower|subscriber)\b",
        r"\b(celebrity|kardashian|musk|taylor swift|drake)\b",
    ],
}


@dataclass
class MarketContext:
    """Real-world context gathered for a specific market."""
    market_id: str
    question: str
    category: str
    headlines: List[str] = field(default_factory=list)
    key_facts: List[str] = field(default_factory=list)
    data_points: Dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0
    sentiment: float = 0.0  # Headline sentiment score: -1.0 (negative) to +1.0 (positive)

    def to_prompt_context(self) -> str:
        """Format context for inclusion in a Claude prompt."""
        parts = []
        if self.headlines:
            parts.append("Recent headlines:")
            for h in self.headlines[:10]:
                parts.append(f"  - {h}")
        if self.key_facts:
            parts.append("\nKey facts:")
            for f in self.key_facts[:10]:
                parts.append(f"  - {f}")
        if self.data_points:
            parts.append("\nData:")
            for k, v in list(self.data_points.items())[:10]:
                parts.append(f"  - {k}: {v}")
        return "\n".join(parts) if parts else "No additional context available."


class NewsAggregator:
    """
    Fetches real-world data relevant to Polymarket markets.

    Usage:
        aggregator = NewsAggregator()
        context = aggregator.get_context(market_question, market_id)
    """

    def __init__(self) -> None:
        self._session = get_session()
        self._cache: Dict[str, MarketContext] = {}

    def get_context(self, question: str, market_id: str) -> MarketContext:
        """
        Gather real-world context for a market question.

        Caches results for NEWS_CACHE_TTL seconds.
        """
        # Check cache
        cached = self._cache.get(market_id)
        if cached and (time.time() - cached.fetched_at) < NEWS_CACHE_TTL:
            return cached

        category = self._categorize_market(question)
        context = MarketContext(
            market_id=market_id,
            question=question,
            category=category,
            fetched_at=time.time(),
        )

        try:
            # Fetch category-specific data
            if category == "sports":
                self._fetch_sports_context(question, context)
            elif category == "esports":
                self._fetch_esports_context(question, context)
            elif category in ("politics", "geopolitics"):
                self._fetch_politics_context(question, context)
            elif category == "crypto":
                self._fetch_crypto_context(question, context)
            elif category == "finance":
                self._fetch_finance_context(question, context)
            elif category == "weather":
                self._fetch_weather_context(question, context)
            elif category == "culture":
                self._fetch_culture_context(question, context)

            # Always fetch general news related to the question
            self._fetch_news_headlines(question, context)

        except Exception as exc:
            logger.debug("News aggregation error for '%s': %s", question[:50], exc)

        context.sentiment = self.score_sentiment(context.headlines)
        self._cache[market_id] = context
        return context

    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def score_sentiment(headlines: list) -> float:
        """
        Score sentiment of a list of headlines using keyword matching.

        Returns a score from -1.0 (very negative) to +1.0 (very positive).
        Returns 0.0 if no relevant keywords are found.
        """
        positive_words = [
            "surge", "rally", "win", "wins", "won", "approve", "approved",
            "pass", "passes", "passed", "record high", "beat", "beats",
            "breakthrough", "rise", "rises", "rising", "gain", "gains",
            "victory", "succeed", "success", "growth", "boom",
        ]
        negative_words = [
            "crash", "crashes", "fall", "falls", "fell", "reject", "rejected",
            "fail", "fails", "failed", "scandal", "lose", "loses", "lost",
            "record low", "miss", "misses", "missed", "decline", "declines",
            "drop", "drops", "plunge", "collapse", "crisis", "down",
        ]

        if not headlines:
            return 0.0

        scores = []
        for headline in headlines:
            h = headline.lower()
            pos = sum(1 for w in positive_words if w in h)
            neg = sum(1 for w in negative_words if w in h)
            total = pos + neg
            if total > 0:
                scores.append((pos - neg) / total)

        if not scores:
            return 0.0
        return sum(scores) / len(scores)
    # Category detection
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _categorize_market(question: str) -> str:
        """Determine the category of a market based on its question text."""
        q = question.lower()
        scores: Dict[str, int] = {}

        for category, patterns in CATEGORY_PATTERNS.items():
            score = 0
            for pattern in patterns:
                matches = re.findall(pattern, q, re.IGNORECASE)
                score += len(matches)
            if score > 0:
                scores[category] = score

        if not scores:
            return "general"

        return max(scores, key=scores.get)

    # ─────────────────────────────────────────────────────────────────────────
    # Category-specific data fetchers
    # ─────────────────────────────────────────────────────────────────────────

    def _fetch_news_headlines(self, question: str, context: MarketContext) -> None:
        """Fetch relevant news via Google News RSS."""
        # Extract key terms from the question for search
        search_terms = self._extract_search_terms(question)
        if not search_terms:
            return

        try:
            url = "https://news.google.com/rss/search"
            params = {"q": search_terms, "hl": "en-US", "gl": "US", "ceid": "US:en"}
            resp = self._session.get(url, params=params, timeout=10)
            resp.raise_for_status()

            root = ElementTree.fromstring(resp.content)
            items = root.findall(".//item")

            for item in items[:10]:
                title = item.findtext("title", "")
                pub_date = item.findtext("pubDate", "")
                if title:
                    context.headlines.append(f"{title} ({pub_date})" if pub_date else title)

        except Exception as exc:
            logger.debug("Google News fetch failed: %s", exc)

    def _fetch_sports_context(self, question: str, context: MarketContext) -> None:
        """Fetch sports scores and standings."""
        # ESPN Headlines RSS
        try:
            feeds = {
                "nba": "https://www.espn.com/espn/rss/nba/news",
                "nfl": "https://www.espn.com/espn/rss/nfl/news",
                "mlb": "https://www.espn.com/espn/rss/mlb/news",
                "nhl": "https://www.espn.com/espn/rss/nhl/news",
                "soccer": "https://www.espn.com/espn/rss/soccer/news",
            }

            q = question.lower()
            for sport, feed_url in feeds.items():
                if sport in q or (sport == "soccer" and any(
                    kw in q for kw in ["premier", "champions", "la liga", "serie a", "mls"]
                )):
                    try:
                        resp = self._session.get(feed_url, timeout=8)
                        if resp.ok:
                            root = ElementTree.fromstring(resp.content)
                            for item in root.findall(".//item")[:5]:
                                title = item.findtext("title", "")
                                if title:
                                    context.key_facts.append(title)
                    except Exception:
                        pass

        except Exception as exc:
            logger.debug("Sports context fetch failed: %s", exc)

        # Try to get odds from a free API
        try:
            resp = self._session.get(
                "https://www.thesportsdb.com/api/v1/json/3/eventsday.php",
                params={"d": time.strftime("%Y-%m-%d")},
                timeout=8,
            )
            if resp.ok:
                data = resp.json()
                events = data.get("events") or []
                for event in events[:10]:
                    name = event.get("strEvent", "")
                    sport = event.get("strSport", "")
                    home_score = event.get("intHomeScore", "")
                    away_score = event.get("intAwayScore", "")
                    if name:
                        score_str = f" ({home_score}-{away_score})" if home_score else ""
                        context.data_points[name] = f"{sport}{score_str}"
        except Exception:
            pass

    def _fetch_esports_context(self, question: str, context: MarketContext) -> None:
        """Fetch esports news and results."""
        self._fetch_news_headlines(f"esports {question}", context)

    def _fetch_politics_context(self, question: str, context: MarketContext) -> None:
        """Fetch political news and polling data."""
        # AP News RSS for politics
        try:
            resp = self._session.get(
                "https://rsshub.app/apnews/topics/politics",
                timeout=8,
            )
            if resp.ok:
                root = ElementTree.fromstring(resp.content)
                for item in root.findall(".//item")[:5]:
                    title = item.findtext("title", "")
                    if title:
                        context.key_facts.append(f"AP: {title}")
        except Exception:
            pass

        # Also try Reuters
        try:
            resp = self._session.get(
                "https://rsshub.app/reuters/world",
                timeout=8,
            )
            if resp.ok:
                root = ElementTree.fromstring(resp.content)
                for item in root.findall(".//item")[:5]:
                    title = item.findtext("title", "")
                    if title:
                        context.key_facts.append(f"Reuters: {title}")
        except Exception:
            pass

    def _fetch_crypto_context(self, question: str, context: MarketContext) -> None:
        """Fetch crypto prices and market data."""
        try:
            # CoinGecko free API
            resp = self._session.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={
                    "ids": "bitcoin,ethereum,solana,ripple,dogecoin",
                    "vs_currencies": "usd",
                    "include_24hr_change": "true",
                    "include_market_cap": "true",
                },
                timeout=8,
            )
            if resp.ok:
                data = resp.json()
                for coin, info in data.items():
                    price = info.get("usd", 0)
                    change = info.get("usd_24h_change", 0)
                    direction = "up" if change > 0 else "down"
                    context.data_points[coin.title()] = (
                        f"${price:,.2f} ({direction} {abs(change):.1f}% 24h)"
                    )
        except Exception:
            pass

        # Crypto fear & greed index
        try:
            resp = self._session.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=5,
            )
            if resp.ok:
                data = resp.json().get("data", [{}])[0]
                value = data.get("value", "?")
                label = data.get("value_classification", "?")
                context.data_points["Fear & Greed Index"] = f"{value} ({label})"
        except Exception:
            pass

    def _fetch_finance_context(self, question: str, context: MarketContext) -> None:
        """Fetch financial news and economic indicators."""
        self._fetch_news_headlines(f"economy finance {question}", context)

    def _fetch_culture_context(self, question: str, context: MarketContext) -> None:
        """Fetch entertainment and culture news."""
        self._fetch_news_headlines(f"entertainment {question}", context)

    def _fetch_weather_context(self, question: str, context: MarketContext) -> None:
        """
        Fetch weather data from NOAA/NWS (National Weather Service) API.

        NOAA forecasts are free, no API key required, and ~94% accurate.
        Uses weather.gov API for US locations and OpenMeteo for global.
        """
        # Extract location from question
        location = self._extract_location(question)

        # Try NOAA/NWS API (US locations, most accurate)
        if location:
            self._fetch_noaa_forecast(location, context)

        # Try Open-Meteo for global weather data (free, no key)
        self._fetch_openmeteo(question, context)

        # Also get weather news
        self._fetch_news_headlines(f"weather {question}", context)

    def _fetch_noaa_forecast(self, location: str, context: MarketContext) -> None:
        """Fetch forecast from NOAA/NWS weather.gov API (US only, free)."""
        try:
            # Step 1: Geocode location to lat/lon using NOAA
            # Use common US city coordinates as fallback
            lat, lon = None, None
            loc_lower = location.lower()
            for city, coords in CITY_COORDS.items():
                if city in loc_lower:
                    lat, lon = coords
                    break

            if lat is None:
                return

            # Step 2: Get the forecast grid
            headers = {"User-Agent": "PolymarketBot/1.0 (trading@bot.com)"}
            resp = self._session.get(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers=headers, timeout=8,
            )
            if not resp.ok:
                return
            point_data = resp.json()
            forecast_url = point_data.get("properties", {}).get("forecast", "")

            if not forecast_url:
                return

            # Step 3: Get the actual forecast
            resp = self._session.get(forecast_url, headers=headers, timeout=8)
            if not resp.ok:
                return
            forecast = resp.json()

            periods = forecast.get("properties", {}).get("periods", [])
            for period in periods[:6]:  # Next 3 days (day + night)
                name = period.get("name", "")
                temp = period.get("temperature", "")
                temp_unit = period.get("temperatureUnit", "F")
                wind = period.get("windSpeed", "")
                detail = period.get("detailedForecast", "")
                precip = period.get("probabilityOfPrecipitation", {}).get("value")

                context.key_facts.append(
                    f"NOAA {name}: {temp}\u00b0{temp_unit}, wind {wind}"
                    + (f", precip {precip}%" if precip else "")
                )
                if detail:
                    context.data_points[f"NOAA {name}"] = detail[:150]

        except Exception as exc:
            logger.debug("NOAA forecast fetch failed: %s", exc)

    def _fetch_openmeteo(self, question: str, context: MarketContext) -> None:
        """Fetch weather data from Open-Meteo API (global, free, no key)."""
        try:
            # Extract any temperature numbers from the question
            temp_match = re.search(r"(\d+)\s*[\u00b0]?\s*[FfCc]", question)
            target_temp = int(temp_match.group(1)) if temp_match else None

            # Get general weather summary for a major city if mentioned
            location = self._extract_location(question)
            if not location:
                return

            # Use Open-Meteo geocoding
            geo_resp = self._session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": location, "count": 1},
                timeout=5,
            )
            if not geo_resp.ok:
                return
            results = geo_resp.json().get("results", [])
            if not results:
                return

            lat = results[0]["latitude"]
            lon = results[0]["longitude"]
            city_name = results[0].get("name", location)

            # Get 7-day forecast
            weather_resp = self._session.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,snowfall_sum,wind_speed_10m_max",
                    "temperature_unit": "fahrenheit",
                    "timezone": "auto",
                    "forecast_days": 7,
                },
                timeout=5,
            )
            if not weather_resp.ok:
                return
            data = weather_resp.json().get("daily", {})

            dates = data.get("time", [])
            highs = data.get("temperature_2m_max", [])
            lows = data.get("temperature_2m_min", [])
            precip = data.get("precipitation_sum", [])
            snow = data.get("snowfall_sum", [])
            wind = data.get("wind_speed_10m_max", [])

            for i in range(min(len(dates), 7)):
                day_str = dates[i] if i < len(dates) else "?"
                hi = highs[i] if i < len(highs) else "?"
                lo = lows[i] if i < len(lows) else "?"
                rain = precip[i] if i < len(precip) else 0
                snw = snow[i] if i < len(snow) else 0
                wnd = wind[i] if i < len(wind) else "?"

                extras = []
                if rain and float(rain) > 0:
                    extras.append(f"rain {rain}mm")
                if snw and float(snw) > 0:
                    extras.append(f"snow {snw}cm")
                extra_str = (" | " + ", ".join(extras)) if extras else ""

                context.data_points[f"{city_name} {day_str}"] = (
                    f"High {hi}\u00b0F / Low {lo}\u00b0F, wind {wnd}mph{extra_str}"
                )

            if target_temp and highs:
                max_high = max(float(h) for h in highs if h)
                min_low = min(float(l) for l in lows if l)
                context.key_facts.append(
                    f"7-day range for {city_name}: {min_low:.0f}\u00b0F - {max_high:.0f}\u00b0F "
                    f"(target: {target_temp}\u00b0F)"
                )

        except Exception as exc:
            logger.debug("Open-Meteo fetch failed: %s", exc)

    @staticmethod
    def _extract_location(question: str) -> Optional[str]:
        """Extract a city or location name from a market question."""
        import re
        q = question.lower()

        # Common city names
        cities = [
            "new york", "nyc", "los angeles", "chicago", "houston", "phoenix",
            "philadelphia", "san antonio", "san diego", "dallas", "miami",
            "atlanta", "boston", "seattle", "denver", "washington", "dc",
            "san francisco", "las vegas", "portland", "detroit", "minneapolis",
            "tampa", "orlando", "nashville", "austin", "columbus", "charlotte",
            "london", "paris", "tokyo", "berlin", "sydney", "toronto",
            "mumbai", "dubai", "singapore", "hong kong", "seoul",
        ]
        for city in cities:
            if city in q:
                return city

        # Try to find "in <Location>" pattern
        match = re.search(r"\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)", question)
        if match:
            return match.group(1)

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_search_terms(question: str) -> str:
        """
        Extract the most relevant search terms from a market question.

        Strips common question words and keeps the meaningful nouns/names.
        """
        # Remove common question structures
        q = question.lower()
        for prefix in ["will ", "does ", "is ", "are ", "has ", "have ",
                        "can ", "do ", "did ", "was ", "were ", "would ",
                        "should ", "could "]:
            if q.startswith(prefix):
                q = q[len(prefix):]

        # Remove trailing question marks and date qualifiers
        q = re.sub(r"\?$", "", q)
        q = re.sub(r"\b(by|before|after|on|in|during)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b.*", "", q)
        q = re.sub(r"\b\d{4}\b", "", q)  # Remove years

        # Remove stop words
        stop_words = {"the", "a", "an", "and", "or", "but", "of", "to", "for",
                      "with", "at", "from", "by", "this", "that", "it", "be",
                      "more", "than", "any", "some", "most", "least", "over",
                      "under", "above", "below", "between", "next"}
        words = [w for w in q.split() if w not in stop_words and len(w) > 2]

        return " ".join(words[:6])  # Limit to 6 key terms
