import json
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils.app_paths import data_dir


def crash_report_path() -> Path:
  return data_dir() / "crash_report.json"


def html_snapshot_path() -> Path:
  return data_dir() / "crash_page.html"


def write_crash_report(
  *,
  profile_id: str,
  profile_name: str,
  context: str,
  error_type: str,
  error_message: str,
  tb: str,
  page_html: str = "",
  target_file: str = "services/serp_bot.py",
  extra: Optional[dict[str, Any]] = None,
) -> Path:
  report_path = crash_report_path()
  snapshot_path = html_snapshot_path()
  report_path.parent.mkdir(parents=True, exist_ok=True)

  if page_html:
    snapshot_path.write_text(page_html, encoding="utf-8")

  payload = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "profile_id": profile_id,
    "profile_name": profile_name,
    "context": context,
    "error_type": error_type,
    "error_message": error_message,
    "traceback": tb,
    "page_html_path": str(snapshot_path) if page_html else "",
    "target_file": target_file,
    "extra": extra or {},
  }
  report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
  return report_path


def capture_exception(
  *,
  profile_id: str,
  profile_name: str,
  context: str,
  exc: BaseException,
  page_html: str = "",
  target_file: str = "services/serp_bot.py",
) -> Path:
  return write_crash_report(
    profile_id=profile_id,
    profile_name=profile_name,
    context=context,
    error_type=type(exc).__name__,
    error_message=str(exc),
    tb=traceback.format_exc(),
    page_html=page_html,
    target_file=target_file,
  )
