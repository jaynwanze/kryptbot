import os, asyncio
from functools import partial
from telegram.ext import Updater, CommandHandler
import logging

ALLOWED_CHAT_ID = int(os.getenv("TG_CHAT_ID", "0"))  # your channel id
ALLOWED_USER_IDS = {int(x) for x in os.getenv("TG_ALLOW_USER_IDS", "").split(",") if x.strip().isdigit()}

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

def trades_cmd(router, update, context):
    if not _authorized(update):
        return update.effective_message.reply_text("Sorry, not allowed.")
    arg = context.args[0] if context.args else "10"
    txt = router.format_past_trades(n=int(arg)) if arg.isdigit() else router.format_past_trades(day=arg)
    logging.info("[%s] Past trades requested: %s", update.effective_user.id, arg)
    return update.effective_message.reply_text(txt or "No trades to show.")

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
