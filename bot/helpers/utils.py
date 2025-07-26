import math
from bot.helpers import config
import pandas as pd

def round_price(price: float, pair: str) -> float:
    """Round price to the nearest tick size for the given trading pair."""
    tick = config.TICK_SIZE.get(pair)
    if tick is None:
        raise ValueError(f"Unknown tick size for '{pair}'. "
                         "Add it to config.TICK_SIZE or fetch it from the exchange.")
    quantised = round(price / tick) * tick          # snap to grid
    return round(quantised, int(-math.log10(tick))) # keep the right decimals

def align_by_close(df: pd.DataFrame, tf_min: int) -> pd.DataFrame:
    df = df.copy()
    # If your index is candle start, move it to close
    df.index = df.index + pd.Timedelta(minutes=tf_min)
    return df

def next_open_price(df, i, side: str, pair: str, slip_bps: float) -> float:
    j = min(i + 1, len(df) - 1)
    o = float(df.iloc[j].o)
    sgn = 1 if side == "LONG" else -1
    px = o * (1 + sgn * slip_bps / 10_000)     # adverse slippage
    return round_price(px, pair)

def fees_usd(entry: float, exit_: float, qty: float, fee_bps: float) -> float:
    return ((entry + exit_) * qty) * (fee_bps / 10_000.0)  # taker in+out