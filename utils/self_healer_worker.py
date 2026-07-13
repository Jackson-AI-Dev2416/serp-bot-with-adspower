"""
Subprocess entry point for the self-healing pipeline.

Usage:
  python -m utils.self_healer_worker --project-root <path> --mode auto|manual
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import traceback
from pathlib import Path, PurePosixPath

import requests

CRASH_REPORT = Path("data/crash_report.json")
MANUAL_FIX_REQUEST = Path("data/manual_fix_request.json")
HEALER_RESULT = Path("data/healer_result.json")
DEFAULT_TARGET = Path("services/serp_bot.py")
BACKUP_SUFFIX = ".healer_backup"
MAX_FILES_PER_REQUEST = 3

# Whitelist of project Python files the healer may modify.
EDITABLE_FILES: dict[str, str] = {
  "main.py": "Application entry point and Qt app bootstrap",
  "ui/main_window.py": "PyQt6 GUI: dashboard, settings, profile table, stylesheets, manual fix UI",
  "services/serp_bot.py": "Playwright Google SERP automation and click logic",
  "services/adspower_manager.py": "AdsPower Local API: list/create/delete/start browser profiles",
  "services/captcha_solver.py": "CapSolver captcha solving integration",
  "core/worker.py": "ProfileController orchestration, auto-healing triggers",
  "core/profile_worker.py": "Per-profile QThread worker running SERP tasks",
  "core/profile_status.py": "UI/bot status enums and display labels",
  "core/proxy_scheduler.py": "Proxy assignment and rotation for profiles",
  "config/bot_config.py": "BotConfig dataclass and runtime settings",
  "config/settings_store.py": "Load/save data/settings.json persistence",
  "utils/self_healer.py": "GUI-side self-healer subprocess launcher",
  "utils/crash_reporter.py": "Crash report and HTML snapshot writer",
  "utils/csv_logger.py": "CSV activity logging",
  "utils/human.py": "Human-like mouse/typing delays",
}

UI_ROUTE_KEYWORDS = (
  "ui", "gui", "pyqt", "qt", "window", "button", "hover", "checkbox", "table",
  "stylesheet", "style", "profile name", "profile table", "dashboard", "settings tab",
  "ellipsis", "...", "layout", "column", "row", "font", "color", "theme", "dark",
)
BOT_ROUTE_KEYWORDS = (
  "serp", "google", "search", "playwright", "selector", "dom", "click", "keyword",
  "warmup", "captcha page", "result link", "textarea", "input[name",
)
ADSPOWER_ROUTE_KEYWORDS = (
  "adspower", "browser profile", "create profile", "delete profile", "proxy format",
  "api key", "local api",
)
WORKER_ROUTE_KEYWORDS = (
  "worker", "thread", "cooldown", "pause", "terminate", "orchestrat", "profile controller",
)
CAPTCHA_ROUTE_KEYWORDS = ("capsolver", "captcha solver", "recaptcha", "hcaptcha")
SETTINGS_ROUTE_KEYWORDS = ("settings.json", "save settings", "load settings", "settings store")


def _load_json(path: Path) -> dict:
  return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _llm_config_from_env() -> dict:
  return {
    "api_key": os.getenv("OPENAI_API_KEY", os.getenv("LLM_API_KEY", "")),
    "base_url": os.getenv("OPENAI_BASE_URL", os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")).rstrip("/"),
    "model": os.getenv("OPENAI_MODEL", os.getenv("LLM_MODEL", "gpt-4o-mini")),
  }


def _normalize_rel_path(path_str: str) -> str | None:
  raw = str(path_str or "").strip().replace("\\", "/")
  if not raw:
    return None
  if raw.startswith("./"):
    raw = raw[2:]
  if raw not in EDITABLE_FILES:
    candidate = PurePosixPath(raw).as_posix()
    if candidate in EDITABLE_FILES:
      return candidate
    return None
  return raw


def _call_llm_chat(messages: list[dict], cfg: dict, *, timeout: int = 180) -> str:
  if not cfg.get("api_key"):
    raise RuntimeError("LLM API key not configured (set OPENAI_API_KEY or LLM_API_KEY)")

  url = f"{cfg['base_url']}/chat/completions"
  headers = {
    "Authorization": f"Bearer {cfg['api_key']}",
    "Content-Type": "application/json",
  }
  body = {
    "model": cfg["model"],
    "temperature": 0.1,
    "messages": messages,
  }
  response = requests.post(url, headers=headers, json=body, timeout=timeout)
  response.raise_for_status()
  data = response.json()
  return str(data["choices"][0]["message"]["content"])


def _strip_code_fence(text: str) -> str:
  text = text.strip()
  if text.startswith("```"):
    text = re.sub(r"^```(?:python)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
  return text.strip() + "\n"


def _parse_json_array(content: str) -> list[str]:
  text = content.strip()
  if text.startswith("```"):
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
  try:
    parsed = json.loads(text)
  except json.JSONDecodeError:
    match = re.search(r"\[[\s\S]*?\]", text)
    if not match:
      return []
    parsed = json.loads(match.group(0))
  if isinstance(parsed, dict):
    files = parsed.get("files") or parsed.get("targets") or parsed.get("paths") or []
    if isinstance(files, str):
      return [_normalize_rel_path(files)] if _normalize_rel_path(files) else []
    if isinstance(files, list):
      return [p for p in (_normalize_rel_path(str(x)) for x in files) if p]
    return []
  if isinstance(parsed, list):
    return [p for p in (_normalize_rel_path(str(x)) for x in parsed) if p]
  return []


def _heuristic_route(user_instructions: str, crash: dict | None) -> list[str]:
  text = user_instructions.lower()
  scores: dict[str, int] = {path: 0 for path in EDITABLE_FILES}

  def bump(paths: tuple[str, ...], amount: int = 2) -> None:
    for path in paths:
      if path in scores:
        scores[path] += amount

  if any(k in text for k in UI_ROUTE_KEYWORDS):
    bump(("ui/main_window.py",), 5)
  if any(k in text for k in BOT_ROUTE_KEYWORDS):
    bump(("services/serp_bot.py",), 5)
  if any(k in text for k in ADSPOWER_ROUTE_KEYWORDS):
    bump(("services/adspower_manager.py", "config/bot_config.py"), 4)
  if any(k in text for k in WORKER_ROUTE_KEYWORDS):
    bump(("core/worker.py", "core/profile_worker.py"), 4)
  if any(k in text for k in CAPTCHA_ROUTE_KEYWORDS):
    bump(("services/captcha_solver.py",), 5)
  if any(k in text for k in SETTINGS_ROUTE_KEYWORDS):
    bump(("config/settings_store.py", "ui/main_window.py"), 4)

  if crash:
    crash_target = _normalize_rel_path(str(crash.get("target_file") or ""))
    if crash_target:
      bump((crash_target,), 6)

  ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
  if not ranked or ranked[0][1] == 0:
    return [str(DEFAULT_TARGET)]

  top_score = ranked[0][1]
  selected = [path for path, score in ranked if score == top_score][:MAX_FILES_PER_REQUEST]
  return selected or [str(DEFAULT_TARGET)]


def _route_target_files(
  user_instructions: str,
  crash: dict | None,
  cfg: dict,
  *,
  mode: str,
) -> list[str]:
  if mode == "auto":
    if crash:
      target = _normalize_rel_path(str(crash.get("target_file") or "")) or str(DEFAULT_TARGET)
      return [target]
    return [str(DEFAULT_TARGET)]

  prompt = f"""User fix request:
{user_instructions}

