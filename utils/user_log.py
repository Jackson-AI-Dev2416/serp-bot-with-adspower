"""Map verbose internal log lines to short user-facing activity messages."""

from __future__ import annotations

import re
from typing import Optional

# Tags that are never profile names (first bracket in a log line).
_SYSTEM_TAGS = frozenset({
  "UI",
  "Worker",
  "Controller",
  "AI Fix",
  "AdsPower",
  "SelfHealer",
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
  r"(?:Searching|Resuming) keyword \((\d+)/(\d+)\):\s*(.+?)(?:\s*\(|$)",
  re.IGNORECASE,
)
_KEYWORD_FOUND = re.compile(
  r"Found\s+(\S+)\s+at page (\d+), rank (\d+) for '(.+?)'",
  re.IGNORECASE,
)
_IP_CAPTURED = re.compile(r"Session baseline IP captured:\s*(\S+)", re.IGNORECASE)


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

  if tag == "IP":
    if "Checking proxy IP" in payload or "Checking IP" in payload:
      return _fmt(profile, "Checking IP...")
    ip_match = _IP_CAPTURED.search(payload)
    if ip_match:
      return _fmt(profile, f"IP confirmed: {ip_match.group(1)}")
    if "Could not capture session baseline IP" in payload:
      return _fmt(profile, "Failed - IP check unavailable")
    if "Session IP changed before keyword 2" in payload:
      return _fmt(profile, "Failed - IP changed mid-session")
    return None

  if tag == "Warmup" and re.search(r"Running \d+ warm-up", payload, re.IGNORECASE):
    return _fmt(profile, "Warming up...")

  if tag == "Captcha":
    if "CAPTCHA DETECTED" in payload:
      return _fmt(profile, "CAPTCHA detected")
    if "CAPTCHA SOLVE REQUEST" in payload or "Automated solving" in payload:
      return _fmt(profile, "CAPTCHA auto-solving...")
    if "CAPTCHA AUTO SOLVED" in payload:
      return _fmt(profile, "CAPTCHA auto-solve succeeded")
    if "CAPTCHA SOLVE FAILED" in payload:
      return _fmt(profile, "CAPTCHA auto-solve failed")
    if "Auto solve unresolved" in payload:
      return _fmt(profile, "CAPTCHA - waiting for manual solve")
    return None

  # --- Keyword flow (body without nested tag) ---
  search = _KEYWORD_SEARCH.search(body)
  if search:
    idx, total, keyword = search.group(1), search.group(2), search.group(3).strip()
    return _fmt(profile, f"Keyword {idx}/{total} - searching: {keyword}")

  found = _KEYWORD_FOUND.search(body)
  if found:
    domain, page_num, rank, keyword = found.groups()
    return _fmt(
      profile,
      f"Keyword - found {domain} (page {page_num}, rank {rank}): {keyword}",
    )

  if body.startswith("Target not found for "):
    keyword = body.replace("Target not found for ", "").strip().strip("'\"")
    return _fmt(profile, f"Keyword - not found: {keyword}")

  if tag == "Target" and "Dwelling on site" in payload:
    return _fmt(profile, "Visiting target site...")

  if tag == "Target" and "did not open within" in payload:
    return _fmt(profile, "Failed - target site did not open")

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

  if "checked all available SERP pages" in body.lower() and profile:
    kw_match = re.search(r"for '([^']+)'", body)
    keyword = kw_match.group(1) if kw_match else "keyword"
    return _fmt(profile, f"Keyword - not found (all pages): {keyword}")

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
    "stopped": "Stopped",
    "error": "Finished - error",
    "blocked": "Finished - blocked by Google",
    "ip_changed": "Finished - IP changed",
    "ip_unavailable": "Finished - IP unavailable",
    "tunnel_error": "Finished - proxy tunnel error",
  }
  if short:
    return outcome.replace("_", " ")
  return mapping.get(outcome, f"Finished - {outcome.replace('_', ' ')}")
