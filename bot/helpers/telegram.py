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


def alert_side_fvg_orderflow_signal(
    pair: str,
    bar,
    tf: str,
    side: str,
    stop_price: float,      # Rename to stop_price
    tp1_price: float,       # Rename to tp1_price
    tp2_price: float,      # Rename to tp2_price
    header: str = "FVG Order Flow",
):
    """Alert for FVG Order Flow signals with dual TP"""
    try:
        # Calculate offsets for percentage display
        entry = bar.c
        stop_pct = abs(stop_price - entry) / entry * 100
        tp1_pct = abs(tp1_price - entry) / entry * 100
        tp2_pct = abs(tp2_price - entry) / entry * 100

        msg = (
            f"🎯 *{header}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Pair: `{pair}`\n"
            f"⏰ TF: `{tf}m`\n"
            f"📍 Side: *{side}*\n"
            f"💰 Price: `{entry:.5f}`\n"  # Changed to 5 decimals for crypto
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🛡 Stop: `${stop_price:.5f}` ({stop_pct:.2f}%)\n"
            f"🎯 TP1: `${tp1_price:.5f}` ({tp1_pct:.2f}%)\n"
            f"🎯 TP2: `${tp2_price:.5f}` ({tp2_pct:.2f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 ADX: `{bar.adx:.1f}`\n"
            f"📊 Volume: `{bar.v:.0f}`\n"
        )

        bot.send_message(
            TG_CHAT_ID, escape_markdown(msg, version=2), parse_mode="MarkdownV2"
        )
    except Exception as e:
        logging.error(f"Telegram alert failed: {e}")
