# Changelog

## v41 (2026-03-30)

### Copy Trading Improvements

- **Lowered Market Cooldown** (#5)
  - Reduced from 300s (5 min) to 120s (2 min) between signals on the same market
  - Faster reaction to evolving opportunities without losing dedup protection

- **Wallet Tier System** (#6)
  - Top 3 discovered wallets by composite score = "high confidence" tier
  - High-confidence wallets bypass market cooldown entirely — their signals
    are always acted on immediately
  - Standard wallets still respect the 120s cooldown
  - Tier assignment refreshed every scan cycle from WalletDiscovery cache
  - Tier label (HIGH/STD) included in trade reason for dashboard visibility

- **Profitable Re-Entry** (#7)
  - Markets where a previous copy-trade position closed profitably are
    eligible for re-entry, even if they appear in the cooldown/side-taken maps
  - Profitable exit detected from: resolution WIN ($1) or sold above entry price
  - Re-entry flag is consumed on use (one re-entry per profitable exit)
  - Side-taken record is cleared on re-entry to allow fresh direction

## v34 (2026-03-27)

### New Features

- **Enhanced Wallet Scoring + Category-Locking**
  - New scoring formula: S(w) = α·PnL + β·Consistency + γ·Specialization − δ·MaxDD
  - Strict filters: minimum 80 resolved trades, no single trade > 30% of PnL, average entry 25¢–65¢, active within 14 days
  - Per-category performance tracking via `category_scores` dict on WalletProfile
  - `get_wallet_categories(address)` API for querying a wallet's strong categories
  - Copy-trading now respects category-locking: skips trades outside a wallet's strong categories

- **Liquidity Provider (LP) Rewards Strategy** (`strategies/lp_rewards.py`)
  - Fetches high-liquidity markets from Gamma API
  - Places bid/ask limit orders around midpoint to earn LP rewards
  - Configurable: LP_CAPITAL_PCT, LP_MAX_MARKETS, LP_REFRESH_INTERVAL
  - Registered in strategies/__init__.py and main.py

- **Base Rate + Hold-to-Resolution Rules**
  - Category-based base rate estimates for all market types
  - Positions in low-base-rate categories get 50% size reduction
  - HOLD_TO_RESOLUTION config: skip early exits (TP/time) unless EV flips negative
  - Stat: held-to-resolution trades average +74% vs +18% for early exits

- **Bayesian Position Re-evaluation**
  - `reevaluate_position()` method on AIProbabilityEngine
  - Implements P(H|E) = P(E|H) × P(H) / P(E) via Claude
  - Runs every REEVALUATE_INTERVAL cycles for open AI-powered positions
  - Auto-flags positions for exit when updated EV turns negative

- **Log Returns for P&L**
  - `compute_log_returns()` on TradeHistory for accurate multi-position aggregation
  - Strategy optimizer now tracks log returns alongside arithmetic P&L
  - Log returns sum correctly across positions and time periods

### Codebase Optimizations

- **Shared HTTP Session Pool** (`http_client.py`)
  - Single requests.Session with connection pooling (10/host, 20 total) and 3-retry logic
  - Replaced 8+ individual sessions across: ai_probability_engine, binance_indicators, copy_trading, cross_market_arb, crypto_mean_reversion, weather_forecast_arb, wallet_discovery, news_aggregator, market_scanner, position_tracker, redeemer

- **Import Cleanup**
  - Moved `import re as _re` in ai_probability_engine.py to top-level `import re`
  - Moved inline `import math`, `import json` in weather_forecast_arb.py to top-level
  - Moved inline `import json` in cross_market_arb.py to top-level
  - Moved inline `import json` in market_scanner.py to top-level
  - Moved inline `import datetime`, `import statistics` in wallet_discovery.py to top-level

- **Market Classification Deduplication** (`classify_market()` in market_scanner.py)
  - Single source of truth for categorising markets by question text
  - Replaces duplicated regex lists in contrarian_extreme, crypto_mean_reversion, sports_momentum, weather_forecast_arb, news_aggregator
  - Caches classification results for performance

- **Shared Price History Tracker** (`price_history.py`)
  - PriceHistoryTracker utility with rolling window, average, velocity, extreme detection
  - Available for strategies that track token price history

- **Shared AI Engine**
  - AIProbabilityEngine created once at bot level, shared between AIPoweredStrategy and TradeManager
  - Eliminates duplicate engine instances

### Configuration Changes

New environment variables in `.env.example`:
- `LP_ENABLED`, `LP_CAPITAL_PCT`, `LP_MAX_MARKETS`, `LP_REFRESH_INTERVAL`
- `BASE_RATE_MIN`, `BASE_RATE_SIZE_CUT`, `HOLD_TO_RESOLUTION`
- `REEVALUATE_INTERVAL`

### Other

- Version banner updated to v1.3
- `close_session()` called on shutdown for clean HTTP teardown
- Wallet discovery `MAX_INACTIVE_DAYS` increased from 7 to 14
