"""Passive proxy wire-byte metering via CDP Network events (no extra HTTP)."""

from __future__ import annotations

from typing import Callable, Optional

from playwright.sync_api import Page

_FLUSH_THRESHOLD_BYTES = 128 * 1024


class WireTrafficMeter:
  """
  Counts encoded upload/download bytes observed by Chromium's network stack.

  Uses CDP listeners only — never re-fetches URLs or reads response bodies.

  Classification is session-phase based:
  - before mark_site_visit_started(): all bytes → other
  - after successful target open: all bytes → site (target)
  """

  def __init__(
    self,
    *,
    on_flush: Optional[Callable[[], None]] = None,
  ) -> None:
    self._on_flush = on_flush
    self._site_visit_started = False
    self.wire_download_bytes = 0
    self.wire_upload_bytes = 0
    self.wire_target_bytes = 0
    self.wire_other_bytes = 0
    self._pages_attached: set[int] = set()
    self._cdp_sessions: list[object] = []
    self._request_meta: dict[str, tuple[str, int]] = {}
    self._pending_wire = 0
    self._pending_target = 0
    self._pending_other = 0

  @property
  def count_as_site(self) -> bool:
    return self._site_visit_started

  def mark_site_visit_started(self) -> None:
    """After target click/touch succeeds, count all further wire bytes as site traffic."""
    self._site_visit_started = True

  @property
  def wire_total_bytes(self) -> int:
    return self.wire_download_bytes + self.wire_upload_bytes

  def attach(self, page: Page) -> bool:
    if page is None or page.is_closed():
      return False
    page_key = id(page)
    if page_key in self._pages_attached:
      return True
    try:
      session = page.context.new_cdp_session(page)
      session.send("Network.enable")
      session.on("Network.requestWillBeSent", self._on_request_will_be_sent)
      session.on("Network.loadingFinished", self._on_loading_finished)
      self._cdp_sessions.append(session)
      self._pages_attached.add(page_key)
      return True
    except Exception:
      return False

  def take_pending_delta(self, *, force: bool = False) -> tuple[int, int, int]:
    if not force and self._pending_wire < _FLUSH_THRESHOLD_BYTES:
      return 0, 0, 0
    delta = self._pending_wire
    target = self._pending_target
    other = self._pending_other
    self._pending_wire = 0
    self._pending_target = 0
    self._pending_other = 0
    return delta, target, other

  def _record_wire(self, size_bytes: int) -> None:
    if size_bytes <= 0:
      return
    if self._site_visit_started:
      self.wire_target_bytes += size_bytes
      self._pending_target += size_bytes
    else:
      self.wire_other_bytes += size_bytes
      self._pending_other += size_bytes
    self._pending_wire += size_bytes
    if self._on_flush and self._pending_wire >= _FLUSH_THRESHOLD_BYTES:
      try:
        self._on_flush()
      except Exception:
        pass

  def _on_request_will_be_sent(self, params: dict) -> None:
    request = params.get("request") or {}
    rid = str(params.get("requestId") or "")
    url = str(request.get("url") or "")
    upload = self._estimate_request_upload(request)
    if rid:
      self._request_meta[rid] = (url, upload)
    if upload <= 0:
      return
    self.wire_upload_bytes += upload
    self._record_wire(upload)

  def _on_loading_finished(self, params: dict) -> None:
    rid = str(params.get("requestId") or "")
    encoded = int(params.get("encodedDataLength") or 0)
    if rid in self._request_meta:
      self._request_meta.pop(rid, None)
    elif rid:
      self._request_meta.pop(rid, None)
    if encoded <= 0:
      return
    self.wire_download_bytes += encoded
    self._record_wire(encoded)

  @staticmethod
  def _estimate_request_upload(request: dict) -> int:
    total = 0
    post = request.get("postData") or ""
    if isinstance(post, str) and post:
      total += len(post.encode("utf-8", errors="ignore"))
    headers = request.get("headers") or {}
    if isinstance(headers, dict):
      for key, value in headers.items():
        total += len(str(key)) + len(str(value)) + 4
    method = str(request.get("method") or "GET")
    url = str(request.get("url") or "")
    total += len(method) + len(url) + 24
    return max(0, total)
