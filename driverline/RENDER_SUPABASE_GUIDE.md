# Driverline on Render + Supabase (no phone, no tunnel script)

**Status:** the Supabase support was tested only against a local SQLite stand-in. The first live run may show small bugs. Copy any error to Claude.

## What you get, and the limits
- A permanent address like `https://yourapp.onrender.com` with HTTPS. The MT5 allow-list stops changing.
- Supabase keeps accounts, journals and analysis history even when Render restarts.
- **Render free web services sleep after about 15 minutes without visitors**, so the first visitor after a quiet spell waits roughly a minute. In-memory items (the 5-minute AI cache, login-attempt counters) reset when it wakes.
- **Supabase free projects pause after about a week with no activity.** Restart them from the Supabase dashboard if that ever happens.
- Check Render's and Supabase's pricing pages for the current free limits and whether signup asks for a card. These change.
- Upgrading later (no sleeping, no pausing) needs no code change.

## Step 1: GitHub
1. Make a **private** repository and push the project. Make sure `.gitignore` contains `data/`, `.env`, `*.log`, `__pycache__/`, `*.zip`.
2. `requirements.txt` already lists everything, including `psycopg[binary]`.

## Step 2: Supabase
1. Create a project. Choose a region near your members. Save the database password.
2. Open the project's **Connect** panel and copy a **pooler** connection string (Transaction pooler or Session pooler). Render connects over IPv4, and Supabase's direct connection may be IPv6-only.
3. Put your password in place of `[YOUR-PASSWORD]`. That whole line is your `DATABASE_URL`.

## Step 3: Render
1. New, **Web Service**, connect your GitHub repo.
2. Language: Python. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips='*'`
4. Instance type: Free (or paid later).
5. Add these environment variables:

| Name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Claude API key |
| `DATABASE_URL` | the Supabase pooler string from Step 2 |
| `DRIVERLINE_SECRET` | a long random string (`python3 -c "import secrets;print(secrets.token_hex(32))"`). **Required**, or everyone is logged out on every restart |
| `DRIVERLINE_SECURE` | `1` |
| `DRIVERLINE_BASE_URL` | your Render address, e.g. `https://yourapp.onrender.com` (add after the first deploy) |
| `DRIVERLINE_ADMIN_EMAIL` | your admin email |
| `DRIVERLINE_ADMIN_PASSWORD` | a strong password (8+ characters). **Delete this variable after your first login** |
| `SMTP_*` (optional) | see `DEPLOY_GUIDE.md`, for password-reset emails |

6. Deploy. Open the address, sign in with the admin email and password, then create invite codes on the admin page. Render's free plan has no shell, which is why the admin account is created from those two variables the first time the server starts.

**Security:** the app switches on row-level security for every table it creates in Supabase, so the project's public REST API cannot read your members' data. After the first deploy, check Supabase's Security Advisor shows no "RLS disabled" warnings.

## Step 4 (optional): bring your existing phone data across
Only if you have members and journals worth keeping. From a computer or a Termux that can install `psycopg[binary]`:
```
DATABASE_URL="postgresql://..." python -m app.migrate_to_postgres ~/driverline/data/journal.db
```
It is safe to run twice. For a small pilot it may be easier to start fresh and re-send invite codes.

## Troubleshooting
| Symptom | Fix |
|---|---|
| `No module named psycopg` | `psycopg[binary]` is missing from `requirements.txt` on the deploy |
| Timeout or "network unreachable" to the database | Use the **pooler** string, not the direct one |
| Everyone logged out after each restart | `DRIVERLINE_SECRET` is not set |
| Reset links point to the wrong address | Set `DRIVERLINE_BASE_URL` |
| First page load takes about a minute | Render free wakes from sleep. Normal |
| Admin login fails on a new deploy | Check the two admin variables, then redeploy. An existing account is never overwritten |

---

# Smart Analysis Timing (Opportunity Radar)

**What it is:** a ranking of the supported symbols by how worthwhile it is to spend one analysis credit on them right now. It never calls Claude and never uses a credit. The score is not a win probability.

**How the score is built** (weights and thresholds are configurable):
- **Setup readiness** (current): from the same candle indicators the app already computes, on M15 and H1: structure clarity, EMA alignment, directional efficiency, volatility versus its recent range, momentum, and whether price is ranging.
- **Time of day** (only if proven): each symbol's candle history (about 50 days) is tested for a real time-of-day effect, using day-level statistics so overlapping candles don't fake significance. If none is found, the time component is dropped and the app says "No meaningful historical time advantage detected."
- **Claude's real results:** every fresh analysis is logged (symbol, hour, TRADE or NO TRADE, setup type, confidence, no-trade reason, the market state at that moment). Once a symbol has about 30 logged analyses, its actual trade rate in similar conditions starts to count, and the weight grows to full at about 60.
- Missing components are left out and the rest re-weighted, so nothing is invented while data is thin.

**Important honesty note:** until enough real analyses exist, the "actionable" idea from candle history is a proxy based on how clean the structure looks. It is not Claude's own decision. The proxy is replaced step by step by Claude's real results.

**What it never claims:** no predictions, no profit or win probability, and no countdown for Boom, Crash or Jump spikes. A spike that just happened lowers the readiness instead (conditions may still be settling).

**Tuning:** set `DRIVERLINE_RADAR_CONFIG` to JSON, for example `{"min_recommend": 70, "weights": {"ready": 0.6}}`. Keys are in `DEFAULT_CONFIG` at the top of `app/radar.py`. The admin page shows what the radar has learned per symbol.

**Deferred until there is enough data:** scoring by actual trade outcomes (profit quality), per-regime calibration of readiness against Claude's real trade rate, and richer Boom, Crash and Jump features.
