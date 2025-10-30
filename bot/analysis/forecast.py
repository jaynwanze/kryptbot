# bot/analysis/forecast.py - ENHANCED VERSION
"""
AI-powered market forecasting using OpenAI GPT-4
Enhanced with detailed market analysis and structured output
"""
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from openai import AsyncOpenAI

from bot.infra.bybit_client import REST

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def generate_forecast(router, pairs: List[str], drop_stats: Dict) -> str:
    """
    Generate enhanced AI forecast with detailed market analysis.

    Args:
        router: RiskRouter instance with trading state and engines
        pairs: List of active trading pairs
        drop_stats: Aggregated rejection statistics

    Returns:
        Formatted forecast message for Telegram
    """

    # Gather enhanced context with 12h market data
    context = await _build_enhanced_context(router, pairs, drop_stats)

    # Build enhanced prompt with detailed format
    prompt = _build_enhanced_prompt(context, pairs, drop_stats)

    try:
        # Call OpenAI API with enhanced system prompt
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": """You are an expert crypto trading analyst specializing in FVG Order Flow strategies.

Your morning briefings are highly valued for being:
- Data-driven and specific (not generic)
- Structured and scannable
- Actionable (tells traders what to watch)
- Realistic (acknowledges quiet days are normal)

Use clear sections, precise numbers, and the exact format requested."""
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_tokens=800,  # Increased for detailed format
            temperature=0.7
        )

        forecast = response.choices[0].message.content

        # Add timestamp header
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        forecast_with_header = f"""🔮 *FVG ORDER FLOW FORECAST* ({timestamp})

{forecast}

_Strategy: 15M FVG + Order Flow | Target: 64%+ WR | Expect: 3-5 trades/day avg_"""

        return forecast_with_header

    except Exception as e:
        logging.error(f"Forecast generation failed: {e}")
        return f"⚠️ Forecast unavailable: {str(e)}"


async def _build_enhanced_context(router, pairs: List[str], drop_stats: Dict) -> Dict:
    """
    Gather enhanced context with 12h market data for detailed analysis.

    Returns:
        Dict with market data, price action, FVG activity, etc.
    """

    context = {
        'recent_trades': 'No recent trades yet',
        'open_positions': 'No open positions',
        'price_summary': 'Insufficient price data',
        'current_price': 0,
        'current_adx': 0,
        'current_atr': 0,
        'trend_state': 'Unknown',
        'fvgs_detected_12h': 0,
        'signals_generated_12h': 0,
        'high_12h': 0,
        'low_12h': 0,
    }

    # Recent trades
    recent = list(router.closed_trades)[-20:]
    if recent:
        wins = sum(1 for t in recent if t['net'] > 0)
        total_pnl = sum(t['net'] for t in recent)
        context['recent_trades'] = f"Last 20: {wins}/20 wins ({wins/len(recent)*100:.1f}%), Net: ${total_pnl:.2f}"

    # Open positions
    if router.book:
        context['open_positions'] = f"{len(router.book)} open: " + ", ".join(
            f"{p.signal.symbol}({p.signal.side}@{p.signal.entry:.5f})"
            for p in list(router.book.values())[:3]
        )

    # Get market data from first pair's engine (if available)
    if pairs and hasattr(router, 'engines'):
        first_pair = pairs[0]
        engine = router.engines.get(first_pair)

        if engine and hasattr(engine, 'df') and engine.df is not None and len(engine.df) >= 48:
            df = engine.df

            # Last 12 hours (48 bars on 15m)
            bars_12h = df.iloc[-48:]
            current_bar = df.iloc[-1]

            # Price action summary
            start_price = bars_12h.iloc[0]['c']
            current_price = current_bar['c']
            high_12h = bars_12h['h'].max()
            low_12h = bars_12h['l'].min()
            range_pct = ((high_12h - low_12h) / low_12h) * 100
            change_pct = ((current_price - start_price) / start_price) * 100

            context['price_summary'] = f"""- Start (12h ago): ${start_price:.5f}
