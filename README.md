# Polymarket Multi-Strategy Trading Bot

A production-ready, modular Python trading bot for [Polymarket](https://polymarket.com) (Polygon network).  It runs ten independent strategies simultaneously with adaptive self-learning, Bayesian position re-evaluation, and can be deployed locally or on a remote VPS.

---

## Table of Contents

1. [What the Bot Does](#what-the-bot-does)
2. [Prerequisites](#prerequisites)
3. [Installation](#installation)
4. [Getting Your Private Key](#getting-your-private-key)
5. [Configuration (.env)](#configuration)
6. [Trading Modes (Paper / Micro / Live)](#trading-modes)
7. [Wallet Auto-Discovery](#wallet-auto-discovery)
8. [Trade Management](#trade-management)
9. [Trade History & Performance Tracking](#trade-history)
10. [Self-Learning Optimizer](#self-learning-optimizer)
11. [Deploying to a VPS](#deploying-to-a-vps)
12. [Architecture](#architecture)
13. [Adding a New Strategy](#adding-a-new-strategy)
14. [Risk Warnings](#risk-warnings)
15. [Changelog](#changelog)

---

## What the Bot Does

### Strategy 1 — Sum-to-One Arbitrage (`arbitrage`)

Every Polymarket binary market resolves to exactly **$1.00** per winning share.  Since YES + NO = $1.00 at resolution, whenever the combined *ask* price of both sides falls below `1.00 - 2% fee`, there is a risk-free arbitrage.

The bot:
1. Fetches best-ask prices for YES and NO on every scanned market.
2. Computes `edge = 0.98 - (yes_ask + no_ask)`.
3. If `edge > ARBITRAGE_MIN_EDGE` (default 2%), places simultaneous FOK (fill-or-kill) orders on both sides.
4. Sizes the trade to available order-book depth, capped by `MAX_POSITION_SIZE`.

### Strategy 2 — Copy Trading (`copy_trading`)

Mirrors BUY activity from a configurable target wallet.

The bot:
1. Polls `https://data-api.polymarket.com/activity?user={TARGET_WALLET}` for new trades.
2. Filters to BUY side only (never mirrors SELL orders).
3. Skips trades older than `COPY_TRADE_MAX_AGE` seconds.
4. Skips if current price has drifted > 5 cents from the target's fill price.
5. Places a GTC limit order for `COPY_TRADE_SIZE` USD on the same token.

### Strategy 3 — Signal/Value (`signal_based`)

Scores markets across four weighted signals:

| Signal | Weight | Description |
|--------|--------|-------------|
| Volume spike | 30% | Current volume is ≥ 2× the EMA baseline |
| Price momentum | 25% | Strong directional move detected in recent history |
| Value / mispricing | 25% | Price in the 5–30 cent range with meaningful volume |
| Spread width | 20% | Wide bid/ask spread in a sub-50-cent market |

If the composite score exceeds `SIGNAL_MIN_EDGE` (default 5%), the bot places a GTC limit order 1 cent above the best ask.

### Strategy 4 — Crypto Mean Reversion (`crypto_mean_reversion`)

Targets short-duration crypto Up/Down markets (e.g. "Will BTC go up in the next 5 minutes?"). Uses Binance kline data to detect overextended moves and trades the reversion.

### Strategy 5 — Contrarian Extreme (`contrarian_extreme`)

Fades markets priced at extremes (90%+ or sub-10%). These prices imply near-certainty, but surprise outcomes yield asymmetric payoffs.

### Strategy 6 — AI-Powered (`ai_powered`)

Uses Claude to estimate true probabilities for Polymarket markets, then trades when the market price diverges by more than `AI_MIN_EDGE` (default 8%). Evaluates up to 10 markets per cycle. Requires `ANTHROPIC_API_KEY`.

### Strategy 7 — Sports Momentum (`sports_momentum`)

Detects live sports event momentum from rapid price/volume movements and rides the trend with tight trailing stops.

### Strategy 8 — Cross-Market Arbitrage (`cross_market_arb`)

Identifies pricing inconsistencies across related event groups using KL-divergence and temporal consistency checks.

### Strategy 9 — Weather Forecast Arbitrage (`weather_forecast_arb`)

Compares NOAA/Open-Meteo forecast data against Polymarket weather market prices to find mispriced weather outcomes.

### Strategy 10 — LP Rewards (`lp_rewards`) *(v34)*

Earns liquidity provider rewards by placing bid/ask limit orders around the midpoint on high-liquidity markets.

The bot:
1. Fetches reward-eligible markets from the Gamma API.
2. Allocates `LP_CAPITAL_PCT` (default 20%) of bankroll across up to `LP_MAX_MARKETS` markets.
3. Places bid and ask orders at midpoint ± 2¢ spread.
4. Refreshes orders every `LP_REFRESH_INTERVAL` seconds (default 300s) to stay near midpoint.

---

## Prerequisites

- **Python 3.10+** (3.12 recommended)
- **Polymarket account** with a funded wallet (USDC on Polygon)
- **Private key** for your trading wallet (see below)
- Internet access to reach Polymarket APIs

---

## Installation

```bash
# 1. Clone / download the bot files
cd polymarket-bot

# 2. Create a virtual environment (recommended)
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the example config and fill it in
cp .env.example .env
nano .env   # or use any editor
```

---

## Getting Your Private Key

### For Email / Magic.link accounts (SIGNATURE_TYPE=1)

1. Go to [https://reveal.polymarket.com](https://reveal.polymarket.com).
2. Sign in with the same email you use on Polymarket.
3. Copy your private key — it looks like `0x` followed by 64 hex characters.
4. Paste it as `PRIVATE_KEY` in your `.env`.
5. Set `POLYMARKET_PROXY_ADDRESS` to the wallet address shown on your profile.
6. Set `SIGNATURE_TYPE=1`.

### For Browser Wallet accounts (MetaMask etc.) (SIGNATURE_TYPE=2)

1. In MetaMask: Settings → Security & Privacy → Reveal Private Key.
2. Set `POLYMARKET_PROXY_ADDRESS` to your Polymarket proxy address (visible in the URL after logging in).
3. Set `SIGNATURE_TYPE=2`.

### For raw EOA wallets (SIGNATURE_TYPE=0)

1. Export the private key from your hardware wallet or key management tool.
2. Set `SIGNATURE_TYPE=0`.
3. Leave `POLYMARKET_PROXY_ADDRESS` blank.
4. **Important:** Run the allowance script once to approve the Polymarket exchange contract to spend your USDC:
   ```
   https://github.com/Polymarket/py-clob-client/blob/master/examples/set_allowance.py
   ```

> **Security:** Never share your private key. Never commit `.env` to git.

---

## Configuration

Edit `.env` (copy from `.env.example`):

```dotenv
# Required
PRIVATE_KEY=0xYOUR_KEY_HERE
POLYMARKET_PROXY_ADDRESS=0xYOUR_PROXY_ADDRESS   # for sig types 1 & 2
SIGNATURE_TYPE=1                                # 0=EOA, 1=Magic, 2=browser

# Copy-trading target
TARGET_WALLET=0xTARGET_WALLET

# Safety: start with paper trading!
PAPER_TRADE=true

# Risk limits
MAX_POSITION_SIZE=50        # max $ per trade
MAX_TOTAL_EXPOSURE=500      # max total $ across all positions
MAX_POSITIONS=10            # max concurrent positions
MIN_LIQUIDITY=10000         # min market volume $
KILL_SWITCH_THRESHOLD=-100  # halt if daily P&L drops below this

# Strategy tuning
ARBITRAGE_MIN_EDGE=0.02     # 2% minimum edge after fees
COPY_TRADE_SIZE=10          # $ per copy trade
COPY_TRADE_MAX_AGE=120      # skip trades older than 2 min
SIGNAL_MIN_EDGE=0.05        # 5% composite score to trade
MAX_SLIPPAGE=0.03           # abort if price moved > 3 cents

# Bot behaviour
POLL_INTERVAL=30            # seconds between scans
LOG_LEVEL=INFO
```

---

## Trading Modes

The bot supports three trading modes, configured via `TRADING_MODE` in `.env` or a CLI flag.

### Paper Mode (`--paper`)

All trade decisions are logged but **no orders are ever submitted** to Polymarket.  Use this to:
- Validate strategy logic before risking real money
- Test configuration changes
- Monitor the bot's behaviour without financial exposure

```bash
python main.py --paper
# or: TRADING_MODE=paper in .env
```

Watch `logs/bot.log` to verify the bot is finding signals and risk limits are working as expected.

### Micro Mode (`--micro`) ⭐ Recommended First Live Step

Places **real orders** on Polymarket, but caps each trade at `MICRO_TRADE_SIZE` USD (default $1.50).  This is the recommended intermediate step between paper and live trading:

- Verifies your wallet credentials and API connectivity work end-to-end
- Produces real performance data in `trade_history.csv`
- Limits maximum financial exposure to $1-2 per trade

```bash
python main.py --micro
# or: TRADING_MODE=micro in .env
```

In micro mode, the risk manager applies relaxed size checks (since each trade is tiny), but the position count limit and kill switch are still enforced.

### Live Mode (`--live`)

Full-size orders using the sizes calculated by each strategy.  Only switch to live mode after successfully validating in paper and micro modes.

```bash
python main.py --live
# or: TRADING_MODE=live in .env
```

Start with conservative risk limits:

```dotenv
MAX_POSITION_SIZE=10
MAX_TOTAL_EXPOSURE=100
KILL_SWITCH_THRESHOLD=-20
```

Run a single strategy for initial testing:

```bash
python main.py --live --strategies arbitrage
python main.py --live --strategies copy_trading
python main.py --live --strategies signal_based
```

---

## Wallet Auto-Discovery

Instead of manually specifying a `TARGET_WALLET` to copy, the bot can automatically discover profitable wallets from the Polymarket leaderboard.

### How it works

1. Fetches the top traders from the leaderboard API across multiple time periods (WEEK and MONTH) and categories.
2. For each candidate, fetches their closed positions and applies **strict filters** *(v34)*:
   - Minimum **80 resolved trades** (filters out luck)
   - No single trade > 30% of total PnL (filters out one-hit wonders)
   - Average entry price between 25¢–65¢ (filters out extreme-price gamblers)
   - Active within the last **14 days**
3. Scores each wallet using an enhanced composite formula *(v34)*:
   - `S(w) = α·PnL + β·Consistency + γ·Specialization − δ·MaxDD`
   - PnL (α=0.35): Total realized profit, normalized
   - Consistency (β=0.25): Inverse standard deviation of monthly returns
   - Specialization (γ=0.25): Herfindahl index of category distribution (focused traders score higher)
   - Max Drawdown (δ=0.15): Peak-to-trough drawdown penalty
4. **Category-locking** *(v34)*: Tracks per-category performance for each wallet. Copy-trading only mirrors trades in categories where the wallet has demonstrated strength.
5. Returns the top `MAX_COPY_WALLETS` wallets to the copy-trading strategy.

Discovery results are cached for `WALLET_DISCOVERY_INTERVAL` seconds (default 6 hours) to avoid excessive API calls.

### Configuration

```dotenv
AUTO_DISCOVER_WALLETS=true    # Enable auto-discovery
WALLET_DISCOVERY_INTERVAL=21600  # Re-discover every 6 hours
MIN_WIN_RATE=0.55             # Require at least 55% win rate
MIN_CLOSED_POSITIONS=20       # Require at least 20 closed positions
MAX_COPY_WALLETS=3            # Follow top 3 wallets
WALLET_CATEGORIES=OVERALL     # Leaderboard categories (comma-separated)
TARGET_WALLET=                # Leave blank to use auto-discovery
```

**Manual override:** Setting `TARGET_WALLET` takes precedence over auto-discovery.  The bot will always use the manually configured wallet when set.

---

## Trade Management

The `TradeManager` monitors all open positions each cycle and automatically exits positions based on configurable rules.

### Hold-to-Resolution Mode *(v34)*

When `HOLD_TO_RESOLUTION=true` (default), the bot prefers holding positions to market resolution rather than taking early exits. Take-profit and time-based exits are skipped unless the Bayesian re-evaluation determines that expected value has turned negative. Stat: held-to-resolution trades average +74% profit vs +18% for early exits.

### Bayesian Re-evaluation *(v34)*

Every `REEVALUATE_INTERVAL` cycles (default 10), the bot uses Claude to re-evaluate open AI-powered positions with Bayes' theorem: `P(H|E) = P(E|H) × P(H) / P(E)`. If the updated probability shows negative EV, the position is flagged for exit regardless of hold-to-resolution mode.

### Base Rate Sizing *(v34)*

Before executing any BUY signal, the bot checks the market's category base rate. If the category has a historical base rate below `BASE_RATE_MIN` (default 12%), position size is reduced by `BASE_RATE_SIZE_CUT` (default 50%).

### Exit rules (evaluated in priority order)

| Priority | Rule | Default | Description |
|----------|------|---------|-------------|
| 1 | **Stop-loss** | -10% | Submit a FOK (market) sell order immediately when unrealised P&L drops below threshold |
| 2 | **Trailing stop** | 5% retracement | Once a position gains 10%+, set a trailing stop that moves with the price and triggers on a 5% pullback from peak |
| 3 | **Take-profit** | +15% | Submit a GTC (limit) sell order when unrealised P&L exceeds the threshold (skipped if `HOLD_TO_RESOLUTION=true` and EV is positive) |
| 4 | **Time exit** | 24 hours | Force-close any position that has been open longer than `MAX_HOLD_TIME` seconds (skipped if `HOLD_TO_RESOLUTION=true` and EV is positive) |

### Configuration

```dotenv
TAKE_PROFIT_PCT=0.15          # Take profit at +15%
STOP_LOSS_PCT=0.10            # Stop loss at -10%
MAX_HOLD_TIME=86400           # Exit after 24 hours (in seconds)
TRAILING_STOP_ACTIVATION=0.10 # Activate trailing stop after +10% gain
TRAILING_STOP_PCT=0.05        # Trailing stop triggers on 5% pullback from peak
HOLD_TO_RESOLUTION=true       # Prefer holding to resolution (v34)
REEVALUATE_INTERVAL=10        # Re-evaluate AI positions every N cycles (v34)
BASE_RATE_MIN=0.12            # Minimum base rate for full sizing (v34)
BASE_RATE_SIZE_CUT=0.50       # Size cut for low base rate categories (v34)
```

### Trailing stop example

1. Bot buys YES at $0.40.
2. Price rises to $0.44 (+10%) — trailing stop activates.  Stop set at $0.44 × (1 - 0.05) = $0.418.
3. Price rises further to $0.50 — stop ratchets up to $0.50 × (1 - 0.05) = $0.475.
4. Price falls to $0.47 — below the trailing stop of $0.475 → bot sells.

---

## Trade History

Every executed trade (paper, micro, or live) is recorded to `trade_history.csv` in the bot's working directory.  The file persists across bot restarts.

### CSV columns

| Column | Description |
|--------|-------------|
| `timestamp` | Unix timestamp of the trade |
| `strategy` | Strategy that generated the signal (`arbitrage`, `copy_trading`, `signal_based`, `crypto_mean_reversion`, `contrarian_extreme`, `ai_powered`, `sports_momentum`, `cross_market_arb`, `weather_forecast_arb`, `lp_rewards`, `trade_manager`) |
| `market_id` | Polymarket condition ID |
| `token_id` | Outcome token ID |
| `side` | `BUY` or `SELL` |
| `price` | Fill price |
| `size` | Shares traded |
| `usd_value` | USD value of the trade (price × size) |
| `order_type` | `GTC` or `FOK` |
| `mode` | `paper`, `micro`, or `live` |
| `order_id` | Exchange order ID (empty for paper trades) |
| `status` | Order status from the exchange |
| `reason` | Human-readable signal reason |

### Performance report

On shutdown, the bot automatically prints a summary:

```
============================================================
  TRADE HISTORY REPORT
============================================================
  History file : trade_history.csv
  Total trades : 47
  BUYs         : 31
  SELLs        : 16
  Volume (USD) : $284.50

  By Mode:
    live            12 trades
    micro           35 trades

  By Strategy:
    Strategy             Trades   BUYs  SELLs       Volume
    -------------------- ------- ------ ------- ------------
    arbitrage                 8      8      0       $32.00
    copy_trading             18     12      6       $96.40
    signal_based             11      8      3       $62.80
    trade_manager            10      0     10       $93.30
============================================================
```

---

---

## Self-Learning Optimizer

The bot includes an adaptive self-learning engine (`strategy_optimizer.py`) that analyses its own trade history and automatically improves its performance over time.

### How It Works

Every `OPTIMIZER_INTERVAL` seconds (default: 1 hour) — and only after at least `OPTIMIZER_MIN_TRADES` trades (default: 50) — the optimizer runs a four-step pipeline:

1. **Strategy performance analysis** — Computes win rate, profit factor, Sharpe ratio, and average P&L per strategy by pairing BUY→SELL round-trips from the trade history CSV.

2. **Market regime detection** — Classifies the current environment as `trending`, `mean_reverting`, `choppy`, or `unknown` based on which trade types are succeeding or failing.

3. **Strategy weight adjustment** — Shifts allocation toward strategies with higher risk-adjusted returns.  A strategy producing 60%+ win rate gets more signals executed; one below 40% gets throttled.  Every strategy always retains at least 10% allocation to prevent data starvation.

4. **Parameter tuning** — Adjusts individual thresholds based on trade outcomes:
   - `SIGNAL_MIN_EDGE` — raised if signal strategy has low win rate, lowered if very high
   - `TAKE_PROFIT_PCT` — raised if too many trades hit TP (leaving money on the table), lowered if too few reach TP
   - `STOP_LOSS_PCT` — widened if too many stop-loss exits, tightened if too few
   - `MAX_HOLD_TIME` — shortened if time exits lose money, lengthened if profitable
   - Signal sub-weights (volume, momentum, value, spread) — shifted toward sub-signals correlated with winning trades

### Conservative Guardrails

| Guardrail | Detail |
|-----------|--------|
| **Minimum data** | No adaptation until 50+ trades are recorded |
| **Maximum shift** | Parameters can change at most ±15% per cycle |
| **Cumulative cap** | Total drift from baseline limited to ±30% |
| **Hard limits** | Every parameter has absolute min/max bounds |
| **Strategy floor** | Even the worst strategy keeps ≥10% allocation |
| **Performance floor** | If overall win rate drops below 35%, all parameters revert to baseline |
| **Persistence** | State saved to `optimizer_state.json` — survives restarts |
| **Logging** | Every adjustment is logged with before/after values |

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OPTIMIZER_ENABLED` | `true` | Enable/disable the self-learning engine |
| `OPTIMIZER_MIN_TRADES` | `50` | Minimum trades before adapting |
| `OPTIMIZER_INTERVAL` | `3600` | Seconds between optimization cycles |
| `OPTIMIZER_MAX_SHIFT` | `0.15` | Maximum parameter change per cycle (15%) |
| `OPTIMIZER_LOOKBACK` | `200` | Number of recent trades to analyse |

### Monitoring

The optimizer logs its activity at INFO level:

```
Optimizer cycle 3 starting (127 trades available)
Perf [arbitrage]: trades=18 wr=61.1% pnl=$2.34 avg=$0.1300 pf=1.85 sharpe=0.42 hold=1.2h
Perf [signal_based]: trades=52 wr=53.8% pnl=$1.12 avg=$0.0215 pf=1.23 sharpe=0.31 hold=4.7h
Weight adjusted: arbitrage 33.3% → 38.1%
Weight adjusted: signal_based 33.4% → 29.2%
Regime change detected: unknown → trending (stop_loss_rate=8.3%)
Optimizer cycle 3 complete
```

To review the full optimization history, inspect `optimizer_state.json` — it contains the performance log, current weights, baseline parameters, and all tuned values.

---

## Deploying to a VPS

### Hetzner VPS setup (Ubuntu 22.04)

```bash
# 1. Connect to your VPS
ssh root@YOUR_VPS_IP

# 2. Install Python and pip
apt update && apt install -y python3 python3-pip python3-venv git

# 3. Copy bot files (from your local machine)
scp -r polymarket-bot/ root@YOUR_VPS_IP:/opt/polymarket-bot/

# 4. Set up virtual environment
cd /opt/polymarket-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 5. Configure .env
cp .env.example .env
nano .env   # fill in your credentials
```

### Running with PM2 (recommended)

PM2 keeps the bot running 24/7 and auto-restarts on crash:

```bash
# Install Node.js and PM2
apt install -y nodejs npm
npm install -g pm2

# Start the bot
pm2 start "venv/bin/python main.py --live" \
    --name polymarket-bot \
    --cwd /opt/polymarket-bot \
    --log /opt/polymarket-bot/logs/pm2.log \
    --restart-delay 5000

# Auto-start on reboot
pm2 startup
pm2 save

# Useful PM2 commands
pm2 logs polymarket-bot       # live log stream
pm2 status                    # process status
pm2 stop polymarket-bot       # stop the bot
pm2 restart polymarket-bot    # restart
```

### Running with systemd (alternative)

```bash
# Create service file
cat > /etc/systemd/system/polymarket-bot.service << 'EOF'
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/polymarket-bot
ExecStart=/opt/polymarket-bot/venv/bin/python main.py --live
Restart=always
RestartSec=10
StandardOutput=append:/opt/polymarket-bot/logs/bot.log
StandardError=append:/opt/polymarket-bot/logs/bot.log
EnvironmentFile=/opt/polymarket-bot/.env

[Install]
WantedBy=multi-user.target
EOF

# Enable and start
systemctl daemon-reload
systemctl enable polymarket-bot
systemctl start polymarket-bot
systemctl status polymarket-bot
```

---

## Architecture

```
polymarket-bot/
│
├── main.py               ← Entry point; orchestrates all components
│   └── TradingBot        ← Main class: init → run loop → shutdown
│
├── config.py             ← Typed config loaded from .env
├── logger_setup.py       ← Structured logging (console + rotating file)
├── client_manager.py     ← ClobClient singleton + L2 credential init
├── http_client.py        ← Shared HTTP session pool (v34)
│
├── market_scanner.py     ← Gamma API market discovery + classify_market()
│   └── MarketScanner     ← Caches MarketInfo with TTL
│
├── websocket_manager.py  ← Real-time WebSocket for market + user channels
│   └── WebSocketManager  ← Async; runs in background thread
│
├── ai_probability_engine.py ← Claude-powered probability estimation + Bayesian re-eval
├── binance_indicators.py    ← Binance kline data for crypto strategies
├── news_aggregator.py       ← News feed aggregation for AI context
├── price_history.py         ← Shared price history tracker (v34)
│
├── strategies/
│   ├── base.py              ← BaseStrategy ABC + TradeSignal dataclass
│   ├── arbitrage.py         ← Sum-to-one arb (FOK orders)
│   ├── copy_trading.py      ← Mirror wallet(s) with category-locking (v34)
│   ├── signal_based.py      ← Volume/momentum/value/spread composite
│   ├── crypto_mean_reversion.py ← Mean-reversion on crypto Up/Down markets
│   ├── contrarian_extreme.py    ← Fade extreme prices (90%+)
│   ├── ai_powered.py           ← Claude-powered probability vs market price
│   ├── sports_momentum.py      ← Live sports event momentum trading
│   ├── cross_market_arb.py     ← KL-divergence cross-event arbitrage
│   ├── weather_forecast_arb.py ← NOAA/Open-Meteo vs market weather prices
│   └── lp_rewards.py           ← LP rewards market-making (v34)
│
├── execution.py          ← Order building, slippage check, submission
│   └── Executor          ← Wraps py-clob-client; paper/micro/live modes
│
├── risk_manager.py       ← Pre-trade gates; kill switch; daily P&L
│   └── RiskManager
│
├── position_tracker.py   ← Open positions, P&L, persistence (JSON)
│   └── PositionTracker
│
├── trade_manager.py      ← Position exits + Bayesian re-eval + base rate sizing (v34)
│   └── TradeManager      ← Called each cycle; closes positions automatically
│
├── trade_history.py      ← Persistent CSV trade log + log returns (v34)
│   └── TradeHistory      ← Records all trades; prints report on shutdown
│
├── wallet_discovery.py   ← Enhanced wallet scoring + category tracking (v34)
│   └── WalletDiscovery   ← Leaderboard API; scores and ranks traders
│
├── strategy_optimizer.py ← Self-learning adaptive engine
│   └── StrategyOptimizer ← Analyses trade history; tunes weights & params
│
├── redeemer.py           ← Auto-redeem resolved positions
│
├── .env.example          ← Config template
├── requirements.txt      ← Python dependencies
├── CHANGELOG.md          ← Version changelog
├── ROADMAP.md            ← Future development plans
├── trade_history.csv     ← Auto-created: persistent trade log (all modes)
├── optimizer_state.json  ← Auto-created: optimizer state (survives restarts)
└── logs/
    └── bot.log           ← Rotating log file

Data flow:
  MarketScanner ──────────────────────────────────────────────────┐
  WebSocketManager (real-time updates) ──────────────────────────►│
  WalletDiscovery (leaderboard API, cached 6h) ──────────────────►│
                                                                   ▼
  Strategy.scan() → [TradeSignal] → RiskManager.approve_trade()
                                           │ approved
                                           ▼
                                    Executor.execute()  ← paper/micro/live
                                           │ result
                                           ▼
                                  PositionTracker.record_trade()
                                  RiskManager.update_pnl()
                                  TradeHistory.record_trade()
                                           │
                           (next cycle) TradeManager.manage_positions()
                                    checks TP/SL/trail/time → SELL signal
```

**APIs used:**
- `https://gamma-api.polymarket.com/markets` — Market discovery (public)
- `https://clob.polymarket.com` — Order placement, pricing (auth required for trading)
- `https://data-api.polymarket.com` — Positions, trade history (public)
- `wss://ws-subscriptions-clob.polymarket.com/ws/market` — Real-time prices (public)
- `wss://ws-subscriptions-clob.polymarket.com/ws/user` — Order/trade events (auth)

---

## Adding a New Strategy

1. Create `strategies/my_strategy.py`:

```python
from strategies.base import BaseStrategy, TradeSignal
from typing import List

class MyStrategy(BaseStrategy):
    def name(self) -> str:
        return "my_strategy"

    def scan(self) -> List[TradeSignal]:
        signals = []
        for market in self.market_scanner.get_markets():
            # ... your logic ...
            signal = TradeSignal(
                strategy=self.name(),
                market_id=market.market_id,
                token_id=market.yes_token.token_id,
                side="BUY",
                price=0.45,
                size=10.0,
                confidence=0.7,
                reason="My custom reason",
                order_type="GTC",
            )
            signals.append(signal)
        return signals
```

2. Register it in `strategies/__init__.py`.
3. Add it to the `all_strategies` dict in `main.py`.
4. Run with `python main.py --strategies my_strategy`.

---

## Risk Warnings

**Trading prediction markets carries significant financial risk.  You can lose all funds you deploy.**

- Always run in `PAPER_TRADE=true` mode first and verify behaviour before going live.
- Start with small position sizes (`MAX_POSITION_SIZE=5`) while validating the bot.
- The arbitrage strategy assumes FOK orders will fill simultaneously.  In practice, one leg may fill while the other does not, creating an unhedged position.
- The copy-trading strategy blindly mirrors another wallet.  The target wallet may make poor trades.
- The signal strategy uses simple heuristics; it is not a sophisticated model.
- Polymarket markets can resolve unexpectedly.  A "YES" at 5 cents can go to $0.
- Set `KILL_SWITCH_THRESHOLD` to a loss you are comfortable with.
- The authors of this software accept no responsibility for financial losses.

**Use at your own risk.**

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a detailed list of changes in each version.
