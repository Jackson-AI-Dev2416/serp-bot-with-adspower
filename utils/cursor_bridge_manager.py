"""
Shared Cursor SDK bridge lifecycle for the AI Fix tab.

Reuses one bridge per workspace and reconnects when localhost RPC fails
(WinError 10061 / connection refused).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from utils.cursor_sdk_windows_patch import apply_cursor_sdk_windows_patch

apply_cursor_sdk_windows_patch()

try:
  import httpx
  from cursor_sdk import Client
  from cursor_sdk._client import close_default_client
  from cursor_sdk.errors import CursorAgentError, CursorSDKError
  from cursor_sdk.types import LocalAgentOptions

  CURSOR_BRIDGE_AVAILABLE = True
except ImportError:
  CURSOR_BRIDGE_AVAILABLE = False


def is_bridge_connection_error(exc: BaseException) -> bool:
  text = str(exc).lower()
  if "10061" in text or "actively refused" in text or "connection refused" in text:
    return True
  if "connecterror" in text and "bridge" in text:
    return True
  if isinstance(exc, CursorAgentError) and exc.is_retryable:
    return True
  cause = getattr(exc, "__cause__", None)
  if cause is not None and cause is not exc:
    return is_bridge_connection_error(cause)
  return False


class CursorBridgeManager:
  """Thread-safe singleton bridge for Cursor local agents."""

  _instance: CursorBridgeManager | None = None
  _instance_lock = threading.Lock()

  def __init__(self) -> None:
    self._lock = threading.Lock()
    self._client: Client | None = None
    self._workspace: str | None = None

  @classmethod
  def shared(cls) -> CursorBridgeManager:
    with cls._instance_lock:
      if cls._instance is None:
        cls._instance = cls()
      return cls._instance

  def get_client(self, workspace: Path, *, force_new: bool = False) -> Client:
    if not CURSOR_BRIDGE_AVAILABLE:
      raise RuntimeError("cursor-sdk is not installed")

    workspace_str = str(workspace.resolve())
    with self._lock:
      if not force_new and self._client is not None and self._workspace == workspace_str:
        if self._client_is_alive(self._client):
          return self._client
        self._close_unlocked()

      self._close_unlocked()
      close_default_client()
      self._client = self._launch_client(workspace_str)
      self._workspace = workspace_str
      return self._client

  def reset(self) -> None:
    with self._lock:
      self._close_unlocked()
    close_default_client()

  def shutdown(self) -> None:
    self.reset()

  def _launch_client(self, workspace_str: str) -> Client:
    local = LocalAgentOptions(cwd=workspace_str)
    last_error: Exception | None = None

    for launch_attempt in range(1, 4):
      http_client = httpx.Client(trust_env=False, timeout=httpx.Timeout(60.0, connect=30.0))
      client: Client | None = None
      try:
        client = Client.launch_bridge(
          workspace=workspace_str,
          timeout=45,
          local=local,
          http_client=http_client,
        )
        for _ in range(24):
          try:
            client.ping()
            return client
          except Exception as exc:
            last_error = exc
            time.sleep(0.25)
        raise RuntimeError(f"Bridge ping timed out: {last_error}")
      except Exception as exc:
        last_error = exc
        if client is not None:
          try:
            client.close()
          except Exception:
            pass
        close_default_client()
        time.sleep(0.5 * launch_attempt)
      finally:
        if client is None:
          http_client.close()

    raise RuntimeError(f"Cursor bridge failed to start: {last_error}") from last_error

  def _client_is_alive(self, client: Client) -> bool:
    try:
      client.ping()
      return True
    except Exception:
      return False

  def _close_unlocked(self) -> None:
    if self._client is None:
      return
    try:
      self._client.close()
    except Exception:
      pass
    self._client = None
    self._workspace = None
