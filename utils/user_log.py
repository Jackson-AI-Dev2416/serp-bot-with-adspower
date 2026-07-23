"""Map verbose internal log lines to short user-facing activity messages."""

from __future__ import annotations

import re
from html import escape
from typing import Optional

# Tags that are never profile names (first bracket in a log line).
_SYSTEM_TAGS = frozenset({
  "UI",
  "Worker",
  "Controller",
  "AdsPower",
  "CrashReport",
  "Network",
  "Search",
  "Tab",
  "Mobile",
  "Consent",
  "Retry",
  "Start",
  "CapSolver",
  "Target",
  "Session",
  "Warmup",
  "IP",
  "Captcha",
})

_PROFILE_LINE = re.compile(r"^\[([^\]]+)\]\s*(.*)$", re.DOTALL)
_KEYWORD_SEARCH = re.compile(
  r"(?:Searching|Resuming) (?:attempt|keyword) (?:\(\d+/\d+\)|\(\d+/\d+\)):\s*'(.+?)'\s*→\s*(\S+)",
  re.IGNORECASE,
)
_SEARCHING_ATTEMPT = re.compile(
  r"(?:Searching|Resuming) attempt \d+/\d+:\s*'(.+?)'\s*→\s*(\S+)",
  re.IGNORECASE,
)
_TARGET_PAIR = re.compile(
  r"Target pair for this profile:\s*'(.+?)'\s*→\s*(\S+)",
  re.IGNORECASE,
)
_KEYWORD_FOUND = re.compile(
  r"Found\s+(\S+)\s+at page (\d+), rank (\d+) for '(.+?)'",
  re.IGNORECASE,
)
_IP_CAPTURED = re.compile(r"Proxy IP captured:\s*(\S+)", re.IGNORECASE)
_WARMUP_QUERY = re.compile(
  r"(?:Google entry|SERP) search box:\s*(.+)$",
  re.IGNORECASE,
)
_DWELL_TARGET = re.compile(
  r"Dwelling on site:\s*'(.+?)'\s*→\s*(\S+)",
  re.IGNORECASE,
)
_NOT_FOUND_TARGET = re.compile(
  r"not found target site:\s*'(.+?)'\s*→\s*(\S+)",
  re.IGNORECASE,
)
_FAILED_OPEN_TARGET = re.compile(
  r"failed open target site:\s*'(.+?)'\s*→\s*(\S+)",
  re.IGNORECASE,
)
_ERROR_BLOCKED = re.compile(
  r"error-blocked:\s*'(.+?)'\s*→\s*(\S+)",
  re.IGNORECASE,
)

_PRE_DELETE_FAILURE_OUTCOMES = frozenset({
  "error",
  "blocked",
  "ip_changed",
  "ip_unavailable",
  "tunnel_error",
})


def _split_profile(message: str) -> tuple[Optional[str], str]:
  text = message.strip()
  match = _PROFILE_LINE.match(text)
  if not match:
    return None, text
  tag, rest = match.group(1), match.group(2).strip()
  if tag in _SYSTEM_TAGS:
    return None, text
  return tag, rest


def _fmt(profile: Optional[str], text: str) -> str:
  if profile:
    return f"[{profile}] {text}"
  return text


def build_pre_delete_log_line(
  profile_name: str,
  outcome: str,
  keyword: str,
  domain: str,
) -> Optional[str]:
  """Internal log line shown in the activity panel before profile removal."""
  kw = (keyword or "").strip()
  dom = (domain or "").strip()
  if not kw or not dom:
    return None
  name = (profile_name or "").strip()
  if not name:
    return None
  if outcome == "not_found":
    return f"[{name}] not found target site: '{kw}' → {dom}"
  if outcome == "failed":
    return f"[{name}] failed open target site: '{kw}' → {dom}"
  if outcome in _PRE_DELETE_FAILURE_OUTCOMES:
    return f"[{name}] error-blocked: '{kw}' → {dom}"
  if outcome == "proxy_connect_failed":
    return f"[{name}] Failed connect proxy"
  return None


