# ────────────────────────────────────────────────────────────────
#  Configuration
# ────────────────────────────────────────────────────────────────
from typing import List


PAIR = "OPUSDT"  # Bybit symbol: SOL/ATOM/WAVES/XRP
TF_SECONDS = 15 * 60  # 15‑minute bars
INTERVAL = "15"  # stream interval, string
LOOKBACK_BARS = 800  # kept in memory (≈ 8 days)

# Strategy param
# portfolio guards
MAX_SIGNAL_AGE_SEC   = 30
COALESCE_SEC         = 2
MAX_PER_SIDE_OPEN = 1  # max open positions per side (buy/sell)
MAX_TOTAL_RISK_PCT = 0.30  # e.g., 30% of equity at risk across all opens
RISK_PCT = 0.1  # not used (alerts only)
STAKE_SIZE_USD = 1_000  # ‘cash you allocate’ per entry
LEVERAGE = 20
RR_TARGET = 1.5
ATR_MULT_SL = 1.0
SL_CUSHION_MULT = 1.6  # was a hidden “1.6”; make it explicit & multiplicative
WICK_BUFFER = (
    0.25  # in ATR units              # extra cushion for SL (to avoid false hits)
)
ADX_FLOOR = 25
STO_K_MIN_LONG = 45
STO_K_MIN_SHORT = 30
HTF_DAYS = 15  # days of history to seek untapped highs/lows
HTF_LEVEL_TF = "1H"  # build levels from hourly bars
# SESSION_WINDOWS = {  # UTC sessions
#     "asia": (0, 8),  # 00–08 UTC
#     "eu": (7, 15),  # 07–15 UTC
#     "ny": (12, 20),  # 12–20 UTC
# }
SESSION_WINDOWS = {"all": (0, 24)}
###  LTF confirmation
FVG_MIN_PX = 0.0005  # was 0.0005
FIB_EXT = 0.79  # 79 % retrace / extension
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
SLIP_BPS = 0.001  #  bps of adverse slippage on entry
FEE_BPS = 10  # 10 bps per side (taker ~0.05% each way? adjust)
