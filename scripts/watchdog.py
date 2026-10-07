"""Keep the trading service running overnight, and say so when it did not.

The service is unattended for hours at a time. Without this, a crash at 2am is
indistinguishable from a quiet night: no trades, no alert, and nothing to find
in the morning but a truncated log.

The design leans entirely on `run_service.py`'s existing single-instance lock,
which makes the restart logic trivial and safe: launching the service when one
is already running is a no-op that returns immediately, so this loop does not
need to know whether the service is alive. It just tries, and either supervises
a fresh one or falls straight through to the next check.

Two things it deliberately does NOT do:

* **It never resets trading state.** Every limit that matters - the daily loss
  floor, the trade counts, the one-position rule - lives in the database and is
  read fresh on each decision, so a restart cannot hand the strategy a clean
  slate it has not earned. A crash loop is therefore safe by construction: it
  can waste restarts, but it cannot trade past a breached limit.
* **It never restarts silently.** A service that dies repeatedly is broken in
  a way a quick restart will not fix, so after `MAX_RESTARTS_PER_HOUR` it says
  so and slows to one attempt every `BACKOFF_SECONDS`.

It no longer gives up for good (operator, 2026-09-30: "every system collecting
data in the shadow still continue collecting their data"). A stopped watchdog
stopped every shadow recorder on the instance until someone restarted it by
hand, and a fault that clears - a locked file, a network outage at startup -
never got the chance to recover. The service also no longer exits on a bug in
its poll (main.report_cycle_error), so repeated exits now mean it cannot start.
"""

import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def service_python() -> str:
    """The interpreter that can actually import btc15_signal.

    NOT `sys.executable`. On Windows the venv's pythonw.exe is a launcher stub
    that re-execs the base interpreter, so a watchdog started through it reports
    the BASE interpreter as sys.executable - and the base interpreter has no
    btc15_signal installed. Every restart then died on import in about a second,
    which is exactly how the watchdog burned through its restart budget and gave
    up while the service was down and a position was open.
    """
    for name in ("pythonw.exe", "python.exe", "python"):
        candidate = PROJECT / ".venv" / ("Scripts" if os.name == "nt" else "bin") / name
        if candidate.exists():
            return str(candidate)
    return sys.executable


# THIS INSTANCE: its runtime folder (lock, pid, log) and its name in messages.
INSTANCE = (os.environ.get("BTC15_INSTANCE") or "").strip()
RUNTIME = PROJECT / (f"runtime-{INSTANCE}" if INSTANCE else "runtime")
LOG = RUNTIME / "watchdog.log"
NAME = (INSTANCE or "btc").upper()

CHECK_SECONDS = 60
# A check that fails while the service still holds its lock is said once this
# many in a row happen - one alone is noise, not an outage.
PROBE_FAILURES_TO_SAY = 10
MAX_RESTARTS_PER_HOUR = 6
# Past the limit: one attempt every 15 minutes, never a permanent stop.
BACKOFF_SECONDS = 900
# A run this long was healthy: the count of quick failures starts over.
HEALTHY_RUN_SECONDS = 600
# Below this, the service died on startup rather than after running - almost
# always a config or import error, which restarting will not cure.
INSTANT_DEATH_SECONDS = 20


