import random
import threading

from PyQt6.QtCore import QThread, pyqtSignal

from config.bot_config import BotConfig
from core.profile_status import ProfileStatus
from services.adspower_manager import AdsPowerManager, ProfileSpec
from services.serp_bot import SerpBot
from utils.keyword_exclusion import KeywordExclusionStore
from utils.keyword_rotation import KeywordRotationStore


class ProfileWorkerThread(QThread):
  log = pyqtSignal(str)
  profile_update = pyqtSignal(str, str, str)
  traffic_update = pyqtSignal(str, str, int)
  keyword_excluded = pyqtSignal(str)
  profiles_changed = pyqtSignal()
  profile_deleted = pyqtSignal(str)
  finished_profile = pyqtSignal(str, str)

  def __init__(
    self,
    config: BotConfig,
    profile: ProfileSpec,
    stop_event: threading.Event,
    keyword_rotation: KeywordRotationStore,
    keyword_exclusion: KeywordExclusionStore,
    failure_callback=None,
    parent=None,
  ):
    super().__init__(parent)
    self.config = config
    self.profile = profile
    self.stop_event = stop_event
    self.keyword_rotation = keyword_rotation
    self.keyword_exclusion = keyword_exclusion
    self._failure_callback = failure_callback

  def _emit_log(self, message: str) -> None:
    self.log.emit(message)

  def _emit_profile_update(self, status: ProfileStatus, cooldown: int = 0) -> None:
    key, text = status.to_ui(cooldown)
    self.profile_update.emit(self.profile.profile_id, key, text)

  def _emit_ui_status(self, status_key: str, display_text: str) -> None:
    self.profile_update.emit(self.profile.profile_id, status_key, display_text)

  def run(self) -> None:
    adspower = AdsPowerManager(
      self.config.adspower_url,
      self.config.adspower_api_key,
      self._emit_log,
    )
    serp_bot = SerpBot(self.config, self._emit_log)
    assigned_keywords = self._assigned_keywords_for_profile()
    proxy_key = self._proxy_key()

    def on_failure(failed_profile: ProfileSpec, context: str, exc: BaseException) -> None:
      self._emit_profile_update(ProfileStatus.ERROR)
      if self._failure_callback:
        self._failure_callback(failed_profile.profile_id)

    outcome = "error"
    max_tunnel_attempts = 2
    for attempt in range(1, max_tunnel_attempts + 1):
      try:
        ws_endpoint = adspower.start_profile(self.profile.profile_id)
        outcome = serp_bot.run_session(
          ws_endpoint,
          self.profile,
          stop_event=self.stop_event,
          keywords_override=assigned_keywords,
          on_status=self._emit_profile_update,
          on_ui_status=self._emit_ui_status,
          on_traffic=lambda total, delta: self.traffic_update.emit(
            self.profile.profile_id,
            proxy_key,
            int(delta),
          ),
          on_failure=on_failure,
          on_keyword_exhausted=self._on_keyword_exhausted,
        )
      except Exception as exc:
        self._emit_log(f"[{self.profile.name}] Error: {exc}")
        self._emit_profile_update(ProfileStatus.ERROR)
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

  @staticmethod
  def _should_delete_profile_after_run(outcome: str) -> bool:
    return outcome in ("success", "not_found", "error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error")

  def _assigned_keywords_for_profile(self) -> list[str]:
    keywords = [keyword.strip() for keyword in (self.config.keywords or []) if keyword and keyword.strip()]
    if not keywords:
      return []
    max_per_profile = max(1, int(self.config.max_keywords_per_profile or len(keywords)))
    assigned = self.keyword_rotation.allocate(
      target_domain=self.config.target_domain,
      keywords=keywords,
      batch_size=max_per_profile,
    )
    self._emit_log(
      f"[{self.profile.name}] Assigned keyword batch ({len(assigned)}): "
      + ", ".join(assigned[:3])
      + (" ..." if len(assigned) > 3 else "")
    )
    return assigned

  def _proxy_key(self) -> str:
    if self.profile.proxy_host in ("", "—"):
      return "—"
    user = (self.profile.proxy_user or "").strip()
    auth_prefix = f"{user}@" if user else ""
    if self.profile.proxy_port:
      return f"{auth_prefix}{self.profile.proxy_host}:{self.profile.proxy_port}"
    return f"{auth_prefix}{self.profile.proxy_host}"

  def _on_keyword_exhausted(self, keyword: str) -> None:
    self._emit_log(
      f"[{self.profile.name}] Checked all available SERP pages for '{keyword}' "
      "without finding the target. Keyword is kept in the list."
    )

  def _delete_profile_with_retry(self, adspower: AdsPowerManager, reason: str) -> bool:
    profile_id = self.profile.profile_id
    try:
      adspower.force_terminate_profile(profile_id)
    except Exception as exc:
      self._emit_log(f"[{self.profile.name}] Force terminate warning: {exc}")
    for attempt in range(1, 4):
      try:
        adspower.delete_profiles([profile_id])
        return True
      except Exception as exc:
        self._emit_log(f"[{self.profile.name}] Delete retry {attempt}/3 failed ({reason}): {exc}")
        try:
          exists = adspower.verify_profile_ids([profile_id])
          if not exists:
            return True
        except Exception as verify_exc:
          self._emit_log(f"[{self.profile.name}] Verify delete failed: {verify_exc}")
        self.stop_event.wait(0.8 * attempt)
    return False
