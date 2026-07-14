import csv
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TypedDict


class KeywordClickSummary(TypedDict):
    keyword: str
    total: int
    windows: int
    mobile: int

_RESULT_FILENAME_RE = re.compile(r"^result_(\d{8})_(\d{6})\.csv$", re.IGNORECASE)

# Excel on Windows opens CSV as ANSI/CP949 unless a UTF-8 BOM is present.
_CSV_ENCODING = "utf-8-sig"


def _ensure_csv_utf8_bom(filepath: Path) -> None:
    if not filepath.exists() or filepath.stat().st_size == 0:
        return
    with filepath.open("rb") as handle:
        if handle.read(3) == b"\xef\xbb\xbf":
            return
    with filepath.open("r", encoding="utf-8", newline="") as handle:
        content = handle.read()
    with filepath.open("w", encoding=_CSV_ENCODING, newline="") as handle:
        handle.write(content)


def new_session_click_log_path(data_dir: str | Path = "data") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(data_dir) / f"result_{stamp}.csv"


class SessionClickCsvLogger:
    _HEADERS = ("datetime", "profile_name", "device", "keyword", "url", "page", "rank", "overall_rank")
    _PATH_LOCKS: dict[str, threading.Lock] = {}

    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        key = str(self.filepath.resolve())
        if key not in self._PATH_LOCKS:
            self._PATH_LOCKS[key] = threading.Lock()
        self._lock = self._PATH_LOCKS[key]
        with self._lock:
            if not self.filepath.exists() or self.filepath.stat().st_size == 0:
                with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as handle:
                    csv.writer(handle).writerow(self._HEADERS)

    def log(
        self,
        *,
        profile_name: str,
        device: str,
        keyword: str,
        url: str,
        page: int,
        rank: int,
        overall_rank: int,
    ) -> None:
        row = (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            profile_name,
            (device or "").strip() or "Unknown",
            keyword,
            (url or "").strip(),
            page,
            rank,
            overall_rank,
        )
        with self._lock:
            with self.filepath.open("a", newline="", encoding=_CSV_ENCODING) as handle:
                csv.writer(handle).writerow(row)

    @staticmethod
    def count_rows(filepath: str | Path) -> int:
        path = Path(filepath)
        if not path.exists() or path.stat().st_size == 0:
            return 0
        with path.open("r", newline="", encoding=_CSV_ENCODING) as handle:
            reader = csv.reader(handle)
            rows = list(reader)
        if len(rows) <= 1:
            return 0
        return max(0, len(rows) - 1)

    @staticmethod
    def read_rows(filepath: str | Path) -> tuple[list[str], list[list[str]]]:
        path = Path(filepath)
        if not path.exists() or path.stat().st_size == 0:
            return [], []
        with path.open("r", newline="", encoding=_CSV_ENCODING) as handle:
            reader = csv.reader(handle)
            rows = list(reader)
        if not rows:
            return [], []
        headers = [str(cell).strip() for cell in rows[0]]
        data = [list(row) for row in rows[1:] if any(str(cell).strip() for cell in row)]
        return headers, data


def _header_index(headers: list[str], name: str) -> int:
    lowered = [header.strip().lower() for header in headers]
    target = name.strip().lower()
    try:
        return lowered.index(target)
    except ValueError:
        return -1


def _is_windows_device(device: str) -> bool:
    lowered = (device or "").strip().lower()
    return lowered.startswith("windows") or lowered.startswith("win")


def _is_mobile_device(device: str) -> bool:
    lowered = (device or "").strip().lower()
    return lowered in {"android", "ios", "mobile"} or lowered.startswith("android")


def aggregate_keyword_clicks(headers: list[str], rows: list[list[str]]) -> list[KeywordClickSummary]:
    keyword_idx = _header_index(headers, "keyword")
    device_idx = _header_index(headers, "device")
    if keyword_idx < 0:
        return []

    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        keyword = (row[keyword_idx] if keyword_idx < len(row) else "").strip()
        if not keyword:
            continue
        device = (row[device_idx] if device_idx >= 0 and device_idx < len(row) else "").strip()
        bucket = totals.setdefault(keyword, {"total": 0, "windows": 0, "mobile": 0})
        bucket["total"] += 1
        if _is_windows_device(device):
            bucket["windows"] += 1
        elif _is_mobile_device(device):
            bucket["mobile"] += 1

    summaries: list[KeywordClickSummary] = [
        {
            "keyword": keyword,
            "total": counts["total"],
            "windows": counts["windows"],
            "mobile": counts["mobile"],
        }
        for keyword, counts in totals.items()
    ]
    summaries.sort(key=lambda item: (-item["total"], item["keyword"].casefold()))
    return summaries


