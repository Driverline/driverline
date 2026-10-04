import json
import websockets
from .config import DERIV_WS_URL, TIMEFRAMES, CANDLE_COUNT


def _error_message(resp: dict):
    """Deriv reports errors as {"error": {...}} or {"errors": [{...}]}."""
    err = resp.get("error")
    if isinstance(err, dict):
        return err.get("message") or str(err)
    errs = resp.get("errors")
    if isinstance(errs, list) and errs:
        first = errs[0]
        return first.get("message") if isinstance(first, dict) else str(first)
    return None


async def _request(ws, payload: dict, expect: str) -> dict:
    await ws.send(json.dumps(payload))
    for _ in range(5):  # skip any unrelated message before the one we asked for
        resp = json.loads(await ws.recv())
        msg = _error_message(resp)
        if msg:
            raise RuntimeError(f"Deriv API error: {msg}")
        if expect in resp:
            return resp
    raise RuntimeError(f"Deriv sent no '{expect}' in its response")


async def fetch_candles(symbol: str, count: int = CANDLE_COUNT) -> dict[str, list[dict]]:
    """Return {timeframe: [ {epoch, open, high, low, close}, ... ]} oldest -> newest."""
    out: dict[str, list[dict]] = {}
    async with websockets.connect(DERIV_WS_URL, open_timeout=15, ping_interval=20) as ws:
        for tf, gran in TIMEFRAMES.items():
            resp = await _request(ws, {
                "ticks_history": symbol,
                "adjust_start_time": 1,
                "count": count,
                "end": "latest",
                "style": "candles",
                "granularity": gran,
            }, expect="candles")
            candles = [
                {k: float(c[k]) if k != "epoch" else int(c[k])
                 for k in ("epoch", "open", "high", "low", "close")}
                for c in resp["candles"]
            ]
            if not candles:
                raise RuntimeError(f"No candles returned for {symbol} {tf}")
            out[tf] = candles
    return out


async def fetch_active_symbols() -> set[str]:
    async with websockets.connect(DERIV_WS_URL, open_timeout=15) as ws:
        resp = await _request(ws, {"active_symbols": "brief"}, expect="active_symbols")
    names = {s.get("symbol") or s.get("underlying_symbol") for s in resp["active_symbols"]}
    names.discard(None)
    if not names:
        raise RuntimeError("Could not read symbol names from Deriv's active_symbols response")
    return names
