import csv
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from utils.app_paths import data_dir as resolve_data_dir

_CSV_ENCODING = "utf-8-sig"
_HEADERS = ("keyword", "site", "device", "page", "rank", "updated_at")


def _normalize_site(site: str) -> str:
  cleaned = (site or "").strip().lower()
  if cleaned.startswith("http://"):
    cleaned = cleaned[7:]
  elif cleaned.startswith("https://"):
    cleaned = cleaned[8:]
  return cleaned.split("/", 1)[0].strip().removeprefix("www.")


def _device_bucket(device: str = "", *, mobile: Optional[bool] = None) -> str:
  if mobile is True:
    return "mobile"
  if mobile is False:
    return "windows"
  lowered = (device or "").strip().lower()
  if "android" in lowered or lowered == "mobile":
    return "mobile"
  if "windows" in lowered or lowered == "win":
    return "windows"
  return lowered or "windows"


class SerpResultStore:
  """Persistent keyword+site+device rank history in data/result.csv."""

  def __init__(self, filepath: str | Path | None = None):
    self.filepath = Path(filepath) if filepath is not None else resolve_data_dir() / "result.csv"
    self.filepath.parent.mkdir(parents=True, exist_ok=True)
    self._lock = threading.Lock()
    with self._lock:
      if not self.filepath.exists():
        with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as handle:
          csv.writer(handle).writerow(_HEADERS)

  def upsert(
    self,
    *,
    keyword: str,
    site: str,
    device: str,
    page: int,
    rank: int,
    mobile: Optional[bool] = None,
  ) -> None:
    cleaned_kw = (keyword or "").strip()
    cleaned_site = _normalize_site(site)
    if not cleaned_kw or not cleaned_site:
      return
    bucket = _device_bucket(device, mobile=mobile)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = {
      "keyword": cleaned_kw,
      "site": cleaned_site,
      "device": bucket,
      "page": int(page),
      "rank": int(rank),
      "updated_at": stamp,
    }
    key = (cleaned_kw, cleaned_site, bucket)
    with self._lock:
      try:
        records = self._read_unlocked()
        records[key] = row
        self._write_unlocked(records)
      except PermissionError:
        pass

  def resolve_mixed_profile_os(self, keyword: str, site: str) -> str:
    """Pick AdsPower OS for mixed mode from result.csv click history.

    - Mobile history page 1 or 2: Android.
    - Windows history page 1 and no mobile history: Android.
    - Everything else: Windows (no history, mobile 3+, windows 2+ only).
    """
    mobile_page = self.get_page_hint(keyword, site, mobile=True)
    if mobile_page in (1, 2):
      return "Android"
    windows_page = self.get_page_hint(keyword, site, mobile=False)
    if mobile_page is None and windows_page == 1:
      return "Android"
    return "Windows"

  def mixed_profile_os_reason(self, keyword: str, site: str) -> str:
    mobile_page = self.get_page_hint(keyword, site, mobile=True)
    if mobile_page == 1:
      return "mobile history page 1"
    if mobile_page == 2:
      return "mobile history page 2"
    if mobile_page is not None and mobile_page >= 3:
      return f"mobile history page {mobile_page}"
    win_page = self.get_page_hint(keyword, site, mobile=False)
    if win_page == 1 and mobile_page is None:
      return "windows history page 1 (no mobile — try android)"
    if win_page is not None:
      return f"windows history page {win_page}"
    return "no history (windows)"

  def get_page_hint(
    self,
    keyword: str,
    site: str,
    *,
    mobile: bool = False,
  ) -> Optional[int]:
    cleaned_kw = (keyword or "").strip()
    cleaned_site = _normalize_site(site)
    if not cleaned_kw or not cleaned_site:
      return None
    bucket = _device_bucket(mobile=mobile)
    with self._lock:
      records = self._read_unlocked()
    row = records.get((cleaned_kw, cleaned_site, bucket))
    if not row:
      return None
    try:
      page = int(row.get("page", 0) or 0)
    except (TypeError, ValueError):
      return None
    return page if page > 0 else None

  def _read_unlocked(self) -> dict[tuple[str, str, str], dict]:
    records: dict[tuple[str, str, str], dict] = {}
    if not self.filepath.exists():
      return records
    with self.filepath.open("r", newline="", encoding=_CSV_ENCODING) as handle:
      reader = csv.DictReader(handle)
      for row in reader:
        keyword = (row.get("keyword") or "").strip()
        site = _normalize_site(row.get("site") or "")
        device = _device_bucket(row.get("device") or "")
        if not keyword or not site:
          continue
        try:
          page = int(row.get("page", 0) or 0)
          rank = int(row.get("rank", 0) or 0)
        except (TypeError, ValueError):
          continue
        if page <= 0:
          continue
        key = (keyword, site, device)
        incoming = {
          "keyword": keyword,
          "site": site,
          "device": device,
          "page": page,
          "rank": rank if rank > 0 else 1,
          "updated_at": (row.get("updated_at") or "").strip()
          or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        existing = records.get(key)
        if not existing or incoming["updated_at"] >= existing.get("updated_at", ""):
          records[key] = incoming
    return records

  def _write_unlocked(self, records: dict[tuple[str, str, str], dict]) -> None:
    ordered = sorted(
      records.values(),
      key=lambda item: (
        item.get("keyword") or "",
        item.get("site") or "",
        item.get("device") or "",
      ),
    )
    with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as handle:
      writer = csv.writer(handle)
      writer.writerow(_HEADERS)
      for row in ordered:
        writer.writerow([
          row["keyword"],
          row["site"],
          row["device"],
          row["page"],
          row["rank"],
          row["updated_at"],
        ])
