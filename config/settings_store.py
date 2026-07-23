import json
from pathlib import Path
from typing import Any, Optional

from utils.app_paths import data_dir


def settings_path() -> Path:
  return data_dir() / "settings.json"


def save_settings(data: dict[str, Any]) -> None:
  path = settings_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(
    json.dumps(data, ensure_ascii=False, indent=2),
    encoding="utf-8",
  )


def load_settings() -> Optional[dict[str, Any]]:
  path = settings_path()
  if not path.exists():
    return None
  return json.loads(path.read_text(encoding="utf-8"))