def log(line: str) -> None:
    """Printed AND kept: under pythonw nothing printed is ever seen, so the
    10:50 check failure of 2026-09-30 left no trace. Never raises."""
    print(line, flush=True)
    try:
        RUNTIME.mkdir(exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:  # noqa: BLE001
        pass


def lock_held() -> bool:
    """Does a running service hold this instance's lock right now? The same
    byte run_service.py locks. Never raises; unsure reads as NOT held, which
    leads to the ordinary restart path - the safe side."""
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        with (RUNTIME / "service.lock").open("a+b") as fh:
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return False
    except Exception:  # noqa: BLE001
        return False


def notify(text: str) -> bool:
    """Best-effort Telegram message. A watchdog must never die of its own alert.
    True only when Telegram accepted it."""
    sys.path.insert(0, str(PROJECT / "src"))
    try:
        from btc15_signal.config import Settings

        settings = Settings()
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return False
        payload = urllib.parse.urlencode(
            {"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        with urllib.request.urlopen(url, data=payload, timeout=10):
            return True
    except Exception:  # noqa: BLE001 - never let notification failure stop the watch
        return False


def main() -> None:
    restarts: deque[float] = deque()
    backing_off = False
    told_failing = False
    probe_failures = 0
    log(f"watchdog: supervising {PROJECT} ({NAME})")
    while True:
        started = time.time()
        # If a service already holds the lock this returns at once and nothing
        # was restarted; only a genuine gap produces a supervised run.
        try:
            proc = subprocess.run(
                [service_python(), "scripts/run_service.py"],
                cwd=PROJECT,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            # The process could not even be created (memory, a venv being
            # replaced). That must not end the watchdog - it used to.
            log(f"watchdog: could not launch the service ({exc!r})")
            time.sleep(CHECK_SECONDS)
            continue
        ran_for = time.time() - started
        output = ((getattr(proc, "stderr", "") or "") + (getattr(proc, "stdout", "") or ""))
        output = " | ".join(output.strip().splitlines()[-6:])[-600:]

        if ran_for < INSTANT_DEATH_SECONDS and proc.returncode != 0 and lock_held():
            # THE CHECK FAILED, THE SERVICE DID NOT. Every minute the check
            # runs run_service.py, which imports the whole application before
            # it finds the lock held; if that fails, the running service is
            # untouched. It used to be reported as "SERVICE RESTARTED ... died
            # on startup" (2026-09-30 10:50) although nothing had restarted.
            probe_failures += 1
            log(f"watchdog: check failed (exit {proc.returncode}) while the service "
                f"holds its lock - not a restart ({probe_failures} in a row): {output}")
            if probe_failures == PROBE_FAILURES_TO_SAY:
                notify(f"\u26a0\ufe0f <b>WATCHDOG CHECK FAILING \u00b7 {NAME}</b>\n"
                       f"<i>{probe_failures} checks in a row failed; the service is still "
                       "running. If it stops, it may not come back - this needs a look.</i>")
            time.sleep(CHECK_SECONDS)
            continue
        probe_failures = 0

        if ran_for < 5 and proc.returncode == 0:
            # Healthy: the lock was held - by the service this watchdog
            # started, or one started by hand. Either way a later failure is a
            # new episode, announced afresh (review 2026-09-30).
            restarts.clear()
            backing_off = False
            time.sleep(CHECK_SECONDS)
            continue

        now = time.time()
        if ran_for >= HEALTHY_RUN_SECONDS:
            restarts.clear()            # it ran properly; count afresh
            backing_off = False
        restarts.append(now)
        while restarts and now - restarts[0] > 3600:
            restarts.popleft()

        how = "died on startup" if ran_for < INSTANT_DEATH_SECONDS else "stopped"
        log(f"watchdog: service {how} after {ran_for:.0f}s (exit {proc.returncode}); "
            f"{len(restarts)} restart(s) this hour: {output}")

        if len(restarts) > MAX_RESTARTS_PER_HOUR and not backing_off:
            backing_off = True
            told_failing = False
        if backing_off:
            if not told_failing:
                # Retried on every attempt until Telegram takes it: at logon the
                # network may not be up yet, and one lost message here would
                # leave a dead instance silent.
                told_failing = notify(
                    f"\U0001f6d1 <b>SERVICE KEEPS FAILING \u00b7 {NAME}</b>\n"
                    f"<i>The service {how} {len(restarts)} times in an hour. "
                    "Retrying every 15 minutes so trading and recording come back "
                    "as soon as they can - this needs a look. After a fix, start it "
                    "by hand (run_X.ps1) rather than wait.</i>"
                )
            log(f"watchdog: failing repeatedly; next attempt in {BACKOFF_SECONDS // 60} min")
            time.sleep(BACKOFF_SECONDS)
            continue

        notify(
            f"♻️ <b>SERVICE RESTARTED \u00b7 {NAME}</b>\n"
            f"<i>It {how} after {ran_for / 60:.0f} min and was brought back up. "
            "Trading limits carry over - they live in the database, not in memory.</i>"
        )
        time.sleep(5)


if __name__ == "__main__":
    main()
