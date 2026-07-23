import random
import threading
import time

from PyQt6.QtCore import QThread, pyqtSignal

from config.bot_config import BotConfig
from core.profile_status import ProfileStatus, short_error_detail
from services.adspower_manager import AdsPowerManager, ProfileSpec
from services.serp_bot import SerpBot
from utils.keyword_exclusion import KeywordExclusionStore
from utils.pair_rotation import PairRotationStore
from utils.csv_logger import log_session_failure, outcome_to_failure_url
from utils.session_log import append_session_log
from utils.user_log import build_pre_delete_log_line


class ProfileWorkerThread(QThread):
  _SESSION_STALL_SEC = 600.0
  _PROFILE_STALL_POLL_SEC = 30.0

  log = pyqtSignal(str)
  profile_update = pyqtSignal(str, str, str)
  traffic_update = pyqtSignal(str, str, "qint64", "qint64", "qint64", "qint64", "qint64", "qint64")
  target_click_logged = pyqtSignal()
  captcha_stat = pyqtSignal(str)
  keyword_excluded = pyqtSignal(str)
  profiles_changed = pyqtSignal()
  profile_deleted = pyqtSignal(str)
  finished_profile = pyqtSignal(str, str)

  def __init__(
    self,
    config: BotConfig,
    profile: ProfileSpec,
    stop_event: threading.Event,
    keyword_exclusion: KeywordExclusionStore,
    pair_rotation: PairRotationStore | None = None,
    failure_callback=None,
    parent=None,
  ):
    super().__init__(parent)
    self.config = config
    self.profile = profile
    self.stop_event = stop_event
    self.keyword_exclusion = keyword_exclusion
    self.pair_rotation = pair_rotation or PairRotationStore()
    self._failure_callback = failure_callback
    self._last_progress_at = 0.0
    self._stall_stop = threading.Event()

  def _touch_progress(self) -> None:
    self._last_progress_at = time.time()

  def _start_stall_watchdog(self, adspower: AdsPowerManager) -> None:
    self._stall_stop.clear()
    self._touch_progress()

    def _poll() -> None:
      while not self._stall_stop.is_set():
        if self.stop_event.is_set():
          return
        if time.time() - self._last_progress_at >= self._SESSION_STALL_SEC:
          self._emit_log(
            f"[{self.profile.name}] Session stall watchdog: no progress for "
            f"{int(self._SESSION_STALL_SEC // 60)} min — forcing stop"
          )
          self.stop_event.set()
          try:
            adspower.stop_profile(self.profile.profile_id)
          except Exception as exc:
            self._emit_log(f"[{self.profile.name}] Stall watchdog AdsPower stop failed: {exc}")
          return
        self._stall_stop.wait(self._PROFILE_STALL_POLL_SEC)

    threading.Thread(
      target=_poll,
      daemon=True,
      name=f"stall-{self.profile.name}",
    ).start()

  def _stop_stall_watchdog(self) -> None:
    self._stall_stop.set()

  def _emit_log(self, message: str) -> None:
    self._touch_progress()
    append_session_log(message)
    self.log.emit(message)

  def _emit_profile_update(
    self,
    status: ProfileStatus,
    cooldown: int = 0,
    detail: str = "",
  ) -> None:
    key, text = status.to_ui(cooldown_seconds=cooldown, detail=detail)
    self.profile_update.emit(self.profile.profile_id, key, text)

  def _emit_ui_status(self, status_key: str, display_text: str) -> None:
    self._touch_progress()
    self.profile_update.emit(self.profile.profile_id, status_key, display_text)

  def run(self) -> None:
    adspower = AdsPowerManager(
      self.config.adspower_url,
      self.config.adspower_api_key,
      self._emit_log,
    )
    serp_bot = SerpBot(self.config, self._emit_log)
    keyword, domain = self._assign_profile_pair()
    self.profile.assigned_keyword = keyword
    self.profile.assigned_domain = domain
    proxy_key = self._proxy_key()
    self._start_stall_watchdog(adspower)

    def on_failure(failed_profile: ProfileSpec, context: str, exc: BaseException) -> None:
      self._touch_progress()
      self._emit_profile_update(
        ProfileStatus.ERROR,
        detail=short_error_detail(exc, context),
      )
      if self._failure_callback:
        self._failure_callback(failed_profile.profile_id)

    outcome = "error"
    max_tunnel_attempts = 2
    try:
      for attempt in range(1, max_tunnel_attempts + 1):
        try:
          self._emit_profile_update(ProfileStatus.LAUNCHING)
          ws_endpoint = adspower.start_profile(self.profile.profile_id)
          outcome = serp_bot.run_session(
            ws_endpoint,
            self.profile,
            stop_event=self.stop_event,
            assigned_keyword=keyword,
            assigned_domain=domain,
            on_ui_status=self._emit_ui_status,
            on_traffic=lambda delta, delta_target, delta_other, wire_delta, wire_target, wire_other: self.traffic_update.emit(
              self.profile.profile_id,
              proxy_key,
              int(delta),
              int(delta_target),
              int(delta_other),
              int(wire_delta),
              int(wire_target),
              int(wire_other),
            ),
            on_failure=on_failure,
            on_target_click=self.target_click_logged.emit,
            on_captcha_stat=self.captcha_stat.emit,
          )
        except Exception as exc:
          self._emit_log(f"[{self.profile.name}] Error: {exc}")
          self._emit_profile_update(
            ProfileStatus.ERROR,
            detail=short_error_detail(exc, "launch"),
          )
          if self._failure_callback:
            self._failure_callback(self.profile.profile_id)
          outcome = "error"
        finally:
          adspower.stop_profile(self.profile.profile_id)

        if outcome != "tunnel_error":
          break
        if attempt >= max_tunnel_attempts or self.stop_event.is_set():
          break
        wait_seconds = random.uniform(max(8.0, float(self.config.launch_interval_min)), max(12.0, float(self.config.launch_interval_max)))
        self._emit_log(
          f"[{self.profile.name}] Tunnel connection failed. "
          f"Waiting {wait_seconds:.1f}s before retry ({attempt}/{max_tunnel_attempts})."
        )
        self.stop_event.wait(wait_seconds)

      if self._should_delete_profile_after_run(outcome):
        pre_delete_line = build_pre_delete_log_line(
          self.profile.name,
          outcome,
          keyword or self.profile.assigned_keyword or "",
          domain or self.profile.assigned_domain or "",
        )
        if pre_delete_line:
          self._emit_log(pre_delete_line)
        failure_url = outcome_to_failure_url(outcome)
        if failure_url:
          log_path = (self.config.session_click_log_path or "").strip()
          if log_path:
            log_session_failure(
              log_path,
              profile_name=self.profile.name,
              device=self.profile.device_label,
              keyword=(keyword or self.profile.assigned_keyword or "").strip(),
              site=(domain or self.profile.assigned_domain or "").strip(),
              failure_url=failure_url,
            )
        if self._delete_profile_with_retry(adspower, reason=f"outcome:{outcome}"):
          self._emit_log(
            f"[{self.profile.name}] Profile deleted after run ({outcome}); "
            "proxy entry kept for future reuse."
          )
          self.profiles_changed.emit()
          self.profile_deleted.emit(self.profile.profile_id)
        else:
          self._emit_log(f"[{self.profile.name}] Profile delete failed after retries ({outcome})")

      self.finished_profile.emit(self.profile.profile_id, outcome)
    finally:
      self._stop_stall_watchdog()

  @staticmethod
  def _should_delete_profile_after_run(outcome: str) -> bool:
    return outcome in ("success", "not_found", "failed", "error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error", "proxy_connect_failed")

  def _assign_profile_pair(self) -> tuple[str, str]:
    keywords = [keyword.strip() for keyword in (self.config.keywords or []) if keyword and keyword.strip()]
    domains = self.config.get_target_domains()
    if not keywords or not domains:
      return ("", "")

    if (self.profile.assigned_keyword or "").strip() and (self.profile.assigned_domain or "").strip():
      return self.profile.assigned_keyword.strip(), self.profile.assigned_domain.strip()

    keyword, domain = self.pair_rotation.allocate_pair(keywords, domains)
    self._emit_log(
      f"[{self.profile.name}] Assigned pair: '{keyword}' → {domain}"
    )
    return keyword, domain

  def _proxy_key(self) -> str:
    if self.profile.proxy_host in ("", "—"):
      return "—"
    user = (self.profile.proxy_user or "").strip()
    auth_prefix = f"{user}@" if user else ""
    if self.profile.proxy_port:
      return f"{auth_prefix}{self.profile.proxy_host}:{self.profile.proxy_port}"
    return f"{auth_prefix}{self.profile.proxy_host}"

  def _delete_profile_with_retry(self, adspower: AdsPowerManager, reason: str) -> bool:
    profile_id = self.profile.profile_id
    try:
      adspower.force_terminate_profile(profile_id)
    except Exception as exc:
      self._emit_log(f"[{self.profile.name}] Force terminate warning: {exc}")
    self._emit_log(
      f"[{self.profile.name}] Waiting 4s for AdsPower to release profile before delete"
    )
    self.stop_event.wait(4.0)
    for attempt in range(1, 8):
      try:
        adspower.delete_profiles([profile_id])
        return True
      except Exception as exc:
        self._emit_log(f"[{self.profile.name}] Delete retry {attempt}/7 failed ({reason}): {exc}")
        try:
          exists = adspower.verify_profile_ids([profile_id])
          if not exists:
            return True
        except Exception as verify_exc:
          self._emit_log(f"[{self.profile.name}] Verify delete failed: {verify_exc}")
        backoff = min(30.0, 3.0 * (2 ** (attempt - 1)))
        if AdsPowerManager._is_rate_limit_error(str(exc)):
          backoff = max(backoff, min(45.0, 5.0 * attempt))
        self.stop_event.wait(backoff)
    return False
