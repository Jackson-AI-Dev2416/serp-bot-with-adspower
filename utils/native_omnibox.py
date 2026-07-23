"""
OS-level omnibox control for AdsPower Chrome windows (Windows host / VM).

Binds to the browser process opened via Playwright CDP (ws_endpoint port → PID →
HWND). warmup_native_search() places the window on the primary monitor, opens the
address bar (F6/UIA/viewport), human-types, Enter, and verifies via Playwright.
"""

from __future__ import annotations

import ctypes
import random
import re
import subprocess
import threading
import time
from ctypes import wintypes
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

_OMNIBOX_LOCK = threading.Lock()

_BROWSER_TITLE_HINTS = (
  "chrome",
  "sunbrowser",
  "adspower",
  "chromium",
  "google",
)

_OMNIBOX_UIA_FRAGMENTS = (
  "address and search bar",
  "address and search",
  "search or type web address",
  "search google or type a url",
  "주소 및 검색 창",
  "주소 창 및 검색창",
  "검색 또는 url 입력",
)

_MONITOR_DEFAULTTOPRIMARY = 1

_SWP_SHOWWINDOW = 0x0040

# SunBrowser mobile chrome: tabs + omnibox strip (screen pixels, 100% scale).
_MOBILE_OMNIBOX_Y_OFFSET = 98


class _MONITORINFO(ctypes.Structure):
  _fields_ = [
    ("cbSize", wintypes.DWORD),
    ("rcMonitor", wintypes.RECT),
    ("rcWork", wintypes.RECT),
    ("dwFlags", wintypes.DWORD),
  ]


def _sleep(lo: float, hi: float) -> None:
  time.sleep(random.uniform(lo, hi))


def _type_text_human(text: str, lo: float, hi: float) -> None:
  import pyautogui
  import pyperclip

  _sleep(0.35, 0.75)
  pyautogui.hotkey("ctrl", "a")
  _sleep(max(0.04, lo * 0.5), max(0.1, hi * 0.6))
  pyautogui.press("backspace")
  _sleep(max(0.06, lo * 0.6), max(0.14, hi * 0.8))

  chunk = text or ""
  if not chunk:
    return

  for ch in chunk:
    pyperclip.copy(ch)
    pyautogui.hotkey("ctrl", "v")
    _sleep(lo, hi)


def _human_click(x: int, y: int) -> None:
  import pyautogui

  try:
    start_x, start_y = pyautogui.position()
  except Exception:
    start_x, start_y = x, y
  mid_x = int((start_x + x) / 2) + random.randint(-24, 24)
  mid_y = int((start_y + y) / 2) + random.randint(-16, 16)
  pyautogui.moveTo(mid_x, mid_y, duration=random.uniform(0.1, 0.22))
  pyautogui.moveTo(x, y, duration=random.uniform(0.07, 0.16))
  pyautogui.click()


def _score_window_title(title: str, profile_name: str, profile_id: str) -> int:
  title_lower = (title or "").lower()
  if not title_lower.strip():
    return 0
  score = 0
  name_lower = (profile_name or "").lower()
  id_lower = (profile_id or "").lower()
  if name_lower and name_lower in title_lower:
    score += 1000
  if id_lower and id_lower in title_lower:
    score += 800
  if any(hint in title_lower for hint in _BROWSER_TITLE_HINTS):
    score += 200
  if re.search(r"\bs-\d{3}\b", title_lower) and name_lower and name_lower in title_lower:
    score += 300
  return score


def _extract_ws_port(ws_endpoint: str) -> int:
  parsed = urlparse(ws_endpoint or "")
  if not parsed.port:
    raise ValueError(f"No port in CDP ws endpoint: {ws_endpoint!r}")
  return int(parsed.port)


