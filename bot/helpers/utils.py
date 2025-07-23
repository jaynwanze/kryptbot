import math
from bot.helpers import config
import logging
import pandas as pd
from telegram import Bot

# helpers/utils.py  (or wherever you placed it)
def round_price(price: float, pair: str) -> float:
    """Round price to the nearest tick size for the given trading pair."""
    tick = config.TICK_SIZE.get(pair)
    if tick is None:
        raise ValueError(f"Unknown tick size for '{pair}'. "
                         "Add it to config.TICK_SIZE or fetch it from the exchange.")
    quantised = round(price / tick) * tick          # snap to grid
    return round(quantised, int(-math.log10(tick))) # keep the right decimals


def alert_side(pair: str, bar: pd.Series, side: str, bot: Bot, chat_id: str) -> None:
    """Send a nicely‑formatted alert to Telegram."""
    stop_off = (config.ATR_MULT_SL * 1.6 + config.WICK_BUFFER) * bar.atr
    if side == "LONG":
        sl = round_price(bar.c - stop_off, pair)
        tp = round_price(bar.c + config.ATR_MULT_TP * bar.atr, pair)
        emoji = "📈"
    else:
        sl = round_price(bar.c + stop_off, pair)
        tp = round_price(bar.c - config.ATR_MULT_TP * bar.atr, pair)
        emoji = "📉"

    msg = (
        "*LRS MULTI PAIR ENGINE LIVE TRADING BOT*\n"
        f"{emoji} *{pair} {config.INTERVAL}-m {side} signal*\n"
        f"`{bar.name:%Y-%m-%d %H:%M}` UTC\n"
        f"Entry  : `{bar.c:.4f}`\n"
        f"Stop   : `{sl:.4f}`\n"
        f"Target : `{tp:.4f}`\n"
        f"ADX    : `{bar.adx:.1f}`  |  StochK: `{bar.k_fast:.1f}`"
    )
    try:
        bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        logging.info("Telegram alert sent  – %s  %s", pair, side)
    except Exception as exc:
        logging.error("Telegram error: %s", exc)