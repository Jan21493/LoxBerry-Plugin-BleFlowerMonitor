"""Bounded per-poll logs and a live progress stream, independent of log level."""

from contextlib import contextmanager
from datetime import datetime
import logging
import json
from pathlib import Path
import subprocess
import time

MAX_LOG_BYTES = 2 * 1024 * 1024


class NativeLog(logging.Handler):
    def __init__(self, filename, level=None):
        super().__init__()
        native_level = ["7"] if level == logging.DEBUG else []
        helper = Path(__file__).resolve().parents[1] / "miflora-log.pl"
        self.process = subprocess.Popen(
            ["perl", str(helper), str(filename), filename.parent.name, *native_level],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
        )
        self.threshold = logging.DEBUG
        self.truncated = False
        self.size = 0
        self.setFormatter(logging.Formatter("%(message)s"))

    def send(self, method, message):
        self.process.stdin.write(json.dumps({"method": method, "message": message}) + "\n")
        self.process.stdin.flush()

    def start(self, message):
        self.send("LOGSTART", message)
        if self.process.stdout.readline().strip() != "READY":
            raise RuntimeError("Cannot start LoxBerry FlowerCare log session")

    def end(self, message):
        self.send("LOGEND", message)
        self.process.stdin.close()
        if self.process.wait(timeout=10) != 0:
            raise RuntimeError("Cannot finish LoxBerry FlowerCare log session")

    def emit(self, record):
        if getattr(record, "boundary", False):
            return
        if record.levelno < self.threshold or self.truncated:
            return
        message = self.format(record)
        size = len(message.encode("utf-8")) + 100
        if self.size + size > MAX_LOG_BYTES:
            self.send("WARN", "Log size limit reached; further details omitted")
            self.truncated = True
            return
        self.size += size
        method = ("ERR" if record.levelno >= logging.ERROR else "WARN" if record.levelno >= logging.WARNING
                  else "OK" if getattr(record, "success", False) else "INF" if record.levelno >= logging.INFO else "DEB")
        self.send(method, message)

    def close(self):
        if hasattr(self, "process"):
            if self.process.poll() is None:
                self.process.stdin.close()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    self.process.wait(timeout=5)
            self.process.stdout.close()
        super().close()


class ProgressLog(logging.FileHandler):
    def __init__(self, filename):
        super().__init__(filename, mode="w", encoding="utf-8")
        self.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))

    def emit(self, record):
        if (record.name == "root" and record.levelno >= logging.INFO
                and self.stream.tell() < MAX_LOG_BYTES):
            super().emit(record)


@contextmanager
def poll_session(datafile, logfile, level=None):
    directory = Path(logfile).parent
    directory.mkdir(parents=True, exist_ok=True)
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    filename = directory / f"{session_id}_xiaomi-miflora.log"
    root = logging.getLogger()
    old_level = root.level
    old_handlers = {handler: handler.level for handler in root.handlers}
    details = NativeLog(filename, level)
    try:
        details.start(f"FlowerCare query {session_id}")
    except (OSError, RuntimeError):
        details.close()
        raise
    progress = ProgressLog(datafile.parent / "poll-progress.log")
    root.addHandler(details)
    root.addHandler(progress)
    root.setLevel(logging.DEBUG)
    start = time.monotonic()
    outcome = {"status": "ERROR"}
    try:
        datafile.write_text("", encoding="utf-8")
        logging.info("START %s - Flower Monitor scan starting", session_id, extra={"boundary": True})
        yield outcome
    finally:
        end_message = "%s - %s - %.1f s" % (session_id, outcome["status"], time.monotonic() - start)
        logging.info("STOP %s", end_message, extra={"boundary": True})
        root.removeHandler(details)
        root.removeHandler(progress)
        progress.close()
        root.setLevel(old_level)
        for handler, handler_level in old_handlers.items():
            handler.setLevel(handler_level)
        try:
            if outcome["status"] == "ERROR":
                details.send("ERR", "FlowerCare query failed")
            details.end(end_message)
        finally:
            details.close()