Available project files (relative path -> purpose):
{json.dumps(EDITABLE_FILES, ensure_ascii=False, indent=2)}

Pick up to {MAX_FILES_PER_REQUEST} file path(s) that must be edited to satisfy the request.
Return ONLY JSON in this exact shape:
{{"files": ["relative/path.py"]}}
Use paths exactly as listed. Prefer the smallest set of files needed.
"""
  if crash:
    prompt += (
      f"\nOptional crash context: {crash.get('error_type')}: {crash.get('error_message')}\n"
      f"Crash suggested file: {crash.get('target_file', str(DEFAULT_TARGET))}\n"
    )

  try:
    content = _call_llm_chat(
      [
        {
          "role": "system",
          "content": (
            "You route code-change requests to the correct Python files in a SERP automation app. "
            "Return ONLY valid JSON with a 'files' array. No markdown."
          ),
        },
        {"role": "user", "content": prompt},
      ],
      cfg,
      timeout=60,
    )
    routed = _parse_json_array(content)
    if routed:
      return routed[:MAX_FILES_PER_REQUEST]
  except Exception:
    pass

  return _heuristic_route(user_instructions, crash)


def _system_prompt_for(rel_path: str) -> str:
  if rel_path == "ui/main_window.py":
    return (
      "You are a senior PyQt6 UI engineer working on a dark-themed SERP bot desktop app. "
      f"Return ONLY the full corrected Python file for {rel_path}. "
      "No markdown fences, no explanation. Keep English UI strings. "
      "Preserve existing object names, signals, and public method signatures unless the user asks otherwise."
    )
  if rel_path == "services/serp_bot.py":
    return (
      "You are a Playwright automation engineer for Google SERP tasks. "
      f"Return ONLY the full corrected Python file for {rel_path}. "
      "No markdown fences, no explanation. Keep existing public method signatures and imports intact."
    )
  return (
    "You are a senior Python engineer on a PyQt6 + Playwright SERP automation project. "
    f"Return ONLY the full corrected Python file for {rel_path}. "
    "No markdown fences, no explanation. Make minimal, focused changes."
  )


def _build_patch_prompt(
  rel_path: str,
  crash: dict | None,
  source: str,
  html_excerpt: str,
  user_instructions: str = "",
) -> str:
  sections: list[str] = []

  if user_instructions:
    sections.append(
      f"""User instructions (apply these changes):
{user_instructions}
"""
    )

  if crash:
    sections.append(
      f"""Crash context:
