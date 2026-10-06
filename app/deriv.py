import asyncio
import json
import websockets
from .config import DERIV_WS_URL, TIMEFRAMES, TF_ALL, CANDLE_COUNT


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


async def _candles(ws, symbol: str, gran: int, count: int) -> list:
    """Up to `count` candles, oldest first. One request returns at most about 1000, so older pages are fetched
    by moving the `end` time back until enough candles arrive or the history runs out."""
    got: dict = {}
    end = "latest"
    while len(got) < count:
        resp = await _request(ws, {"ticks_history": symbol, "adjust_start_time": 1, "count": min(1000, count - len(got)),
                                   "end": end, "style": "candles", "granularity": gran}, expect="candles")
        page = resp["candles"]
        before = len(got)
        for c in page:
            got[int(c["epoch"])] = c
        if not page or len(got) == before:      # nothing new: the history has ended
            break
        end = int(page[0]["epoch"]) - 1
        await asyncio.sleep(0.2)                # stay well inside Deriv's rate limits
    return [got[k] for k in sorted(got)][-count:]


async def fetch_multi(symbols: list[str], tfs: tuple, count) -> dict:
    """Candles for several symbols over ONE connection. {symbol: {tf: candles}}; symbols that fail are skipped.
    `count` may be a number or a {tf: number} dict."""
    out: dict = {}
    async with websockets.connect(DERIV_WS_URL, open_timeout=15, ping_interval=20) as ws:
        for sym in symbols:
            got = {}
            for tf in tfs:
                try:
                    page = await _candles(ws, sym, TF_ALL[tf], count[tf] if isinstance(count, dict) else count)
                    got[tf] = [{k: float(c[k]) if k != "epoch" else int(c[k])
                                for k in ("epoch", "open", "high", "low", "close")} for c in page]
                except Exception:
                    got = {}
                    break
            if got:
                out[sym] = got
    return out