def format_user_log(message: str) -> Optional[str]:
  """Return a user-facing log line, or None to hide from the activity panel."""
  raw = message.strip()
  if not raw:
    return None

  profile, body = _split_profile(raw)
  if profile is None and raw.startswith("["):
    body = raw

  # --- Automation / controller (no profile prefix) ---
  if "[Controller]" in raw or raw.startswith("[Controller]"):
    if "Global automated bot started" in raw:
      return "Automation started"
    if "Global bot stop requested" in raw:
      return "Automation stopped"
    manual = re.search(r"Manual start:\s*(.+)$", raw)
    if manual:
      return None
    graceful = re.search(r"Graceful stop requested for\s+(.+?)(?:\s*\(automation\))?$", raw)
    if graceful:
      return _fmt(graceful.group(1).strip(), "Stopping...")
    force = re.search(r"Force terminated\s+(.+)$", raw)
    if force:
      return _fmt(force.group(1).strip(), "Force stopped")
    finished = re.search(r"\[Controller\]\s+(\S+)\s+finished\s+\(([^)]+)\)", raw)
    if finished:
      name, outcome = finished.group(1), finished.group(2)
      return _fmt(name, _outcome_label(outcome))
    return None

  if "[Worker]" in raw:
    launch = re.search(r"Launching\s+(.+?)\s+via proxy", raw)
    if launch:
      return None
    tunnel = re.search(r"Tunnel connection failed on\s+(.+?)\.", raw)
    if tunnel:
      return _fmt(tunnel.group(1).strip(), "Failed - proxy tunnel error")
    deleted = re.search(r"Deleted profile\s+(.+?)\s+after run", raw)
    if deleted:
      return _fmt(deleted.group(1).strip(), "Finished - profile removed")
    error = re.search(r"Error on\s+(.+?):\s*(.+)$", raw)
    if error:
      return _fmt(error.group(1).strip(), f"Failed - {error.group(2).strip()}")
    return None

  # Strip nested [Tag] prefix from profile-scoped body.
  nested = re.match(r"^\[([^\]]+)\]\s*(.*)$", body, re.DOTALL)
  tag = ""
  payload = body
  if nested and nested.group(1) in _SYSTEM_TAGS:
    tag = nested.group(1)
    payload = nested.group(2).strip()

  # --- Profile session lifecycle ---
  if tag == "Session" and payload == "Starting":
    return _fmt(profile, "Starting")

  if tag == "Session":
    return None

  if tag == "IP":
    if "Checking proxy IP" in payload or "Checking IP" in payload:
      return _fmt(profile, "Checking proxy IP...")
    ip_match = _IP_CAPTURED.search(payload)
    if ip_match:
      return _fmt(profile, f"Proxy IP confirmed: {ip_match.group(1)}")
    if "Could not capture proxy IP" in payload:
      return _fmt(profile, "Failed - proxy IP check unavailable")
    if "Session IP changed before keyword 2" in payload:
      return _fmt(profile, "Failed - proxy IP changed mid-session")
    return None

  if tag == "Warmup":
    warmup_query = _WARMUP_QUERY.search(payload)
    if warmup_query:
      return _fmt(profile, f"WarmingUp({warmup_query.group(1).strip()})")
    if re.search(r"Running \d+ warm-up", payload, re.IGNORECASE):
      return None

  if tag == "Captcha":
    if "Consecutive captcha limit reached" in payload:
      return _fmt(profile, "Stopped - repeated captcha (profile removed)")
    if "CAPTCHA DETECTED" in payload:
      return _fmt(profile, "CAPTCHA detected")
    if "CAPTCHA SOLVE REQUEST: API" in payload or "Automated solving" in payload:
      return _fmt(profile, "CAPTCHA auto-solving...")
    if "CAPTCHA SOLVE REQUEST: MANUAL" in payload:
      return _fmt(profile, "CAPTCHA - manual solve required")
    if "Manual mode" in payload and "waiting up to" in payload:
      return _fmt(profile, "CAPTCHA - manual solve (60s limit)")
    if "Manual wait in progress" in payload:
      return _fmt(profile, "CAPTCHA - still waiting for manual solve")
    if "Manual wait timed out" in payload:
      return _fmt(profile, "CAPTCHA manual timeout - removing profile")
    if "Manual captcha cleared" in payload:
      return _fmt(profile, "CAPTCHA manual solve succeeded")
    if "CAPTCHA AUTO SOLVED" in payload:
      return _fmt(profile, "CAPTCHA auto-solve succeeded")
    if "CAPTCHA SOLVE FAILED" in payload:
      detail = ""
      if "CAPTCHA SOLVE FAILED (" in payload:
        detail = payload.split("CAPTCHA SOLVE FAILED (", 1)[1].rstrip(")").strip()
      if detail and detail != "no token from CapSolver":
        return _fmt(profile, f"CAPTCHA auto-solve failed — {detail}")
      return _fmt(profile, "CAPTCHA auto-solve failed")
    if "Auto solve unresolved" in payload:
      return _fmt(profile, "CAPTCHA - waiting for manual solve")
    return None

  if tag == "CapSolver":
    return None

  # --- Keyword flow (body without nested tag) ---
  # Target pair is logged to session.log only; UI shows Searching on attempt start.
  if _TARGET_PAIR.search(body):
    return None

  attempt = _SEARCHING_ATTEMPT.search(body)
  if attempt:
    keyword, domain = attempt.group(1).strip(), attempt.group(2).strip()
    return _fmt(profile, f"Searching ({keyword} : {domain})")

  search = _KEYWORD_SEARCH.search(body)
  if search:
    keyword, domain = search.group(1).strip(), search.group(2).strip()
    return _fmt(profile, f"Searching ({keyword} : {domain})")

  legacy = re.search(
    r"(?:Searching|Resuming) keyword \((\d+)/(\d+)\):\s*(.+?)(?:\s*\(|$)",
    body,
    re.IGNORECASE,
  )
  if legacy:
    keyword = legacy.group(3).strip()
    return _fmt(profile, f"Searching ({keyword})")

  found = _KEYWORD_FOUND.search(body)
  if found:
    domain, page_num, rank, keyword = found.groups()
    return _fmt(
      profile,
      f"Found {domain} — page {page_num}, rank {rank} ({keyword})",
    )

  if body.startswith("Target not found for "):
    keyword = body.replace("Target not found for ", "").strip().strip("'\"")
    return _fmt(profile, f"Keyword - not found: {keyword}")

  if tag == "Target" and "Dwelling on site" in payload:
    dwell = _DWELL_TARGET.search(payload)
    if dwell:
      keyword, site = dwell.group(1).strip(), dwell.group(2).strip()
      return _fmt(profile, f"VisitingSite({keyword} : {site})")
    return _fmt(profile, "VisitingSite")

  if tag == "Target" and "Failed open target site" in payload:
    failed_open_target = _FAILED_OPEN_TARGET.search(payload)
    if failed_open_target and profile:
      keyword, site = failed_open_target.group(1).strip(), failed_open_target.group(2).strip()
      return _fmt(profile, f"Failed open target site ({keyword} : {site})")

  if body.startswith("No keywords configured"):
    return _fmt(profile, "Failed - no keywords configured")

  if body.startswith("[Start]") and "Could not open Google" in body:
    return _fmt(profile, "Failed - Google did not load")

  if body.startswith("Session error:"):
    reason = body.split("Session error:", 1)[1].strip()
    return _fmt(profile, f"Failed - {reason}")

  if "Browser closed during session" in body:
    return _fmt(profile, "Stopped")

  if profile and re.match(r"^Error:\s*", body):
    return _fmt(profile, f"Failed - {body[6:].strip()}")

  if "checked all available SERP pages" in body.lower():
    return None

  not_found_target = _NOT_FOUND_TARGET.search(body)
  if not_found_target and profile:
    keyword, site = not_found_target.group(1).strip(), not_found_target.group(2).strip()
    return _fmt(profile, f"Not found target site ({keyword} : {site})")

  failed_open_target = _FAILED_OPEN_TARGET.search(body)
  if failed_open_target and profile:
    # Worker pre-delete line duplicates the earlier [Target] failure message.
    if body.lower().startswith("failed open target site"):
      return None
    keyword, site = failed_open_target.group(1).strip(), failed_open_target.group(2).strip()
    return _fmt(profile, f"Failed open target site ({keyword} : {site})")

  error_blocked = _ERROR_BLOCKED.search(body)
  if error_blocked and profile:
    keyword, site = error_blocked.group(1).strip(), error_blocked.group(2).strip()
    return _fmt(profile, f"Error-blocked ({keyword} : {site})")

  if "Failed connect proxy" in body and profile:
    # Worker pre-delete line duplicates the earlier serp_bot failure message.
    if body.strip() == "Failed connect proxy":
      return None
    return _fmt(profile, "Failed connect proxy")

  if "Tunnel connection failed" in body and profile:
    return _fmt(profile, "Failed - proxy tunnel error, retrying...")

  if "Profile deleted after run" in body and profile:
    outcome_match = re.search(r"\(([^)]+)\)", body)
    outcome = outcome_match.group(1) if outcome_match else "done"
    return _fmt(profile, f"Finished - profile removed ({_outcome_label(outcome, short=True)})")

  # Hide everything else (network, probes, tabs, consent, UI noise, etc.).
  return None


