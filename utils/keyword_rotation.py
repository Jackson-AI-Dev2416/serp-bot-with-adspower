import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class KeywordRotationStore:
  def __init__(self, path: str | Path = "data/keyword_rotation.json"):
    self.path = Path(path)
    self._lock = threading.Lock()

  def allocate(self, target_domain: str, keywords: list[str], batch_size: int) -> list[str]:
    cleaned = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not cleaned:
      return []

    batch = max(1, int(batch_size or 1))
    key = self._rotation_key(target_domain)
    signature = self._keywords_signature(cleaned)

    with self._lock:
      data = self._load_state()
      entry = data.get(key, {})
      cursor = int(entry.get("cursor", 0) or 0)
      saved_signature = str(entry.get("keywords_signature", "") or "")

      if saved_signature != signature:
        cursor = 0

      total = len(cleaned)
      picked: list[str] = []
      for offset in range(batch):
        idx = (cursor + offset) % total
        keyword = cleaned[idx]
        if keyword in picked:
          continue
        picked.append(keyword)

      if len(picked) < batch:
        for keyword in cleaned:
          if len(picked) >= batch:
            break
          if keyword not in picked:
            picked.append(keyword)

      data[key] = {
        "cursor": (cursor + len(picked)) % total,
        "keywords_signature": signature,
        "updated_at": datetime.now(timezone.utc).isoformat(),
      }
      self._save_state(data)

    return picked

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

  @staticmethod
  def _rotation_key(target_domain: str) -> str:
    host = (target_domain or "").strip().lower()
    if host.startswith("http://"):
      host = host[7:]
    elif host.startswith("https://"):
      host = host[8:]
    host = host.split("/", 1)[0].strip()
    host = host.removeprefix("www.")
    return host or "default"

  @staticmethod
  def _keywords_signature(keywords: list[str]) -> str:
    normalized = "\n".join(kw.strip() for kw in keywords)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()
