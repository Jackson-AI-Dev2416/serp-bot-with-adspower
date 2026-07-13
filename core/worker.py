import random
import threading
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config.bot_config import BotConfig
from core.profile_status import ProfileStatus, UiStatusKey
from core.profile_worker import ProfileWorkerThread
from core.proxy_scheduler import ProxyScheduler
from services.adspower_manager import AdsPowerManager, ProfileSpec
from services.serp_bot import SerpBot
from utils.keyword_exclusion import KeywordExclusionStore
from utils.keyword_rotation import KeywordRotationStore


class BotWorkerThread(QThread):
  log = pyqtSignal(str)
  profile_update = pyqtSignal(str, str, str)
  traffic_update = pyqtSignal(str, str, int)
  cycle_progress = pyqtSignal(int, int)
  keyword_excluded = pyqtSignal(str)
  profiles_changed = pyqtSignal()
  profile_created = pyqtSignal(object)
  profile_deleted = pyqtSignal(str)
  finished_ok = pyqtSignal()
  error = pyqtSignal(str)

  def __init__(
    self,
    config: BotConfig,
    profiles: List[ProfileSpec],
    keyword_rotation: KeywordRotationStore,
    keyword_exclusion: KeywordExclusionStore,
    failure_callback=None,
    parent=None,
  ):
    super().__init__(parent)
    self.config = config
    self.profiles = profiles
    self.keyword_rotation = keyword_rotation
    self.keyword_exclusion = keyword_exclusion
    self._failure_callback = failure_callback
    self._stop_requested = False
    self._profile_stop_events: Dict[str, threading.Event] = {}
    self._created_profile_ids: set[str] = set()
    self._force_cleanup_started = False
    self._deleted_profile_ids: set[str] = set()
    self._delete_lock = threading.Lock()
    self._lifecycle_lock = threading.Lock()

  def request_stop(self) -> None:
    self._stop_requested = True
    for event in list(self._profile_stop_events.values()):
      event.set()
    if not self._force_cleanup_started:
      self._force_cleanup_started = True
      threading.Thread(target=self._force_stop_and_delete_profiles_now, daemon=True).start()

  def request_profile_pause(self, profile_id: str) -> bool:
    """Gracefully stop one running profile without stopping the automation loop."""
    event = self._profile_stop_events.get(profile_id)
    if not event:
      return False
    event.set()
    self._emit_log(f"[Worker] Graceful stop requested for profile {profile_id} only")
    return True

  def is_profile_running(self, profile_id: str) -> bool:
    return profile_id in self._profile_stop_events

  def _force_stop_and_delete_profiles_now(self) -> None:
    active_profile_ids = list(dict.fromkeys(list(self._profile_stop_events.keys())))
    created_profile_ids = list(dict.fromkeys(list(self._created_profile_ids)))
    if not active_profile_ids and not created_profile_ids:
      self._emit_log("[Worker] Force stop requested; no active profiles to terminate.")
      return

    self._emit_log(
      "[Worker] FORCE stop: terminating active profiles and deleting only "
      f"program-created profiles (active={len(active_profile_ids)}, created={len(created_profile_ids)})."
    )
    manager = AdsPowerManager(
      self.config.adspower_url,
      self.config.adspower_api_key,
      self._emit_log,
    )

    # Stop active profiles one at a time so AdsPower is not flooded.
    for profile_id in active_profile_ids:
      if profile_id in self._deleted_profile_ids:
        continue
      try:
        with self._lifecycle_lock:
          manager.force_terminate_profile(profile_id)
      except Exception as exc:
        self._emit_log(f"[Worker] Force terminate failed for {profile_id}: {exc}")

    if created_profile_ids:
      self._emit_log("[Worker] Waiting for AdsPower to release stopped profiles before delete...")
      time.sleep(3.0)

    # Delete only profiles created by this automation run.
    for profile_id in created_profile_ids:
      if profile_id in self._deleted_profile_ids:
        continue
      self._delete_profile_with_retry(manager, profile_id, reason="force-stop", skip_terminate=True)

    remaining = [pid for pid in self._created_profile_ids if pid not in self._deleted_profile_ids]
    if remaining:
      self._emit_log(f"[Worker] Retrying delete for {len(remaining)} profile(s) still present after force-stop.")
      time.sleep(2.0)
      for profile_id in remaining:
        self._delete_profile_with_retry(manager, profile_id, reason="force-stop-retry", skip_terminate=True)
    self.profiles_changed.emit()

  def _emit_profile_deleted_once(self, profile_id: str) -> None:
    with self._delete_lock:
      if profile_id in self._deleted_profile_ids:
        return
      self._deleted_profile_ids.add(profile_id)
    self._created_profile_ids.discard(profile_id)
    self.profile_deleted.emit(profile_id)

  def _delete_profile_with_retry(
    self,
    manager: AdsPowerManager,
    profile_id: str,
    reason: str,
    *,
    skip_terminate: bool = False,
  ) -> bool:
    if not profile_id:
      return False
    with self._delete_lock:
      if profile_id in self._deleted_profile_ids:
        return True

    if not skip_terminate:
      try:
        with self._lifecycle_lock:
          manager.force_terminate_profile(profile_id)
      except Exception as exc:
        self._emit_log(f"[Worker] Force terminate warning for {profile_id}: {exc}")

    for attempt in range(1, 8):
      with self._delete_lock:
        if profile_id in self._deleted_profile_ids:
          return True
      try:
        with self._lifecycle_lock:
          manager.delete_profiles([profile_id])
        self._emit_log(f"[Worker] Deleted profile {profile_id} ({reason})")
        self._emit_profile_deleted_once(profile_id)
        return True
      except Exception as exc:
        self._emit_log(f"[Worker] Delete retry {attempt}/7 failed for {profile_id}: {exc}")
        try:
          with self._lifecycle_lock:
            exists = manager.verify_profile_ids([profile_id])
          if not exists:
            self._emit_log(f"[Worker] Profile {profile_id} already absent after delete attempt ({reason})")
            self._emit_profile_deleted_once(profile_id)
            return True
        except Exception as verify_exc:
          self._emit_log(f"[Worker] Verify delete failed for {profile_id}: {verify_exc}")
        backoff = 1.5 * attempt
        if AdsPowerManager._is_rate_limit_error(str(exc)):
          backoff = max(backoff, 3.0 * attempt)
        self._interruptible_sleep(backoff)
    return False

  def _emit_log(self, message: str) -> None:
    self.log.emit(message)

  def _emit_profile_update(self, profile: ProfileSpec, status: ProfileStatus, cooldown: int = 0) -> None:
    key, text = status.to_ui(cooldown)
    self.profile_update.emit(profile.profile_id, key, text)

  def _emit_ui_status(self, profile: ProfileSpec, status_key: str, display_text: str) -> None:
    self.profile_update.emit(profile.profile_id, status_key, display_text)

  def run(self) -> None:
    adspower = AdsPowerManager(
      self.config.adspower_url,
      self.config.adspower_api_key,
      self._emit_log,
    )

    try:
      if self.config.auto_create_profiles:
        self._run_auto_create_mode(adspower)
      else:
        self._run_loaded_profiles_mode(adspower)

      self._emit_log("[Worker] Automation loop finished")
      self.finished_ok.emit()
    except Exception as exc:
      self.error.emit(str(exc))
      self.finished_ok.emit()
    finally:
      if self._stop_requested and self.config.auto_create_profiles:
        self._sweep_remaining_created_profiles(adspower)

  def _run_loaded_profiles_mode(self, adspower: AdsPowerManager) -> None:
    scheduler = ProxyScheduler(self.config.proxy_cooldown_seconds)
    pending = list(self.profiles)
    random.shuffle(pending)
    profile_map = {p.profile_id: p for p in self.profiles}

    while pending and not self._stop_requested:
      launched_any = False
      for profile in list(pending):
        if self._stop_requested:
          break
        proxy_key = profile.proxy_host
        if not scheduler.can_run(proxy_key):
          continue
        scheduler.mark_running(proxy_key)
        pending.remove(profile)
        launched_any = True
        self._run_single_profile(profile, proxy_key=proxy_key, scheduler=scheduler)

        if pending and not self._stop_requested:
          delay = random.uniform(self.config.launch_interval_min, self.config.launch_interval_max)
          self._emit_log(f"[Worker] Waiting {delay:.1f}s before next launch")
          self._interruptible_sleep(delay)

      if not launched_any and pending and not self._stop_requested:
        wait_seconds = scheduler.seconds_until_next_slot()
        for profile_id in [p.profile_id for p in pending]:
          p = profile_map[profile_id]
          if not scheduler.can_run(p.proxy_host):
            self._emit_profile_update(
              p,
              ProfileStatus.COOLDOWN,
              int(scheduler.seconds_until_proxy(p.proxy_host)),
            )
        self._emit_log(f"[Worker] All proxies busy/cooling, retry in {wait_seconds:.0f}s")
        self._interruptible_sleep(min(wait_seconds, 30.0))

  def _run_auto_create_mode(self, adspower: AdsPowerManager) -> None:
    max_concurrent = max(1, int(self.config.automation_threads or 1))
    proxy_list = list(self.config.proxies or [])
    proxy_cursor = 0
    keywords = [keyword.strip() for keyword in (self.config.keywords or []) if keyword and keyword.strip()]
    if not keywords:
      self._emit_log("[Worker] Auto-create mode aborted: no keywords configured.")
      return
    cycles_target = max(1, int(self.config.automation_cycles or 1))
    keywords_per_cycle = len(keywords)
    total_keyword_dispatch_target = keywords_per_cycle * cycles_target
    dispatched_keywords = 0
    self.cycle_progress.emit(0, cycles_target)
    self._emit_log(
      f"[Worker] Auto-create mode enabled (max concurrent: {max_concurrent}, "
      f"cycles target: {cycles_target}, keywords/cycle: {keywords_per_cycle})"
    )

    active_threads: list[threading.Thread] = []
    run_index = 0
    next_create_allowed_at = 0.0

    while not self._stop_requested and dispatched_keywords < total_keyword_dispatch_target:
      active_threads = [thread for thread in active_threads if thread.is_alive()]
      if len(active_threads) >= max_concurrent:
        self._interruptible_sleep(0.4)
        continue

      now = time.time()
      if now < next_create_allowed_at:
        self._interruptible_sleep(min(0.4, next_create_allowed_at - now))
        continue
      if self._stop_requested:
        break

      chosen_proxy = None
      if proxy_list:
        chosen_proxy = proxy_list[proxy_cursor % len(proxy_list)]
        proxy_cursor += 1

      run_index += 1
      profile = self._create_single_profile_for_run(adspower, run_index, chosen_proxy)
      if not profile:
        self._interruptible_sleep(2.0)
        continue
      remaining_keywords = total_keyword_dispatch_target - dispatched_keywords
      assigned_keywords = self._assigned_keywords_for_profile(profile, max_keywords=remaining_keywords)
      if not assigned_keywords:
        self._emit_log(
          f"[Worker] No keywords assigned for {profile.name}; deleting profile and stopping auto loop."
        )
        if self._delete_profile_with_retry(adspower, profile.profile_id, reason="no-keywords-assigned"):
          self.profiles_changed.emit()
        break
      dispatched_keywords += len(assigned_keywords)
      completed_cycles = dispatched_keywords // keywords_per_cycle
      self.cycle_progress.emit(min(completed_cycles, cycles_target), cycles_target)
      self.profiles_changed.emit()
      self.profile_created.emit(profile)
      if self._stop_requested:
        self._emit_log(f"[Worker] Stop requested after creating {profile.profile_id}; deleting it.")
        if self._delete_profile_with_retry(adspower, profile.profile_id, reason="stop-requested-after-create"):
          self.profiles_changed.emit()
        break

      worker_thread = threading.Thread(
        target=self._run_single_profile,
        args=(profile, profile.proxy_host, None, assigned_keywords),
        daemon=True,
      )
      worker_thread.start()
      active_threads.append(worker_thread)

      delay = random.uniform(self.config.launch_interval_min, self.config.launch_interval_max)
      next_create_allowed_at = time.time() + delay
      self._emit_log(
        f"[Worker] Active profiles: {len(active_threads)}/{max_concurrent}. "
        f"Keyword progress: {dispatched_keywords}/{total_keyword_dispatch_target} "
        f"(cycles done: {completed_cycles}/{cycles_target}). "
        f"Next create in {delay:.1f}s (counted from creation time)."
      )

    if not self._stop_requested and dispatched_keywords >= total_keyword_dispatch_target:
      self.cycle_progress.emit(cycles_target, cycles_target)
      self._emit_log(
        f"[Worker] Keyword cycle target reached: {cycles_target} full cycle(s). "
        "No more profiles will be created; waiting active runs to finish."
      )

    while active_threads:
      active_threads = [thread for thread in active_threads if thread.is_alive()]
      if not active_threads:
        break
      self._interruptible_sleep(0.4)

    if not self._stop_requested:
      completed_cycles = min(
        cycles_target,
        (dispatched_keywords + keywords_per_cycle - 1) // keywords_per_cycle,
      )
      self.cycle_progress.emit(completed_cycles, cycles_target)
      if dispatched_keywords >= total_keyword_dispatch_target:
        self._emit_log(
          f"[Worker] Keyword cycle target reached: {cycles_target} full cycle(s). "
          "All assigned profiles finished."
        )
      else:
        self._emit_log(
          f"[Worker] Automation dispatch finished: {dispatched_keywords}/{total_keyword_dispatch_target} "
          f"keyword slots ({completed_cycles}/{cycles_target} cycle(s))."
        )

  def _create_single_profile_for_run(
    self,
    adspower: AdsPowerManager,
    run_number: int,
    proxy: Optional[tuple[str, int, str, str]],
  ) -> Optional[ProfileSpec]:
    if self._stop_requested:
      return None
    try:
      proxy_batch = [proxy] if proxy else []
      created = adspower.create_profiles_batch(
        proxies=proxy_batch,
        group_id=self.config.adspower_group_id,
        total=1,
      )
      profile = created[0]
      self._created_profile_ids.add(profile.profile_id)
      self._emit_log(f"[Worker] Created profile for auto run {run_number}: {profile.name} ({profile.profile_id})")
      return profile
    except Exception as exc:
      self._emit_log(f"[Worker] Failed to create profile for auto run {run_number}: {exc}")
      return None

  def _sweep_remaining_created_profiles(self, adspower: AdsPowerManager) -> None:
    remaining = [pid for pid in list(self._created_profile_ids) if pid not in self._deleted_profile_ids]
    if not remaining:
      return
    self._emit_log(f"[Worker] Final cleanup: deleting {len(remaining)} auto-created profile(s).")
    for profile_id in remaining:
      self._delete_profile_with_retry(adspower, profile_id, reason="stop-sweep", skip_terminate=True)
    self.profiles_changed.emit()

  def _run_single_profile(
    self,
    profile: ProfileSpec,
    proxy_key: str,
    scheduler: Optional[ProxyScheduler],
    assigned_keywords: Optional[list[str]] = None,
  ) -> None:
    if self._stop_requested:
      if self.config.auto_create_profiles and profile.profile_id not in self._deleted_profile_ids:
        manager = AdsPowerManager(
          self.config.adspower_url,
          self.config.adspower_api_key,
          self._emit_log,
        )
        self._delete_profile_with_retry(manager, profile.profile_id, reason="stopped-before-launch")
      return
    manager = AdsPowerManager(
      self.config.adspower_url,
      self.config.adspower_api_key,
      self._emit_log,
    )
    stop_event = threading.Event()
    self._profile_stop_events[profile.profile_id] = stop_event
    self._emit_profile_update(profile, ProfileStatus.RUNNING)
    display_proxy_key = self._proxy_key_for_profile(profile)
    self._emit_log(f"[Worker] Launching {profile.name} via proxy {display_proxy_key}")
    if assigned_keywords is None:
      assigned_keywords = self._assigned_keywords_for_profile(profile)
    outcome = "error"
    max_tunnel_attempts = 2
    profile_stopped = False
    for attempt in range(1, max_tunnel_attempts + 1):
      try:
        ws_endpoint = manager.start_profile(profile.profile_id)

        def stop_profile_once() -> None:
          nonlocal profile_stopped
          if profile_stopped or profile.profile_id in self._deleted_profile_ids:
            return
          with self._lifecycle_lock:
            manager.stop_profile(profile.profile_id)
          profile_stopped = True

        serp_bot = SerpBot(self.config, self._emit_log)

        def on_status(status: ProfileStatus) -> None:
          self._emit_profile_update(profile, status)

        def on_ui_status(status_key: str, display_text: str) -> None:
          self._emit_ui_status(profile, status_key, display_text)

        def on_failure(failed_profile: ProfileSpec, context: str, exc: BaseException) -> None:
          self._emit_profile_update(failed_profile, ProfileStatus.ERROR)
          if self._failure_callback:
            self._failure_callback(failed_profile.profile_id)

        outcome = serp_bot.run_session(
          ws_endpoint,
          profile,
          stop_event=stop_event,
          keywords_override=assigned_keywords,
          on_status=on_status,
          on_ui_status=on_ui_status,
          on_traffic=lambda total, delta: self.traffic_update.emit(
            profile.profile_id,
            display_proxy_key,
            int(delta),
          ),
          on_failure=on_failure,
          on_keyword_exhausted=lambda kw: self._on_keyword_exhausted(profile, kw),
          on_session_cleanup=stop_profile_once,
        )
      except Exception as exc:
        self._emit_log(f"[Worker] Error on {profile.name}: {exc}")
        self._emit_profile_update(profile, ProfileStatus.ERROR)
        outcome = "error"
      finally:
        should_delete = (
          self._should_delete_profile_after_run(outcome)
          and profile.profile_id not in self._deleted_profile_ids
        )
        if not should_delete and profile.profile_id not in self._deleted_profile_ids and not profile_stopped:
          with self._lifecycle_lock:
            manager.stop_profile(profile.profile_id)
          profile_stopped = True

      if outcome != "tunnel_error":
        break
      if attempt >= max_tunnel_attempts or self._stop_requested or stop_event.is_set():
        break

      wait_seconds = random.uniform(max(8.0, float(self.config.launch_interval_min)), max(12.0, float(self.config.launch_interval_max)))
      self._emit_log(
        f"[Worker] Tunnel connection failed on {profile.name}. "
        f"Waiting {wait_seconds:.1f}s before retry ({attempt}/{max_tunnel_attempts})."
      )
      self._interruptible_sleep(wait_seconds)

    if self._should_delete_profile_after_run(outcome) and profile.profile_id not in self._deleted_profile_ids:
      if self._delete_profile_with_retry(manager, profile.profile_id, reason=f"outcome:{outcome}"):
        self._emit_log(f"[Worker] Deleted profile {profile.name} after run ({outcome}); proxy entry kept.")
        self.profiles_changed.emit()
      else:
        self._emit_log(f"[Worker] Failed to delete profile {profile.name} after run ({outcome})")
    if scheduler:
      scheduler.mark_finished(proxy_key)
    self._profile_stop_events.pop(profile.profile_id, None)

    if outcome == "success":
      self._emit_profile_update(profile, ProfileStatus.SUCCESS)
    elif outcome in ("error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error"):
      self._emit_profile_update(profile, ProfileStatus.ERROR)
    elif outcome == "stopped":
      self._emit_profile_update(profile, ProfileStatus.IDLE)
    else:
      self._emit_profile_update(profile, ProfileStatus.IDLE)

    self._emit_profile_update(
      profile,
      ProfileStatus.COOLDOWN,
      self.config.proxy_cooldown_seconds,
    )
    self._emit_log(
      f"[Worker] Proxy {display_proxy_key} cooldown started "
      f"({self.config.proxy_cooldown_seconds // 60} min)"
    )

  def _interruptible_sleep(self, seconds: float) -> None:
    end_time = time.time() + seconds
    while time.time() < end_time and not self._stop_requested:
      time.sleep(0.25)

  def _should_delete_profile_after_run(self, outcome: str) -> bool:
    if outcome in ("success", "not_found", "error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error"):
      return True
    if outcome == "stopped" and self.config.auto_create_profiles:
      return True
    return False

  def _assigned_keywords_for_profile(self, profile: ProfileSpec, max_keywords: Optional[int] = None) -> list[str]:
    keywords = [keyword.strip() for keyword in (self.config.keywords or []) if keyword and keyword.strip()]
    if not keywords:
      return []

    max_per_profile = max(1, int(self.config.max_keywords_per_profile or len(keywords)))
    if max_keywords is not None:
      max_per_profile = min(max_per_profile, max(0, int(max_keywords)))
    if max_per_profile <= 0:
      return []
    assigned = self.keyword_rotation.allocate(
      target_domain=self.config.target_domain,
      keywords=keywords,
      batch_size=max_per_profile,
    )
    self._emit_log(
      f"[Worker] {profile.name} keyword batch ({len(assigned)}): "
      + ", ".join(assigned[:3])
      + (" ..." if len(assigned) > 3 else "")
    )
    return assigned

  def _on_keyword_exhausted(self, profile: ProfileSpec, keyword: str) -> None:
    self._emit_log(
      f"[Worker] {profile.name} checked all available SERP pages for '{keyword}' "
      "without finding the target. Keyword is kept in the list for future runs."
    )

  @staticmethod
  def _proxy_key_for_profile(profile: ProfileSpec) -> str:
    if profile.proxy_host in ("", "—"):
      return "—"
    user = (profile.proxy_user or "").strip()
    auth_prefix = f"{user}@" if user else ""
    if profile.proxy_port:
      return f"{auth_prefix}{profile.proxy_host}:{profile.proxy_port}"
    return f"{auth_prefix}{profile.proxy_host}"