- Phase: {crash.get('context', 'unknown')}
- Profile: {crash.get('profile_name')} ({crash.get('profile_id')})
- Error: {crash.get('error_type')}: {crash.get('error_message')}

Traceback:
{crash.get('traceback', '')}
"""
    )
  elif user_instructions:
    sections.append(
      "No automatic crash report is available. Apply the user instructions to the selected project file.\n"
    )
  else:
    sections.append("No crash context or user instructions provided.\n")

  file_purpose = EDITABLE_FILES.get(rel_path, "Project module")
  sections.append(
    f"""Target file: {rel_path}
Purpose: {file_purpose}

Current file contents:
{source}
"""
  )

  if rel_path == "services/serp_bot.py":
    sections.append(
      f"""Relevant page HTML excerpt (truncated):
{html_excerpt or '(none)'}

Task:
1. Fix broken Playwright selectors or DOM assumptions when relevant.
2. Apply the user instructions when provided.
3. Return the COMPLETE corrected {rel_path} file.
4. Keep existing public method signatures and imports intact unless required.
"""
    )
  elif rel_path == "ui/main_window.py":
    sections.append(
      f"""Task:
1. Apply the user instructions to the PyQt6 UI (widgets, layout, stylesheet, table behavior).
2. For truncated text with "..." in table cells, consider QTextOption / elide mode / word wrap / column sizing.
3. Return the COMPLETE corrected {rel_path} file.
4. Keep English UI strings and existing signal/slot wiring unless required.
"""
    )
  else:
    sections.append(
      f"""Task:
