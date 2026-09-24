"""Crypto prediction bot: learns from past market events and forecasts the next candle."""

from .bot import BotConfig, BotResult, Prediction, PredictionBot

__all__ = ["BotConfig", "BotResult", "Prediction", "PredictionBot"]
__version__ = "0.1.0"
