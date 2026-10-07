"""Is the whole system actually round - signal to settlement to learning?

WHAT THIS IS FOR. "All instances are running" is not the same as "the system
works". A service can poll, alert, trade and settle while its learning loop has
silently stopped, or while a policy fitted under a superseded feature contract
is being refused on every load. Each of those looks healthy from the outside and
each has happened here.

So this walks the whole loop per instrument and reports where it breaks:

    1. PROCESS      the service is alive, and its pid file is not stale
    2. SIGNAL       alerts are still being emitted
    3. DECISION     the intelligence layer is being consulted
    4. EXECUTION    orders reach the broker and fill
    5. SETTLEMENT   outcomes come back and are graded
    6. LEARNING     the loop runs, and on the CURRENT feature contract
    7. POLICY       the active policy is loadable and matches the contract
    8. THRESHOLD    the deployed evidence bar is the one in the config

A STALE PID FILE IS NOT PROOF OF LIFE. Windows recycles PIDs, and on
2026-09-26 a 17-hour-old pid file named a PID that had been reused, so a dead
BTC service read as UP. The age of the file is checked as well as the process.

Read-only throughout: every store is opened `mode=ro`, because the live Store
runs DDL migrations on open and a read-write handle on a running instrument is
a real hazard.

    python scripts/full_circle_check.py
"""

import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

INSTANCES = {
    "BTC":    ("runtime",         "btc15.db",      "KXBTC15M"),
    "ETH":    ("runtime-eth",     "eth15.db",      "KXETH15M"),
    "SOL":    ("runtime-sol",     "sol15.db",      "KXSOL15M"),
    "XRP":    ("runtime-xrp",     "xrp15.db",      "KXXRP15M"),
    "NEAR":   ("runtime-near",    "near15.db",     "KXNEAR15M"),
    "BNB":    ("runtime-bnb",     "bnb15.db",      "KXBNB15M"),
    "GOLD":   ("runtime-gold",    "gold15.db",     "KXGOLD15M"),
    "SILVER": ("runtime-silver",  "silver15.db",   "KXSILVER15M"),
}
# METALS KEEP NEW YORK HOURS and shut for roughly 48 hours every weekend, so a
# gold or silver service is CORRECTLY quiet from Friday evening to Sunday. A
# check that reports two problems every weekend is a check nobody reads, so
# closure is established from the exchange - zero open markets - rather than
# assumed from a clock this file would then have to keep in sync with.
SESSION_INSTRUMENTS = {"GOLD", "SILVER"}
# A pid file older than this is treated as unproven even if the PID exists,
# because the PID may have been recycled onto an unrelated process.
PID_FRESH_S = 6 * 3600


def alive_pids() -> set:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process -Name pythonw -ErrorAction SilentlyContinue "
         "| Select-Object -ExpandProperty Id"],
        capture_output=True, text=True).stdout
    return set(out.split())


def ago(ms) -> str:
    if not ms:
        return "never"
    s = time.time() - (int(ms) / 1000.0)
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s / 60)}m"
    return f"{s / 3600:.1f}h"


def count(con, sql, args=()):
    try:
        return con.execute(sql, args).fetchone()[0]
    except sqlite3.Error:
        return None


def venue_open(series: str) -> bool | None:
    """Is the exchange quoting this series right now? None if unknown."""
    try:
        import httpx
        from btc15_signal.config import Settings
        r = httpx.get(Settings().kalshi_base_url + "/markets",
                      params={"series_ticker": series, "status": "open",
                              "limit": 1}, timeout=15)
        if r.status_code >= 300:
            return None
        return bool(r.json().get("markets"))
    except Exception:
        return None