def _outcome_label(outcome: str, *, short: bool = False) -> str:
  mapping = {
    "success": "Finished - success",
    "not_found": "Finished - target not found",
    "failed": "Finished - failed to open target",
    "stopped": "Stopped",
    "error": "Finished - error",
    "blocked": "Finished - blocked by Google",
    "ip_changed": "Finished - IP changed",
    "ip_unavailable": "Finished - IP unavailable",
    "tunnel_error": "Finished - proxy tunnel error",
    "proxy_connect_failed": "Finished - proxy connect failed",
  }
  if short:
    return outcome.replace("_", " ")
  return mapping.get(outcome, f"Finished - {outcome.replace('_', ' ')}")


def format_user_log_html(user_line: str, time_stamp: str) -> str:
  """Render a user-facing log line with time / profile / body colors for QTextEdit."""
  profile_match = re.match(r"^\[([^\]]+)\]\s*(.*)$", user_line, re.DOTALL)
  if profile_match:
    profile, body = profile_match.group(1), profile_match.group(2)
    body_lower = body.lower()
    if body_lower.startswith("searching"):
      body_color = "#f8fafc"
    elif body_lower.startswith("found"):
      body_color = "#10b981"
    elif body_lower.startswith("visitingsite"):
      body_color = "#34d399"
    elif "captcha" in body_lower:
      body_color = "#f59e0b"
    elif (
      body_lower.startswith("not found target site")
      or body_lower.startswith("failed open target site")
      or body_lower.startswith("error-blocked")
      or "failed" in body_lower
      or "not found" in body_lower
    ):
      body_color = "#fca5a5"
    elif body_lower.startswith("stopping") or body_lower.startswith("stopped"):
      body_color = "#f97316"
    else:
      body_color = "#e2e8f0"
    return (
      f'<span style="color:#64748b">{escape(time_stamp)}</span> '
      f'<span style="color:#38bdf8;font-weight:600">[{escape(profile)}]</span> '
      f'<span style="color:{body_color}"> {escape(body)}</span>'
    )
  return (
    f'<span style="color:#64748b">{escape(time_stamp)}</span> '
    f'<span style="color:#cbd5e1"> {escape(user_line)}</span>'
  )
