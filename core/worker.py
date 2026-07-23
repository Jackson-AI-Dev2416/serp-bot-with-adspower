import random
import threading
import time
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from config.bot_config import BotConfig
from core.profile_status import (
  CAPTCHA_ELAPSED_KEYS,
  ERROR_ELAPSED_KEYS,
  ProfileStatus,
  SESSION_ELAPSED_KEYS,
  UiStatusKey,
  short_error_detail,
)
from core.profile_worker import ProfileWorkerThread
from core.proxy_scheduler import ProxyScheduler
from services.adspower_manager import AdsPowerManager, ProfileSpec
from services.serp_bot import SerpBot
from utils.keyword_exclusion import KeywordExclusionStore
from utils.keyword_rotation import KeywordRotationStore
from utils.pair_rotation import PairRotationStore
from utils.session_log import append_session_log
from utils.serp_result_store import SerpResultStore
from utils.csv_logger import log_session_failure, outcome_to_failure_url
from utils.user_log import build_pre_delete_log_line


class BotWorkerThread(QThread):
  _DELETE_API_SEMAPHORE = threading.Semaphore(2)
  _POST_TERMINATE_DELETE_DELAY_SECONDS = 4.0
  _ORPHAN_DELETE_DELAY_SECONDS = 180.0
  _FORCE_STOP_MIN_PROFILE_AGE_SECONDS = 45.0
  _SESSION_STALL_SEC = 600.0
  _PROFILE_STALL_POLL_SEC = 30.0

  @staticmethod
  def _profile_create_retry_delay(
    failure_streak: int,
    error_text: str,
    launch_interval_min: float,
  ) -> float:
    streak = max(1, int(failure_streak))
    msg = (error_text or "").lower()
    base = max(10.0, float(launch_interval_min or 10))
    if "cannot connect" in msg or "10061" in msg or "connection refused" in msg:
      return min(60.0, 5.0 * (2 ** min(streak - 1, 3)))
    if "timed out" in msg or "timeout" in msg:
      return min(45.0, 15.0 + 5.0 * min(streak - 1, 5))
    return min(30.0, base + 2.0 * min(streak - 1, 4))

  log = pyqtSignal(str)
  profile_update = pyqtSignal(str, str, str)
  traffic_update = pyqtSignal(str, str, "qint64", "qint64", "qint64", "qint64", "qint64", "qint64")
  cycle_progress = pyqtSignal(int, int)
  keyword_excluded = pyqtSignal(str)
  profiles_changed = pyqtSignal()
  profile_created = pyqtSignal(object)
  profile_deleted = pyqtSignal(str)
  finished_ok = pyqtSignal()
  profile_finished = pyqtSignal(str, str)
  target_click_logged = pyqtSignal()
  captcha_stat = pyqtSignal(str)
  error = pyqtSignal(str)

  def __init__(
    self,
    config: BotConfig,
    profiles: List[ProfileSpec],
    keyword_rotation: KeywordRotationStore,
    keyword_exclusion: KeywordExclusionStore,
    pair_rotation: Optional[PairRotationStore] = None,
    failure_callback=None,
    parent=None,
  ):
    super().__init__(parent)
    self.config = config
    self.profiles = profiles
    self.keyword_rotation = keyword_rotation
    self.keyword_exclusion = keyword_exclusion
    self.pair_rotation = pair_rotation or PairRotationStore()
    self.result_store = SerpResultStore()
    self._failure_callback = failure_callback
    self._stop_requested = False
    self._profile_stop_events: Dict[str, threading.Event] = {}
    self._profile_phases: Dict[str, str] = {}
    self._graceful_stop_pending: set[str] = set()
    self._phase_lock = threading.Lock()
    self._created_profile_ids: set[str] = set()
    self._profile_created_at: Dict[str, float] = {}
    self._shutdown_waiter_started = False
    self._deleted_profile_ids: set[str] = set()
    self._delete_lock = threading.Lock()
    self._lifecycle_lock = threading.Lock()
    self._orphan_delete_queue: Dict[str, str] = {}
    self._orphan_queue_lock = threading.Lock()
    self._orphan_sweeper_started = False
    self._visit_site_started_at: Dict[str, float] = {}
    self._dwell_force_timers: Dict[str, threading.Timer] = {}
    self._profile_last_progress_at: Dict[str, float] = {}
    self._profile_stall_stop_flags: Dict[str, threading.Event] = {}
    self._session_exhausted_pairs: set[tuple[str, str]] = set()

  @staticmethod
  def _session_pair_key(keyword: str, domain: str) -> tuple[str, str]:
    return PairRotationStore.pair_key(keyword, domain)

  def _mark_session_pair_exhausted(self, keyword: str, domain: str) -> None:
    key = self._session_pair_key(keyword, domain)
    if key in self._session_exhausted_pairs:
      return
    self._session_exhausted_pairs.add(key)
    self._emit_log(
      f"[Worker] Session skip: '{keyword.strip()}' → {domain.strip()} "
      "(full SERP to max pages, target not found — excluded until Stop/Start)"
    )

  def _dwell_force_stop_seconds(self) -> float:
    dwell_max = float(getattr(self.config, "dwell_max", 180) or 180)
    return dwell_max + 30.0

  def _clear_dwell_tracking(self, profile_id: str) -> None:
    self._visit_site_started_at.pop(profile_id, None)
    timer = self._dwell_force_timers.pop(profile_id, None)
    if timer:
      timer.cancel()

  def _touch_profile_progress(self, profile_id: str) -> None:
    self._profile_last_progress_at[profile_id] = time.time()

  def _start_session_stall_watchdog(
    self,
    profile: ProfileSpec,
    manager: AdsPowerManager,
    stop_event: threading.Event,
  ) -> None:
    stall_stop = threading.Event()
    self._profile_stall_stop_flags[profile.profile_id] = stall_stop
    self._touch_profile_progress(profile.profile_id)

    def _poll() -> None:
      while not stall_stop.is_set():
        if stop_event.is_set():
          return
        last = self._profile_last_progress_at.get(profile.profile_id, 0.0)
        if time.time() - last >= self._SESSION_STALL_SEC:
          self._emit_log(
            f"[Worker] Session stall watchdog: no progress for "
            f"{int(self._SESSION_STALL_SEC // 60)} min on {profile.name} — forcing stop"
          )
          self._force_profile_stop_now(profile.profile_id, "Session stall watchdog")
          try:
            with self._lifecycle_lock:
              manager.stop_profile(profile.profile_id)
          except Exception as exc:
            self._emit_log(f"[Worker] Stall watchdog AdsPower stop failed: {exc}")
          return
        stall_stop.wait(self._PROFILE_STALL_POLL_SEC)

    threading.Thread(
      target=_poll,
      daemon=True,
      name=f"stall-{profile.name}",
    ).start()

  def _stop_session_stall_watchdog(self, profile_id: str) -> None:
    stall_stop = self._profile_stall_stop_flags.pop(profile_id, None)
    if stall_stop:
      stall_stop.set()
    self._profile_last_progress_at.pop(profile_id, None)

  def _force_profile_stop_now(self, profile_id: str, reason: str) -> None:
    event = self._profile_stop_events.get(profile_id)
    if event and not event.is_set():
      self._emit_log(f"[Worker] {reason} for {profile_id}")
      event.set()
    with self._phase_lock:
      self._graceful_stop_pending.discard(profile_id)

  def _schedule_dwell_force_stop(self, profile_id: str) -> None:
    existing = self._dwell_force_timers.pop(profile_id, None)
    if existing:
      existing.cancel()
    started = self._visit_site_started_at.get(profile_id, time.time())
    deadline = started + self._dwell_force_stop_seconds()
    delay = max(0.5, deadline - time.time())
    if delay <= 0.5:
      self._force_profile_stop_now(profile_id, reason="Dwell budget exceeded")
      return

    def _fire() -> None:
      self._dwell_force_timers.pop(profile_id, None)
      self._force_profile_stop_now(profile_id, reason="Dwell force-stop timer")

    timer = threading.Timer(delay, _fire)
    timer.daemon = True
    self._dwell_force_timers[profile_id] = timer
    timer.start()

  def _on_profile_phase(self, profile_id: str, status_key: str) -> None:
    if status_key == UiStatusKey.VISITING_SITE.value:
      self._visit_site_started_at[profile_id] = time.time()
    else:
      self._clear_dwell_tracking(profile_id)
    with self._phase_lock:
      self._profile_phases[profile_id] = status_key
      pending = profile_id in self._graceful_stop_pending
    if pending and status_key != UiStatusKey.VISITING_SITE.value:
      event = self._profile_stop_events.get(profile_id)
      if event and not event.is_set():
        event.set()
        with self._phase_lock:
          self._graceful_stop_pending.discard(profile_id)
        self._emit_log(
          f"[Worker] Profile {profile_id} finished dwell — stopping now."
        )

  def _request_profile_stop(self, profile_id: str, *, global_stop: bool = False) -> bool:
    event = self._profile_stop_events.get(profile_id)
    if not event:
      return False
    with self._phase_lock:
      phase = self._profile_phases.get(profile_id, "")
    if phase == UiStatusKey.VISITING_SITE.value:
      with self._phase_lock:
        self._graceful_stop_pending.add(profile_id)
      self._schedule_dwell_force_stop(profile_id)
      self._emit_log(
        f"[Worker] Stop deferred for {profile_id} — finishing target dwell first."
      )
    else:
      event.set()
      if not global_stop:
        self._emit_log(f"[Worker] Stop requested for profile {profile_id}")
    return True

  def _ensure_shutdown_waiter(self) -> None:
    if self._shutdown_waiter_started:
      return
    self._shutdown_waiter_started = True
    threading.Thread(target=self._wait_for_graceful_shutdown, daemon=True).start()

  def _graceful_shutdown_timeout_seconds(self) -> float:
    dwell_max = float(getattr(self.config, "dwell_max", 180) or 180)
    with self._phase_lock:
      phases = list(self._profile_phases.values())
    if phases and all(phase != UiStatusKey.VISITING_SITE.value for phase in phases):
      return max(45.0, dwell_max * 0.1)
    return max(120.0, dwell_max + 45.0)

  def _wait_for_graceful_shutdown(self) -> None:
    deadline = time.time() + self._graceful_shutdown_timeout_seconds()
    while time.time() < deadline:
      with self._phase_lock:
        active_ids = list(self._profile_stop_events.keys())
        pending = list(self._graceful_stop_pending)
      if not active_ids:
        self._emit_log("[Worker] Graceful stop complete — all profiles finished.")
        return
      for profile_id in pending:
        with self._phase_lock:
          phase = self._profile_phases.get(profile_id, "")
        if phase != UiStatusKey.VISITING_SITE.value:
          event = self._profile_stop_events.get(profile_id)
          if event and not event.is_set():
            event.set()
            with self._phase_lock:
              self._graceful_stop_pending.discard(profile_id)
            self._emit_log(
              f"[Worker] Profile {profile_id} left dwell — stopping now."
            )
      with self._phase_lock:
        active_ids = list(self._profile_stop_events.keys())
      for profile_id in active_ids:
        with self._phase_lock:
          phase = self._profile_phases.get(profile_id, "")
        if phase != UiStatusKey.VISITING_SITE.value:
          continue
        started = self._visit_site_started_at.get(profile_id, 0.0)
        if started > 0 and time.time() >= started + self._dwell_force_stop_seconds():
          self._force_profile_stop_now(
            profile_id,
            reason="Dwell budget exceeded (shutdown waiter)",
          )
      with self._phase_lock:
        active_ids = list(self._profile_stop_events.keys())
      if not active_ids:
        self._emit_log("[Worker] Graceful stop complete — all profiles finished.")
        return
      time.sleep(1.0)
    self._emit_log(
      "[Worker] Graceful stop timeout — force-terminating remaining profiles."
    )
    self._force_stop_and_delete_profiles_now()

  def request_stop(self) -> None:
    self._stop_requested = True
    self._emit_log(
      "[Worker] Graceful stop requested — SERP/warmup profiles stop now; "
      "target dwell completes first."
    )
    for profile_id in list(self._profile_stop_events.keys()):
      self._request_profile_stop(profile_id, global_stop=True)
    self._ensure_shutdown_waiter()

  def request_profile_pause(self, profile_id: str) -> bool:
    """Gracefully stop one running profile without stopping the automation loop."""
    return self._request_profile_stop(profile_id, global_stop=False)

  def is_profile_running(self, profile_id: str) -> bool:
    return profile_id in self._profile_stop_events

  def _should_skip_force_delete(self, profile_id: str) -> bool:
    created_at = self._profile_created_at.get(profile_id, 0.0)
    if created_at <= 0:
      return False
    age = time.time() - created_at
    if age >= self._FORCE_STOP_MIN_PROFILE_AGE_SECONDS:
      return False
    if profile_id in self._profile_stop_events:
      self._emit_log(
        f"[Worker] Skipping force-delete for young profile {profile_id} "
        f"({age:.0f}s old, still active)"
      )
      return True
    return False

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
      if self._should_skip_force_delete(profile_id):
        continue
      self._delete_profile_with_retry(manager, profile_id, reason="force-stop", skip_terminate=True)

    remaining = [
      pid for pid in self._created_profile_ids
      if pid not in self._deleted_profile_ids and not self._should_skip_force_delete(pid)
    ]
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
    self._profile_created_at.pop(profile_id, None)
    self.profile_deleted.emit(profile_id)

  def _schedule_orphan_delete(self, profile_id: str, reason: str) -> None:
    if not profile_id:
      return
    with self._orphan_queue_lock:
      if profile_id in self._deleted_profile_ids:
        return
      if profile_id not in self._orphan_delete_queue:
        self._orphan_delete_queue[profile_id] = reason
        self._emit_log(
          f"[Worker] Queued orphan delete for {profile_id} "
          f"(retry in {int(self._ORPHAN_DELETE_DELAY_SECONDS)}s, reason={reason})"
        )
      if not self._orphan_sweeper_started:
        self._orphan_sweeper_started = True
        threading.Thread(target=self._orphan_delete_sweeper_loop, daemon=True).start()

  def _orphan_delete_sweeper_loop(self) -> None:
    while not self._stop_requested:
      self._interruptible_sleep(30.0)
      with self._orphan_queue_lock:
        pending = {
          profile_id: reason
          for profile_id, reason in list(self._orphan_delete_queue.items())
          if profile_id not in self._deleted_profile_ids
        }
      if not pending:
        with self._orphan_queue_lock:
          if not self._orphan_delete_queue:
            self._orphan_sweeper_started = False
        return

      manager = AdsPowerManager(
        self.config.adspower_url,
        self.config.adspower_api_key,
        self._emit_log,
      )
      for profile_id, reason in list(pending.items()):
        if profile_id in self._deleted_profile_ids:
          with self._orphan_queue_lock:
            self._orphan_delete_queue.pop(profile_id, None)
          continue
        self._emit_log(f"[Worker] Orphan delete sweep for {profile_id} ({reason})")
        if self._delete_profile_with_retry(
          manager,
          profile_id,
          reason=f"orphan-sweep:{reason}",
          skip_terminate=True,
        ):
          with self._orphan_queue_lock:
            self._orphan_delete_queue.pop(profile_id, None)
          self.profiles_changed.emit()
        else:
          self._interruptible_sleep(self._ORPHAN_DELETE_DELAY_SECONDS)

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
      self._emit_log(
        f"[Worker] Waiting {self._POST_TERMINATE_DELETE_DELAY_SECONDS:.0f}s "
        f"for AdsPower to release {profile_id} before delete"
      )
      self._interruptible_sleep(self._POST_TERMINATE_DELETE_DELAY_SECONDS)

    for attempt in range(1, 8):
      with self._delete_lock:
        if profile_id in self._deleted_profile_ids:
          return True
      try:
        with self._DELETE_API_SEMAPHORE:
          with self._lifecycle_lock:
            manager.delete_profiles([profile_id])
        self._emit_log(f"[Worker] Deleted profile {profile_id} ({reason})")
        self._emit_profile_deleted_once(profile_id)
        with self._orphan_queue_lock:
          self._orphan_delete_queue.pop(profile_id, None)
        return True
      except Exception as exc:
        self._emit_log(f"[Worker] Delete retry {attempt}/7 failed for {profile_id}: {exc}")
        try:
          with self._lifecycle_lock:
            exists = manager.verify_profile_ids([profile_id])
          if not exists:
            self._emit_log(f"[Worker] Profile {profile_id} already absent after delete attempt ({reason})")
            self._emit_profile_deleted_once(profile_id)
            with self._orphan_queue_lock:
              self._orphan_delete_queue.pop(profile_id, None)
            return True
        except Exception as verify_exc:
          self._emit_log(f"[Worker] Verify delete failed for {profile_id}: {verify_exc}")
        backoff = min(30.0, 3.0 * (2 ** (attempt - 1)))
        if AdsPowerManager._is_rate_limit_error(str(exc)):
          backoff = max(backoff, min(45.0, 5.0 * attempt))
        self._interruptible_sleep(backoff)
    self._schedule_orphan_delete(profile_id, reason)
    return False

  def _emit_log(self, message: str) -> None:
    append_session_log(message)
    self.log.emit(message)

  def _emit_profile_update(
    self,
    profile: ProfileSpec,
    status: ProfileStatus,
    cooldown: int = 0,
    detail: str = "",
  ) -> None:
    key, text = status.to_ui(cooldown_seconds=cooldown, detail=detail)
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
    if proxy_list:
      random.shuffle(proxy_list)
      self._emit_log(
        f"[Worker] Proxy list shuffled for this run ({len(proxy_list)} proxy/proxies)"
      )
    keywords = [keyword.strip() for keyword in (self.config.keywords or []) if keyword and keyword.strip()]
    if not keywords:
      self._emit_log("[Worker] Auto-create mode aborted: no keywords configured.")
      return
    domains = self.config.get_target_domains()
    if not domains:
      self._emit_log("[Worker] Auto-create mode aborted: no target domains configured.")
      return
    cycles_target = max(1, int(self.config.automation_cycles or 1))
    pairs_per_cycle = len(keywords) * len(domains)
    total_profile_dispatch_target = pairs_per_cycle * cycles_target
    dispatched_profiles = 0
    self.cycle_progress.emit(0, cycles_target)
    self._emit_log(
      f"[Worker] Auto-create mode enabled (max concurrent: {max_concurrent}, "
      f"cycles target: {cycles_target}, pairs/cycle: {pairs_per_cycle} "
      f"({len(keywords)} keywords × {len(domains)} sites))"
    )

    if not adspower.wait_until_ready(
      max_wait_sec=120.0,
      poll_sec=5.0,
      stopped=lambda: self._stop_requested,
    ):
      self._emit_log("[Worker] Auto-create mode aborted: AdsPower API did not become ready.")
      return

    active_threads: list[threading.Thread] = []
    run_index = 0
    next_create_allowed_at = 0.0
    create_fail_streak = 0

    while not self._stop_requested and dispatched_profiles < total_profile_dispatch_target:
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
      assigned_pair = self._allocate_next_pair()
      if not assigned_pair[0] or not assigned_pair[1]:
        self._emit_log(
          "[Worker] No keyword/domain pair available for auto run "
          f"{run_index}; skipping profile slot."
        )
        self._interruptible_sleep(1.0)
        continue

      forced_os: Optional[str] = None
      if self._is_mixed_profile_os_mode():
        keyword, domain = assigned_pair
        forced_os = self.result_store.resolve_mixed_profile_os(keyword, domain)
        reason = self.result_store.mixed_profile_os_reason(keyword, domain)
        self._emit_log(
          f"[Worker] Mixed OS for '{keyword}' → {domain}: {forced_os} ({reason})"
        )

      profile = self._create_single_profile_for_run(
        adspower, run_index, chosen_proxy, forced_os=forced_os,
      )
      if not profile:
        create_fail_streak += 1
        retry_delay = self._profile_create_retry_delay(
          create_fail_streak,
          getattr(self, "_last_profile_create_error", ""),
          self.config.launch_interval_min,
        )
        next_create_allowed_at = time.time() + retry_delay
        self._emit_log(
          f"[Worker] Profile create failed ({create_fail_streak}x). "
          f"Next attempt in {retry_delay:.0f}s."
        )
        continue
      create_fail_streak = 0
      remaining_slots = total_profile_dispatch_target - dispatched_profiles
      if remaining_slots <= 0:
        break
      profile.assigned_keyword = assigned_pair[0]
      profile.assigned_domain = assigned_pair[1]
      self._emit_log(
        f"[Worker] {profile.name} assigned pair: '{assigned_pair[0]}' → {assigned_pair[1]}"
      )
      dispatched_profiles += 1
      completed_cycles = dispatched_profiles // pairs_per_cycle
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
        args=(profile, profile.proxy_host, None, assigned_pair),
        daemon=True,
      )
      worker_thread.start()
      active_threads.append(worker_thread)

      delay = random.uniform(self.config.launch_interval_min, self.config.launch_interval_max)
      next_create_allowed_at = time.time() + delay
      self._emit_log(
        f"[Worker] Active profiles: {len(active_threads)}/{max_concurrent}. "
        f"Profile progress: {dispatched_profiles}/{total_profile_dispatch_target} "
        f"(cycles done: {completed_cycles}/{cycles_target}). "
        f"Next create in {delay:.1f}s (counted from creation time)."
      )

    if not self._stop_requested and dispatched_profiles >= total_profile_dispatch_target:
      self.cycle_progress.emit(cycles_target, cycles_target)
      self._emit_log(
        f"[Worker] Pair cycle target reached: {cycles_target} full cycle(s). "
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
        (dispatched_profiles + pairs_per_cycle - 1) // pairs_per_cycle,
      )
      self.cycle_progress.emit(completed_cycles, cycles_target)
      if dispatched_profiles >= total_profile_dispatch_target:
        self._emit_log(
          f"[Worker] Pair cycle target reached: {cycles_target} full cycle(s). "
          "All assigned profiles finished."
        )
      else:
        self._emit_log(
          f"[Worker] Automation dispatch finished: {dispatched_profiles}/{total_profile_dispatch_target} "
          f"profile slots ({completed_cycles}/{cycles_target} cycle(s))."
        )

  def _create_single_profile_for_run(
    self,
    adspower: AdsPowerManager,
    run_number: int,
    proxy: Optional[tuple[str, int, str, str]],
    *,
    forced_os: Optional[str] = None,
  ) -> Optional[ProfileSpec]:
    if self._stop_requested:
      return None
    try:
      proxy_batch = [proxy] if proxy else []
      self._emit_log(f"[Worker] Creating profile for auto run {run_number} via AdsPower API...")
      forced_os_types = [forced_os] if forced_os else None
      created = adspower.create_profiles_batch(
        proxies=proxy_batch,
        group_id=self.config.adspower_group_id,
        total=1,
        profile_os_mode=self.config.profile_os_mode,
        forced_os_types=forced_os_types,
      )
      profile = created[0]
      self._created_profile_ids.add(profile.profile_id)
      self._profile_created_at[profile.profile_id] = time.time()
      self._emit_profile_update(profile, ProfileStatus.CREATING_PROFILE)
      self._emit_log(f"[Worker] Created profile for auto run {run_number}: {profile.name} ({profile.profile_id})")
      return profile
    except Exception as exc:
      self._last_profile_create_error = str(exc)
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
    assigned_pair: Optional[tuple[str, str]] = None,
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
    self._emit_profile_update(profile, ProfileStatus.LAUNCHING)
    display_proxy_key = self._proxy_key_for_profile(profile)
    self._emit_log(f"[Worker] Launching {profile.name} via proxy {display_proxy_key}")
    if assigned_pair is None:
      assigned_pair = self._assign_profile_pair(profile)
    keyword, domain = assigned_pair
    if keyword and domain:
      profile.assigned_keyword = keyword
      profile.assigned_domain = domain
    outcome = "error"
    max_tunnel_attempts = 2
    profile_stopped = False
    last_serp_bot: Optional[SerpBot] = None
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

        def profile_log(msg: str) -> None:
          self._touch_profile_progress(profile.profile_id)
          self._emit_log(msg)

        serp_bot = SerpBot(self.config, profile_log)
        last_serp_bot = serp_bot
        self._start_session_stall_watchdog(profile, manager, stop_event)

        def on_ui_status(status_key: str, display_text: str) -> None:
          self._touch_profile_progress(profile.profile_id)
          self._on_profile_phase(profile.profile_id, status_key)
          self._emit_ui_status(profile, status_key, display_text)

        def on_failure(failed_profile: ProfileSpec, context: str, exc: BaseException) -> None:
          self._touch_profile_progress(profile.profile_id)
          self._emit_profile_update(
            failed_profile,
            ProfileStatus.ERROR,
            detail=short_error_detail(exc, context),
          )
          if self._failure_callback:
            self._failure_callback(failed_profile.profile_id)

        try:
          outcome = serp_bot.run_session(
            ws_endpoint,
            profile,
            stop_event=stop_event,
            assigned_keyword=keyword,
            assigned_domain=domain,
            on_ui_status=on_ui_status,
            on_traffic=lambda delta, delta_target, delta_other, wire_delta, wire_target, wire_other: self.traffic_update.emit(
              profile.profile_id,
              display_proxy_key,
              int(delta),
              int(delta_target),
              int(delta_other),
              int(wire_delta),
              int(wire_target),
              int(wire_other),
            ),
            on_failure=on_failure,
            on_keyword_exhausted=lambda kw: self._on_keyword_exhausted(profile, kw),
            on_target_click=self.target_click_logged.emit,
            on_captcha_stat=self.captcha_stat.emit,
            on_session_cleanup=stop_profile_once,
          )
        finally:
          self._stop_session_stall_watchdog(profile.profile_id)
      except Exception as exc:
        self._emit_log(f"[Worker] Error on {profile.name}: {exc}")
        self._emit_profile_update(
          profile,
          ProfileStatus.ERROR,
          detail=short_error_detail(exc, "launch"),
        )
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

    if (
      self.config.skip_exhausted_pairs_in_session
      and outcome == "not_found"
      and last_serp_bot is not None
      and last_serp_bot.last_search_exhaustion_eligible
      and keyword
      and domain
    ):
      self._mark_session_pair_exhausted(keyword, domain)

    if self._should_delete_profile_after_run(outcome) and profile.profile_id not in self._deleted_profile_ids:
      pre_delete_line = build_pre_delete_log_line(
        profile.name,
        outcome,
        keyword or profile.assigned_keyword or "",
        domain or profile.assigned_domain or "",
      )
      if pre_delete_line:
        self._emit_log(pre_delete_line)
      failure_url = outcome_to_failure_url(outcome)
      if failure_url:
        log_path = (self.config.session_click_log_path or "").strip()
        if log_path:
          log_session_failure(
            log_path,
            profile_name=profile.name,
            device=profile.device_label,
            keyword=(keyword or profile.assigned_keyword or "").strip(),
            site=(domain or profile.assigned_domain or "").strip(),
            failure_url=failure_url,
          )
      if self._delete_profile_with_retry(manager, profile.profile_id, reason=f"outcome:{outcome}"):
        self._emit_log(f"[Worker] Deleted profile {profile.name} after run ({outcome}); proxy entry kept.")
        self.profiles_changed.emit()
      else:
        self._emit_log(
          f"[Worker] Failed to delete profile {profile.name} after run ({outcome}); "
          "orphan sweep scheduled"
        )
    if scheduler:
      scheduler.mark_finished(proxy_key)
    self._profile_stop_events.pop(profile.profile_id, None)
    self._clear_dwell_tracking(profile.profile_id)
    with self._phase_lock:
      self._profile_phases.pop(profile.profile_id, None)
      self._graceful_stop_pending.discard(profile.profile_id)

    if outcome in ("error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error", "proxy_connect_failed"):
      self._emit_profile_update(
        profile,
        ProfileStatus.ERROR,
        detail=outcome.replace("_", " "),
      )
    elif outcome == "stopped":
      self._emit_profile_update(profile, ProfileStatus.IDLE)
    elif outcome not in ("success", "not_found", "failed"):
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
    self.profile_finished.emit(profile.profile_id, outcome)

  def _interruptible_sleep(self, seconds: float) -> None:
    end_time = time.time() + seconds
    while time.time() < end_time and not self._stop_requested:
      time.sleep(0.25)

  def _should_delete_profile_after_run(self, outcome: str) -> bool:
    if outcome in ("success", "not_found", "failed", "error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error", "proxy_connect_failed"):
      return True
    if outcome == "stopped" and self.config.auto_create_profiles:
      return True
    return False

  def _is_mixed_profile_os_mode(self) -> bool:
    mode = (self.config.profile_os_mode or "mixed").strip().lower()
    return mode in ("mixed", "")

  def _allocate_next_pair(self) -> tuple[str, str]:
    keywords = [keyword.strip() for keyword in (self.config.keywords or []) if keyword and keyword.strip()]
    domains = self.config.get_target_domains()
    if not keywords or not domains:
      return ("", "")
    skip_pairs = (
      self._session_exhausted_pairs
      if self.config.skip_exhausted_pairs_in_session
      else None
    )
    keyword, domain = self.pair_rotation.allocate_pair(
      keywords,
      domains,
      skip_pairs=skip_pairs,
    )
    if not keyword or not domain:
      if skip_pairs:
        self._emit_log(
          "[Worker] No keyword/domain pair available: all pairs session-skipped "
          "after full SERP scans with no target."
        )
    return keyword, domain

  def _assign_profile_pair(self, profile: ProfileSpec) -> tuple[str, str]:
    keywords = [keyword.strip() for keyword in (self.config.keywords or []) if keyword and keyword.strip()]
    domains = self.config.get_target_domains()
    if not keywords or not domains:
      return ("", "")

    if (profile.assigned_keyword or "").strip() and (profile.assigned_domain or "").strip():
      return profile.assigned_keyword.strip(), profile.assigned_domain.strip()

    keyword, domain = self._allocate_next_pair()
    self._emit_log(
      f"[Worker] {profile.name} assigned pair: '{keyword}' → {domain}"
    )
    return keyword, domain

  def _on_keyword_exhausted(self, profile: ProfileSpec, keyword: str) -> None:
    domain = (profile.assigned_domain or "").strip()
    self._emit_log(
      f"[Worker] {profile.name} checked all configured SERP pages for "
      f"'{keyword}' → {domain or 'target'} without finding the target."
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
  proxy_traffic_update = pyqtSignal(str, "qint64")
  profile_traffic_update = pyqtSignal(str, "qint64", "qint64", "qint64", "qint64")
  cycle_progress_update = pyqtSignal(int, int)
  keyword_excluded = pyqtSignal(str)
  profiles_sync_requested = pyqtSignal()
  profile_created = pyqtSignal(object)
  profile_deleted = pyqtSignal(str)
  global_finished = pyqtSignal()
  profile_finished = pyqtSignal(str, str)
  target_click_logged = pyqtSignal()
  captcha_stat = pyqtSignal(str)

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
    self._captcha_started_at: Dict[str, float] = {}
    self._error_started_at: Dict[str, float] = {}
    self._status: Dict[str, ProfileStatus] = {}
    self._keyword_rotation = KeywordRotationStore()
    self._keyword_exclusion = KeywordExclusionStore()
    self._pair_rotation = PairRotationStore()
    self._proxy_traffic_totals: Dict[str, int] = {}
    self._profile_traffic_totals: Dict[str, int] = {}
    self._session_traffic_total: int = 0
    self._session_target_traffic: int = 0
    self._session_other_traffic: int = 0

  def clear_keyword_exclusions(self, target_domain: str) -> int:
    return self._keyword_exclusion.clear_domain(target_domain)

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
      pair_rotation=self._pair_rotation,
    )
    self._global_worker.log.connect(self.log.emit)
    self._global_worker.profile_update.connect(self._on_profile_update)
    self._global_worker.traffic_update.connect(self._on_traffic_update)
    self._global_worker.cycle_progress.connect(self.cycle_progress_update.emit)
    self._global_worker.keyword_excluded.connect(self.keyword_excluded.emit)
    self._global_worker.profiles_changed.connect(self.profiles_sync_requested.emit)
    self._global_worker.profile_created.connect(self.profile_created.emit)
    self._global_worker.profile_deleted.connect(self.profile_deleted.emit)
    self._global_worker.profile_finished.connect(self.profile_finished.emit)
    self._global_worker.target_click_logged.connect(self.target_click_logged.emit)
    self._global_worker.captcha_stat.connect(self.captcha_stat.emit)
    self._global_worker.error.connect(lambda msg: self._emit_log(f"[Worker] {msg}"))
    self._global_worker.finished_ok.connect(self.global_finished.emit)
    self._global_worker.start()
    self._emit_log("[Controller] Global automated bot started")
    return True

  def stop_global(self) -> None:
    if self._global_worker and self._global_worker.isRunning():
      self._global_worker.request_stop()
      self._emit_log("[Controller] Global bot stop requested")

  def reset_session_traffic(self) -> None:
    self._session_traffic_total = 0
    self._session_target_traffic = 0
    self._session_other_traffic = 0
    self._profile_traffic_totals.clear()
    self._proxy_traffic_totals.clear()

  def get_session_traffic_total(self) -> int:
    return int(self._session_traffic_total)

  def start_profile_manual(self, profile_id: str, config: BotConfig) -> bool:
    profile = self._profiles.get(profile_id)
    if not profile:
      self._emit_log(f"[Controller] Unknown profile {profile_id}")
      return False

    if profile_id in self._profile_workers and self._profile_workers[profile_id].isRunning():
      self._emit_log(f"[Controller] {profile.name} is already running")
      return False

    proxy_key = profile.proxy_host
    if not self._scheduler.can_run(proxy_key):
      remaining = int(self._scheduler.seconds_until_proxy(proxy_key))
      self._emit_log(f"[Controller] Proxy {proxy_key} busy/cooling ({remaining}s left)")
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
      keyword_exclusion=self._keyword_exclusion,
      pair_rotation=self._pair_rotation,
    )
    worker.log.connect(self.log.emit)
    worker.profile_update.connect(self._on_profile_update)
    worker.traffic_update.connect(self._on_traffic_update)
    worker.keyword_excluded.connect(self.keyword_excluded.emit)
    worker.profiles_changed.connect(self.profiles_sync_requested.emit)
    worker.profile_deleted.connect(self.profile_deleted.emit)
    worker.finished_profile.connect(self._on_profile_finished)
    worker.target_click_logged.connect(self.target_click_logged.emit)
    worker.captcha_stat.connect(self.captcha_stat.emit)
    self._profile_workers[profile_id] = worker
    key, text = ProfileStatus.LAUNCHING.to_ui()
    self.profile_update.emit(profile_id, key, text)
    worker.start()
    self._emit_log(f"[Controller] Manual start: {profile.name}")
    return True

  def pause_profile(self, profile_id: str) -> None:
    profile = self._profiles.get(profile_id)
    profile_name = profile.name if profile else profile_id

    stop_event = self._stop_events.get(profile_id)
    if stop_event:
      stop_event.set()
      self._emit_log(f"[Controller] Graceful stop requested for {profile_name}")
      return

    if self._global_worker and self._global_worker.isRunning():
      if self._global_worker.request_profile_pause(profile_id):
        self._emit_log(f"[Controller] Graceful stop requested for {profile_name} (automation)")
      else:
        self._emit_log(f"[Controller] {profile_name} is not running")
      return

    self._emit_log(f"[Controller] {profile_name} is not running")

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
    self._emit_log(f"[Controller] Force terminated {profile_name}")

  def _emit_log(self, message: str) -> None:
    append_session_log(message)
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

  def _on_traffic_update(
    self,
    profile_id: str,
    proxy_key: str,
    delta_allowed: int,
    delta_target_allowed: int = 0,
    delta_other_allowed: int = 0,
    delta_wire: int = 0,
    delta_wire_target: int = 0,
    delta_wire_other: int = 0,
  ) -> None:
    _ = delta_allowed
    _ = delta_target_allowed
    _ = delta_other_allowed
    if delta_wire <= 0:
      return
    profile_total = self._profile_traffic_totals.get(profile_id, 0) + int(delta_wire)
    self._profile_traffic_totals[profile_id] = profile_total
    total = self._proxy_traffic_totals.get(proxy_key, 0) + int(delta_wire)
    self._proxy_traffic_totals[proxy_key] = total
    self._session_traffic_total += int(delta_wire)
    self._session_target_traffic += int(delta_wire_target)
    self._session_other_traffic += int(delta_wire_other)
    self.proxy_traffic_update.emit(proxy_key, total)
    self.profile_traffic_update.emit(
      profile_id,
      profile_total,
      self._session_traffic_total,
      self._session_target_traffic,
      self._session_other_traffic,
    )

  def _map_status_key(self, profile_id: str, status_key: str) -> None:
    mapping = {
      UiStatusKey.CREATING_PROFILE.value: ProfileStatus.CREATING_PROFILE,
      UiStatusKey.LAUNCHING.value: ProfileStatus.LAUNCHING,
      UiStatusKey.CHECKING_IP.value: ProfileStatus.CHECKING_IP,
      UiStatusKey.WARMING_UP.value: ProfileStatus.WARMING_UP,
      UiStatusKey.SEARCHING.value: ProfileStatus.SEARCHING,
      UiStatusKey.VISITING_SITE.value: ProfileStatus.VISITING_SITE,
      UiStatusKey.CAPTCHA.value: ProfileStatus.CAPTCHA_WAIT,
      UiStatusKey.CAPTCHA_MANUAL.value: ProfileStatus.CAPTCHA_MANUAL,
      UiStatusKey.ERROR.value: ProfileStatus.ERROR,
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

    if outcome in ("error", "blocked", "ip_changed", "ip_unavailable", "tunnel_error", "proxy_connect_failed"):
      key, text = ProfileStatus.ERROR.to_ui(detail=outcome.replace("_", " "))
      self.profile_update.emit(profile_id, key, text)
    elif outcome != "success":
      key, text = ProfileStatus.IDLE.to_ui()
      self.profile_update.emit(profile_id, key, text)

    cooldown = self._config.proxy_cooldown_seconds if self._config else 1800
    self._cooldown_until[profile_id] = time.time() + cooldown
    key, text = ProfileStatus.COOLDOWN.to_ui(cooldown)
    self.profile_update.emit(profile_id, key, text)
    profile_name = profile.name if profile else profile_id
    self._emit_log(f"[Controller] {profile_name} finished ({outcome}), proxy cooldown started")
    self.profile_finished.emit(profile_id, outcome)

  def get_session_elapsed(self, profile_id: str) -> int:
    started = self._session_started_at.get(profile_id)
    if not started:
      return 0
    return max(0, int(time.time() - started))

  def get_captcha_elapsed(self, profile_id: str) -> int:
    started = self._captcha_started_at.get(profile_id)
    if not started:
      return 0
    return max(0, int(time.time() - started))

  def get_error_elapsed(self, profile_id: str) -> int:
    started = self._error_started_at.get(profile_id)
    if not started:
      return 0
    return max(0, int(time.time() - started))

  def _track_session_elapsed(self, profile_id: str, status_key: str, display_text: str) -> None:
    if status_key == UiStatusKey.CLOSED.value:
      self._clear_session_elapsed(profile_id)
      return

    if status_key in SESSION_ELAPSED_KEYS:
      self._session_started_at.setdefault(profile_id, time.time())

    if status_key in CAPTCHA_ELAPSED_KEYS:
      self._captcha_started_at.setdefault(profile_id, time.time())
    else:
      self._captcha_started_at.pop(profile_id, None)

    if status_key in ERROR_ELAPSED_KEYS:
      self._error_started_at.setdefault(profile_id, time.time())
    else:
      self._error_started_at.pop(profile_id, None)

  def _clear_session_elapsed(self, profile_id: str) -> None:
    self._session_started_at.pop(profile_id, None)
    self._captcha_started_at.pop(profile_id, None)
    self._error_started_at.pop(profile_id, None)

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
      self._captcha_started_at.pop(profile_id, None)
      self._error_started_at.pop(profile_id, None)
      self._profile_traffic_totals.pop(profile_id, None)
    if profile_ids:
      self._emit_log(f"[Controller] Removed {len(profile_ids)} profile(s) from local state")

  def get_status(self, profile_id: str) -> ProfileStatus:
    return self._status.get(profile_id, ProfileStatus.IDLE)
