import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from utils.app_paths import data_dir


class KeywordRotationStore:
  def __init__(self, path: str | Path | None = None):
    self.path = Path(path) if path is not None else data_dir() / "keyword_rotation.json"
    self._lock = threading.Lock()

  def allocate(
    self,
    target_domain: str,
    keywords: list[str],
    batch_size: int,
    *,
    pool_id: str = "",
    target_domains: list[str] | None = None,
  ) -> list[str]:
    cleaned = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not cleaned:
      return []

    batch = max(1, int(batch_size or 1))
    key = self._rotation_key(target_domain, target_domains=target_domains)
    if pool_id:
      key = f"{key}:{pool_id}"
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
  def _rotation_key(target_domain: str, *, target_domains: list[str] | None = None) -> str:
    raw = [d.strip() for d in (target_domains or []) if d and d.strip()]
    if not raw and (target_domain or "").strip():
      raw = [target_domain.strip()]
    normalized: list[str] = []
    seen: set[str] = set()
    for domain in raw:
      host = domain.lower()
      if host.startswith("http://"):
        host = host[7:]
      elif host.startswith("https://"):
        host = host[8:]
      host = host.split("/", 1)[0].strip().removeprefix("www.")
      if not host or host in seen:
        continue
      seen.add(host)
      normalized.append(host)
    if not normalized:
      return "default"
    return "|".join(sorted(normalized))

  @staticmethod
  def _keywords_signature(keywords: list[str]) -> str:
    normalized = "\n".join(kw.strip() for kw in keywords)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()
