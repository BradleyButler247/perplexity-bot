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

All strategies inherit from BaseStrategy and implement the scan() method.
"""

from strategies.base import BaseStrategy, TradeSignal
from strategies.arbitrage import ArbitrageStrategy
from strategies.copy_trading import CopyTradingStrategy
from strategies.signal_based import SignalBasedStrategy
from strategies.crypto_mean_reversion import CryptoMeanReversionStrategy
from strategies.contrarian_extreme import ContrarianExtremeStrategy

__all__ = [
    "BaseStrategy",
    "TradeSignal",
    "ArbitrageStrategy",
    "CopyTradingStrategy",
    "SignalBasedStrategy",
    "CryptoMeanReversionStrategy",
    "ContrarianExtremeStrategy",
]
