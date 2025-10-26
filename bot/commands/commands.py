from collections import defaultdict
import os, asyncio
from functools import partial
import ccxt
from telegram.ext import Updater, CommandHandler
import logging
from decimal import Decimal
import pandas as pd
from telegram.constants import ParseMode
from bot.engines.risk_router import RiskRouter
from bot.analysis.forecast import generate_forecast

ALLOWED_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0"))  # your channel id
ALLOWED_USER_IDS = {
    int(x) for x in os.getenv("TG_ALLOW_USER_IDS", "").split(",") if x.strip().isdigit()
}

async def forecast_cmd_async(config, router, update, context):
    """Async forecast command."""
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")
    
    # Send "thinking" message
    thinking_msg = update.effective_message.reply_text("🤖 Analyzing market conditions...")
    
    try:
        pairs = getattr(config, "PAIRS_LRS", [])
        
        # Get aggregated drop stats
        async with config.GLOBAL_DROP_STATS_LOCK:
            # Sum across all pairs
            totals = defaultdict(int)
            for pair_stats in config.GLOBAL_DROP_STATS.values():
                for key, val in pair_stats.items():
                    totals[key] += val
        
        # Generate forecast
        forecast = await generate_forecast(router, pairs, totals)
        
        # Delete thinking message and send forecast
        thinking_msg.delete()
        return update.effective_message.reply_text(
            forecast,
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logging.error(f"Forecast command failed: {e}")
        thinking_msg.delete()
        return update.effective_message.reply_text(f"⚠️ Forecast failed: {str(e)}")
    
async def format_recent_trades_detail(router, hours=24):
    """Show last N trades with details"""
    recent = list(router.closed_trades)[-10:]
    if not recent:
        return "No recent trades."
    
    lines = ["📊 *Recent Trades (Last 10):*\n"]
    for t in recent:
        symbol = t['symbol']
        side = t['side'].upper()
        net = t['net']
        emoji = "✅" if net > 0 else "❌"
        
        lines.append(
            f"{emoji} `{symbol}` {side}: "
            f"`${net:+.2f}` (fees: ${t['fees']:.2f})"
        )
    
    return "\n".join(lines)

async def fetch_executions(http, since_ms: int, until_ms: int):
    params = {
        "category": "linear",
        "startTime": since_ms,
        "endTime": until_ms,
        "limit": 200,
    }
    res = await asyncio.to_thread(http.get_executions, **params)
    items = (res.get("result", {}) or {}).get("list", []) or []
    out = []
    for it in items:
        out.append(
            {
                "symbol": it["symbol"],
                "side": it["side"],  # "Buy"/"Sell"
                "px": Decimal(str(it["execPrice"])),
                "qty": Decimal(str(it["execQty"])),
                "fee": Decimal(str(it.get("execFee", "0"))),
                "time": int(it.get("execTime", it.get("createdTime", 0))),
                "execId": it.get("execId") or it.get("id"),
                "orderId": it.get("orderId"),
            }
        )
    # de-dupe
    seen, uniq = set(), []
    for x in out:
        if x["execId"] in seen:
            continue
        seen.add(x["execId"])
        uniq.append(x)
    return uniq


async def fetch_closed_pnl(http, since_ms: int, until_ms: int):
    params = {
        "category": "linear",
        "settleCoin": "USDT",
        "startTime": since_ms,
        "endTime": until_ms,
        "limit": 200,
    }
    res = await asyncio.to_thread(
        http.get_closed_pnl, **params
    )  # your client’s wrapper
    items = (res.get("result", {}) or {}).get("list", []) or []
    rows = []
    for it in items:
        rows.append(
            {
                "symbol": it["symbol"],
                "side": it.get("side"),
                "closedPnl": float(it.get("closedPnl", 0.0)),
                "cumEntryValue": float(it.get("cumEntryValue", 0.0)),
                "cumExitValue": float(it.get("cumExitValue", 0.0)),
                "fees": float(it.get("fee", it.get("cumFee", 0.0))),
                "time": int(it.get("updatedTime", it.get("createdTime", 0))),
            }
        )
    return rows


async def fetch_positions_snapshot(http):
    res = await asyncio.to_thread(
        http.get_positions,
        category="linear",
        settleCoin="USDT",
    )
    items = (res.get("result", {}) or {}).get("list", []) or []
    out = []
    for it in items:
        if float(it.get("size", 0) or 0) == 0:
            continue
        out.append(
            {
                "symbol": it["symbol"],
                "side": it.get("side"),
                "size": float(it.get("size")),
                "avgPx": float(it.get("avgPrice", it.get("entryPrice", 0))),
                "uPnl": float(it.get("unrealisedPnl", 0.0)),
            }
        )
    return out


def realised_pnl_from_fills(fills):
    size = Decimal("0")  # +long / -short
    avg = Decimal("0")
    realised = Decimal("0")
    fees = Decimal("0")

    for f in sorted(fills, key=lambda z: z["time"]):
        qty, px, side = f["qty"], f["px"], f["side"]
        fees += f["fee"]
        if side == "Buy":
            if size >= 0:
                # add/flip to long
                new_size = size + qty
                avg = (
                    (avg * size + px * qty) / new_size
                    if new_size != 0
                    else Decimal("0")
                )
                size = new_size
            else:
                # reduce short
                reduce = min(qty, -size)
                realised += (avg - px) * reduce
                size += reduce
                rem = qty - reduce
                if rem > 0:
                    avg = px
                    size = rem
        else:  # Sell
            if size <= 0:
                new_size = size - qty
                avg = (
                    (avg * abs(size) + px * qty) / abs(new_size)
                    if new_size != 0
                    else Decimal("0")
                )
                size = new_size
            else:
                reduce = min(qty, size)
                realised += (px - avg) * reduce
                size -= reduce
                rem = qty - reduce
                if rem > 0:
                    avg = px
                    size = -rem

    return float(realised - fees), float(fees)


def today_utc_bounds():
    now = pd.Timestamp.utcnow()
    start = now.normalize()  # 00:00 UTC today
    end = start + pd.Timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def last_24h_bounds():
    end = pd.Timestamp.utcnow()
    start = end - pd.Timedelta(hours=24)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _authorized(update):
    chat_ok = (ALLOWED_CHAT_ID == 0) or (
        update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID
    )
    user_ok = (not ALLOWED_USER_IDS) or (
        update.effective_user and update.effective_user.id in ALLOWED_USER_IDS
    )
    if not chat_ok and not user_ok:
        logging.warning("[%s] Unauthorized access attempt", update.effective_user.id)
    return chat_ok or user_ok


def _run(router, coro):
    """Run a coroutine on the engine loop & return its result (or None)."""
    fut = asyncio.run_coroutine_threadsafe(coro, router.loop)
    try:
        return fut.result(timeout=10)
    except Exception:
        return None


# ── commands ───────────────────────────────────────────────────────────────
def forecast_cmd(config, router, update, context):
    """Wrapper to run async forecast command."""
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")
    
    logging.info("[%s] Forecast requested", update.effective_user.id)
    
    # Run async command in router's event loop
    fut = asyncio.run_coroutine_threadsafe(
        forecast_cmd_async(config, router, update, context),
        router.loop
    )
    try:
        return fut.result(timeout=30)  # 30 sec timeout for OpenAI API
    except Exception as e:
        logging.error(f"Forecast command error: {e}")
        return update.effective_message.reply_text("⚠️ Forecast timed out or failed.")
    
def ping_cmd(update, context):
    logging.info("[%s] Ping command received", update.effective_user.id)
    return update.effective_message.reply_text("pong ✅")

def dropstats_cmd(router, update, context):
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")
    
    txt = _run(router, router.format_drop_stats()) or "No stats available."
    logging.info("[%s] Drop stats requested", update.effective_user.id)
    return update.effective_message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

def summary_cmd(router, update, context):
    if not _authorized(update):
        logging.warning("[%s] Summary command denied", update.effective_user.id)
        return update.effective_message.reply_text("Sorry, not allowed here.")
    day = None
    if context.args:
        a = context.args[0].lower()
        if a in ("y", "yday", "yesterday"):
            from datetime import datetime, timedelta

            day = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
        elif a not in ("t", "today"):
            day = a
    _run(router, router.telegram_daily_summary(day))
    logging.info(
        "[%s] Daily summary requested: %s", update.effective_user.id, day or "today"
    )
    return update.effective_message.reply_text(f"Summary for {day or 'today'} sent ✅")


def open_cmd(router, update, context):
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")
    txt = _run(router, router.format_open_positions()) or "No open positions."
    logging.info("[%s] Open positions requested", update.effective_user.id)
    return update.effective_message.reply_text(txt)


def fees_cmd(router, update, context):
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")
    minutes = int(context.args[0]) if context.args else 180
    txt = (
        _run(router, router.format_recent_fees(minutes))
        or f"No executions in last {minutes} min."
    )
    logging.info("[%s] Recent fees requested: %s", update.effective_user.id, minutes)
    return update.effective_message.reply_text(txt)

def trades_detail_cmd(router, update, context):
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")
    
    txt = _run(router, format_recent_trades_detail(router)) or "No trades."
    return update.effective_message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

def trades_cmd(router, update, context):
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")

    try:
        # Parse time period from command or args
        period_days = 1  # default to 24h

        # Check if command has suffix like /trades_7d
        command_text = update.effective_message.text.split()[0]  # e.g., "/trades_7d"
        if '_' in command_text:
            period_str = command_text.split('_')[1]  # e.g., "7d"
            if period_str.endswith('d') and period_str[:-1].isdigit():
                period_days = int(period_str[:-1])

        # Or check first argument like "/trades 7"
        elif context.args and context.args[0].isdigit():
            period_days = int(context.args[0])

        # Calculate time bounds
        end_time = pd.Timestamp.utcnow()
        start_time = end_time - pd.Timedelta(days=period_days)
        s_ms = int(start_time.timestamp() * 1000)
        e_ms = int(end_time.timestamp() * 1000)

        # For closed PnL, use today's bounds if period is 1 day, otherwise use full period
        if period_days == 1:
            sDay, eDay = today_utc_bounds()
        else:
            sDay, eDay = s_ms, e_ms

        fills = _run(router, fetch_executions(router.http, s_ms, e_ms)) or []
        pnl_rows = _run(router, fetch_closed_pnl(router.http, sDay, eDay)) or []
        positions = _run(router, fetch_positions_snapshot(router.http)) or []

        day_realised = sum((r.get("closedPnl", 0.0) - r.get("fees", 0.0)) for r in pnl_rows)
        r_from_fills, fees_from_fills = realised_pnl_from_fills(fills)

        # Dynamic period label
        period_label = f"{period_days}d" if period_days != 1 else "24h"

        lines = []
        lines.append(f"📊 *Trades (last {period_label})*: {len(fills)} fills")
        lines.append(f"• Realised (closed {period_label}): `{day_realised:.2f}` USDT")
        lines.append(f"• Cross-check (fills {period_label}): `{r_from_fills:.2f}` USDT (fees {fees_from_fills:.2f})")

        if positions:
            lines.append("\n🟡 *Open Positions*")
            for p in positions:
                lines.append(f"• {p['symbol']} {p['side']} {p['size']} @ {p['avgPx']:.4f}  uPnL={p['uPnl']:.2f}")
        else:
            lines.append("\n🟢 No open positions.")

        return update.effective_message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logging.warning("trades_cmd failed: %s", e)
        return update.effective_message.reply_text("Couldn't fetch trades right now.")



def run_command_bot(router:RiskRouter, config):
    """Start PTB13 polling;(we're inside your engine)."""
    # no updater.idle() here
    updater = Updater(os.environ["TELE_TOKEN"], use_context=True)
    dp = updater.dispatcher

    # Basic commands
    dp.add_handler(CommandHandler("ping", ping_cmd))
    dp.add_handler(CommandHandler("summary", partial(summary_cmd, router)))
    dp.add_handler(CommandHandler("open", partial(open_cmd, router)))
    dp.add_handler(CommandHandler("fees", partial(fees_cmd, router)))
    dp.add_handler(CommandHandler("dropstats", partial(dropstats_cmd, router)))
    dp.add_handler(CommandHandler("trades_detail", partial(trades_detail_cmd, router)))

   # Advanced commands
    trades_commands = ["trades", "trades_7d", "trades_30d", "trades_60d", "trades_90d", "trades_180d", "trades_365d"]
    dp.add_handler(CommandHandler(trades_commands, partial(trades_cmd, router)))
    dp.add_handler(CommandHandler("forecast", partial(forecast_cmd, config, router)))

    updater.start_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=True,
        timeout=20,
        read_latency=3.0,
    )
    logging.info("Telegram bot started.")
    # IMPORTANT: include channel_post so commands in your channel are seen
    updater.start_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=True,  # replaces deprecated 'clean=True'
        timeout=20,
        read_latency=3.0,
    )
    logging.info("Telegram bot started.")
