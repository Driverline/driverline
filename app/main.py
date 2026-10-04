import asyncio
import os
import time
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import admin, auth, ea, journal
from .config import INSTRUMENTS
from .deriv import fetch_candles, fetch_active_symbols
from .indicators import analyze_all
from .summary import summarize_facts
from .trade_card import generate_trade_card

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
CACHE_TTL = int(os.environ.get("DRIVERLINE_CACHE_SECONDS", 300))
_cache: dict[str, tuple[float, dict]] = {}   # instrument key -> (time, successful card payload)
_locks: dict[str, asyncio.Lock] = {}


async def _facts(key: str):
    if key not in INSTRUMENTS:
        raise HTTPException(404, f"Unknown instrument. Available: {list(INSTRUMENTS)}")
    symbol, name = INSTRUMENTS[key]
    try:
        candles = await fetch_candles(symbol)
    except Exception as e:
        raise HTTPException(502, f"Deriv: {e}")
    return symbol, name, analyze_all(candles)


def _friendly(msg: str) -> str:
    if "credit balance" in msg:
        return "AI analysis is temporarily unavailable. Computed facts are shown instead."
    if "ANTHROPIC_API_KEY" in msg:
        return "AI analysis is not configured on the server."
    if "structured answer" in msg or "cut off" in msg or "declined" in msg:
        return "The AI could not give an answer this time. Please try again."
    return msg[:200]


def _fresh(key):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None


async def instruments(request):
    auth.require_user(request)
    return JSONResponse([{"key": k, "name": v[1]} for k, v in INSTRUMENTS.items()])


async def verify(request):
    auth.require_admin(request)
    try:
        live = await fetch_active_symbols()
    except Exception as e:
        raise HTTPException(502, f"Deriv: {e}")
    return JSONResponse({k: {"code": v[0], "found": v[0] in live} for k, v in INSTRUMENTS.items()})


async def analysis(request):
    auth.require_user(request)
    symbol, name, facts = await _facts(request.path_params["key"])
    return JSONResponse({"symbol": symbol, "name": name, "facts": facts})


async def tradecard(request):
    user = auth.require_user(request)
    key = request.path_params["key"]
    out = _fresh(key)
    if out:
        return JSONResponse({**out, "cached": True})
    async with _locks.setdefault(key, asyncio.Lock()):
        out = _fresh(key)  # another member may have filled the cache while we waited
        if out:
            return JSONResponse({**out, "cached": True})
        symbol, name, facts = await _facts(key)
        out = {"symbol": symbol, "name": name, "facts": facts, "summary": summarize_facts(facts), "card": None}
        if not auth.has_quota(user):
            used, limit = auth.quota(user)
            out["card_error"] = f"Daily AI limit reached ({used}/{limit}). Showing computed facts only."
        else:
            try:
                out["card"] = await generate_trade_card(name, facts)
                auth.consume_quota(user)
                _cache[key] = (time.time(), dict(out))
            except Exception as e:  # fall back to computed facts only
                out["card_error"] = _friendly(str(e))
    return JSONResponse(out)


async def http_error(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


routes = [
    Route("/api/instruments", instruments),
    Route("/api/verify", verify),
    Route("/api/analysis/{key}", analysis),
    Route("/api/tradecard/{key}", tradecard),
    *auth.routes,
    *admin.routes,
    *ea.routes,
    *journal.routes,
    Mount("/", app=StaticFiles(directory=str(STATIC_DIR), html=True), name="static"),
]

app = Starlette(routes=routes, exception_handlers={HTTPException: http_error})
