import json
from pathlib import Path
from typing import Any, Optional

SETTINGS_PATH = Path("data/settings.json")


def save_settings(data: dict[str, Any]) -> None:
  SETTINGS_PATH.parent.mkdir(exist_ok=True)
  SETTINGS_PATH.write_text(
    json.dumps(data, ensure_ascii=False, indent=2),
    encoding="utf-8",
  )


def load_settings() -> Optional[dict[str, Any]]:
  if not SETTINGS_PATH.exists():
    return None
  return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
