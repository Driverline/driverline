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

Return ONLY a JSON object, no prose, no markdown fences, with exactly these keys:
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
  "next_step": "what to wait for or do next"
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


async def claude_text(system: str, content: str, max_tokens: int = 1200) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("Set ANTHROPIC_API_KEY in your environment")
    payload = {"model": MODEL, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": content}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    async with httpx.AsyncClient(timeout=90) as client:
        r = await client.post(API_URL, json=payload, headers=headers)
    if r.status_code != 200:
        raise RuntimeError(f"Claude API {r.status_code}: {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json()["content"])


async def generate_trade_card(symbol_name: str, facts: dict) -> dict:
    text = await claude_text(SYSTEM, f"Symbol: {symbol_name}\nFacts:\n{json.dumps(facts)}")
    return validate_card(parse_json(text))
