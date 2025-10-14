# ────────────────────────────────────────────────────────────────
#  Configuration
# ────────────────────────────────────────────────────────────────
from typing import List


PAIR = "AAVEUSDT"  # Bybit symbol: SOL/ATOM/WAVES/XRP
TF_SECONDS = 15 * 60  # 15‑minute bars
INTERVAL = "15"  # stream interval, string
LOOKBACK_BARS = 800  # kept in memory (≈ 8 days)

# Strategy param
# portfolio guards
MAX_TRADES_PER_DAY = 3  # across all pairs
MAX_SIGNAL_AGE_SEC   = 30
COALESCE_SEC         = 2
MAX_PER_SIDE_OPEN = 1  # max open positions per side (buy/sell)
MAX_TOTAL_RISK_PCT = 0.30  # e.g., 30% of equity at risk across all opens
RISK_PCT = 0.1  # not used (alerts only)
STAKE_SIZE_USD = 1_000  # ‘cash you allocate’ per entry
LEVERAGE = 20
RR_TARGET = 1.5
ATR_MULT_SL = 1.0
SL_CUSHION_MULT = 1.3  # was a hidden “1.6”; make it explicit & multiplicative
WICK_BUFFER = (
    0.25  # in ATR units              # extra cushion for SL (to avoid false hits)
)
# Core filters - STRICT
ADX_HARD_FLOOR = 28              # Higher floor for quality
NEAR_HTF_MAX_ATR_MOM = 0.9       # Must be close to HTF levels
MOMENTUM_STO_K_LONG = 35         # Tighter oversold
MOMENTUM_STO_K_SHORT = 65        # Tighter overbought
MIN_H1_SLOPE = 0.05              # Moderate slope requirement

# Cooldowns
COOLDOWN_DAYS_AFTER_SL = 0.5    # 12 hours
COOLDOWN_DAYS_AFTER_TP = 0.25   # 6 hours

# Also consider:
COOLDOWN_DAYS_AFTER_TP = 0.0     # remove TP cooldown entirely
MAX_TRADES_PER_DAY = 5           # allow more opportunities
SESSION_WINDOWS = {
    "asia": (0, 8),   # 00–08 UTC
    "eu":   (7, 16),  # 07–16 UTC
    "ny":   (13, 22), # 13–22 UTC
}

# ── MR Scalp profile (quiet-regime filler)
# SCALP_ON = True                  # quick on/off
# SCALP_ADX_MAX = 18               # only when sleepy
# SCALP_NEAR_MAX_ATR = 0.6         # must be very close to HTF level
# SCALP_K_LONG = 10                # oversold extreme
# SCALP_K_SHORT = 90               # overbought extreme
# SCALP_ATR_MULT_SL = 0.9          # tighter stop than momentum
# SCALP_TP_MULT_OF_SL = 0.7        # small TP => high hit-rate
# SCALP_RISK_SCALE = 0.60          # 60% of your normal size
# SCALP_TIME_STOP_BARS = 3         # (optional) exit if not hit in N bars

HTF_DAYS = 15  # days of history to seek untapped highs/lows
HTF_LEVEL_TF = "1H"  # build levels from hourly bars
###  LTF confirmation
FVG_MIN_PX = 0.0005  # was 0.0005
FIB_EXT = 0.618   # 61.8 % retrace / extension
TICK_SIZE = {  # expand as needed
    "ETHUSDT": 0.01,
    "AAVEUSDT": 0.01,
    "SOLUSDT": 0.01,
    "FILUSDT": 0.001,
    "ATOMUSDT": 0.001,
    "AVAXUSDT": 0.001,
    "RENDERUSDT": 0.001,
    "NEARUSDT": 0.001,
    "APTUSDT": 0.001,
    "WAVESUSDT": 0.0001,
    "LDOUSDT": 0.0001,
    "LINKUSDT": 0.001,
    "XRPUSDT": 0.0001,
    "APEUSDT": 0.0001,
    "DOTUSDT": 0.0001,
    "OPUSDT": 0.0001,
    "ARUSDT": 0.001,
    "UNIUSDT": 0.001,
    "FXSUSDT": 0.0001,
    "DYDXUSDT": 0.0001,
    "INJUSDT": 0.001,
    "ARBUSDT": 0.0001,
    "DOGEUSDT": 0.00001,
    "ADAUSDT": 0.0001,
    "MNTUSDT": 0.00001,
    "GMTUSDT": 0.00001,
    "CHZUSDT": 0.00001,
    "WOOUSDT": 0.00001,
    "SUIUSDT": 0.0001,
    "SEIUSDT": 0.00001,
}
# ────────────────────────────────────────────────────────────────
#  Pairs & history
PAIRS_LRS: List[str] = [
    # "GMTUSDT",
    # "MNTUSDT",
    # CHZUSDT
    # INTEROP
    "ATOMUSDT",
    "DOTUSDT",
    "INJUSDT",
    # "L1",
    "SOLUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "APTUSDT",
    "NEARUSDT",
    "SUIUSDT",
    # L2
    "OPUSDT",
    "ARBUSDT",
    # MEME
    "DOGEUSDT",
    # DEFI
    "AAVEUSDT",
    "UNIUSDT",
    "LDOUSDT",
    # STORAGE
    "FILUSDT",
    "ARUSDT",
    # PAYMENTS
    "XRPUSDT",
    # ORACLE
    "LINKUSDT",
    # AI
    "RENDERUSDT",
]

CLUSTER = {
    "NEARUSDT": "L1",
    "AVAXUSDT": "L1",
    "SOLUSDT": "L1",
    "ADAUSDT": "L1",
    "APTUSDT": "L1",
    "SUIUSDT": "L1",
    "ARBUSDT": "L2",
    "OPUSDT": "L2",
    "DOTUSDT": "INTEROP",
    "ATOMUSDT": "INTEROP",
    "INJUSDT": "INTEROP",
    "FILUSDT": "STORAGE",
    "XRPUSDT": "PAYMENTS",
    "LINKUSDT": "ORACLE",
    "UNIUSDT": "DEFI",
    "AAVEUSDT": "DEFI",
    "LDOUSDT": "DEFI",
    "RENDERUSDT": "AI",
    "DOGEUSDT": "MEME",
}

# execution realism
SLIP_BPS = 0.0002  # 2 bps
FEE_BPS = 10  # 10 bps per side (taker ~0.05% each way? adjust)
