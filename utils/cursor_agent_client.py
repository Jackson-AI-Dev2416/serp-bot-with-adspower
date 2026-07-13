"""
PyQt6 bridge for Cursor SDK local agent runs with streaming updates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from utils.cursor_bridge_manager import (
  CURSOR_BRIDGE_AVAILABLE,
  CursorBridgeManager,
  is_bridge_connection_error,
)
from utils.cursor_models import normalize_cursor_model
from utils.cursor_sdk_windows_patch import apply_cursor_sdk_windows_patch

apply_cursor_sdk_windows_patch()

try:
  from cursor_sdk import Agent, AgentOptions, CursorAgentError, LocalAgentOptions
  from cursor_sdk.types import (
    TextDeltaUpdate,
    ThinkingDeltaUpdate,
    ToolCallCompletedUpdate,
    ToolCallStartedUpdate,
  )
  CURSOR_SDK_AVAILABLE = True
except ImportError:
  CURSOR_SDK_AVAILABLE = False

MAX_BRIDGE_ATTEMPTS = 3


def _tool_label(tool_call: dict) -> str:
  name = str(tool_call.get("name") or tool_call.get("toolName") or "tool")
  args = tool_call.get("args") or tool_call.get("arguments") or {}
  if isinstance(args, str):
    try:
      args = json.loads(args)
    except json.JSONDecodeError:
      args = {"raw": args}
  if not isinstance(args, dict):
    args = {}
  path = args.get("path") or args.get("file") or args.get("target_file")
  if path:
    return f"{name} → {path}"
  command = args.get("command")
  if command:
    return f"{name} → {command}"
  return name


class CursorAgentRunThread(QThread):
  """Runs one Cursor agent turn off the GUI thread."""

  text_delta = pyqtSignal(str)
  status_line = pyqtSignal(str)
  completed = pyqtSignal(bool, str, str)

  def __init__(
    self,
    *,
    project_root: Path,
    api_key: str,
    model: str,
    prompt: str,
    agent_id: str = "",
    parent=None,
  ):
    super().__init__(parent)
    self.project_root = project_root.resolve()
    self.api_key = api_key.strip()
    self.model = normalize_cursor_model(model)
    self.prompt = prompt.strip()
    self.agent_id = agent_id.strip()
    self._cancel_requested = False
    self._bridge = CursorBridgeManager.shared()

  def request_cancel(self) -> None:
    self._cancel_requested = True

  def _agent_options(self) -> AgentOptions:
    return AgentOptions(
      api_key=self.api_key,
      model=self.model,
      local=LocalAgentOptions(cwd=str(self.project_root)),
    )

  def _run_agent_turn(self, *, force_new_bridge: bool) -> tuple[bool, str, str]:
    client = self._bridge.get_client(self.project_root, force_new=force_new_bridge)
    options = self._agent_options()

    if self.agent_id:
      self.status_line.emit(f"Resuming agent {self.agent_id}…")
      agent_ctx = Agent.resume(self.agent_id, options, client=client)
    else:
      self.status_line.emit("Starting Cursor local agent…")
      agent_ctx = Agent.create(options, client=client)

    with agent_ctx as agent:
      resolved_agent_id = str(getattr(agent, "agent_id", "") or "")
      self.status_line.emit(f"Agent ready ({resolved_agent_id or 'local'})")
      run = agent.send(self.prompt)
      self.status_line.emit(f"Run started ({run.id})")

      for event in run.events():
        if self._cancel_requested:
          if run.supports("cancel"):
            run.cancel()
          break

        update = getattr(event, "interaction_update", None)
        if update is not None:
          if isinstance(update, TextDeltaUpdate):
            if update.text:
              self.text_delta.emit(update.text)
          elif isinstance(update, ThinkingDeltaUpdate):
            if update.text:
              self.text_delta.emit(update.text)
          elif isinstance(update, ToolCallStartedUpdate):
            self.status_line.emit(f"▶ {_tool_label(update.tool_call)}")
          elif isinstance(update, ToolCallCompletedUpdate):
            self.status_line.emit(f"✓ {_tool_label(update.tool_call)}")
          continue

        sdk_message = getattr(event, "sdk_message", None)
        if sdk_message is None:
          continue
        if getattr(sdk_message, "type", "") != "assistant":
          continue
        content = getattr(getattr(sdk_message, "message", None), "content", ())
        for block in content:
          text = getattr(block, "text", "")
          if text:
            self.text_delta.emit(text)

      result = run.wait()
      success = result.status == "finished"
      message = result.result or result.status
      if success:
        message = message or "Done. Restart the app if Python/UI files were changed."
      elif result.status == "error":
        message = message or "Agent run failed."
      return success, message, resolved_agent_id or self.agent_id

  def run(self) -> None:
    if not CURSOR_SDK_AVAILABLE or not CURSOR_BRIDGE_AVAILABLE:
      self.completed.emit(False, "cursor-sdk is not installed. Run: pip install cursor-sdk", "")
      return
    if not self.api_key:
      self.completed.emit(
        False,
        "Cursor API Key is required. Get one at Cursor Dashboard → Integrations.",
        "",
      )
      return
    if not self.prompt:
      self.completed.emit(False, "Prompt is empty.", "")
      return

    last_error = "Cursor agent failed"
    for attempt in range(1, MAX_BRIDGE_ATTEMPTS + 1):
      force_new_bridge = attempt > 1
      if force_new_bridge:
        self.status_line.emit(f"Reconnecting Cursor bridge ({attempt}/{MAX_BRIDGE_ATTEMPTS})…")
        self._bridge.reset()

      try:
        success, message, agent_id = self._run_agent_turn(force_new_bridge=force_new_bridge)
        self.completed.emit(success, message, agent_id)
        return
      except CursorAgentError as exc:
        last_error = f"Cursor API error: {exc}"
        if is_bridge_connection_error(exc) and attempt < MAX_BRIDGE_ATTEMPTS:
          continue
        hint = ""
        if "401" in str(exc) or "auth" in str(exc).lower():
          hint = " Check your Cursor API Key (Dashboard → Integrations)."
        self.completed.emit(False, f"{last_error}{hint}", self.agent_id)
        return
      except OSError as exc:
        last_error = f"Agent error: {exc}"
        if sys.platform == "win32" and getattr(exc, "winerror", None) in (10038, 10061):
          if attempt < MAX_BRIDGE_ATTEMPTS:
            continue
          if exc.winerror == 10038:
            last_error = "Windows socket error while starting Cursor bridge. Restart the app and try again."
          else:
            last_error = "Cursor bridge connection refused. Retried — restart the app if this persists."
        self.completed.emit(False, last_error, self.agent_id)
        return
      except Exception as exc:
        last_error = f"Agent error: {exc}"
        if is_bridge_connection_error(exc) and attempt < MAX_BRIDGE_ATTEMPTS:
          continue
        self.completed.emit(False, last_error, self.agent_id)
        return

    self.completed.emit(False, last_error, self.agent_id)


class CursorChatController:
  """Tracks one conversational agent session for the AI Fix tab."""

  def __init__(self, project_root: Path):
    self.project_root = project_root.resolve()
    self._agent_id = ""
    self._worker: CursorAgentRunThread | None = None
    self._bridge = CursorBridgeManager.shared()

  @property
  def agent_id(self) -> str:
    return self._agent_id

  def is_running(self) -> bool:
    return bool(self._worker and self._worker.isRunning())

  def reset_session(self) -> None:
    self._agent_id = ""
    if self._worker and self._worker.isRunning():
      self._worker.request_cancel()
    self._bridge.reset()

  def restore_session(self, agent_id: str) -> None:
    self._agent_id = (agent_id or "").strip()

  def shutdown(self) -> None:
    if self._worker and self._worker.isRunning():
      self._worker.request_cancel()
    self._bridge.shutdown()

  def start_run(
    self,
    *,
    api_key: str,
    model: str,
    prompt: str,
    parent=None,
  ) -> CursorAgentRunThread | None:
    if self.is_running():
      return None
    worker = CursorAgentRunThread(
      project_root=self.project_root,
      api_key=api_key,
      model=model,
      prompt=prompt,
      agent_id=self._agent_id,
      parent=parent,
    )
    worker.completed.connect(self._on_completed)
    self._worker = worker
    worker.start()
    return worker

  def _on_completed(self, success: bool, message: str, agent_id: str) -> None:
    if agent_id:
      self._agent_id = agent_id
    self._worker = None
