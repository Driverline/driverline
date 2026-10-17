"""Tables and helpers for the research engine: stored candles, dataset registry, model versions, runs and trials."""
import hashlib
import json
import time
from contextlib import closing

from .. import auth, dbx

SCHEMA = """
    CREATE TABLE IF NOT EXISTS market_candles(symbol TEXT NOT NULL, tf INTEGER NOT NULL, epoch BIGINT NOT NULL,
        open DOUBLE PRECISION NOT NULL, high DOUBLE PRECISION NOT NULL, low DOUBLE PRECISION NOT NULL,
        close DOUBLE PRECISION NOT NULL, n_ticks INTEGER, up_ticks INTEGER, down_ticks INTEGER, max_up_run INTEGER,
        max_down_run INTEGER, dir_changes INTEGER, PRIMARY KEY(symbol, tf, epoch));
    CREATE TABLE IF NOT EXISTS research_datasets(id {ID}, symbol TEXT NOT NULL, tf INTEGER NOT NULL, first_epoch BIGINT,
        last_epoch BIGINT, row_count INTEGER NOT NULL, sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('valid','warn','invalid')), report TEXT NOT NULL, created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_datasets_symbol ON research_datasets(symbol, tf, id);
    CREATE TABLE IF NOT EXISTS model_versions(id {ID}, family TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'research_only'
            CHECK (status IN ('research_only','demo_validated','approved','suspended')),
        definition TEXT NOT NULL, feature_spec_hash TEXT, code_hash TEXT, status_reason TEXT,
        created_at BIGINT NOT NULL, status_changed_at BIGINT NOT NULL, UNIQUE(name, version));
    CREATE TABLE IF NOT EXISTS research_runs(id {ID}, kind TEXT NOT NULL, symbol TEXT, dataset_id BIGINT,
        model_version_id BIGINT, split TEXT, params TEXT, result TEXT, test_unlocked_at BIGINT, started_at BIGINT NOT NULL,
        finished_at BIGINT, status TEXT NOT NULL CHECK (status IN ('running','done','failed')));
    CREATE INDEX IF NOT EXISTS ix_runs_kind ON research_runs(kind, id);
    CREATE TABLE IF NOT EXISTS research_trials(id {ID}, run_id BIGINT NOT NULL, label TEXT NOT NULL, segment TEXT,
        params TEXT, metrics TEXT, created_at BIGINT NOT NULL);
    CREATE INDEX IF NOT EXISTS ix_trials_run ON research_trials(run_id);
    CREATE TABLE IF NOT EXISTS ai_signals(id {ID}, model_version_id BIGINT NOT NULL, symbol TEXT NOT NULL, tf INTEGER NOT NULL,
        bar_epoch BIGINT NOT NULL, decision TEXT NOT NULL CHECK (decision IN ('BUY','SELL')),
        entry_ref DOUBLE PRECISION NOT NULL, atr DOUBLE PRECISION NOT NULL, stop DOUBLE PRECISION NOT NULL,
        target DOUBLE PRECISION NOT NULL, rr DOUBLE PRECISION NOT NULL, planned_entry_epoch BIGINT NOT NULL,
        expires_at BIGINT NOT NULL, rationale TEXT NOT NULL, confidence DOUBLE PRECISION,
        evidence_status TEXT NOT NULL CHECK (evidence_status IN ('research_only','demo_validated','approved')),
        feature_ref TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'admin' CHECK (visibility IN ('admin','members')),
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','target','stop','timeout')),
        result_r DOUBLE PRECISION, closed_at BIGINT, created_at BIGINT NOT NULL,
        UNIQUE(model_version_id, symbol, bar_epoch));
    CREATE INDEX IF NOT EXISTS ix_ai_signals_model ON ai_signals(model_version_id, id);
    CREATE TABLE IF NOT EXISTS risk_events(id {ID}, model_version_id BIGINT NOT NULL, symbol TEXT NOT NULL,
        bar_epoch BIGINT NOT NULL, kind TEXT NOT NULL, detail TEXT, created_at BIGINT NOT NULL,
        UNIQUE(model_version_id, symbol, bar_epoch, kind));
    CREATE INDEX IF NOT EXISTS ix_risk_events_model ON risk_events(model_version_id, id)"""


def db():
    auth.db().close()
    return dbx.connect("engine", SCHEMA)


def now() -> int:
    return int(time.time())


# ---------------------------------------------------------------- candles
def upsert_candles(symbol: str, tf: int, rows: list) -> int:
    """rows: dicts with epoch, open, high, low, close. A revised last candle replaces the old one; tick statistics
    already stored for a bar are kept."""
    n = 0
    with closing(db()) as con:
        for i in range(0, len(rows), 100):
            chunk = rows[i:i + 100]
            ph = ",".join(["(?,?,?,?,?,?,?)"] * len(chunk))
            args = []
            for r in chunk:
                args += [symbol, tf, int(r["epoch"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])]
            con.execute("INSERT INTO market_candles(symbol,tf,epoch,open,high,low,close) VALUES " + ph +
                        " ON CONFLICT(symbol,tf,epoch) DO UPDATE SET open=excluded.open, high=excluded.high, "
                        "low=excluded.low, close=excluded.close", args)
            n += len(chunk)
        con.commit()
    return n


