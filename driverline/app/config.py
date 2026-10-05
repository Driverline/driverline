import os

# Deriv's current public market-data WebSocket (no login or app_id needed).
# The old ws.derivws.com/websockets/v3 endpoint returned HTTP 520. Override with the
# DERIV_WS_URL environment variable if Deriv moves it again.
DERIV_WS_URL = os.environ.get("DERIV_WS_URL", "wss://api.derivws.com/trading/v1/options/ws/public")

# key -> (Deriv symbol code, display name). Add new instruments here.
# Run /api/verify to confirm every code exists on your Deriv account.
INSTRUMENTS = {
    "step":     ("stpRNG",     "Step Index"),
    "v10":      ("R_10",       "Volatility 10"),
    "v25":      ("R_25",       "Volatility 25"),
    "v50":      ("R_50",       "Volatility 50"),
    "v75":      ("R_75",       "Volatility 75"),
    "v100":     ("R_100",      "Volatility 100"),
    "v10_1s":   ("1HZ10V",     "Volatility 10 (1s)"),
    "v25_1s":   ("1HZ25V",     "Volatility 25 (1s)"),
    "v50_1s":   ("1HZ50V",     "Volatility 50 (1s)"),
    "v75_1s":   ("1HZ75V",     "Volatility 75 (1s)"),
    "v100_1s":  ("1HZ100V",    "Volatility 100 (1s)"),
    "boom300":  ("BOOM300N",   "Boom 300"),
    "boom500":  ("BOOM500",    "Boom 500"),
    "boom1000": ("BOOM1000",   "Boom 1000"),
    "crash300": ("CRASH300N",  "Crash 300"),
    "crash500": ("CRASH500",   "Crash 500"),
    "crash1000":("CRASH1000",  "Crash 1000"),
    "jump10":   ("JD10",       "Jump 10"),
    "jump25":   ("JD25",       "Jump 25"),
    "jump50":   ("JD50",       "Jump 50"),
    "jump75":   ("JD75",       "Jump 75"),
    "jump100":  ("JD100",      "Jump 100"),
}

# Timeframe label -> granularity in seconds (Deriv candle granularities)
TIMEFRAMES = {
    "D1": 86400,
    "H4": 14400,
    "H1": 3600,
    "M30": 1800,
    "M15": 900,
}

CANDLE_COUNT = 300