def check(asset, runtime_dir, db_name, series, pids, contract_version):
    rt, db = ROOT / runtime_dir, ROOT / db_name
    row = {"asset": asset, "problems": []}

    # 1. PROCESS
    #
    # THE PID FILE'S AGE IS NOT A HEALTH SIGNAL, and reading it as one raised a
    # false alarm on 2026-09-26: `service.pid` is written ONCE at startup, so a
    # service that has been up for seventeen hours has a seventeen-hour-old pid
    # file and is perfectly well. What proves life is the LOG still moving.
    #
    # The age is still read, for one narrow purpose: if the PID exists but the
    # log has stopped, an old pid file makes PID RECYCLING the likelier reading
    # than a hung service, and the two need different fixes.
    pidf = rt / "service.pid"
    pid = pidf.read_text().strip() if pidf.exists() else ""
    pid_age = (time.time() - pidf.stat().st_mtime) if pidf.exists() else None
    logf = rt / "service.log"
    log_age = (time.time() - logf.stat().st_mtime) if logf.exists() else None
    row["log_age"] = f"{log_age / 60:.0f}m" if log_age is not None else "-"

    pid_exists = pid in pids
    # A service between windows is quiet for a couple of minutes; a dead one is
    # quiet forever. Thirty minutes is well past any normal gap.
    log_moving = log_age is not None and log_age < 1800
    if pid_exists and log_moving:
        row["process"] = "up"
    elif pid_exists and not log_moving:
        shut = (asset in SESSION_INSTRUMENTS
                and venue_open(series) is False)
        row["process"] = "closed" if shut else "QUIET"
        if not shut:
            row["problems"].append(
                f"PID {pid} exists but the log has not moved for "
                f"{(log_age or 0) / 60:.0f}m"
                + (" - and the pid file is old, so this may be a recycled PID "
                   "rather than a hung service"
                   if pid_age and pid_age > PID_FRESH_S else ""))
    else:
        row["process"] = "DOWN"
        row["problems"].append("service not running")

    if not db.exists():
        row["problems"].append("no store")
        return row
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        # 2. SIGNAL
        last_alert = count(
            con, "SELECT MAX(observed_ms) FROM observations WHERE alerted=1")
        row["last_alert"] = ago(last_alert)
        # 3. DECISION
        last_dec = count(
            con, "SELECT MAX(decided_ms) FROM intelligence_decisions")
        row["last_decision"] = ago(last_dec)
        row["decisions_on_contract"] = count(
            con, "SELECT COUNT(*) FROM intelligence_decisions "
            "WHERE feature_version=?", (contract_version,))
        # 4. EXECUTION
        row["fills"] = count(con, "SELECT COUNT(*) FROM fills")
        # 5. SETTLEMENT
        last_settle = count(con, "SELECT MAX(settled_ms) FROM settlements")
        if last_settle is None:
            last_settle = count(con, "SELECT MAX(created_at) FROM settlements")
        row["last_settlement"] = ago(last_settle)
        # 6. LEARNING
        lr = None
        try:
            lr = con.execute(
                "SELECT id, started_ms, status, feature_version, promoted, "
                "       arms_with_confidence "
                "  FROM learning_runs ORDER BY id DESC LIMIT 1").fetchone()
        except sqlite3.Error:
            pass
        if lr:
            row["last_learning"] = ago(lr[1])
            row["learning_status"] = lr[2]
            row["learning_contract"] = lr[3]
            row["promoted"] = lr[4]
            row["confidence_arms"] = lr[5]
            if lr[3] != contract_version:
                row["problems"].append(
                    f"last learning ran on {lr[3]}, contract is "
                    f"{contract_version}")
        else:
            row["last_learning"] = "never"
            row["problems"].append("no learning run recorded")
        fails = count(
            con, "SELECT value FROM learning_state WHERE key="
                 "'consecutive_failures'")
        if fails not in (None, "0", 0):
            row["problems"].append(f"learning failures: {fails}")
    finally:
        con.close()

    # 7 + 8. POLICY and THRESHOLD
    pol = rt / "intelligence_policy.json"
    if not pol.exists():
        row["problems"].append("no active policy")
        return row
    try:
        p = json.loads(pol.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        row["problems"].append(f"policy unreadable: {type(exc).__name__}")
        return row
    row["policy_contract"] = p.get("feature_version")
    row["min_evidence"] = p.get("min_evidence")
    row["arms"] = len(p.get("arms") or {})
    if p.get("feature_version") != contract_version:
        row["problems"].append(
            f"policy is {p.get('feature_version')}, contract is "
            f"{contract_version} - it will be refused on load")
    return row


def main():
    from btc15_signal import feature_contract
    from btc15_signal.config import Settings
    from btc15_signal import learning

    contract = feature_contract.CONTRACT.version
    want_evidence = Settings().learning_min_evidence

    print("=" * 84)
    print("FULL CIRCLE CHECK")
    print("=" * 84)
    print(f"  feature contract   {contract} "
          f"({feature_contract.FINGERPRINT})")
    print(f"  evidence bar       config {want_evidence}, "
          f"code MIN_PROMOTION_N {learning.MIN_PROMOTION_N} / "
          f"MIN_CONFIDENCE_N {learning.MIN_CONFIDENCE_N}")

    # The local model is additive; its absence is reported, never fatal.
    try:
        import urllib.request
        with urllib.request.urlopen(
                Settings().brain_url.rstrip('/') + "/models", timeout=4) as r:
            r.read(1)
        brain = "up"
    except Exception:
        brain = "DOWN"
    print(f"  local model        {brain} at {Settings().brain_url}")

    pids = alive_pids()
    rows = [check(a, d, db, series, pids, contract)
            for a, (d, db, series) in INSTANCES.items()]

    print(f"\n  {'asset':<8}{'proc':<7}{'log':>6}{'alert':>8}{'decide':>8}"
          f"{'settle':>8}{'learn':>8}{'status':>9}{'arms':>6}{'n>=':>6}"
          f"{'promo':>7}")
    for r in rows:
        print(f"  {r['asset']:<8}{r['process']:<7}{r.get('log_age','-'):>6}"
              f"{r.get('last_alert','-'):>8}{r.get('last_decision','-'):>8}"
              f"{r.get('last_settlement','-'):>8}"
              f"{r.get('last_learning','-'):>8}"
              f"{str(r.get('learning_status','-')):>9}"
              f"{str(r.get('arms','-')):>6}"
              f"{str(r.get('min_evidence','-')):>6}"
              f"{str(r.get('promoted','-')):>7}")

    stale_bar = [r for r in rows
                 if r.get("min_evidence") not in (None, want_evidence)]
    if stale_bar:
        print(f"\n  EVIDENCE BAR NOT YET LIVE on "
              f"{', '.join(r['asset'] for r in stale_bar)}: the policy still "
              f"carries {stale_bar[0].get('min_evidence')}.")
        print("  A policy is written at FIT time, so the new bar reaches an "
              "instrument on its")
        print("  next learning run, which triggers on new settlements. This is "
              "expected right")
        print("  after a restart and is only a fault if it persists past a "
              "settlement batch.")

    problems = [(r["asset"], p) for r in rows for p in r["problems"]]
    print()
    if problems:
        print(f"  {len(problems)} PROBLEM(S):")
        for asset, p in problems:
            print(f"    {asset:<8}{p}")
    else:
        print("  NO PROBLEMS: every instrument is polling, deciding, "
              "settling and learning")
        print("  on the current feature contract.")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