def _pid_listening_on_port(port: int) -> Optional[int]:
  try:
    proc = subprocess.run(
      ["netstat", "-ano"],
      capture_output=True,
      text=True,
      timeout=15,
      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
  except Exception:
    return None
  if proc.returncode != 0:
    return None
  needle = f":{port}"
  for line in proc.stdout.splitlines():
    upper = line.upper()
    if "LISTENING" not in upper:
      continue
    if needle not in line:
      continue
    parts = line.split()
    if len(parts) < 5:
      continue
    try:
      return int(parts[-1])
    except ValueError:
      continue
  return None


def _primary_monitor_work_rect() -> Tuple[int, int, int, int]:
  """(left, top, width, height) of the primary monitor work area."""
  user32 = ctypes.windll.user32
  point = wintypes.POINT(0, 0)
  monitor = user32.MonitorFromPoint(point, _MONITOR_DEFAULTTOPRIMARY)
  info = _MONITORINFO()
  info.cbSize = ctypes.sizeof(_MONITORINFO)
  if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
  work = info.rcWork
  return (work.left, work.top, work.right - work.left, work.bottom - work.top)


def _point_on_primary_monitor(x: int, y: int) -> bool:
  px, py, pw, ph = _primary_monitor_work_rect()
  return px <= x < px + pw and py <= y < py + ph


def _window_rect(hwnd: int) -> Tuple[int, int, int, int]:
  rect = wintypes.RECT()
  if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
    return (0, 0, 0, 0)
  return (rect.left, rect.top, rect.right, rect.bottom)


def _window_area(hwnd: int) -> int:
  left, top, right, bottom = _window_rect(hwnd)
  return max(0, right - left) * max(0, bottom - top)


def _enum_windows_for_pid(pid: int) -> List[Tuple[int, str, int]]:
  user32 = ctypes.windll.user32
  WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
  matches: List[Tuple[int, str, int]] = []

  def callback(hwnd: int, _lparam: int) -> bool:
    if not user32.IsWindowVisible(hwnd):
      return True
    pid_out = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_out))
    if int(pid_out.value) != pid:
      return True
    length = user32.GetWindowTextLengthW(hwnd)
    title_buf = ctypes.create_unicode_buffer(length + 1)
    if length > 0:
      user32.GetWindowTextW(hwnd, title_buf, length + 1)
    title = title_buf.value or ""
    if user32.GetWindow(hwnd, 4):
      return True
    area = _window_area(hwnd)
    if area <= 0:
      return True
    matches.append((int(hwnd), title, area))
    return True

  user32.EnumWindows(WNDENUMPROC(callback), 0)
  return matches


def _pick_best_hwnd(
  candidates: List[Tuple[int, str, int]],
  profile_name: str,
  profile_id: str,
) -> int:
  if not candidates:
    raise RuntimeError("no visible top-level windows for browser PID")

  def sort_key(item: Tuple[int, str, int]) -> Tuple[int, int]:
    hwnd, title, area = item
    return (_score_window_title(title, profile_name, profile_id), area)

  hwnd, _title, _area = max(candidates, key=sort_key)
  return hwnd


def _window_hwnd(window) -> int:
  try:
    return int(window.handle)
  except Exception:
    return int(window.wrapper_object().handle)


def _window_on_primary_monitor(hwnd: int) -> bool:
  left, top, right, bottom = _window_rect(hwnd)
  if right <= left or bottom <= top:
    return False
  px, py, pw, ph = _primary_monitor_work_rect()
  cx = (left + right) // 2
  cy = (top + bottom) // 2
  return px <= cx < px + pw and py <= cy < py + ph


def _place_vm_window(hwnd: int, mobile: bool, log: Callable[[str], None]) -> None:
  """Move browser onto the primary monitor. Mobile: never shrink below current size."""
  user32 = ctypes.windll.user32
  user32.ShowWindow(hwnd, 9)  # SW_RESTORE
  _sleep(0.06, 0.14)

  px, py, pw, ph = _primary_monitor_work_rect()
  margin = 24
  left, top, right, bottom = _window_rect(hwnd)
  cur_w = max(1, right - left)
  cur_h = max(1, bottom - top)

  if mobile:
    width = cur_w
    height = cur_h
    if not _window_on_primary_monitor(hwnd) or left < px:
      x = px + margin
      y = py + margin
      if x + width > px + pw:
        x = max(px, px + pw - width - margin)
      if y + height > py + ph:
        y = max(py, py + ph - height - margin)
      user32.SetWindowPos(hwnd, 0, x, y, width, height, _SWP_SHOWWINDOW)
      log(
        f"[Omnibox] Mobile window moved to primary monitor ({x}, {y}) "
        f"— kept AdsPower size {width}x{height}"
      )
    else:
      log(
        f"[Omnibox] Mobile window already on primary ({left}, {top}) "
        f"{width}x{height} — size unchanged"
      )
    return

  width = min(1320, max(1100, pw - margin * 2))
  height = min(860, max(720, ph - margin * 2))
  x = px + margin
  y = py + margin
  user32.SetWindowPos(hwnd, 0, x, y, width, height, _SWP_SHOWWINDOW)
  log(f"[Omnibox] Desktop window placed on primary ({x}, {y}) {width}x{height}")


