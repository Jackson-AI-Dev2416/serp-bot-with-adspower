"""
Windows compatibility patch for cursor-sdk bridge discovery.

On Windows, selectors.DefaultSelector() only supports sockets, not subprocess
pipe fds. cursor-sdk's _read_discovery registers stderr on a selector, which
raises WinError 10038. This module replaces it with a polling loop.
"""

from __future__ import annotations

import codecs
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any

_PATCHED = False


def apply_cursor_sdk_windows_patch() -> None:
  global _PATCHED
  if _PATCHED or sys.platform != "win32":
    return

  try:
    import cursor_sdk._bridge as bridge_mod
  except ImportError:
    return

  if getattr(bridge_mod, "_serp_bot_windows_discovery_patch", False):
    _PATCHED = True
    return

  parse_discovery_line = bridge_mod.parse_discovery_line
  CursorSDKError = bridge_mod.CursorSDKError

  def _read_discovery_windows(
    process: subprocess.Popen[str], timeout: float
  ) -> Mapping[str, Any]:
    if process.stderr is None:
      raise CursorSDKError("Bridge process stderr is unavailable")

    stderr_fd = process.stderr.fileno()
    was_blocking = os.get_blocking(stderr_fd)
    os.set_blocking(stderr_fd, False)
    try:
      decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
      deadline = time.monotonic() + timeout
      stderr_lines: list[str] = []
      pending = ""

      def drain_available() -> Mapping[str, Any] | None:
        nonlocal pending
        while True:
          try:
            chunk = os.read(stderr_fd, 8192)
          except BlockingIOError:
            return None
          if not chunk:
            final_text = decoder.decode(b"", final=True)
            if final_text:
              pending += final_text
            if pending:
              line = pending
              pending = ""
              stderr_lines.append(line)
              return parse_discovery_line(line)
            return None
          pending += decoder.decode(chunk)
          while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line += "\n"
            stderr_lines.append(line)
            discovery = parse_discovery_line(line)
            if discovery is not None:
              return discovery

      while time.monotonic() < deadline:
        discovery = drain_available()
        if discovery is not None:
          return discovery
        exit_code = process.poll()
        if exit_code is not None:
          discovery = drain_available()
          if discovery is not None:
            return discovery
          raise CursorSDKError(
            f"Bridge exited before discovery with status {exit_code}: "
            + "".join(stderr_lines)
            + pending
          )
        time.sleep(0.05)
      raise CursorSDKError("Timed out waiting for bridge discovery")
    finally:
      os.set_blocking(stderr_fd, was_blocking)

  bridge_mod._read_discovery = _read_discovery_windows
  bridge_mod._serp_bot_windows_discovery_patch = True
  _PATCHED = True
