from __future__ import annotations

import json
import logging
from datetime import datetime

from src.core.config import LOG_DIR, LOG_RETENTION_DAYS, SECURITY_BLACKLIST_FILE


# ── Logging ──────────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log_file = LOG_DIR / f"contrib_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s")
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    stream_handler = logging.StreamHandler()
    if hasattr(stream_handler.stream, "reconfigure"):
        stream_handler.stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
    file_handler.setFormatter(fmt)
    stream_handler.setFormatter(fmt)
    stream_handler.setLevel(logging.ERROR)  # warnings and below stay in file only
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(file_handler)
        root.addHandler(stream_handler)
    else:
        for handler in root.handlers:
            try:
                handler.flush()
            except Exception:
                pass
            try:
                handler.close()
            except Exception:
                pass
        root.handlers.clear()
        root.addHandler(file_handler)
        root.addHandler(stream_handler)
    return logging.getLogger(__name__)


def cleanup_old_logs() -> None:
    try:
        cutoff = datetime.now().timestamp() - (LOG_RETENTION_DAYS * 86400)
        for logfile in LOG_DIR.glob("*.log"):
            if logfile.stat().st_mtime < cutoff:
                logfile.unlink()
    except Exception:
        pass


# ── Repo blacklist ───────────────────────────────────────────
def _load_blacklist() -> dict:
    if SECURITY_BLACKLIST_FILE.exists():
        try:
            return json.loads(SECURITY_BLACKLIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def blacklist_source_repo(full_name: str, reason: str) -> None:
    data = _load_blacklist()
    data[full_name.lower()] = {
        "reason": reason,
        "recorded_at": datetime.now().isoformat(),
    }
    SECURITY_BLACKLIST_FILE.parent.mkdir(exist_ok=True)
    SECURITY_BLACKLIST_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_security_blacklisted_sources() -> set[str]:
    return set(_load_blacklist().keys())
