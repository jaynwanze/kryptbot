import os, asyncio
from functools import partial
from telegram.ext import Updater, CommandHandler
import logging
from decimal import Decimal
import pandas as pd

ALLOWED_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0"))  # your channel id
ALLOWED_USER_IDS = {int(x) for x in os.getenv("TG_ALLOW_USER_IDS", "").split(",") if x.strip().isdigit()}

async def fetch_executions(http, since_ms: int, until_ms: int):
    params = {"category": "linear", "startTime": since_ms, "endTime": until_ms, "limit": 200}
    res = await asyncio.to_thread(http.get_executions, **params)
    items = (res.get("result", {}) or {}).get("list", []) or []
    out = []
    for it in items:
        out.append({
            "symbol": it["symbol"],
            "side": it["side"],                             # "Buy"/"Sell"
            "px": Decimal(str(it["execPrice"])),
            "qty": Decimal(str(it["execQty"])),
            "fee": Decimal(str(it.get("execFee", "0"))),
            "time": int(it.get("execTime", it.get("createdTime", 0))),
            "execId": it.get("execId") or it.get("id"),
            "orderId": it.get("orderId"),
        })
    # de-dupe
    seen, uniq = set(), []
    for x in out:
        if x["execId"] in seen: 
            continue
        seen.add(x["execId"])
        uniq.append(x)
    return uniq

async def fetch_closed_pnl(http, since_ms: int, until_ms: int):
    params = {"category": "linear", "startTime": since_ms, "endTime": until_ms, "limit": 200}
    res = await asyncio.to_thread(http.get_closed_pnl, **params)  # your client’s wrapper
    items = (res.get("result", {}) or {}).get("list", []) or []
    rows = []
    for it in items:
        rows.append({
            "symbol": it["symbol"],
            "side": it.get("side"),
            "closedPnl": float(it.get("closedPnl", 0.0)),
            "cumEntryValue": float(it.get("cumEntryValue", 0.0)),
            "cumExitValue": float(it.get("cumExitValue", 0.0)),
            "fees": float(it.get("fee", it.get("cumFee", 0.0))),
            "time": int(it.get("updatedTime", it.get("createdTime", 0))),
        })
    return rows

async def fetch_positions_snapshot(http):
    res = await asyncio.to_thread(http.get_positions, category="linear")
    items = (res.get("result", {}) or {}).get("list", []) or []
    out = []
    for it in items:
        if float(it.get("size", 0) or 0) == 0: 
            continue
        out.append({
            "symbol": it["symbol"],
            "side": it.get("side"),
            "size": float(it.get("size")),
            "avgPx": float(it.get("avgPrice", it.get("entryPrice", 0))),
            "uPnl": float(it.get("unrealisedPnl", 0.0)),
        })
    return out

def realised_pnl_from_fills(fills):
    size = Decimal("0")      # +long / -short
    avg  = Decimal("0")
    realised = Decimal("0")
    fees = Decimal("0")

    for f in sorted(fills, key=lambda z: z["time"]):
        qty, px, side = f["qty"], f["px"], f["side"]
        fees += f["fee"]
        if side == "Buy":
            if size >= 0:
                # add/flip to long
                new_size = size + qty
                avg = (avg*size + px*qty) / new_size if new_size != 0 else Decimal("0")
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
                avg = (avg*abs(size) + px*qty) / abs(new_size) if new_size != 0 else Decimal("0")
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
    start = now.normalize()             # 00:00 UTC today
    end   = start + pd.Timedelta(days=1)
    return int(start.timestamp()*1000), int(end.timestamp()*1000)

def last_24h_bounds():
    end = pd.Timestamp.utcnow()
    start = end - pd.Timedelta(hours=24)
    return int(start.timestamp()*1000), int(end.timestamp()*1000)


def _authorized(update):
    chat_ok = (ALLOWED_CHAT_ID == 0) or (update.effective_chat and update.effective_chat.id == ALLOWED_CHAT_ID)
    user_ok = (not ALLOWED_USER_IDS) or (update.effective_user and update.effective_user.id in ALLOWED_USER_IDS)
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
def ping_cmd(update, context):
    logging.info("[%s] Ping command received", update.effective_user.id)
    return update.effective_message.reply_text("pong ✅")

def summary_cmd(router: RiskRouter, update, context):
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
    logging.info("[%s] Daily summary requested: %s", update.effective_user.id, day or 'today')
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
    txt = _run(router, router.format_recent_fees(minutes)) or f"No executions in last {minutes} min."
    logging.info("[%s] Recent fees requested: %s", update.effective_user.id, minutes)
    return update.effective_message.reply_text(txt)

def trades_cmd(router: RiskRouter, update, context):
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")

    s24, e24 = last_24h_bounds()
    sDay, eDay = today_utc_bounds()

    # run three async fetches on the engine loop, concurrently
    loop = router.loop
    fut_fills = asyncio.run_coroutine_threadsafe(fetch_executions(router.http, s24, e24), loop)
    fut_pnl   = asyncio.run_coroutine_threadsafe(fetch_closed_pnl(router.http, sDay, eDay), loop)
    fut_pos   = asyncio.run_coroutine_threadsafe(fetch_positions_snapshot(router.http), loop)

    try:
        fills     = fut_fills.result(timeout=12)
        pnl_rows  = fut_pnl.result(timeout=12)
        positions = fut_pos.result(timeout=12)
    except Exception as e:
        logging.warning("trades_cmd failed: %s", e)
        fills, pnl_rows, positions = [], [], []

    # realised PnL (Bybit closed-PnL list is authoritative)
    day_realised = sum((r.get("closedPnl") or 0.0) - (r.get("fees") or 0.0) for r in pnl_rows)
    # cross-check via fills (24h)
    r_from_fills, fees_from_fills = realised_pnl_from_fills(fills)

    def fmt_pos(p):
        return f"{p['symbol']} {p['side']} {p['size']} @ {p['avgPx']:.4f}  uPnL={p['uPnl']:.2f}"

    lines = []
    lines.append(f"📊 *Trades (last 24h)*: {len(fills)} fills")
    lines.append(f"• Realised (closed today): `{day_realised:.2f}` USDT")
    lines.append(f"• Cross-check (fills 24h): `{r_from_fills:.2f}` USDT (fees {fees_from_fills:.2f})")

    if positions:
        lines.append("")
        lines.append("🟡 *Open Positions*")
        for p in positions:
            lines.append(f"• {fmt_pos(p)}")
    else:
        lines.append("")
        lines.append("🟢 No open positions.")

    logging.info("[%s] Past trades requested", update.effective_user.id)
    return update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")

def run_command_bot(router):
    """Start PTB13 polling;(we're inside your engine)."""
        # no updater.idle() here
    updater = Updater(os.environ["TELE_TOKEN"], use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("ping",    ping_cmd))
    dp.add_handler(CommandHandler("summary", partial(summary_cmd, router)))
    dp.add_handler(CommandHandler("open",    partial(open_cmd, router)))
    dp.add_handler(CommandHandler("fees",    partial(fees_cmd, router)))
    dp.add_handler(CommandHandler("trades",  partial(trades_cmd, router)))

    # IMPORTANT: include channel_post so commands in your channel are seen
    updater.start_polling(
        allowed_updates=["message", "channel_post"],
        drop_pending_updates=True,   # replaces deprecated 'clean=True'
        timeout=20, read_latency=3.0
    )
    logging.info("Telegram bot started.")
