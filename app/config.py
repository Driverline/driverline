import json
import os
import re

# Deriv's current public market-data WebSocket (no login or app_id needed).
# The old ws.derivws.com/websockets/v3 endpoint returned HTTP 520. Override with the
# DERIV_WS_URL environment variable if Deriv moves it again.
DERIV_WS_URL = os.environ.get("DERIV_WS_URL", "wss://api.derivws.com/trading/v1/options/ws/public")

# key -> (Deriv symbol code, display name). Add new instruments here.
# Run /api/verify to confirm every code exists on your Deriv account.
SYNTHETIC = {
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

# Forex majors and gold (real markets). Analysis only for now: they are NOT scanned for live signals and NOT part
# of the signal backtest until they have been backtested on their own with realistic costs.
# Display names are the plain MT5 symbol names so they match the symbols in members' Market Watch.
REAL_MARKETS = {
    "eurusd":   ("frxEURUSD",  "EURUSD"),
    "gbpusd":   ("frxGBPUSD",  "GBPUSD"),
    "usdjpy":   ("frxUSDJPY",  "USDJPY"),
    "usdchf":   ("frxUSDCHF",  "USDCHF"),
    "audusd":   ("frxAUDUSD",  "AUDUSD"),
    "usdcad":   ("frxUSDCAD",  "USDCAD"),
    "nzdusd":   ("frxNZDUSD",  "NZDUSD"),
    "xauusd":   ("frxXAUUSD",  "XAUUSD"),
}
INSTRUMENTS = {**SYNTHETIC, **REAL_MARKETS}


# Assumed round-trip trading cost per trade for the signal backtest and for tracking live signals, in PRICE units
# (spread plus a little slippage). These are deliberately on the cautious side of typical retail spreads: check them
# against your own broker and override with the DRIVERLINE_SPREADS environment variable, for example
# {"eurusd": 0.0001, "xauusd": 0.25}. Synthetic indices keep the flat fraction-of-ATR cost used so far.
SPREADS = {"eurusd": 0.00015, "gbpusd": 0.00020, "usdjpy": 0.018, "usdchf": 0.00022, "audusd": 0.00018,
           "usdcad": 0.00022, "nzdusd": 0.00025, "xauusd": 0.40}
try:
    SPREADS.update({k: float(v) for k, v in json.loads(os.environ.get("DRIVERLINE_SPREADS", "{}")).items()
                    if k in REAL_MARKETS and float(v) > 0})
except (ValueError, TypeError, AttributeError):
    pass


def spread(key: str) -> float:
    """Assumed trading cost in price units; 0 means "use the flat synthetic-index cost"."""
    return SPREADS.get(key, 0.0)


def market(key: str) -> str:
    """"synthetic", "forex" or "metal"."""
    return "synthetic" if key in SYNTHETIC else ("metal" if key == "xauusd" else "forex")


# Brokers add endings to symbol names (XAUUSDm, XAUUSDmicro, EURUSD.a, EURUSDpro ...). One instrument can therefore
# have several MT5 symbols; match_key() maps any of them back to the instrument.
_SUFFIXES = {"m", "micro", "mini", "pro", "raw", "ecn", "std", "c", "i", "r", "z", "a", "b", "x"}


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower().replace("index", ""))


def match_key(symbol: str):
    """Instrument key for an MT5 symbol name, or None. Exact names win; broker endings are accepted for forex and gold only."""
    s = _flat(symbol)
    for key, (_, name) in INSTRUMENTS.items():
        if s == _flat(name):
            return key
    for key, (_, name) in REAL_MARKETS.items():
        base = _flat(name)
        if s.startswith(base) and s[len(base):] in _SUFFIXES:
            return key
    return None

# Timeframe label -> granularity in seconds (Deriv candle granularities)
TIMEFRAMES = {
    "D1": 86400,
    "H4": 14400,
    "H1": 3600,
    "M30": 1800,
    "M15": 900,
}

# Extra timeframes used only by the signal engine and its backtest
TF_ALL = {**TIMEFRAMES, "M5": 300, "M1": 60, "H4": 14400}

CANDLE_COUNT = 300
