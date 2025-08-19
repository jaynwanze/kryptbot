from telegram import Bot
from dotenv import load_dotenv
from telegram.utils.helpers import escape_markdown
import sys, os, logging
import pandas as pd

# ───────── Telegram  ─────────
load_dotenv()
TG_TOKEN = os.getenv("TELE_TOKEN")
TG_CHAT_ID = int(os.getenv("TG_CHAT_ID"))
bot = Bot(token=TG_TOKEN)


def alert_side(
    pair: str,
    bar: pd.Series,
    timeframe: str,
    side: str,
    stop_off: float,
    tp_dist: float,
    header: str = "LRS MULTI-PAIR Engine",
) -> None:
    if side == "LONG":
        sl, tp, emoji = bar.c - stop_off, bar.c + tp_dist, "📈"
    else:
        sl, tp, emoji = bar.c + stop_off, bar.c - tp_dist, "📉"

    msg_raw = (
        f"{emoji} *({header})* {pair} {timeframe}m {side}\n"
        f"`{bar.name:%Y-%m-%d %H:%M}` UTC\n"
        f"Entry  : `{bar.c:.3f}`\n"
        f"Stop   : `{sl:.3f}`\n"
        f"Target : `{tp:.3f}`\n"
        f"ADX    : `{bar.adx:.1f}` | StochK: `{bar.k_fast:.1f}`"
        f" | StochS: `{bar.k_slow:.1f}` | StochD: `{bar.d_slow:.1f}`\n"
    )

    try:
        bot.send_message(
            TG_CHAT_ID, escape_markdown(msg_raw, version=2), parse_mode="MarkdownV2"
        )
        logging.info("[%s] Telegram alert sent (%s)", pair, side)
    except Exception as exc:
        logging.error("[%s] Telegram error: %s", pair, exc)


def bybit_alert(msg: str) -> None:
    try:
        bot.send_message(
            TG_CHAT_ID, escape_markdown(msg, version=2), parse_mode="MarkdownV2"
        )
        logging.info("[Bybit] Telegram alert sent")
    except Exception as exc:
        logging.error("[Bybit] Telegram error: %s", exc)