def parse_result_file_stamp(filepath: str | Path) -> Optional[datetime]:
    match = _RESULT_FILENAME_RE.match(Path(filepath).name)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)}_{match.group(2)}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def format_result_session_label(filepath: str | Path) -> str:
    stamp = parse_result_file_stamp(filepath)
    if stamp is None:
        return Path(filepath).name
    return stamp.strftime("%Y-%m-%d %H:%M:%S")


def list_session_result_files(data_dir: str | Path = "data") -> list[Path]:
    root = Path(data_dir)
    if not root.exists():
        return []
    files = [path for path in root.glob("result_*.csv") if _RESULT_FILENAME_RE.match(path.name)]
    files.sort(key=lambda path: parse_result_file_stamp(path) or datetime.min, reverse=True)
    return files


def session_result_window(
    filepath: str | Path,
    all_files: list[Path] | None = None,
) -> tuple[Optional[datetime], Optional[datetime]]:
    path = Path(filepath)
    start = parse_result_file_stamp(path)
    if start is None:
        return None, None
    files = all_files if all_files is not None else list_session_result_files(path.parent)
    ordered = sorted(files, key=lambda item: parse_result_file_stamp(item) or datetime.min)
    try:
        index = ordered.index(path.resolve())
    except ValueError:
        try:
            index = next(i for i, item in enumerate(ordered) if item.name == path.name)
        except StopIteration:
            return start, None
    if index + 1 < len(ordered):
        return start, parse_result_file_stamp(ordered[index + 1])
    return start, None


def count_target_not_found_in_session(
    session_start: datetime,
    session_end_exclusive: Optional[datetime] = None,
    *,
    session_log_path: str | Path = "data/session.log",
) -> int:
    log_path = Path(session_log_path)
    if not log_path.exists():
        return 0
    count = 0
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if "Target not found for" not in line:
                continue
            try:
                stamp = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if stamp < session_start:
                continue
            if session_end_exclusive is not None and stamp >= session_end_exclusive:
                continue
            count += 1
    return count


class CsvRankLogger:
    _HEADERS = ("timestamp", "keyword", "target_domain", "page", "rank", "profile_name")

    def __init__(self, filepath: str | Path):
        self.filepath = Path(filepath)
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            if self.filepath.exists():
                _ensure_csv_utf8_bom(self.filepath)
            if not self.filepath.exists():
                with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as f:
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
            with self.filepath.open("a", newline="", encoding=_CSV_ENCODING) as f:
                csv.writer(f).writerow(row)

    def count_clicks(self, target_domain: str | None = None) -> int:
        if not self.filepath.exists():
            return 0
        filter_domain = self._normalize_domain(target_domain) if target_domain else None
        count = 0
        with self._lock:
            with self.filepath.open("r", newline="", encoding=_CSV_ENCODING) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if filter_domain:
                        row_domain = self._normalize_domain(row.get("target_domain", ""))
                        if row_domain != filter_domain:
                            continue
                    count += 1
        return count

    @staticmethod
    def _normalize_domain(value: str) -> str:
        cleaned = (value or "").strip().lower()
        if cleaned.startswith("http://"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("https://"):
            cleaned = cleaned[8:]
        cleaned = cleaned.split("/")[0].strip()
        return cleaned.removeprefix("www.")


class KeywordHistoryLogger:
    _HEADERS = ("timestamp", "keyword", "page", "rank", "total_rank")

    def __init__(self, target_domain: str, data_dir: str | Path = "data"):
        domain_key = self._domain_key(target_domain)
        filename = f"{domain_key}_last_keyword.csv"
        self.filepath = Path(data_dir) / filename
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._lock:
            if self.filepath.exists():
                _ensure_csv_utf8_bom(self.filepath)
            if not self.filepath.exists():
                with self.filepath.open("w", newline="", encoding=_CSV_ENCODING) as f:
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
            with self.filepath.open("a", newline="", encoding=_CSV_ENCODING) as f:
                csv.writer(f).writerow(row)

    def get_last_page_hint(self, keyword: str) -> Optional[int]:
        if not self.filepath.exists():
            return None
        last_page = None
        lookup = keyword.strip()
        with self._lock:
            with self.filepath.open("r", newline="", encoding=_CSV_ENCODING) as f:
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
