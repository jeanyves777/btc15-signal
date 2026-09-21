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
* **It never restarts silently forever.** A service that dies repeatedly is
  broken in a way a restart will not fix, so after `MAX_RESTARTS_PER_HOUR` it
  stops trying and says so, rather than hiding a fault behind a loop.
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


CHECK_SECONDS = 60
MAX_RESTARTS_PER_HOUR = 6
# Below this, the service died on startup rather than after running - almost
# always a config or import error, which restarting will not cure.
INSTANT_DEATH_SECONDS = 20


def notify(text: str) -> None:
    """Best-effort Telegram message. A watchdog must never die of its own alert."""
    sys.path.insert(0, str(PROJECT / "src"))
    try:
        from btc15_signal.config import Settings

        settings = Settings()
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            return
        payload = urllib.parse.urlencode(
            {"chat_id": settings.telegram_chat_id, "text": text, "parse_mode": "HTML"}
        ).encode()
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        with urllib.request.urlopen(url, data=payload, timeout=10):
            pass
    except Exception:  # noqa: BLE001 - never let notification failure stop the watch
        pass


def main() -> None:
    restarts: deque[float] = deque()
    print(f"watchdog: supervising {PROJECT}", flush=True)
    while True:
        started = time.time()
        # If a service already holds the lock this returns at once and nothing
        # was restarted; only a genuine gap produces a supervised run.
        proc = subprocess.run(
            [service_python(), "scripts/run_service.py"],
            cwd=PROJECT,
            capture_output=True,
            text=True,
        )
        ran_for = time.time() - started

        if ran_for < 5 and proc.returncode == 0:
            time.sleep(CHECK_SECONDS)  # healthy: the lock was held
            continue

        now = time.time()
        restarts.append(now)
        while restarts and now - restarts[0] > 3600:
            restarts.popleft()

        how = "died on startup" if ran_for < INSTANT_DEATH_SECONDS else "stopped"
        print(
            f"watchdog: service {how} after {ran_for:.0f}s (exit {proc.returncode}); "
            f"{len(restarts)} restart(s) this hour",
            flush=True,
        )

        if len(restarts) > MAX_RESTARTS_PER_HOUR:
            notify(
                "\U0001f6d1 <b>WATCHDOG STOPPED</b>\n"
                f"<i>The trading service {how} {len(restarts)} times in an hour. "
                "Not restarting again - this needs a look. No further trades will "
                "be placed.</i>"
            )
            print("watchdog: too many restarts; giving up", flush=True)
            return

        notify(
            "♻️ <b>SERVICE RESTARTED</b>\n"
            f"<i>It {how} after {ran_for / 60:.0f} min and was brought back up. "
            "Trading limits carry over - they live in the database, not in memory.</i>"
        )
        time.sleep(5)


if __name__ == "__main__":
    main()
