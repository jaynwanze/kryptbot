import math
from bot.helpers import config
import pandas as pd

# helpers/utils.py  (or wherever you placed it)
def round_price(price: float, pair: str) -> float:
    """Round price to the nearest tick size for the given trading pair."""
    tick = config.TICK_SIZE.get(pair)
    if tick is None:
        raise ValueError(f"Unknown tick size for '{pair}'. "
                         "Add it to config.TICK_SIZE or fetch it from the exchange.")
    quantised = round(price / tick) * tick          # snap to grid
    return round(quantised, int(-math.log10(tick))) # keep the right decimals