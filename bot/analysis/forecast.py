# bot/analysis/forecast.py - ENHANCED VERSION
"""
AI-powered market forecasting using OpenAI GPT-4
Now includes realistic trade expectations for FVG Order Flow strategy
"""
import os
import logging
from datetime import datetime
from typing import Dict, List, Optional
from openai import AsyncOpenAI

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def generate_forecast(router, pairs: List[str], drop_stats: Dict) -> str:
    """
    Generate AI forecast using OpenAI with current market context.

    Args:
        router: RiskRouter instance with trading state
        pairs: List of active trading pairs
        drop_stats: Aggregated rejection statistics

    Returns:
        Formatted forecast message for Telegram
    """

    # Gather context
    context = await _build_context(router, pairs, drop_stats)

    # Build comprehensive prompt with FVG-specific expectations
    prompt = f"""You are an expert crypto trading analyst specializing in FVG (Fair Value Gap) Order Flow strategies. Analyze the current market conditions and provide a concise trading forecast.

# Current System Status

## Active Pairs ({len(pairs)} pairs)
{', '.join(pairs[:15])}{'...' if len(pairs) > 15 else ''}

## Recent Performance
{context['recent_trades']}

## Current Positions
{context['open_positions']}

## Signal Rejection Stats (Recent)
{context['drop_stats_summary']}

## FVG Strategy Parameters
**Timeframe:** 15-minute candles
**Entry Logic:** FVG + Order Flow Momentum (95th percentile) + ADX > 25
**Backtest Results:** 64.81% win rate, 844 trades over 100 days (8-9 trades/day avg)
**Risk:** 5% per trade, 1.5R target (TP1) + 3R (TP2)

**Strategy Characteristics:**
- Highly selective (only 0.64% of bars pass all filters)
- Requires momentum/volatility to generate FVG gaps
- Works best in trending or volatile conditions
- Quiet consolidation days may produce 0 signals

# Your Task

Provide a **concise forecast (200-250 words)** covering:

1. **Market Regime** - Current volatility/momentum conditions and how they affect FVG formation
2. **Expected Trade Pattern** - Be specific about expected signals today based on current conditions:
   * **Quiet/Consolidating**: 0-2 trades expected
   * **Moderate Activity**: 3-5 trades expected
   * **Volatile/Trending**: 6-10+ trades expected

3. **Hot Pairs** - Which 2-3 pairs most likely to generate FVG signals and why (momentum, recent volatility)

4. **Quality Assessment** - Based on rejection stats, are signals being filtered appropriately or is market too choppy?

5. **Key Catalysts** - Any upcoming events (macro news, BTC moves) that could trigger volatility and FVG setups

6. **Risk Flags** - Note if:
   - ADX consistently low (< 25) across pairs
   - No FVG patterns forming despite movement
   - Excessive rejections suggest over-filtering

Format for Telegram with markdown. Be realistic - don't promise trades if market is consolidating. FVG strategies can have 0-trade days, and that's normal and healthy. Focus on WHEN conditions are likely to improve."""

    try:
        # Call OpenAI API
        response = await client.chat.completions.create(
            model="gpt-4o",  # Latest model
            messages=[
                {
                    "role": "system",
                    "content": """You are an expert crypto trading analyst specializing in FVG Order Flow and momentum-based strategies.

                    Key principles:
                    - FVG strategies are selective by design - low signal count is often healthy
                    - Set realistic expectations - don't promise trades if market is flat
                    - Volatility and momentum drive FVG formation
                    - Zero-trade days are normal in consolidation
                    - Focus on quality over quantity"""
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=600,  # Increased for more detailed analysis
            temperature=0.7
        )

        forecast = response.choices[0].message.content

        # Add timestamp header with strategy reminder
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        forecast_with_header = f"""🔮 *FVG ORDER FLOW FORECAST* ({timestamp})

{forecast}

_Strategy: 15M FVG + Order Flow | Target: 64%+ WR | Expect: 3-5 trades/day avg_"""

        return forecast_with_header

    except Exception as e:
        logging.error(f"Forecast generation failed: {e}")
        return f"⚠️ Forecast unavailable: {str(e)}"


async def _build_context(router, pairs: List[str], drop_stats: Dict) -> Dict:
    """Gather all context data for the forecast with FVG-specific metrics."""

    # Recent trades (last 20 for better sample)
    recent = list(router.closed_trades)[-20:]
    if recent:
        wins = sum(1 for t in recent if t['net'] > 0)
        total_pnl = sum(t['net'] for t in recent)
        avg_win = sum(t['net'] for t in recent if t['net'] > 0) / wins if wins else 0
        avg_loss = sum(t['net'] for t in recent if t['net'] < 0) / (len(recent) - wins) if (len(recent) - wins) > 0 else 0
        recent_summary = f"Last 20 trades: {wins}/20 wins ({wins/len(recent)*100:.1f}%), Net: ${total_pnl:.2f}, Avg W/L: ${avg_win:.2f}/${avg_loss:.2f}"
    else:
        recent_summary = "No recent trades yet"

    # Open positions
    if router.book:
        open_summary = f"{len(router.book)} open: " + ", ".join(
            f"{p.signal.symbol}({p.signal.side}@{p.signal.entry:.5f})"
            for p in list(router.book.values())[:3]
        )
    else:
        open_summary = "No open positions"

    # Enhanced drop stats for FVG strategy
    total_no_fvg = drop_stats.get("no_fvg_long", 0) + drop_stats.get("no_fvg_short", 0)
    total_adx = drop_stats.get("veto_adx", 0)
    total_momentum = drop_stats.get("low_momentum", 0)
    total_htf = drop_stats.get("htf_conflict", 0)
    total_session = drop_stats.get("off_session", 0)

    total_rejections = sum(drop_stats.values())

    drop_summary = f"""
**Total Rejections:** {total_rejections}
- No FVG Pattern: {total_no_fvg} ({total_no_fvg/total_rejections*100:.1f}% if total_rejections else 0)
- ADX Too Low: {total_adx} ({total_adx/total_rejections*100:.1f}% if total_rejections else 0)
- Low Momentum Score: {total_momentum} ({total_momentum/total_rejections*100:.1f}% if total_rejections else 0)
- HTF Conflicts: {total_htf} ({total_htf/total_rejections*100:.1f}% if total_rejections else 0)
- Off Session: {total_session} ({total_session/total_rejections*100:.1f}% if total_rejections else 0)
    """.strip()

    return {
        "recent_trades": recent_summary,
        "open_positions": open_summary,
        "drop_stats_summary": drop_summary,
    }