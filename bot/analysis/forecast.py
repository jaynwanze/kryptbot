# bot/analysis/forecast.py
import os
import asyncio
from datetime import datetime, timedelta
from openai import AsyncClient
import logging
from bot.infra.bybit_client import REST

client = AsyncClient(api_key=os.getenv("OPENAI_API_KEY"))

async def generate_forecast(router, pairs: list, drop_stats: dict) -> str:
    """
    Generate AI forecast using OpenAI API with current market context.
    """
    
    # Gather context
    context = await _build_context(router, pairs, drop_stats)
    
    # Build prompt
    prompt = f"""You are an expert crypto trading analyst. Analyze the current market conditions and provide a trading forecast.

# Current System Status

## Active Pairs ({len(pairs)} pairs)
{', '.join(pairs)}... (showing all pairs)

## Current Market Prices
{context['prices']}

## Recent Performance
{context['recent_trades']}

## Current Positions
{context['open_positions']}

## Key Levels (from HTF analysis)
{context['htf_levels']}

## Signal Rejection Stats (Last Hour)
{context['drop_stats_summary']}

## Market Clusters
- L1 Coins: SOLUSDT, AVAXUSDT, ADAUSDT, NEARUSDT, APTUSDT, SUIUSDT
- L2 Coins: OPUSDT, ARBUSDT
- DeFi: AAVEUSDT, UNIUSDT, LDOUSDT
- Interop: ATOMUSDT, DOTUSDT
- Payments: XRPUSDT
- AI: RENDERUSDT
- Meme: DOGEUSDT
- Oracle: LINKUSDT

## Trading Strategy
- ADX Floor: 28 (strong momentum required)
- Entry: K_fast < 35 (longs) or > 65 (shorts)
- LTF Confirmations: Requires 1/3 (BOS, FVG, Fib)
- Risk: 10% per trade, max 3 trades/day

# Your Task

Provide a concise forecast (max 200 words) covering:

1. **Market Bias** - Overall direction based on recent trades and rejections
2. **Hot Pairs** - Which 3-5 pairs most likely to signal in next 24h
3. **Trade Outlook** - Expected number of signals and win rate
4. **Key Levels** - Any notable HTF levels being tested
5. **Risk Assessment** - Current market conditions (trending vs choppy)

Be specific, actionable, and format for Telegram (use markdown).
"""

    try:
        # Call OpenAI API
        message = await client.chat.completions.create(
            model="gpt-4o",
            max_tokens=500,
            temperature=0.7,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        forecast = message.choices[0].message.content

        # Add timestamp
        forecast_with_ts = f"🔮 *AI FORECAST* ({datetime.utcnow():%Y-%m-%d %H:%M} UTC)\n\n{forecast}"
        
        return forecast_with_ts
        
    except Exception as e:
        logging.error(f"Forecast generation failed: {e}")
        return f"⚠️ Forecast unavailable: {str(e)}"


async def _build_context(router, pairs, drop_stats):
    """Gather all context data for the forecast."""
    
    # Recent trades (last 10)
    recent = list(router.closed_trades)[-10:]
    if recent:
        wins = sum(1 for t in recent if t['net'] > 0)
        total_pnl = sum(t['net'] for t in recent)
        recent_summary = f"Last 10 trades: {wins}/10 wins, PnL: ${total_pnl:.2f}"
    else:
        recent_summary = "No recent trades"
    
    # Open positions
    if router.book:
        open_summary = f"{len(router.book)} open: " + ", ".join(
            f"{p.signal.symbol}({p.signal.side})" for p in list(router.book.values())[:3]
        )
    else:
        open_summary = "No open positions"
        
        
    htf_levels_summary = ""
    try:
        # Get latest HTF levels from one of the pairs
        htf_levels_summary = "TODO: HTF levels data not implemented yet."
    except:
        pass
    
    # Drop stats summary
    total_ltf = drop_stats.get("no_ltf_long", 0) + drop_stats.get("no_ltf_short", 0)
    total_adx = drop_stats.get("veto_adx", 0)
    drop_summary = f"LTF rejections: {total_ltf}, ADX rejections: {total_adx}"
    prices_summary = await _get_current_prices(pairs)
    
    return {
        "recent_trades": recent_summary,
        "open_positions": open_summary,
        "drop_stats_summary": drop_summary,
        "prices": prices_summary,
        "htf_levels": htf_levels_summary,  # ADD THIS
    }
    
async def _get_current_prices(pairs):
    """Fetch live prices for context"""
    prices = {}
    # Use your existing CCXT or Bybit client
    for pair in pairs[:5]:  # Top 5 only
        try:
            ticker = await REST.fetch_ticker(pair)
            prices[pair] = ticker['last']
        except:
            pass
    return prices