def _raise_cdp_window(page, log: Callable[[str], None]) -> None:
  try:
    page.bring_to_front()
    browser = page.context.browser
    if browser is None:
      return
    page_cdp = page.context.new_cdp_session(page)
    target_info = page_cdp.send("Target.getTargetInfo")
    target_id = (target_info.get("targetInfo") or {}).get("targetId")
    if not target_id:
      return
    browser_cdp = browser.new_browser_cdp_session()
    win = browser_cdp.send("Browser.getWindowForTarget", {"targetId": target_id})
    window_id = win.get("windowId")
    if window_id is not None:
      browser_cdp.send(
        "Browser.setWindowBounds",
        {"windowId": window_id, "bounds": {"windowState": "normal"}},
      )
    log("[Omnibox] CDP window raised")
  except Exception as exc:
    log(f"[Omnibox] CDP window raise warning: {exc}")


def _find_browser_window(
  page,
  ws_endpoint: str,
  profile_name: str,
  profile_id: str,
  log: Callable[[str], None],
):
  port = _extract_ws_port(ws_endpoint)
  pid = _pid_listening_on_port(port)
  if pid is None:
    raise RuntimeError(
      f"CDP browser process not found on port {port} "
      f"(profile '{profile_name}', id={profile_id or 'n/a'})"
    )

  candidates = _enum_windows_for_pid(pid)
  hwnd = _pick_best_hwnd(candidates, profile_name, profile_id)
  title = next((t for h, t, _ in candidates if h == hwnd), "")
  log(
    f"[Omnibox] Bound to AdsPower window via CDP port {port} "
    f"(pid={pid}, hwnd={hwnd}): {title[:120]}"
  )

  from pywinauto import Desktop

  for backend in ("uia", "win32"):
    try:
      window = Desktop(backend=backend).window(handle=hwnd)
      if window.exists(timeout=0.8):
        return window
    except Exception:
      continue
  raise RuntimeError(f"pywinauto could not attach to hwnd={hwnd} (pid={pid})")


def _focus_window(window, log: Callable[[str], None], *, hwnd: Optional[int] = None) -> None:
  hwnd = hwnd or _window_hwnd(window)
  user32 = ctypes.windll.user32
  try:
    user32.ShowWindow(hwnd, 9)
  except Exception:
    pass
  _sleep(0.08, 0.18)
  focused = False
  try:
    window.set_focus()
    focused = True
  except Exception:
    pass
  if not focused:
    try:
      user32.SetForegroundWindow(hwnd)
      user32.BringWindowToTop(hwnd)
      focused = True
      log("[Omnibox] Window focused via Win32 SetForegroundWindow")
    except Exception as exc:
      raise RuntimeError(f"set_focus failed: {exc}") from exc
  else:
    log("[Omnibox] Window focused")
  _sleep(0.2, 0.45)


