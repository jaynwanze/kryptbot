# ────────────────────────────────────────────────────────────────
#  Configuration
# ────────────────────────────────────────────────────────────────
PAIR            = "SOLUSDT"          # Bybit symbol
TF_SECONDS      = 15 * 60            # 15‑minute bars
INTERVAL        = "15"               # stream interval, string
LOOKBACK_BARS   = 800                # kept in memory (≈ 8 days)

# Strategy param
RISK_PCT        = 0.02               # not used (alerts only)
# ATR_MULT_SL     = 1.5
# ATR_MULT_TP     = 3           # RR 2:1
ATR_MULT_SL     = 1.3       
ATR_MULT_TP     = 2.6     # RR 2:1
WICK_BUFFER     = 0.25               # extra ATR cushion
ADX_FLOOR       = 20
STO_K_MIN_LONG  = 45
STO_K_MIN_SHORT = 30