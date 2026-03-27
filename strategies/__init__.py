"""
strategies/
-----------
Trading strategy implementations for the Polymarket bot.

Available strategies:
  • ArbitrageStrategy              — Sum-to-one arbitrage (YES + NO < 1.00)
  • CopyTradingStrategy            — Mirror a target wallet's BUY activity
  • SignalBasedStrategy            — Value/signal composite scoring
  • CryptoMeanReversionStrategy   — Mean-reversion on 5-min crypto Up/Down markets
  • ContrarianExtremeStrategy     — Fade extreme prices (90%+) for asymmetric payoffs
  • AIPoweredStrategy             — Claude-powered probability estimation vs market price
  • SportsMomentumStrategy        — Ride live sports event momentum from price/volume spikes
  • CrossMarketArbStrategy        — KL-divergence + temporal consistency across event groups
  • WeatherForecastArbStrategy    — NOAA/Open-Meteo forecast vs Polymarket weather prices

All strategies inherit from BaseStrategy and implement the scan() method.
"""

from strategies.base import BaseStrategy, TradeSignal
from strategies.arbitrage import ArbitrageStrategy
from strategies.copy_trading import CopyTradingStrategy
from strategies.signal_based import SignalBasedStrategy
from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
from strategies.contrarian_extreme import ContrarianExtremeStrategy
from strategies.ai_powered import AIPoweredStrategy
from strategies.sports_momentum import SportsMomentumStrategy
from strategies.cross_market_arb import CrossMarketArbStrategy
from strategies.weather_forecast_arb import WeatherForecastArbStrategy
from strategies.lp_rewards import LPRewardsStrategy

__all__ = [
    "BaseStrategy",
    "TradeSignal",
    "ArbitrageStrategy",
    "CopyTradingStrategy",
    "SignalBasedStrategy",
    "CryptoMeanReversionStrategy",
    "ContrarianExtremeStrategy",
    "AIPoweredStrategy",
    "SportsMomentumStrategy",
    "CrossMarketArbStrategy",
    "WeatherForecastArbStrategy",
    "LPRewardsStrategy",
]
