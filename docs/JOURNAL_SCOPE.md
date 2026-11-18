# Journal by account and period; signals page shows active setups only

**Journal.** Each time an MT5 login reports through a connection key, `ea.track_session` keeps one row in `mt5_sessions` (login, server, started_at, last_seen). The same login reporting again continues its session; a different login on the same key (or the same one after 6 hours of silence) starts a new one. Existing connections were backfilled once with started_at = key creation time (the best known).
The journal page now shows ONE account at a time: by default the login connected now, and only trades closed since that login first connected. Nothing is deleted: every imported trade stays in `trades`, and earlier logins and hand-logged trades ("manual") are selectable in the Account menu. Period: everything since connected, Today, Last 7 days, Last 30 days, or a custom range (days are the member's own local days, sent as UTC instants). Stats and the AI review use the same account and period. API: `GET /api/journal?account=<login|manual>&from=&to=`, same for `/stats` and `POST /review`, and `GET /api/journal/accounts`. Without `account`, the connected login (else manual) is used.
Not changed: the daily recap, rewards and other readers of `trades`.

**Signals.** The signals page shows only setups that are open, still fresh and not already traded through Deriv. Expired, invalid, finished and traded setups are hidden, and symbols with nothing active are not listed. "Show past signals (N)" / "Hide past signals" toggles them (remembered in the browser).

Tests: `python3 tests/test_journal_scope.py`. Migration: migrations/2026_11_mt5_sessions.sql.