def _mobile_omnibox_y_offset(window, page) -> int:
  offset = _MOBILE_OMNIBOX_Y_OFFSET
  try:
    rect = window.rectangle()
    vp = page.viewport_size
    vh = float((vp or {}).get("height") or 0)
    if vh > 0:
      chrome_band = max(72, rect.height() - int(vh))
      offset = max(82, min(118, chrome_band // 2 + 28))
  except Exception:
    pass
  return offset


def _viewport_omnibox_screen_point(window, page, mobile: bool) -> Tuple[int, int]:
  rect = window.rectangle()
  x = rect.left + max(12, int(rect.width() * 0.5))
  if mobile:
    y = rect.top + _mobile_omnibox_y_offset(window, page)
  else:
    y = rect.top + max(52, int(rect.height() * 0.048))
  return x, y


def _click_omnibox_uia(window, log: Callable[[str], None]) -> bool:
  for frag in _OMNIBOX_UIA_FRAGMENTS:
    pattern = f"(?i).*{re.escape(frag)}.*"
    for control_type in ("Edit", "ComboBox"):
      try:
        ctrl = window.child_window(title_re=pattern, control_type=control_type)
        if ctrl.exists(timeout=0.35):
          rect = ctrl.rectangle()
          cx = (rect.left + rect.right) // 2
          cy = (rect.top + rect.bottom) // 2
          if _point_on_primary_monitor(cx, cy):
            _human_click(cx, cy)
            log(f"[Omnibox] Address bar clicked (UIA '{frag}') at ({cx}, {cy})")
            return True
      except Exception:
        continue
  try:
    ctrl = window.child_window(class_name_re=".*Omnibox.*", control_type="Edit")
    if ctrl.exists(timeout=0.3):
      rect = ctrl.rectangle()
      cx = (rect.left + rect.right) // 2
      cy = (rect.top + rect.bottom) // 2
      if _point_on_primary_monitor(cx, cy):
        _human_click(cx, cy)
        log(f"[Omnibox] Address bar clicked (UIA Omnibox class) at ({cx}, {cy})")
        return True
  except Exception:
    pass
  return False


def _is_devtools_url(url: str) -> bool:
  return (url or "").strip().lower().startswith("devtools://")


def _url_on_serp_or_sorry(url: str) -> bool:
  lowered = (url or "").lower()
  if _is_devtools_url(lowered):
    return False
  return "/search" in lowered or "/sorry" in lowered


def _dismiss_devtools_panel(log: Callable[[str], None]) -> None:
  import pyautogui

  pyautogui.press("escape")
  _sleep(0.12, 0.28)
  log("[Omnibox] Escape — dismiss DevTools focus if open")


def _open_address_bar_hotkey(mobile: bool, log: Callable[[str], None]) -> None:
  import pyautogui

  if mobile:
    log("[Omnibox] Mobile: skipping F6 (opens DevTools); using viewport tap only")
    return
  pyautogui.hotkey("ctrl", "l")
  log("[Omnibox] Ctrl+L address bar hotkey (desktop)")
  _sleep(0.25, 0.48)


def _open_address_bar(
  window,
  page,
  mobile: bool,
  log: Callable[[str], None],
  *,
  attempt: int,
  hwnd: int,
) -> None:
  _dismiss_devtools_panel(log)
  try:
    page.bring_to_front()
    _sleep(0.1, 0.22)
  except Exception:
    pass

  if _click_omnibox_uia(window, log):
    _sleep(0.18, 0.35)
    return

  x, y = _viewport_omnibox_screen_point(window, page, mobile)
  if not _point_on_primary_monitor(x, y):
    _place_vm_window(hwnd, mobile, log)
    _sleep(0.12, 0.25)
    x, y = _viewport_omnibox_screen_point(window, page, mobile)

  if not _point_on_primary_monitor(x, y):
    if not mobile and attempt >= 2:
      _open_address_bar_hotkey(mobile, log)
      return
    raise RuntimeError(
      f"Address bar tap off primary monitor ({x}, {y}) — check display layout"
    )

  _human_click(x, y)
  _sleep(0.22, 0.45)
  log(f"[Omnibox] Address bar clicked (viewport) at ({x}, {y})")

  if mobile:
    _sleep(0.1, 0.2)
    _human_click(x, y)
  elif attempt >= 2:
    _open_address_bar_hotkey(mobile, log)


def _page_reached_search(page, timeout: float) -> bool:
  deadline = time.time() + max(2.0, float(timeout))
  while time.time() < deadline:
    try:
      context = page.context
      for candidate in list(context.pages):
        if candidate.is_closed():
          continue
        url = (candidate.url or "").lower()
        if _is_devtools_url(url):
          continue
        if _url_on_serp_or_sorry(url):
          try:
            candidate.bring_to_front()
          except Exception:
            pass
          return True
      if not page.is_closed() and _url_on_serp_or_sorry(page.url or ""):
        return True
      page.wait_for_load_state("domcontentloaded", timeout=800)
    except Exception:
      pass
    time.sleep(0.14)
  return False


def _run_omnibox_attempt(
  window,
  page,
  query: str,
  mobile: bool,
  type_lo: float,
  type_hi: float,
  log: Callable[[str], None],
  *,
  attempt: int,
  confirm_seconds: float,
  hwnd: int,
) -> bool:
  import pyautogui

  _open_address_bar(window, page, mobile, log, attempt=attempt, hwnd=hwnd)
  _type_text_human(query, type_lo, type_hi)
  _sleep(type_lo * 0.35, type_hi * 0.55)
  pyautogui.press("enter")
  log("[Omnibox] Enter submitted")
  if _page_reached_search(page, confirm_seconds):
    log("[Omnibox] Playwright confirmed SERP or sorry page")
    return True
  log(
    f"[Omnibox] Playwright tab still not on SERP after {confirm_seconds:.0f}s "
    f"(url={(page.url or '')[:90]})"
  )
  return False


def warmup_native_search(
  *,
  page,
  ws_endpoint: str,
  profile_name: str,
  profile_id: str,
  query: str,
  mobile: bool,
  type_lo: float,
  type_hi: float,
  log: Callable[[str], None],
) -> None:
  """
  VM-tuned blank-tab warm-up: primary monitor, click address bar, human type, Enter.
  Retries once if Playwright tab does not reach /search or /sorry.
  """
  import pyautogui

  pyautogui.FAILSAFE = False
  pyautogui.PAUSE = 0

  label = "Mobile" if mobile else "Desktop"
  confirm_seconds = 15.0 if mobile else 10.0
  log(f"[Omnibox] {label} warmup address bar search: {query}")

  with _OMNIBOX_LOCK:
    _raise_cdp_window(page, log)
    window = _find_browser_window(page, ws_endpoint, profile_name, profile_id, log)
    hwnd = _window_hwnd(window)
    _place_vm_window(hwnd, mobile, log)
    _sleep(0.1, 0.22)
    window = _find_browser_window(page, ws_endpoint, profile_name, profile_id, log)
    hwnd = _window_hwnd(window)
    _focus_window(window, log, hwnd=hwnd)

    for attempt in range(1, 3):
      if attempt > 1:
        log(f"[Omnibox] Warmup search retry {attempt}/2")
        window = _find_browser_window(page, ws_endpoint, profile_name, profile_id, log)
        hwnd = _window_hwnd(window)
        _place_vm_window(hwnd, mobile, log)
        _sleep(0.1, 0.22)
        _focus_window(window, log, hwnd=hwnd)
      if _run_omnibox_attempt(
        window,
        page,
        query,
        mobile,
        type_lo,
        type_hi,
        log,
        attempt=attempt,
        confirm_seconds=confirm_seconds,
        hwnd=hwnd,
      ):
        return

    raise RuntimeError(
      f"Warmup search did not open SERP for '{query}' after 2 native address bar attempt(s)"
    )


def native_omnibox_search(
  *,
  page,
  ws_endpoint: str,
  profile_name: str,
  profile_id: str,
  query: str,
  mobile: bool,
  type_lo: float,
  type_hi: float,
  log: Callable[[str], None],
) -> None:
  """
  Focus the AdsPower profile window (CDP port-bound) and submit via Chrome omnibox.
  Thread-safe: serializes pyautogui across concurrent profile workers.
  """
  import pyautogui

  pyautogui.FAILSAFE = False
  pyautogui.PAUSE = 0

  label = "Mobile" if mobile else "Desktop"
  confirm_seconds = 12.0 if mobile else 8.0
  log(f"[Omnibox] {label} native address bar search: {query}")

  with _OMNIBOX_LOCK:
    _raise_cdp_window(page, log)
    window = _find_browser_window(page, ws_endpoint, profile_name, profile_id, log)
    hwnd = _window_hwnd(window)
    if mobile:
      _place_vm_window(hwnd, mobile, log)
      window = _find_browser_window(page, ws_endpoint, profile_name, profile_id, log)
      hwnd = _window_hwnd(window)
    _focus_window(window, log, hwnd=hwnd)
    if not _run_omnibox_attempt(
      window,
      page,
      query,
      mobile,
      type_lo,
      type_hi,
      log,
      attempt=1,
      confirm_seconds=confirm_seconds,
      hwnd=hwnd,
    ):
      log("[Omnibox] Retrying address bar search once")
      window = _find_browser_window(page, ws_endpoint, profile_name, profile_id, log)
      hwnd = _window_hwnd(window)
      _place_vm_window(hwnd, mobile, log)
      _focus_window(window, log, hwnd=hwnd)
      if not _run_omnibox_attempt(
        window,
        page,
        query,
        mobile,
        type_lo,
        type_hi,
        log,
        attempt=2,
        confirm_seconds=confirm_seconds,
        hwnd=hwnd,
      ):
        raise RuntimeError(
          f"SERP did not open after native omnibox search for '{query}'"
        )
