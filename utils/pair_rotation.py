import hashlib
import json
import random
import threading
from datetime import datetime, timezone
from pathlib import Path

from utils.app_paths import data_dir


class PairRotationStore:
  """Round-robin allocator for (keyword, target domain) pairs."""

  def __init__(self, path: str | Path | None = None):
    self.path = Path(path) if path is not None else data_dir() / "pair_rotation.json"
    self._lock = threading.Lock()

  @staticmethod
  def pair_key(keyword: str, domain: str) -> tuple[str, str]:
    return (keyword.strip(), PairRotationStore.normalize_domain_key(domain))

  @staticmethod
  def _normalize_skip_pairs(skip_pairs: set[tuple[str, str]] | None) -> set[tuple[str, str]]:
    if not skip_pairs:
      return set()
    return {PairRotationStore.pair_key(kw, dom) for kw, dom in skip_pairs}

  def allocate_pair(
    self,
    keywords: list[str],
    domains: list[str],
    *,
    skip_pairs: set[tuple[str, str]] | None = None,
  ) -> tuple[str, str]:
    cleaned_kw = [kw.strip() for kw in keywords if kw and kw.strip()]
    cleaned_dom = [d.strip() for d in domains if d and d.strip()]
    if not cleaned_kw or not cleaned_dom:
      return ("", "")

    signature = self._signature(cleaned_kw, cleaned_dom)
    total_pairs = len(cleaned_kw) * len(cleaned_dom)
    skip_normalized = self._normalize_skip_pairs(skip_pairs)

    with self._lock:
      state = self._load_state()
      entry = state.get("global", {})
      cursor = int(entry.get("cursor", 0) or 0)
      saved_signature = str(entry.get("signature", "") or "")
      if saved_signature != signature:
        cursor = 0

      chosen_keyword = ""
      chosen_domain = ""
      next_cursor = cursor
      for _ in range(total_pairs):
        flat_index = next_cursor % total_pairs
        kw_index = flat_index % len(cleaned_kw)
        dom_index = flat_index // len(cleaned_kw)
        keyword = cleaned_kw[kw_index]
        domain = cleaned_dom[dom_index]
        next_cursor += 1
        if self.pair_key(keyword, domain) in skip_normalized:
          continue
        chosen_keyword = keyword
        chosen_domain = domain
        break

      if not chosen_keyword or not chosen_domain:
        return ("", "")

      state["global"] = {
        "cursor": next_cursor % total_pairs,
        "signature": signature,
        "updated_at": datetime.now(timezone.utc).isoformat(),
      }
      self._save_state(state)

    return chosen_keyword, chosen_domain

  @staticmethod
  def pick_alternate_keyword(keywords: list[str], current: str) -> str:
    cleaned = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not cleaned:
      return current
    choices = [kw for kw in cleaned if kw != current]
    return random.choice(choices) if choices else random.choice(cleaned)

  @staticmethod
  def pick_alternate_domain(domains: list[str], current: str) -> str:
    cleaned = [d.strip() for d in domains if d and d.strip()]
    if not cleaned:
      return current
    choices = [d for d in cleaned if d != current]
    return random.choice(choices) if choices else random.choice(cleaned)

  @staticmethod
  def normalize_domain_key(domain: str) -> str:
    return (domain or "").strip().lower().removeprefix("www.")

  @staticmethod
  def pick_next_untried_domain(domains: list[str], tried: set[str]) -> str | None:
    """Return the next configured domain not yet tried for the current keyword."""
    cleaned = [d.strip() for d in domains if d and d.strip()]
    if not cleaned:
      return None
    tried_keys = {PairRotationStore.normalize_domain_key(d) for d in tried}
    for domain in cleaned:
      if PairRotationStore.normalize_domain_key(domain) not in tried_keys:
        return domain
    return None

  def _load_state(self) -> dict:
    if not self.path.exists():
      return {}
    try:
      parsed = json.loads(self.path.read_text(encoding="utf-8"))
      return parsed if isinstance(parsed, dict) else {}
    except Exception:
      return {}

  def _save_state(self, data: dict) -> None:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

  @staticmethod
  def _signature(keywords: list[str], domains: list[str]) -> str:
    payload = "\n".join(keywords) + "\n---\n" + "\n".join(domains)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