class ProfileController(QObject):
  log = pyqtSignal(str)
  profile_update = pyqtSignal(str, str, str)
  proxy_traffic_update = pyqtSignal(str, int)
  profile_traffic_update = pyqtSignal(str, int, int)
  cycle_progress_update = pyqtSignal(int, int)
  keyword_excluded = pyqtSignal(str)
  profiles_sync_requested = pyqtSignal()
  profile_created = pyqtSignal(object)
  profile_deleted = pyqtSignal(str)
  global_finished = pyqtSignal()

  def __init__(self, parent=None):
    super().__init__(parent)
    self._config: Optional[BotConfig] = None
    self._profiles: Dict[str, ProfileSpec] = {}
    self._scheduler = ProxyScheduler(1800)
    self._global_worker: Optional[BotWorkerThread] = None
    self._profile_workers: Dict[str, ProfileWorkerThread] = {}
    self._stop_events: Dict[str, threading.Event] = {}
    self._cooldown_until: Dict[str, float] = {}
    self._session_started_at: Dict[str, float] = {}
    self._status: Dict[str, ProfileStatus] = {}
    self._self_healer = None
    self._auto_healing_enabled = False
    self._keyword_rotation = KeywordRotationStore()
    self._keyword_exclusion = KeywordExclusionStore()
    self._proxy_traffic_totals: Dict[str, int] = {}
    self._profile_traffic_totals: Dict[str, int] = {}
    self._session_traffic_total: int = 0

  def attach_self_healer(self, healer) -> None:
    self._self_healer = healer

  def clear_keyword_exclusions(self, target_domain: str) -> int:
    return self._keyword_exclusion.clear_domain(target_domain)

  def _handle_automation_failure(self, profile_id: str) -> None:
    key, text = ProfileStatus.ERROR.to_ui()
    self.profile_update.emit(profile_id, key, text)
    if self._auto_healing_enabled and self._self_healer and self._config:
      self._self_healer.trigger_healing(profile_id, self._config)

  def set_profiles(self, profiles: List[ProfileSpec], cooldown_seconds: int = 1800) -> None:
    self._profiles = {p.profile_id: p for p in profiles}
    self._scheduler = ProxyScheduler(cooldown_seconds)
    for profile in profiles:
      if profile.profile_id not in self._status:
        self._status[profile.profile_id] = ProfileStatus.IDLE

  def start_global(self, config: BotConfig, profiles: List[ProfileSpec]) -> bool:
    if self._global_worker and self._global_worker.isRunning():
      return False

    self._config = config
    self.set_profiles(profiles, config.proxy_cooldown_seconds)
    self._global_worker = BotWorkerThread(
      config,
      profiles,
      keyword_rotation=self._keyword_rotation,
      keyword_exclusion=self._keyword_exclusion,
      failure_callback=self._handle_automation_failure,
    )
    self._global_worker.log.connect(self.log.emit)
    self._global_worker.profile_update.connect(self._on_profile_update)
    self._global_worker.traffic_update.connect(self._on_traffic_update)
    self._global_worker.cycle_progress.connect(self.cycle_progress_update.emit)
    self._global_worker.keyword_excluded.connect(self.keyword_excluded.emit)
    self._global_worker.profiles_changed.connect(self.profiles_sync_requested.emit)
    self._global_worker.profile_created.connect(self.profile_created.emit)
    self._global_worker.profile_deleted.connect(self.profile_deleted.emit)
    self._global_worker.error.connect(lambda msg: self.log.emit(f"[Worker] {msg}"))
    self._global_worker.finished_ok.connect(self.global_finished.emit)
    self._global_worker.start()
    self.log.emit("[Controller] Global automated bot started")
    return True

  def stop_global(self) -> None:
    if self._global_worker and self._global_worker.isRunning():
      self._global_worker.request_stop()
      self.log.emit("[Controller] Global bot stop requested")

  def start_profile_manual(self, profile_id: str, config: BotConfig) -> bool:
    profile = self._profiles.get(profile_id)
    if not profile:
      self.log.emit(f"[Controller] Unknown profile {profile_id}")
      return False

    if profile_id in self._profile_workers and self._profile_workers[profile_id].isRunning():
      self.log.emit(f"[Controller] {profile.name} is already running")
      return False

    proxy_key = profile.proxy_host
    if not self._scheduler.can_run(proxy_key):
      remaining = int(self._scheduler.seconds_until_proxy(proxy_key))
      self.log.emit(f"[Controller] Proxy {proxy_key} busy/cooling ({remaining}s left)")
      key, text = ProfileStatus.COOLDOWN.to_ui(remaining)
      self.profile_update.emit(profile_id, key, text)
      return False

    self._config = config
    self._scheduler.mark_running(proxy_key)
    stop_event = threading.Event()
    self._stop_events[profile_id] = stop_event

    worker = ProfileWorkerThread(
      config,
      profile,
      stop_event,
      keyword_rotation=self._keyword_rotation,
      keyword_exclusion=self._keyword_exclusion,
      failure_callback=self._handle_automation_failure,
    )
    worker.log.connect(self.log.emit)
    worker.profile_update.connect(self._on_profile_update)
    worker.traffic_update.connect(self._on_traffic_update)
    worker.keyword_excluded.connect(self.keyword_excluded.emit)
    worker.profiles_changed.connect(self.profiles_sync_requested.emit)
    worker.profile_deleted.connect(self.profile_deleted.emit)
    worker.finished_profile.connect(self._on_profile_finished)
    self._profile_workers[profile_id] = worker
    key, text = ProfileStatus.RUNNING.to_ui()
    self.profile_update.emit(profile_id, key, text)
    worker.start()
    self.log.emit(f"[Controller] Manual start: {profile.name}")
    return True

  def pause_profile(self, profile_id: str) -> None:
    profile = self._profiles.get(profile_id)
    profile_name = profile.name if profile else profile_id

    stop_event = self._stop_events.get(profile_id)
    if stop_event:
      stop_event.set()
      self.log.emit(f"[Controller] Graceful stop requested for {profile_name}")
      return

    if self._global_worker and self._global_worker.isRunning():
      if self._global_worker.request_profile_pause(profile_id):
        self.log.emit(f"[Controller] Graceful stop requested for {profile_name} (automation)")
      else:
        self.log.emit(f"[Controller] {profile_name} is not running")
      return

    self.log.emit(f"[Controller] {profile_name} is not running")

  def force_terminate(self, profile_id: str, config: BotConfig) -> None:
    profile = self._profiles.get(profile_id)
    profile_name = profile.name if profile else profile_id

    stop_event = self._stop_events.get(profile_id)
    if stop_event:
      stop_event.set()
    elif self._global_worker and self._global_worker.isRunning():
      self._global_worker.request_profile_pause(profile_id)

    adspower = AdsPowerManager(config.adspower_url, config.adspower_api_key, self._emit_log)
    adspower.force_terminate_profile(profile_id)
    self._clear_session_elapsed(profile_id)
    key, text = ProfileStatus.IDLE.to_ui()
    self.profile_update.emit(profile_id, key, text)
    self.log.emit(f"[Controller] Force terminated {profile_name}")

  def _emit_log(self, message: str) -> None:
    self.log.emit(message)

  def _on_profile_update(self, profile_id: str, status_key: str, display_text: str) -> None:
    self._map_status_key(profile_id, status_key)
    if status_key == UiStatusKey.CLOSED.value and "[" in display_text:
      try:
        part = display_text.split("[")[1].split("]")[0]
        mins, secs = part.split(":")
        cooldown = int(mins) * 60 + int(secs)
        self._cooldown_until[profile_id] = time.time() + cooldown
      except (IndexError, ValueError):
        pass
    self._track_session_elapsed(profile_id, status_key, display_text)
    self.profile_update.emit(profile_id, status_key, display_text)

  def _on_traffic_update(self, profile_id: str, proxy_key: str, delta_bytes: int) -> None:
    if delta_bytes <= 0:
      return
    profile_total = self._profile_traffic_totals.get(profile_id, 0) + int(delta_bytes)
    self._profile_traffic_totals[profile_id] = profile_total
    total = self._proxy_traffic_totals.get(proxy_key, 0) + int(delta_bytes)
    self._proxy_traffic_totals[proxy_key] = total
    self._session_traffic_total += int(delta_bytes)
    self.proxy_traffic_update.emit(proxy_key, total)
    self.profile_traffic_update.emit(profile_id, profile_total, self._session_traffic_total)

  def _map_status_key(self, profile_id: str, status_key: str) -> None:
    mapping = {
      UiStatusKey.NORMAL.value: ProfileStatus.RUNNING,
      UiStatusKey.CAPTCHA.value: ProfileStatus.CAPTCHA_WAIT,
      UiStatusKey.CAPTCHA_MANUAL.value: ProfileStatus.CAPTCHA_MANUAL,
      UiStatusKey.ERROR.value: ProfileStatus.ERROR,
      UiStatusKey.SELF_HEALING.value: ProfileStatus.SELF_HEALING,
      UiStatusKey.CLOSED.value: ProfileStatus.IDLE,
    }
    self._status[profile_id] = mapping.get(status_key, ProfileStatus.IDLE)

  def _on_profile_finished(self, profile_id: str, outcome: str) -> None:
    profile = self._profiles.get(profile_id)
    proxy_key = profile.proxy_host if profile else ""
    self._scheduler.mark_finished(proxy_key)
    self._stop_events.pop(profile_id, None)
    self._profile_workers.pop(profile_id, None)
    self._clear_session_elapsed(profile_id)

    if outcome == "success":
      status = ProfileStatus.SUCCESS
    elif outcome in ("error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error"):
      status = ProfileStatus.ERROR
    else:
      status = ProfileStatus.IDLE

    key, text = status.to_ui()
    self.profile_update.emit(profile_id, key, text)

    cooldown = self._config.proxy_cooldown_seconds if self._config else 1800
    self._cooldown_until[profile_id] = time.time() + cooldown
    key, text = ProfileStatus.COOLDOWN.to_ui(cooldown)
    self.profile_update.emit(profile_id, key, text)
    profile_name = profile.name if profile else profile_id
    self.log.emit(f"[Controller] {profile_name} finished ({outcome}), proxy cooldown started")

  _ELAPSED_STATUS_KEYS = frozenset({
    UiStatusKey.NORMAL.value,
    UiStatusKey.CAPTCHA.value,
    UiStatusKey.CAPTCHA_MANUAL.value,
    UiStatusKey.SELF_HEALING.value,
    UiStatusKey.ERROR.value,
  })

  def get_session_elapsed(self, profile_id: str) -> int:
    started = self._session_started_at.get(profile_id)
    if not started:
      return 0
    return max(0, int(time.time() - started))

  def _track_session_elapsed(self, profile_id: str, status_key: str, display_text: str) -> None:
    if status_key == UiStatusKey.CLOSED.value:
      self._session_started_at.pop(profile_id, None)
      return
    if status_key in self._ELAPSED_STATUS_KEYS:
      self._session_started_at.setdefault(profile_id, time.time())
      return
    self._session_started_at.pop(profile_id, None)

  def _clear_session_elapsed(self, profile_id: str) -> None:
    self._session_started_at.pop(profile_id, None)

  def get_cooldown_remaining(self, profile_id: str) -> int:
    until = self._cooldown_until.get(profile_id, 0)
    return max(0, int(until - time.time()))

  def remove_profiles(self, profile_ids: List[str]) -> None:
    for profile_id in profile_ids:
      stop_event = self._stop_events.get(profile_id)
      if stop_event:
        stop_event.set()
      self._profile_workers.pop(profile_id, None)
      self._stop_events.pop(profile_id, None)
      self._profiles.pop(profile_id, None)
      self._status.pop(profile_id, None)
      self._cooldown_until.pop(profile_id, None)
      self._session_started_at.pop(profile_id, None)
      self._profile_traffic_totals.pop(profile_id, None)
    if profile_ids:
      self.log.emit(f"[Controller] Removed {len(profile_ids)} profile(s) from local state")

  def get_status(self, profile_id: str) -> ProfileStatus:
    return self._status.get(profile_id, ProfileStatus.IDLE)