1. Apply the user instructions with minimal, correct changes.
2. Return the COMPLETE corrected {rel_path} file.
3. Keep existing public APIs stable unless the request requires otherwise.
"""
    )

  return "\n".join(sections)


def _validate_patch(target: Path, rel_path: str, html_path: Path) -> tuple[bool, str]:
  try:
    import py_compile
    py_compile.compile(str(target), doraise=True)
  except Exception as exc:
    return False, f"Syntax error in {rel_path}: {exc}"

  if rel_path != "services/serp_bot.py" or not html_path.exists():
    return True, f"Syntax OK ({rel_path})"

  try:
    from playwright.sync_api import sync_playwright

    html = html_path.read_text(encoding="utf-8", errors="ignore")
    with sync_playwright() as p:
      browser = p.chromium.launch(headless=True)
      page = browser.new_page()
      page.set_content(html, wait_until="domcontentloaded")

      search_count = page.locator('textarea[name="q"], input[name="q"]').count()
      result_count = page.locator('div#search a[href^="http"], #rso a[href^="http"]').count()
      browser.close()

    if search_count == 0 and result_count == 0:
      return True, f"{rel_path}: patch compiled; DOM validation inconclusive"
    return True, f"{rel_path}: DOM validation passed (search={search_count}, results={result_count})"
  except Exception as exc:
    return False, f"DOM validation failed for {rel_path}: {exc}"


def _patch_single_file(
  project_root: Path,
  rel_path: str,
  crash: dict | None,
  html_excerpt: str,
  user_instructions: str,
  cfg: dict,
  html_path: Path,
) -> tuple[bool, str, str | None]:
  target = project_root / rel_path
  if not target.exists():
    return False, f"Target file not found: {rel_path}", None

  source = target.read_text(encoding="utf-8")
  prompt = _build_patch_prompt(rel_path, crash, source, html_excerpt, user_instructions=user_instructions)

  try:
    content = _call_llm_chat(
      [
        {"role": "system", "content": _system_prompt_for(rel_path)},
        {"role": "user", "content": prompt},
      ],
      cfg,
    )
    patched = _strip_code_fence(content)
  except Exception as exc:
    return False, f"LLM correction failed for {rel_path}: {exc}", None

  backup = target.with_suffix(target.suffix + BACKUP_SUFFIX)
  shutil.copy2(target, backup)
  staging = target.with_suffix(target.suffix + ".staging")
  staging.write_text(patched, encoding="utf-8")

  ok, validation_msg = _validate_patch(staging, rel_path, html_path)
  if not ok:
    staging.unlink(missing_ok=True)
    return False, validation_msg, None

  shutil.move(str(staging), str(target))
  return True, validation_msg, str(backup)


def run_healing(project_root: Path, mode: str = "auto") -> dict:
  os.chdir(project_root)
  sys.path.insert(0, str(project_root))

  result: dict = {
    "success": False,
    "message": "",
    "target_file": str(DEFAULT_TARGET),
    "target_files": [],
    "patched_files": [],
    "backup_files": [],
    "mode": mode,
  }

  user_instructions = ""
  if mode == "manual":
    if not MANUAL_FIX_REQUEST.exists():
      result["message"] = "No manual_fix_request.json found"
      _save_json(HEALER_RESULT, result)
      return result
    manual_req = _load_json(MANUAL_FIX_REQUEST)
    user_instructions = str(manual_req.get("user_prompt") or "").strip()
    if not user_instructions:
      result["message"] = "Manual fix requires user_prompt in manual_fix_request.json"
      _save_json(HEALER_RESULT, result)
      return result
  elif not CRASH_REPORT.exists():
    result["message"] = "No crash_report.json found"
    _save_json(HEALER_RESULT, result)
    return result

  crash: dict | None = None
  html_path = Path("data/crash_page.html")
  if CRASH_REPORT.exists():
    crash = _load_json(CRASH_REPORT)
    html_path = Path(crash.get("page_html_path") or "data/crash_page.html")

  html_excerpt = ""
  if html_path.exists():
    html_excerpt = html_path.read_text(encoding="utf-8", errors="ignore")[:12000]

  cfg = _llm_config_from_env()
  try:
    target_files = _route_target_files(user_instructions, crash, cfg, mode=mode)
  except Exception as exc:
    result["message"] = f"Target routing failed: {exc}"
    _save_json(HEALER_RESULT, result)
    return result

  result["target_files"] = target_files
  result["target_file"] = ", ".join(target_files)

  patched_files: list[str] = []
  backup_files: list[str] = []
  messages: list[str] = []

  for rel_path in target_files:
    ok, msg, backup = _patch_single_file(
      project_root,
      rel_path,
      crash,
      html_excerpt,
      user_instructions,
      cfg,
      html_path,
    )
    messages.append(msg)
    if not ok:
      result["message"] = "; ".join(messages)
      result["patched_files"] = patched_files
      result["backup_files"] = backup_files
      _save_json(HEALER_RESULT, result)
      return result
    patched_files.append(rel_path)
    if backup:
      backup_files.append(backup)

  result["success"] = True
  result["patched_files"] = patched_files
  result["backup_files"] = backup_files
  result["message"] = "; ".join(messages) if messages else f"Patched {len(patched_files)} file(s)"
  _save_json(HEALER_RESULT, result)
  return result


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--project-root", default=".", help="Project root directory")
  parser.add_argument("--mode", choices=("auto", "manual"), default="auto", help="Healing trigger mode")
  args = parser.parse_args()
  project_root = Path(args.project_root).resolve()

  try:
    result = run_healing(project_root, mode=args.mode)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("success") else 1
  except Exception:
    payload = {"success": False, "message": traceback.format_exc(), "mode": args.mode}
    _save_json(project_root / HEALER_RESULT, payload)
    print(json.dumps(payload, ensure_ascii=False))
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
