import csv
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class CsvRankLogger:
    _HEADERS = ("timestamp", "keyword", "target_domain", "page", "rank", "profile_name")

    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.filepath.exists():
            with self.filepath.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self._HEADERS)

    def log(self, keyword: str, target_domain: str, page: int, rank: int, profile_name: str) -> None:
        row = (
            datetime.now(timezone.utc).isoformat(),
            keyword,
            target_domain,
            page,
            rank,
            profile_name,
        )
        with self._lock:
            with self.filepath.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)


class KeywordHistoryLogger:
    _HEADERS = ("timestamp", "keyword", "page", "rank", "total_rank")

    def __init__(self, target_domain: str, data_dir: str | Path = "data"):
        domain_key = self._domain_key(target_domain)
        filename = f"{domain_key}_last_keyword.csv"
        self.filepath = Path(data_dir) / filename
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if not self.filepath.exists():
            with self.filepath.open("w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(self._HEADERS)

    def log(self, keyword: str, page: int, rank: int, total_rank: int) -> None:
        row = (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            keyword,
            page,
            rank,
            total_rank,
        )
        with self._lock:
            with self.filepath.open("a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(row)

    def get_last_page_hint(self, keyword: str) -> Optional[int]:
        if not self.filepath.exists():
            return None
        last_page = None
        lookup = keyword.strip()
        with self._lock:
            with self.filepath.open("r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get("keyword") or "").strip() != lookup:
                        continue
                    total_rank = self._to_int(row.get("total_rank", ""))
                    page = self._to_int(row.get("page", ""))
                    if total_rank and total_rank > 0:
                        last_page = max(1, ((total_rank - 1) // 10) + 1)
                    elif page and page > 0:
                        last_page = page
        return last_page

    @staticmethod
    def _to_int(value: str) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _domain_key(target_domain: str) -> str:
        cleaned = (target_domain or "").strip().lower()
        if cleaned.startswith("http://"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("https://"):
            cleaned = cleaned[8:]
        cleaned = cleaned.split("/")[0].strip()
        cleaned = cleaned.removeprefix("www.")
        cleaned = re.sub(r"[^a-z0-9._-]+", "_", cleaned)
        return cleaned or "site"
