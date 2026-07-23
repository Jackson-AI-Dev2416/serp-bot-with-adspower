"""Thread-safe append to data/session.log (works from worker threads without Qt)."""

from __future__ import annotations

import threading
from datetime import datetime

from utils.app_paths import data_dir

_lock = threading.Lock()


def append_session_log(message: str) -> None:
  text = (message or "").strip()
  if not text:
    return
  try:
    path = data_dir() / "session.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
      with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {text}\n")
  except OSError:
    pass
