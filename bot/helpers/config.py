# ────────────────────────────────────────────────────────────────
#  Configuration
# ────────────────────────────────────────────────────────────────
from typing import List


PAIR            = "ATOMUSDT"          # Bybit symbol: SOL/ATOM/WAVES/XRP
TF_SECONDS      = 15 * 60            # 15‑minute bars
INTERVAL        = "15"               # stream interval, string
LOOKBACK_BARS   = 800                # kept in memory (≈ 8 days)

# Strategy param
RISK_PCT        = 0.1               # not used (alerts only)
STAKE_SIZE_USD   = 1_000          # ‘cash you allocate’ per entry
LEVERAGE         = 20
RR_TARGET         = 1.5
ATR_MULT_SL       = 1.0
SL_CUSHION_MULT   = 1.6     # was a hidden “1.6”; make it explicit & multiplicative
WICK_BUFFER       = 0.25    # in ATR units              # extra cushion for SL (to avoid false hits)
ADX_FLOOR       = 25
STO_K_MIN_LONG  = 45
STO_K_MIN_SHORT = 30
HTF_DAYS          = 15        # days of history to seek untapped highs/lows
HTF_LEVEL_TF      = "1H"      # build levels from hourly bars
SESSION_WINDOWS   = {         # UTC sessions
    "asia" : (0, 8),          # 00–08 UTC
    "eu"   : (7, 15),         # 07–15 UTC
    "ny"   : (12, 20)         # 12–20 UTC
}

###  LTF confirmation
FVG_MIN_PX   = 0.0005  # was 0.0005
FIB_EXT      = 0.79      # 79 % retrace / extension
TICK_SIZE = {           # expand as needed
    "SOLUSDT": 0.001,
    "ATOMUSDT": 0.001,
    "WAVESUSDT": 0.0001,
    "XRPUSDT": 0.0001,
    "ETHUSDT": 0.01,
    "CHZUSDT": 0.001,
    "LINKUSDT": 0.001,
    
}
# ────────────────────────────────────────────────────────────────
#  Pairs & history
PAIRS_LRS: List[str] = ["ATOMUSDT"]
PAIRS_LRS_MULTI: List[str] = ["SOLUSDT", "ETHUSDT", "ATOMUSDT", "WAVESUSDT", "XRPUSDT"]
# execution realism
SLIP_BPS = 0.001        #  bps of adverse slippage on entry
FEE_BPS = 10         # 10 bps per side (taker ~0.05% each way? adjust)
