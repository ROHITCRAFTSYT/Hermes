"""Logging: one line per event, JSON in containers, readable text on the desktop console."""

import contextvars
import json
import logging
import sys
import time

request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_STD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def _extras(record: logging.LogRecord) -> dict:
    return {k: v for k, v in record.__dict__.items() if k not in _STD and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id.get(),
            **_extras(record),
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        rid = request_id.get()
        extra = " ".join(f"{k}={v!r}" for k, v in _extras(record).items())
        line = f"{ts} {record.levelname:<7} {record.name}{f' [{rid}]' if rid != '-' else ''}: " \
               f"{record.getMessage()}{'  ' + extra if extra else ''}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup(level: str = "INFO", fmt: str = "text") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
