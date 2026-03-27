# Roadmap

Future development plans for the Polymarket Trading Bot.

---

## Implemented in v35 ✅

### Daily Drawdown Circuit Breaker
- Halts all trading if daily losses exceed MAX_DAILY_DRAWDOWN_PCT of bankroll
- Auto-resets at midnight UTC
- Pauses 15 minutes after MAX_CONSECUTIVE_LOSSES consecutive losing trades

### Quarter-Kelly Sizing
- Changed from half-Kelly to quarter-Kelly (f*/4) for more conservative bankroll management
- Multiple independent sources agree quarter-Kelly is optimal for small bankrolls

### Strategy Optimizer Fixes
- All 10 strategies now tracked (was only tracking 3 of 10)
- Profit factor calculation fixed (was always ≈1.0 due to avg/avg bug)
- Sharpe ratio computed from actual trade P&L (was using placeholder data)
- Drawdown-based strategy throttling: losing strategies auto-reduced 25-50%

### AI Calibration Tracker
- Logs every AI prediction vs actual outcome to CSV
- Computes Brier score and calibration curve
- Auto-adjusts confidence per category based on historical accuracy
- Dampened adjustments prevent overreaction

### Whale/Large Trade Detection
- Monitors Data API for single trades >$5K USD
- Tracks "spike" events per market with trader address and direction
- Exposes API for strategies to check for informed money activity
- Configurable via WHALE_MIN_TRADE_USD, WHALE_LOOKBACK_MINUTES

### Sentiment Scoring
- Lightweight keyword-based sentiment scorer added to NewsAggregator
- Scores headlines from -1.0 (bearish) to +1.0 (bullish)
- Sentiment score added to MarketContext for AI strategy use

### Correlation-Aware Position Limits
- Blocks new trades when 3+ open positions share the same category
- Prevents concentrated exposure to a single event type
- Category detection uses keyword matching on market questions

---

## ON HOLD — Requires Larger Bankroll ($1K+)

### Portfolio-Level Kelly Criterion
- Replace per-trade Kelly with portfolio-level allocation
- Account for correlation between simultaneous positions
- **Why deferred:** At $5 micro trades, individual Kelly sizing is already floored at minimum. Portfolio-level allocation only matters when positions are large enough to interact.

### Multi-Exchange Arbitrage (Kalshi, Manifold)
- Cross-platform price discrepancy detection
- Unified signal format across exchanges
- **Why deferred:** Cross-platform arb requires capital on multiple exchanges simultaneously. At micro scale, the fees and minimums eat the edge.

### Full Market Making v2
- Dynamic spread adjustment, inventory management, Greeks-aware positioning
- **Why deferred:** Market making requires significant capital ($10K+) to absorb fills and manage inventory risk. Not viable at micro scale.

### Decentralized Multi-Node Deployment
- Redundant bot instances across VPS nodes with automatic failover
- **Why deferred:** Only justified when capital at risk warrants infrastructure redundancy.

### Reinforcement Learning Strategy Weights
- RL agent (PPO/A2C) for dynamic weight allocation
- State = market regime + portfolio + recent performance
- **Why deferred:** Needs hundreds of trades for training data. Also requires GPU/significant compute for model training. Will be viable after 500+ trades accumulated.

---

## ON HOLD — Requires Infrastructure Investment

### Live Web Dashboard
- Flask/FastAPI real-time monitoring UI
- Open positions, P&L curves, strategy performance charts
- Alert system for kill switch and drawdowns
- **Why deferred:** Requires additional VPS port, HTTPS setup, and ongoing maintenance. Will implement when the bot is consistently profitable.

### L2 Order Book Analysis
- Parse full order book depth for support/resistance detection
- Iceberg order detection for informed trader activity
- **Why deferred:** Requires persistent WebSocket connections (currently disabled due to SSL issues on VPS). Will enable when WS is stable.

### Backtesting Framework
- Historical data replay engine
- Walk-forward optimization
- Monte Carlo risk simulation
- **Why deferred:** Requires historical trade data collection (not yet available). Will build after 3+ months of live data accumulated.

---

## ON HOLD — Requires API Access / Cost

### AI Ensemble (Claude + GPT + Gemini)
- Multi-model probability estimation with consensus weighting
- Calibration tracking per model
- **Why deferred:** Each additional model costs API credits. Claude alone is sufficient for micro-scale validation. Will add when ROI justifies the API spend.

### Twitter/X Sentiment Analysis
- Real-time sentiment scoring via Twitter API
- Correlation of sentiment shifts with price movements
- **Why deferred:** Twitter API costs $100+/month for real-time access. Will implement when consistent profits justify the expense.
