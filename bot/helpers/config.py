# ────────────────────────────────────────────────────────────────
#  Configuration
# ────────────────────────────────────────────────────────────────
PAIR            = "SOLUSDT"          # Bybit symbol
TF_SECONDS      = 15 * 60            # 15‑minute bars
INTERVAL        = "15"               # stream interval, string
LOOKBACK_BARS   = 800                # kept in memory (≈ 8 days)

# Strategy param
RISK_PCT        = 0.20               # not used (alerts only)
# ATR_MULT_SL     = 1.5
# ATR_MULT_TP     = 3           # RR 2:1
# config.py  (or wherever you keep constants)
STAKE_SIZE_USD   = 1_000          # ‘cash you allocate’ per entry
LEVERAGE         = 25
ATR_MULT_SL     = 1       
ATR_MULT_TP     = 2     # RR 2:1
WICK_BUFFER     = 0.25               # extra ATR cushion
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
BOS_LOOKBACK = 5     # was 20
FVG_MIN_PX   = 0.0003  # was 0.0005
FIB_EXT      = 0.79      # 79 % retrace / extension