def update_tick_stats(symbol: str, tf: int, stats: dict) -> int:
    """stats: {epoch: {n, up, down, max_up, max_down, changes}} for bars that already exist."""
    n = 0
    with closing(db()) as con:
        for epoch, s in stats.items():
            cur = con.execute("UPDATE market_candles SET n_ticks=?, up_ticks=?, down_ticks=?, max_up_run=?, "
                              "max_down_run=?, dir_changes=? WHERE symbol=? AND tf=? AND epoch=?",
                              (s["n"], s["up"], s["down"], s["max_up"], s["max_down"], s["changes"], symbol, tf, epoch))
            n += cur.rowcount
        con.commit()
    return n


def load_candles(symbol: str, tf: int) -> list:
    with closing(db()) as con:
        rows = con.execute("SELECT epoch, open, high, low, close, n_ticks, up_ticks, down_ticks, max_up_run, "
                           "max_down_run, dir_changes FROM market_candles WHERE symbol=? AND tf=? ORDER BY epoch",
                           (symbol, tf)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- datasets, models, runs, trials
def register_dataset(symbol: str, tf: int, rows: list, report: dict, sha: str, status: str) -> int:
    with closing(db()) as con:
        did = con.insert("INSERT INTO research_datasets(symbol,tf,first_epoch,last_epoch,row_count,sha256,status,report,"
                         "created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (symbol, tf, rows[0]["epoch"] if rows else None, rows[-1]["epoch"] if rows else None, len(rows),
                          sha, status, json.dumps(report), now()))
        con.commit()
    return did


def latest_dataset(symbol: str, tf: int):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM research_datasets WHERE symbol=? AND tf=? ORDER BY id DESC LIMIT 1",
                        (symbol, tf)).fetchone()
    return dict(r) if r else None


def register_model(family: str, name: str, version: int, definition: dict, feature_hash: str, code_hash: str) -> int:
    """Pre-registers a model or baseline BEFORE it is tested. Re-registering the same name and version is a no-op,
    and a changed definition under the same name and version is refused (a new version is required)."""
    d = json.dumps(definition, sort_keys=True)
    with closing(db()) as con:
        r = con.execute("SELECT id, definition FROM model_versions WHERE name=? AND version=?", (name, version)).fetchone()
        if r:
            if r["definition"] != d:
                raise ValueError(f"{name} v{version} already exists with a different definition; use a new version")
            return r["id"]
        mid = con.insert("INSERT INTO model_versions(family,name,version,definition,feature_spec_hash,code_hash,created_at,"
                         "status_changed_at) VALUES(?,?,?,?,?,?,?,?)",
                         (family, name, version, d, feature_hash, code_hash, now(), now()))
        con.commit()
    return mid


def start_run(kind: str, symbol=None, dataset_id=None, model_version_id=None, split=None, params=None) -> int:
    with closing(db()) as con:
        rid = con.insert("INSERT INTO research_runs(kind,symbol,dataset_id,model_version_id,split,params,started_at,status) "
                         "VALUES(?,?,?,?,?,?,?,?)", (kind, symbol, dataset_id, model_version_id,
                                                      json.dumps(split) if split else None,
                                                      json.dumps(params) if params else None, now(), "running"))
        con.commit()
    return rid


def finish_run(run_id: int, result: dict, ok: bool = True):
    with closing(db()) as con:
        con.execute("UPDATE research_runs SET status=?, finished_at=?, result=? WHERE id=?",
                    ("done" if ok else "failed", now(), json.dumps(result), run_id))
        con.commit()


def log_trial(run_id: int, label: str, segment: str, params: dict, metrics: dict):
    """Every variation tried is recorded here, whether or not it looked good."""
    with closing(db()) as con:
        con.execute("INSERT INTO research_trials(run_id,label,segment,params,metrics,created_at) VALUES(?,?,?,?,?,?)",
                    (run_id, label, segment, json.dumps(params), json.dumps(metrics), now()))
        con.commit()


STATUSES = ("research_only", "demo_validated", "approved", "suspended")


def set_model_status(model_id: int, status: str, reason: str) -> None:
    """Changes a model's evidence status. A reason is always required. Moving UP to demo_validated or approved is done
    only by the code that holds the proof (the untouched-test evaluation, the forward-demo evaluation)."""
    if status not in STATUSES or not reason.strip():
        raise ValueError("a valid status and a reason are required")
    with closing(db()) as con:
        cur = con.execute("UPDATE model_versions SET status=?, status_reason=?, status_changed_at=? WHERE id=?",
                          (status, reason[:300], now(), model_id))
        con.commit()
        if cur.rowcount != 1:
            raise ValueError("unknown model")


def get_model(model_id: int):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM model_versions WHERE id=?", (model_id,)).fetchone()
    return dict(r) if r else None


def unlock_test(run_id: int) -> bool:
    """The untouched test segment can be evaluated ONCE per run. True the first time, False ever after."""
    with closing(db()) as con:
        cur = con.execute("UPDATE research_runs SET test_unlocked_at=? WHERE id=? AND test_unlocked_at IS NULL",
                          (now(), run_id))
        con.commit()
        return cur.rowcount == 1


def set_run_result(run_id: int, result: dict) -> None:
    with closing(db()) as con:
        con.execute("UPDATE research_runs SET result=? WHERE id=?", (json.dumps(result), run_id))
        con.commit()


def get_run(run_id: int):
    with closing(db()) as con:
        r = con.execute("SELECT * FROM research_runs WHERE id=?", (run_id,)).fetchone()
    return dict(r) if r else None


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]
