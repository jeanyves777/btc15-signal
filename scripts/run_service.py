"""Run the Windows service with a single-instance lock and rotating local logs."""

import logging
import msvcrt
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from btc15_signal.config import Settings
from btc15_signal.main import run


class LogStream:
    def __init__(self, logger: logging.Logger, token: str) -> None:
        self.logger = logger
        self.token = token

    def write(self, text: str) -> int:
        clean = text.replace(self.token, "[REDACTED]") if self.token else text
        for line in clean.splitlines():
            if line.strip():
                self.logger.info(line)
        return len(text)

    def flush(self) -> None:
        pass


def main() -> None:
    project = Path(__file__).resolve().parents[1]
    os.chdir(project)
    runtime = project / "runtime"
    runtime.mkdir(exist_ok=True)
    with (runtime / "service.lock").open("a+b") as lock:
        try:
            # Size via seek, never read(). msvcrt locks byte 0, and on Windows
            # READING a byte another process has locked raises PermissionError -
            # so the old `lock.read(1)` crashed the second instance with exit 1
            # instead of returning cleanly. The watchdog counted each of those
            # as a failed restart, burned its budget on a perfectly healthy
            # service, and gave up while a position was open.
            if lock.seek(0, os.SEEK_END) == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return  # another instance holds the lock; nothing to do
        settings = Settings()
        logger = logging.getLogger("btc15.service")
        logger.setLevel(logging.INFO)
        handler = RotatingFileHandler(
            runtime / "service.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        sys.stdout = sys.stderr = LogStream(logger, settings.telegram_bot_token)
        (runtime / "service.pid").write_text(str(os.getpid()))
        try:
            run()
        except Exception as exc:
            # Exception messages may include request URLs with credentials.
            logger.error("Service stopped: %s", type(exc).__name__)
            raise SystemExit(1) from None


if __name__ == "__main__":
    main()
