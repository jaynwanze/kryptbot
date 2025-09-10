import math
from bot.helpers import config
import pandas as pd
import csv

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

# Frequency helpers/throttles (tune these to hit ~1–2 trades/month/pair)
# Stricter market-quality veto to reduce frequency
def veto_thresholds(bar):
    vol_norm = bar.atr / bar.atr30
    min_adx = 14 + 5 * vol_norm  
    atr_veto = 0.45 + 0.20 * vol_norm  
    return min_adx, atr_veto

def near_htf_level(bar, htf_row, max_atr=0.8):
    cols = ["D_H","D_L","H4_H","H4_L","asia_H","asia_L","eu_H","eu_L","ny_H","ny_L"]
    levels = [htf_row.get(c) for c in cols if c in htf_row.index and pd.notna(htf_row.get(c))]
    if not levels:
        return False
    dist = min(abs(float(bar.c) - float(L)) for L in levels)
    return dist <= max_atr * float(bar.atr)

def has_open_in_cluster(router, symbol, clusters):
    cid = clusters.get(symbol)
    return cid and any(
        clusters.get(p.signal.symbol) == cid for p in router.book.values()
    )

def in_good_hours(ts, good_hours=set[int]):
    return ts.hour in good_hours

def hours(name, session_windows=dict[str, tuple[int, int]]) -> set[int]:
    h0, h1 = session_windows[name]
    if h0 <= h1:
        return set(range(h0, h1 + 1))
    # wrap across midnight
    return set(range(h0, 24)) | set(range(0, h1 + 1))

def append_csv(name, row, fields, log_dir):
    p = log_dir / name
    new = not p.exists()
    with p.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)