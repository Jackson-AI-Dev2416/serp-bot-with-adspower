"""
GUI-side launcher for the decoupled self-healing subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config.bot_config import BotConfig
from core.profile_status import UiStatusKey

HEALER_RESULT_PATH = Path("data/healer_result.json")
MANUAL_FIX_REQUEST_PATH = Path("data/manual_fix_request.json")
MANUAL_PROFILE_ID = "manual"


class SelfHealerWatchThread(QThread):
  """Waits for subprocess completion and reads healer_result.json."""

  finished_healing = pyqtSignal(str, bool, str)

  def __init__(self, profile_id: str, process: subprocess.Popen, project_root: Path, parent=None):
    super().__init__(parent)
    self.profile_id = profile_id
    self.process = process
    self.project_root = project_root

  def run(self) -> None:
    success = False
    message = "Self-healer subprocess failed"
    self._result_payload: dict = {}
    try:
      self.process.wait(timeout=600)
      result_path = self.project_root / HEALER_RESULT_PATH
      if result_path.exists():
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        self._result_payload = payload
        success = bool(payload.get("success"))
        message = str(payload.get("message") or message)
      else:
        message = f"Self-healer exited with code {self.process.returncode}"
    except subprocess.TimeoutExpired:
      self.process.kill()
      message = "Self-healer timed out after 600s"
    except Exception as exc:
      message = str(exc)

    detail = message
    if self._result_payload.get("patched_files"):
      files = ", ".join(self._result_payload["patched_files"])
      detail = f"{message} | patched: {files}"
    self.finished_healing.emit(self.profile_id, success, detail)


class SelfHealer(QObject):
  """
  Spawns `python -m utils.self_healer_worker` so LLM patching and validation
  never run inside the PyQt6 main thread.
  """

  log = pyqtSignal(str)
  healing_started = pyqtSignal(str, str, str)
  healing_finished = pyqtSignal(str, str, str)

  def __init__(self, project_root: Path | None = None, parent=None):
    super().__init__(parent)
    self.project_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    self._watchers: dict[str, SelfHealerWatchThread] = {}

  def is_healing(self, profile_id: str) -> bool:
    watcher = self._watchers.get(profile_id)
    return bool(watcher and watcher.isRunning())

  def is_any_healing(self) -> bool:
    return any(watcher.isRunning() for watcher in self._watchers.values())

  def trigger_healing(self, profile_id: str, config: BotConfig) -> bool:
    crash_path = self.project_root / "data" / "crash_report.json"
    if not crash_path.exists():
      self.log.emit("[Self-Healer] No crash_report.json — skipping auto-correction")
      return False
    return self._launch(profile_id, config, mode="auto", log_label="auto-correction")

  def trigger_manual_healing(self, profile_id: str, config: BotConfig, user_prompt: str) -> bool:
    prompt = user_prompt.strip()
    if not prompt:
      self.log.emit("[Self-Healer] Manual fix requires a non-empty prompt")
      return False
    if not config.llm_api_key:
      self.log.emit("[Self-Healer] LLM API Key is required for manual fix")
      return False

    request_path = self.project_root / MANUAL_FIX_REQUEST_PATH
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(
      json.dumps(
        {
          "user_prompt": prompt,
          "profile_id": profile_id,
          "mode": "manual",
        },
        ensure_ascii=False,
        indent=2,
      ),
      encoding="utf-8",
    )
    return self._launch(profile_id, config, mode="manual", log_label="manual fix")

  def _launch(self, profile_id: str, config: BotConfig, mode: str, log_label: str) -> bool:
    if self.is_any_healing():
      self.log.emit("[Self-Healer] Already running — wait for the current fix to finish")
      return False

    env = os.environ.copy()
    if config.llm_api_key:
      env["OPENAI_API_KEY"] = config.llm_api_key
      env["LLM_API_KEY"] = config.llm_api_key
    if config.llm_base_url:
      env["OPENAI_BASE_URL"] = config.llm_base_url
      env["LLM_BASE_URL"] = config.llm_base_url
    if config.llm_model:
      env["OPENAI_MODEL"] = config.llm_model
      env["LLM_MODEL"] = config.llm_model

    cmd = [
      sys.executable,
      "-m",
      "utils.self_healer_worker",
      "--project-root",
      str(self.project_root),
      "--mode",
      mode,
    ]

    self.healing_started.emit(profile_id, UiStatusKey.SELF_HEALING.value, UiStatusKey.SELF_HEALING.value)
    self.log.emit(f"[Self-Healer] Launching {log_label} subprocess (profile={profile_id})")

    process = subprocess.Popen(
      cmd,
      cwd=str(self.project_root),
      env=env,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
    )

    watcher = SelfHealerWatchThread(profile_id, process, self.project_root)
    watcher.finished_healing.connect(self._on_watch_finished)
    self._watchers[profile_id] = watcher
    watcher.start()
    return True

  def _on_watch_finished(self, profile_id: str, success: bool, message: str) -> None:
    self._watchers.pop(profile_id, None)
    if success:
      self.log.emit(f"[Self-Healer] Fix applied — {message}")
      self.log.emit("[Self-Healer] Restart the app to load code changes.")
      self.healing_finished.emit(profile_id, UiStatusKey.CLOSED.value, UiStatusKey.CLOSED.value)
    else:
      self.log.emit(f"[Self-Healer] Fix failed: {message}")
      self.healing_finished.emit(profile_id, UiStatusKey.ERROR.value, UiStatusKey.ERROR.value)