- Current: ${current_price:.5f} ({change_pct:+.2f}%)
- 12h Range: ${low_12h:.5f} - ${high_12h:.5f} ({range_pct:.2f}% range)"""

            context['current_price'] = current_price
            context['high_12h'] = high_12h
            context['low_12h'] = low_12h

            # Current indicators
            context['current_adx'] = current_bar.get('adx', 0)
            context['current_atr'] = current_bar.get('atr', 0)

            # Trend state
            adx = context['current_adx']
            if adx > 40:
                context['trend_state'] = "Strong trend"
            elif adx > 25:
                context['trend_state'] = "Moderate trend"
            else:
                context['trend_state'] = "Weak/Ranging"

            # FVG activity (if engine tracks it)
            if hasattr(engine, 'fvg_count_12h'):
                context['fvgs_detected_12h'] = engine.fvg_count_12h

            if hasattr(engine, 'signals_12h'):
                context['signals_generated_12h'] = len(engine.signals_12h)

    return context


def _build_enhanced_prompt(context: Dict, pairs: List[str], drop_stats: Dict) -> str:
    """
    Build enhanced prompt with detailed format requirements.
    """

    # Build rejection breakdown
    total_rej = sum(drop_stats.values())
    if total_rej > 0:
        rejection_lines = []
        for reason, count in sorted(drop_stats.items(), key=lambda x: -x[1]):
            pct = (count / total_rej) * 100
            reason_display = reason.replace('_', ' ').title()
            rejection_lines.append(f"- {reason_display}: {count} ({pct:.0f}%)")
        rejection_breakdown = "\n".join(rejection_lines)
    else:
        rejection_breakdown = "No rejections yet"

    # Active pairs
    active_pairs_text = ", ".join(pairs[:10]) if pairs else "None"
    if len(pairs) > 10:
        active_pairs_text += f"... (+{len(pairs)-10} more)"

    # Current prices (try to fetch live)
    current_prices = {}
    try:
        # Try to get from router/engines
        pass  # Will be filled by _get_current_prices if available
    except:
        pass

    prices_str = f"${context['current_price']:.5f}" if context['current_price'] > 0 else "Unavailable"

    prompt = f"""Analyze market conditions and provide a detailed morning forecast.

# Current System Status

## Active Pairs ({len(pairs)} pairs)
{active_pairs_text}

## Recent Performance
{context['recent_trades']}

## Current Positions
{context['open_positions']}

# Market Data (Last 12 Hours)

## Price Action
{context['price_summary']}

## Signal Activity
- FVGs Detected: {context['fvgs_detected_12h']}
- Signals Generated: {context['signals_generated_12h']}
- Total Rejections: {total_rej}

## Why Signals Failed
{rejection_breakdown}

## Current State (Now)
- Price: {prices_str}
- ADX: {context['current_adx']:.1f}
- ATR: ${context['current_atr']:.5f}
- Trend: {context['trend_state']}

# Strategy Parameters
**Timeframe:** 15-minute candles
**Entry Logic:** FVG + Order Flow (OF > 30) + ADX > 20 + Vol > 1.2x avg
**Backtest Results:** 64.81% win rate, 844 trades over 100 days (8-9 trades/day avg)
**Risk:** 5% per trade, 1.5R target (TP1) + 3R (TP2)

**Strategy Characteristics:**
- Highly selective (only 0.64% of bars pass all filters)
- Requires momentum/volatility to generate FVG gaps
- Works best in trending or volatile conditions
- Quiet consolidation days may produce 0 signals

# Required Format

Provide your forecast in this **EXACT structure**:

## 📊 MARKET ANALYSIS (Last 12 Hours)

### What Happened:
- [Time-stamped price moves with %]
- [ADX evolution and meaning]
- [Volume patterns]

### Current State ({datetime.utcnow().strftime('%H:%M')} UTC):
- **Price**: [value] ([context])
- **ADX**: [value] ([strong/moderate/weak])
- **FVGs**: [detected] ([how many qualified])
- **Main Issue**: [primary rejection reason if applicable]

---

## 🎯 TODAY'S TRADE FORECAST

### **Expected: [X-Y] trades today**

### ✅ Positive Signals:
- [Factor 1 with specifics]
- [Factor 2 with specifics]
- [Factor 3 with specifics]

### ❌ Challenges:
- [Factor 1 with specifics]
- [Factor 2 with specifics]

---

## 📈 BY SESSION (UTC):

| Session | Trades Expected | Reasoning |
|---------|-----------------|-----------|
| **Asia (done)** | [X] ✓ | [specific reason] |
| **London (08-16)** | [X-Y] | [specific reason] |
| **NY (13-22)** | [X-Y] | [specific reason - highest probability] |
| **Late (22-00)** | [X] | [specific reason - usually quiet] |

---

## 🔮 SCENARIOS:

### Best Case ([X] trades):
[What market conditions would create this]

### Base Case ([X-Y] trades):
[Most likely outcome given current state]

### Worst Case ([X] trades):
[Continued consolidation - emphasize this is HEALTHY]

---

## 💡 KEY LEVELS

**Resistance**: {context['high_12h']:.5f} (12h high)
**Support**: {context['low_12h']:.5f} (12h low)
**Catalysts**: [Known events or "Organic flow only"]

---

## ✅ BOTTOM LINE

**[X-Y] trades expected** based on:
- ✅ [Primary positive]
- ⚠️ [Primary challenge]

[One sentence: quality over quantity reminder]

---

**Critical Rules:**
1. Use REAL data from what I provided
2. Specific times (UTC) and percentages
3. Don't invent levels - use the resistance/support I provided
4. Explain WHY signals are failing if rejections are high
5. Be honest about quiet markets
6. Always include all tables/sections
7. 300-400 words total
"""

    return prompt


async def _get_current_prices(pairs):
    """Fetch live prices for context (first 5 pairs)."""
    prices = {}
    for pair in pairs[:5]:
        try:
            ticker = await REST.fetch_ticker(pair)
            prices[pair] = ticker['last']
        except:
            pass
    return prices