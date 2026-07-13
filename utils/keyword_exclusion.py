import json
import threading
from datetime import datetime, timezone
from pathlib import Path


KEYWORD_EXCLUSION_ENABLED = False


class KeywordExclusionStore:
  def __init__(self, path: str | Path = "data/keyword_excluded.json"):
    self.path = Path(path)
    self._lock = threading.Lock()

  def is_excluded(self, target_domain: str, keyword: str) -> bool:
    if not KEYWORD_EXCLUSION_ENABLED:
      return False
    key = self._domain_key(target_domain)
    normalized = self._normalize_keyword(keyword)
    if not normalized:
      return False
    with self._lock:
      data = self._load_state()
      excluded = data.get(key, {}).get("excluded_keywords", [])
      return normalized in set(self._normalize_keyword(item) for item in excluded if item)

  def clear_domain(self, target_domain: str) -> int:
    key = self._domain_key(target_domain)
    with self._lock:
      data = self._load_state()
      entry = data.pop(key, None)
      if entry is None:
        return 0
      self._save_state(data)
      excluded = entry.get("excluded_keywords", []) if isinstance(entry, dict) else []
      return len(excluded)

  def mark_excluded(self, target_domain: str, keyword: str) -> bool:
    if not KEYWORD_EXCLUSION_ENABLED:
      return False
    key = self._domain_key(target_domain)
    normalized = self._normalize_keyword(keyword)
    if not normalized:
      return False
    with self._lock:
      data = self._load_state()
      entry = data.get(key, {})
      current = [self._normalize_keyword(item) for item in entry.get("excluded_keywords", []) if item]
      if normalized in current:
        return False
      current.append(normalized)
      data[key] = {
        "excluded_keywords": sorted(set(current)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
      }
      self._save_state(data)
    return True

  @staticmethod
  def _normalize_keyword(keyword: str) -> str:
    return (keyword or "").strip().lower()

  @staticmethod
  def _domain_key(target_domain: str) -> str:
    host = (target_domain or "").strip().lower()
    if host.startswith("http://"):
      host = host[7:]
    elif host.startswith("https://"):
      host = host[8:]
    host = host.split("/", 1)[0].strip()
    host = host.removeprefix("www.")
    return host or "default"

  def _load_state(self) -> dict:
    if not self.path.exists():
      return {}
    try:
      raw = self.path.read_text(encoding="utf-8")
      parsed = json.loads(raw)
      return parsed if isinstance(parsed, dict) else {}
    except Exception:
      return {}

  def _save_state(self, data: dict) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
