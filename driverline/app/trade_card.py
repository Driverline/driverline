import asyncio
import json
import os
import re
import httpx

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")

SYSTEM = """You are a market-structure education assistant for Deriv Synthetic Indices.
You receive PRE-COMPUTED indicator facts per timeframe as JSON. Interpret only those numbers.
Never invent prices, levels or indicators that are not in the input. Synthetic indices are
RNG-generated, so never claim a pattern is predictive; frame output as structure and risk.
Boom, Crash and Jump indices produce sudden spikes: treat single spike candles as outliers, and
warn that stops can be skipped by spikes.

Respond with a JSON object with these fields:
{
  "headline": "one sentence summary",
  "timeframes": {"D1": "...", "H4": "...", "H1": "...", "M30": "...", "M15": "..."},
  "plain_words": "2-3 sentences a beginner understands",
  "status": "TRADE" or "NO TRADE",
  "direction": "BUY" or "SELL" or null,
  "entry": number or null,
  "stop": number or null,
  "target": number or null,
  "why": "short reason for the status",
  "next_step": "what to wait for or do next",
  "setup_type": "trend_pullback, breakout, range_reversal, momentum_continuation or none",
  "confidence": "integer 0-100: how clear the setup is (0 when NO TRADE)",
  "no_trade_reason": "mixed_timeframes, consolidation, unclear_structure, overextended, poor_risk_reward, spike_risk or other; null when status is TRADE"
}
Default to NO TRADE when timeframes conflict, price is mid-range, or reward:risk is below 1.5.
Entry, stop and target must come from provided swing levels or range boundaries."""

REQUIRED = ["headline", "timeframes", "plain_words", "status", "why", "next_step"]


def parse_json(text: str) -> dict:
    text = re.sub(r"```(?:json)?", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("Model did not return JSON")
    return json.loads(text[start:end + 1])


def validate_card(card: dict) -> dict:
    missing = [k for k in REQUIRED if k not in card]
    if missing:
        raise ValueError(f"Trade card missing keys: {missing}")
    card["status"] = str(card["status"]).upper().strip()
    if card["status"] not in ("TRADE", "NO TRADE"):
        card["status"] = "NO TRADE"
    d = card.get("direction")
    e, s, t = card.get("entry"), card.get("stop"), card.get("target")
    if card["status"] == "TRADE":
        ok = d in ("BUY", "SELL") and all(isinstance(x, (int, float)) for x in (e, s, t))
        if ok and d == "BUY":
            ok = s < e < t
        elif ok and d == "SELL":
            ok = t < e < s
        if ok:
            risk, reward = abs(e - s), abs(t - e)
            card["reward_risk"] = round(reward / risk, 2) if risk else None
            ok = bool(risk) and reward / risk >= 1.0
        if not ok:
            card["status"] = "NO TRADE"
            card["why"] += " (Levels failed server-side validation.)"
    if card["status"] == "NO TRADE":
        card["direction"] = card["entry"] = card["stop"] = card["target"] = None
    return card


_TF = {"type": "string"}
_NUM_OR_NULL = {"anyOf": [{"type": "number"}, {"type": "null"}]}
CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "timeframes": {"type": "object", "properties": {k: _TF for k in ("D1", "H4", "H1", "M30", "M15")},
                       "required": ["D1", "H4", "H1", "M30", "M15"], "additionalProperties": False},
        "plain_words": {"type": "string"},
        "status": {"type": "string", "enum": ["TRADE", "NO TRADE"]},
        "direction": {"anyOf": [{"type": "string", "enum": ["BUY", "SELL"]}, {"type": "null"}]},
        "entry": _NUM_OR_NULL,
        "stop": _NUM_OR_NULL,
        "target": _NUM_OR_NULL,
        "why": {"type": "string"},
        "next_step": {"type": "string"},
        "setup_type": {"type": "string", "enum": ["trend_pullback", "breakout", "range_reversal",
                                                   "momentum_continuation", "none"]},
        "confidence": {"type": "integer"},
        "no_trade_reason": {"anyOf": [{"type": "string", "enum": [
            "mixed_timeframes", "consolidation", "unclear_structure", "overextended",
            "poor_risk_reward", "spike_risk", "other"]}, {"type": "null"}]},
    },
    "required": ["headline", "timeframes", "plain_words", "status", "direction",
                 "entry", "stop", "target", "why", "next_step", "setup_type", "confidence", "no_trade_reason"],
    "additionalProperties": False,
}


async def _post(payload: dict) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Set ANTHROPIC_API_KEY in your environment")
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for attempt in (1, 2):  # one retry for temporary overload or rate-limit errors
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(API_URL, json=payload, headers=headers)
        if r.status_code in (429, 500, 502, 503, 529) and attempt == 1:
            await asyncio.sleep(2)
            continue
        break
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
    return r.json()


async def claude_text(system: str, content: str, max_tokens: int = 1200) -> str:
    data = await _post({"model": MODEL, "max_tokens": max_tokens, "system": system,
                        "messages": [{"role": "user", "content": content}]})
    return "".join(b.get("text", "") for b in data["content"])


async def claude_json(system: str, content: str, schema: dict, max_tokens: int = 4000) -> dict:
    """Asks for JSON that must match `schema` (structured outputs), so the reply always parses."""
    data = await _post({
        "model": MODEL, "max_tokens": max_tokens, "system": system,
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
        "messages": [{"role": "user", "content": content}],
    })
    stop = data.get("stop_reason")
    if stop == "max_tokens":
        raise RuntimeError("The AI answer was cut off. Please try again.")
    if stop == "refusal":
        raise RuntimeError("The AI declined to answer this request.")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    if not text.strip():
        raise RuntimeError("The AI did not return a structured answer. Please try again.")
    return parse_json(text)


async def generate_trade_card(symbol_name: str, facts: dict) -> dict:
    card = await claude_json(SYSTEM, f"Symbol: {symbol_name}\nFacts:\n{json.dumps(facts)}", CARD_SCHEMA)
    if isinstance(card.get("direction"), str):
        card["direction"] = card["direction"].upper()
    return validate_card(card)
