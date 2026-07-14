from html import escape
import re
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QPalette, QTextCursor
from PyQt6.QtWidgets import (
  QCheckBox,
  QComboBox,
  QDoubleSpinBox,
  QFormLayout,
  QFrame,
  QGroupBox,
  QHBoxLayout,
  QHeaderView,
  QLabel,
  QLineEdit,
  QListWidget,
  QListWidgetItem,
  QMainWindow,
  QPlainTextEdit,
  QPushButton,
  QSpinBox,
  QTabWidget,
  QTableWidget,
  QTableWidgetItem,
  QTextEdit,
  QVBoxLayout,
  QWidget,
)

from config.bot_config import BotConfig
from config.settings_store import load_settings, save_settings
from core.profile_status import (
  CAPTCHA_ELAPSED_KEYS,
  ERROR_ELAPSED_KEYS,
  SESSION_ELAPSED_KEYS,
  UiStatusKey,
  ui_label,
)
from core.worker import ProfileController
from services.adspower_manager import AdsPowerManager, ProfileSpec
from utils.cursor_agent_client import CURSOR_SDK_AVAILABLE, CursorChatController
from utils.cursor_models import CURSOR_MODEL_CHOICES, DEFAULT_CURSOR_MODEL, normalize_cursor_model
from utils.csv_logger import (
  SessionClickCsvLogger,
  aggregate_keyword_clicks,
  count_target_not_found_in_session,
  format_result_session_label,
  list_session_result_files,
  new_session_click_log_path,
  session_result_window,
)
from utils.self_healer import SelfHealer

PROFILE_ID_ROLE = Qt.ItemDataRole.UserRole

STYLESHEET = """
QMainWindow {
  background-color: #080a0f;
}
QWidget {
  background-color: transparent;
  color: #e2e8f0;
  font-family: "Segoe UI", "Inter", sans-serif;
  font-size: 13px;
}
QFrame#panelCard {
  background-color: #11141c;
  border: 1px solid #1e2430;
  border-radius: 14px;
}
QLabel#appTitle {
  font-size: 24px;
  font-weight: 700;
  color: #f8fafc;
  letter-spacing: -0.5px;
}
QLabel#appSubtitle {
  font-size: 12px;
  color: #64748b;
  margin-top: 2px;
}
QLabel#sectionTitle {
  font-size: 11px;
  font-weight: 600;
  color: #64748b;
  letter-spacing: 0.8px;
}
QLabel#trafficTotalTitle {
  font-size: 11px;
  font-weight: 600;
  color: #94a3b8;
  letter-spacing: 0.8px;
}
QLabel#trafficTotalValue {
  font-size: 24px;
  font-weight: 700;
  color: #ffffff;
}
QLabel#hintLabel {
  color: #64748b;
  font-size: 11px;
}
QTabWidget::pane {
  border: 1px solid #1e2430;
  border-radius: 14px;
  background: #0d1017;
  top: -1px;
  padding: 14px;
}
QTabBar::tab {
  background: #11141c;
  color: #94a3b8;
  border: 1px solid #1e2430;
  padding: 10px 22px;
  margin-right: 4px;
  border-top-left-radius: 10px;
  border-top-right-radius: 10px;
  font-weight: 600;
}
QTabBar::tab:selected {
  background: #1a1f2b;
  color: #f1f5f9;
  border-bottom-color: #1a1f2b;
}
QTabBar::tab:hover:!selected {
  background: #151922;
  color: #cbd5e1;
}
QGroupBox {
  font-weight: 600;
  font-size: 13px;
  color: #cbd5e1;
  border: 1px solid #1e2430;
  border-radius: 12px;
  margin-top: 16px;
  padding: 16px 12px 12px 12px;
  background: #11141c;
}
QGroupBox::title {
  subcontrol-origin: margin;
  left: 14px;
  padding: 0 8px;
  color: #94a3b8;
}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox {
  background-color: #0a0d14;
  color: #e2e8f0;
  border: 1px solid #252b38;
  border-radius: 8px;
  padding: 8px 10px;
  selection-background-color: #6366f1;
  selection-color: #ffffff;
}
QPlainTextEdit#settingsMultilineEdit, QTextEdit#settingsMultilineEdit {
  background-color: #0a0d14;
  color: #e2e8f0;
}
QPlainTextEdit#settingsMultilineEdit {
  padding: 8px 10px;
}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {
  border: 1px solid #6366f1;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
  background: #1a1f2b;
  border: none;
  width: 18px;
}
QTableWidget#profileTable {
  background-color: #0d1017;
  border: 1px solid #1e2430;
  border-radius: 12px;
  gridline-color: #1a2030;
  alternate-background-color: #0f1219;
  outline: none;
}
QTableWidget#profileTable::item {
  padding: 6px 8px;
  border: none;
}
QTableWidget#profileTable::item:selected {
  background-color: #1e2a4a;
  color: #f1f5f9;
}
QTableWidget#profileTable::indicator {
  width: 20px;
  height: 20px;
  border-radius: 5px;
  border: 2px solid #818cf8;
  background-color: #1a2235;
}
QTableWidget#profileTable::indicator:unchecked:hover {
  border-color: #c7d2fe;
  background-color: #2d3a56;
}
QTableWidget#profileTable::indicator:checked {
  border: 2px solid #c7d2fe;
  background-color: #6366f1;
}
QTableWidget#profileTable::indicator:checked:hover {
  border-color: #e0e7ff;
  background-color: #818cf8;
}
QListWidget#resultSessionList {
  background-color: #0d1017;
  border: 1px solid #1e2430;
  border-radius: 12px;
  outline: none;
  padding: 4px;
}
QListWidget#resultSessionList::item {
  padding: 10px 12px;
  border-radius: 8px;
  color: #cbd5e1;
}
QListWidget#resultSessionList::item:selected {
  background-color: #1e2a4a;
  color: #f1f5f9;
}
QListWidget#resultSessionList::item:hover:!selected {
  background-color: #151922;
}
QTableWidget#resultTable {
  background-color: #0d1017;
  border: 1px solid #1e2430;
  border-radius: 12px;
  gridline-color: #1a2030;
  alternate-background-color: #0f1219;
  outline: none;
}
QTableWidget#resultTable::item {
  padding: 6px 8px;
  border: none;
}
QTableWidget#resultTable::item:selected {
  background-color: #1e2a4a;
  color: #f1f5f9;
}
QHeaderView::section {
  background-color: #11141c;
  color: #94a3b8;
  padding: 10px 8px;
  border: none;
  border-right: 1px solid #1a2030;
  border-bottom: 1px solid #252b38;
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
QCheckBox {
  spacing: 8px;
  color: #e2e8f0;
  font-weight: 600;
}
QCheckBox::indicator {
  width: 20px;
  height: 20px;
  border-radius: 5px;
  border: 2px solid #818cf8;
  background: #1a2235;
}
QCheckBox::indicator:unchecked:hover {
  border-color: #c7d2fe;
  background: #2d3a56;
}
QCheckBox::indicator:checked {
  background: #6366f1;
  border: 2px solid #c7d2fe;
}
QCheckBox::indicator:checked:hover {
  background: #818cf8;
  border-color: #e0e7ff;
}
QPushButton {
  border: none;
  border-radius: 8px;
  padding: 9px 16px;
  font-weight: 600;
  font-size: 12px;
}
QPushButton#btnCreate {
  background-color: #4f46e5;
  color: #ffffff;
}
QPushButton#btnCreate:hover { background-color: #6366f1; }
QPushButton#btnCreate:pressed { background-color: #4338ca; }
QPushButton#btnRefresh {
  background-color: #1e2430;
  color: #cbd5e1;
  border: 1px solid #475569;
}
QPushButton#btnRefresh:hover {
  background-color: #334155;
  border-color: #94a3b8;
  color: #f1f5f9;
}
QPushButton#btnRefresh:pressed { background-color: #1e293b; }
QPushButton#btnKeywordClicks {
  background-color: #1e2430;
  color: #cbd5e1;
  border: 1px solid #475569;
  padding: 7px 14px;
}
QPushButton#btnKeywordClicks:hover {
  background-color: #334155;
  border-color: #94a3b8;
  color: #f1f5f9;
}
QPushButton#btnKeywordClicks:checked {
  background-color: #4f46e5;
  border-color: #818cf8;
  color: #ffffff;
}
QPushButton#btnStartAuto {
  background-color: #059669;
  color: #ffffff;
}
QPushButton#btnStartAuto:hover { background-color: #10b981; }
QPushButton#btnStartAuto:pressed { background-color: #047857; }
QPushButton#btnStopAuto {
  background-color: #dc2626;
  color: #ffffff;
}
QPushButton#btnStopAuto:hover { background-color: #ef4444; }
QPushButton#btnStopAuto:pressed { background-color: #b91c1c; }
QPushButton#btnSaveSettings {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #6366f1, stop:1 #8b5cf6);
  color: #ffffff;
  min-width: 180px;
  padding: 12px 24px;
  font-size: 13px;
  border: 1px solid #7c3aed;
}
QPushButton#btnSaveSettings:hover {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #818cf8, stop:1 #a78bfa);
  border-color: #a5b4fc;
}
QPushButton#btnSaveSettings:pressed {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #4f46e5, stop:1 #6d28d9);
  border-color: #5b21b6;
}
QPushButton#btnSendManualFix {
  background-color: #4f46e5;
  color: #ffffff;
  min-width: 140px;
  border: 1px solid #6366f1;
}
QPushButton#btnSendManualFix:hover {
  background-color: #6366f1;
  border-color: #a5b4fc;
}
QPushButton#btnSendManualFix:pressed { background-color: #4338ca; }
QPushButton#btnSendManualFix:disabled {
  background-color: #1a1f2b;
  color: #475569;
  border: 1px solid #1e2430;
}
QPushButton#btnSendAiFix {
  background-color: #4f46e5;
  color: #ffffff;
  min-width: 120px;
  border: 1px solid #6366f1;
}
QPushButton#btnSendAiFix:hover {
  background-color: #6366f1;
  border-color: #a5b4fc;
}
QPushButton#btnSendAiFix:pressed { background-color: #4338ca; }
QPushButton#btnSendAiFix:disabled {
  background-color: #1a1f2b;
  color: #475569;
  border: 1px solid #1e2430;
}
QTextEdit#aiFixChat {
  background-color: #06080c;
  color: #cbd5e1;
  border: 1px solid #1e2430;
  border-radius: 12px;
  padding: 12px;
  font-family: "Segoe UI", "Inter", sans-serif;
  font-size: 13px;
  selection-background-color: #312e81;
}
QPlainTextEdit#aiFixPrompt {
  background-color: #0a0d14;
  border: 1px solid #252b38;
  border-radius: 10px;
  padding: 10px;
  font-size: 13px;
}
QPushButton#btnRowStart {
  background-color: #065f46;
  color: #ecfdf5;
  padding: 2px 4px;
  font-size: 13px;
  border-radius: 6px;
  border: 1px solid #10b981;
  min-width: 26px;
  max-width: 30px;
}
QPushButton#btnRowStart:hover { background-color: #047857; border-color: #34d399; }
QPushButton#btnRowStart:pressed { background-color: #064e3b; }
QPushButton#btnRowPause {
  background-color: #78350f;
  color: #fef3c7;
  padding: 2px 4px;
  font-size: 13px;
  border-radius: 6px;
  border: 1px solid #f59e0b;
  min-width: 26px;
  max-width: 30px;
}
QPushButton#btnRowPause:hover { background-color: #92400e; border-color: #fbbf24; }
QPushButton#btnRowPause:pressed { background-color: #713f12; }
QPushButton#btnRowKill {
  background-color: #7f1d1d;
  color: #fee2e2;
  padding: 2px 4px;
  font-size: 13px;
  border-radius: 6px;
  border: 1px solid #ef4444;
  min-width: 26px;
  max-width: 30px;
}
QPushButton#btnRowKill:hover { background-color: #991b1b; border-color: #f87171; }
QPushButton#btnRowKill:pressed { background-color: #6b1515; }
QPushButton#btnRowDelete {
  background-color: #1e2430;
  color: #fca5a5;
  padding: 2px 4px;
  font-size: 13px;
  border-radius: 6px;
  border: 1px solid #f87171;
  min-width: 26px;
  max-width: 30px;
}
QPushButton#btnRowDelete:hover {
  background-color: #3f1d1d;
  color: #fecaca;
  border-color: #fca5a5;
}
QPushButton#btnRowDelete:pressed { background-color: #2a1212; }
QPushButton#btnBulkStart {
  background-color: #059669;
  color: white;
  border: 1px solid #10b981;
}
QPushButton#btnBulkStart:hover { background-color: #10b981; }
QPushButton#btnBulkStart:pressed { background-color: #047857; }
QPushButton#btnBulkPause {
  background-color: #d97706;
  color: white;
  border: 1px solid #fbbf24;
}
QPushButton#btnBulkPause:hover { background-color: #f59e0b; }
QPushButton#btnBulkPause:pressed { background-color: #b45309; }
QPushButton#btnBulkKill {
  background-color: #dc2626;
  color: white;
  border: 1px solid #f87171;
}
QPushButton#btnBulkKill:hover { background-color: #ef4444; }
QPushButton#btnBulkKill:pressed { background-color: #b91c1c; }
QPushButton#btnBulkDelete {
  background-color: #1e2430;
  color: #fca5a5;
  border: 1px solid #f87171;
}
QPushButton#btnBulkDelete:hover {
  background-color: #3f1d1d;
  color: #fecaca;
}
QPushButton#btnBulkDelete:pressed { background-color: #2a1212; }
QPushButton:disabled {
  background-color: #1a1f2b;
  color: #475569;
  border: 1px solid #1e2430;
}
QPlainTextEdit#logView {
  background-color: #06080c;
  color: #94a3b8;
  border: 1px solid #1e2430;
  border-radius: 12px;
  padding: 10px;
  font-family: "Cascadia Code", Consolas, monospace;
  font-size: 11px;
  selection-background-color: #312e81;
}
QScrollBar:vertical {
  background: #0d1017;
  width: 10px;
  border-radius: 5px;
}
QScrollBar::handle:vertical {
  background: #2d3548;
  border-radius: 5px;
  min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #475569; }
"""

_SESSION_GREEN = QColor("#34d399")
STATUS_COLORS = {
  UiStatusKey.CREATING_PROFILE.value: _SESSION_GREEN,
  UiStatusKey.LAUNCHING.value: _SESSION_GREEN,
  UiStatusKey.CHECKING_IP.value: _SESSION_GREEN,
  UiStatusKey.WARMING_UP.value: _SESSION_GREEN,
  UiStatusKey.SEARCHING.value: _SESSION_GREEN,
  UiStatusKey.VISITING_SITE.value: _SESSION_GREEN,
  UiStatusKey.CAPTCHA.value: QColor("#fbbf24"),
  UiStatusKey.CAPTCHA_MANUAL.value: QColor("#fbbf24"),
  UiStatusKey.ERROR.value: QColor("#f87171"),
  UiStatusKey.SELF_HEALING.value: QColor("#c084fc"),
  UiStatusKey.CLOSED.value: QColor("#64748b"),
}

_TIMER_SUFFIX_RE = re.compile(r" \[\d{2,}:\d{2}\]$")


class AiFixPromptEdit(QPlainTextEdit):
  def __init__(self, on_send, parent=None):
    super().__init__(parent)
    self._on_send = on_send

  def keyPressEvent(self, event: QKeyEvent) -> None:
    if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
      if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
        super().keyPressEvent(event)
        return
      event.accept()
      self._on_send()
      return
    super().keyPressEvent(event)


class UiMainWindow(QMainWindow):
  COL_CHECK = 0
  COL_NO = 1
  COL_ID = 2
  COL_NAME = 3
  COL_OS = 4
  COL_BROWSER = 5
  COL_PROXY = 6
  COL_TRAFFIC = 7
  COL_STATUS = 8
  COL_ACTIONS = 9

  COLUMNS = (
    "Select",
    "No",
    "ID",
    "Name",
    "Device",
    "Browser",
    "Proxy / IP",
    "Traffic",
    "Status",
    "Actions",
  )

  def __init__(self):
    super().__init__()
    self.setWindowTitle("SERP Bot")
    self.resize(1360, 900)
    self.setStyleSheet(STYLESHEET)

    self._profiles: list[ProfileSpec] = []
    self._controller = ProfileController(self)
    self._project_root = Path(__file__).resolve().parents[1]
    self._cursor_chat = CursorChatController(self._project_root)
    self._self_healer = SelfHealer(project_root=self._project_root, parent=self)
    self._controller.attach_self_healer(self._self_healer)
    self._status_map: dict[str, tuple[str, str]] = {}
    self._profile_traffic_totals: dict[str, int] = {}
    self._session_traffic_total: int = 0
    self._overall_clicks_session: int = 0
    self._session_click_log_path: str = ""
    self._global_running = False
    self._syncing_select_all = False
    self._ai_fix_assistant_open = False
    self._ai_chat_persist_timer = QTimer(self)
    self._ai_chat_persist_timer.setSingleShot(True)
    self._ai_chat_persist_timer.setInterval(500)
    self._ai_chat_persist_timer.timeout.connect(self._persist_ai_chat_state)
    self._live_profile_refresh_timer = QTimer(self)
    self._live_profile_refresh_timer.setSingleShot(True)
    self._live_profile_refresh_timer.setInterval(500)
    self._live_profile_refresh_timer.timeout.connect(self._refresh_profiles_live)
    self._pending_live_refresh = False
    self._live_sync_queue_count = 0
    self._live_sync_inflight = False

    self._build_ui()
    self._load_saved_settings()
    self._wire_signals()

    self._cooldown_timer = QTimer(self)
    self._cooldown_timer.setInterval(1000)
    self._cooldown_timer.timeout.connect(self._tick_cooldowns)
    self._cooldown_timer.start()
    self._log_startup_diagnostics()
    QTimer.singleShot(0, self._run_initial_refresh)

  def _run_initial_refresh(self) -> None:
    self.append_log("[UI] Initial startup refresh triggered.")
    self.on_refresh_profiles()

  def _log_startup_diagnostics(self) -> None:
    try:
      cfg = BotConfig(
        capsolver_api_key="",
        adspower_api_url=self.adspower_api_url.text().strip(),
        adspower_api_key=self.adspower_api_key.text().strip(),
      )
      mgr = AdsPowerManager(cfg.adspower_url, cfg.adspower_api_key)
      ok, message = mgr.check_connection()
      prefix = "[UI] AdsPower OK:" if ok else "[UI] AdsPower WARN:"
      self.append_log(f"{prefix} {message}")
    except Exception as exc:
      self.append_log(f"[UI] Startup diagnostic failed: {exc}")

  def _build_ui(self) -> None:
    self.tabs = QTabWidget()
    self.setCentralWidget(self.tabs)
    self.tabs.addTab(self._build_dashboard_tab(), "Dashboard")
    self.tabs.addTab(self._build_ai_fix_tab(), "AI Fix")
    self.tabs.addTab(self._build_settings_tab(), "Settings")
    self.tabs.addTab(self._build_result_tab(), "Result")
    self._result_tab_index = self.tabs.count() - 1

  @staticmethod
  def _card_panel(inner: QHBoxLayout | QVBoxLayout) -> QFrame:
    panel = QFrame()
    panel.setObjectName("panelCard")
    panel_layout = QVBoxLayout(panel)
    panel_layout.setContentsMargins(14, 12, 14, 12)
    panel_layout.addLayout(inner)
    return panel

  def _build_dashboard_tab(self) -> QWidget:
    tab = QWidget()
    layout = QVBoxLayout(tab)
    layout.setSpacing(14)
    layout.setContentsMargins(4, 4, 4, 4)

    header = QVBoxLayout()
    header.setSpacing(2)
    title = QLabel("SERP Automation")
    title.setObjectName("appTitle")
    subtitle = QLabel("AdsPower  ·  Playwright  ·  CapSolver")
    subtitle.setObjectName("appSubtitle")
    header.addWidget(title)
    header.addWidget(subtitle)
    layout.addLayout(header)

    control_bar = QHBoxLayout()
    control_bar.setSpacing(10)
    self.btn_create = QPushButton("Create Profiles")
    self.btn_create.setObjectName("btnCreate")
    self.btn_refresh = QPushButton("Refresh")
    self.btn_refresh.setObjectName("btnRefresh")
    self.btn_start_auto = QPushButton("Start Automation")
    self.btn_start_auto.setObjectName("btnStartAuto")
    self.btn_stop_auto = QPushButton("Stop Automation")
    self.btn_stop_auto.setObjectName("btnStopAuto")
    self.btn_stop_auto.setEnabled(False)
    self.proxy_traffic_title_label = QLabel("Traffic Total")
    self.proxy_traffic_title_label.setObjectName("trafficTotalTitle")
    self.proxy_traffic_total_label = QLabel("0 B")
    self.proxy_traffic_total_label.setObjectName("trafficTotalValue")
    self.present_cycle_title_label = QLabel("Present Cycle")
    self.present_cycle_title_label.setObjectName("trafficTotalTitle")
    self.present_cycle_value_label = QLabel("0 / 0")
    self.present_cycle_value_label.setObjectName("trafficTotalValue")
    self.overall_clicks_title_label = QLabel("Overall Clicks")
    self.overall_clicks_title_label.setObjectName("trafficTotalTitle")
    self.overall_clicks_value_label = QLabel("0")
    self.overall_clicks_value_label.setObjectName("trafficTotalValue")

    count_label = QLabel("Count")
    count_label.setObjectName("sectionTitle")
    self.profile_count_spin = QSpinBox()
    self.profile_count_spin.setRange(1, 500)
    self.profile_count_spin.setValue(20)
    self.profile_count_spin.setToolTip(
      "Number of profiles to create — cannot exceed available proxies (1:1 assignment)"
    )
    self.profile_count_spin.setFixedWidth(72)
    threads_label = QLabel("Threads")
    threads_label.setObjectName("sectionTitle")
    self.threads_spin = QSpinBox()
    self.threads_spin.setRange(1, 500)
    self.threads_spin.setValue(10)
    self.threads_spin.setFixedWidth(72)
    self.threads_spin.setToolTip("How many profiles to auto-create and run when starting automation")
    cycles_label = QLabel("Cycles")
    cycles_label.setObjectName("sectionTitle")
    self.cycles_spin = QSpinBox()
    self.cycles_spin.setRange(1, 10000)
    self.cycles_spin.setValue(1)
    self.cycles_spin.setFixedWidth(80)
    self.cycles_spin.setToolTip("Stop automation after full keyword list cycles this many times")

    control_bar.addWidget(count_label)
    control_bar.addWidget(self.profile_count_spin)
    control_bar.addWidget(self.btn_create)
    control_bar.addWidget(self.btn_refresh)
    control_bar.addStretch()
    traffic_box = QWidget()
    traffic_layout = QVBoxLayout(traffic_box)
    traffic_layout.setContentsMargins(0, 0, 0, 0)
    traffic_layout.setSpacing(1)
    traffic_layout.addWidget(self.proxy_traffic_title_label, alignment=Qt.AlignmentFlag.AlignHCenter)
    traffic_layout.addWidget(self.proxy_traffic_total_label, alignment=Qt.AlignmentFlag.AlignHCenter)
    cycle_box = QWidget()
    cycle_layout = QVBoxLayout(cycle_box)
    cycle_layout.setContentsMargins(0, 0, 0, 0)
    cycle_layout.setSpacing(1)
    cycle_layout.addWidget(self.present_cycle_title_label, alignment=Qt.AlignmentFlag.AlignHCenter)
    cycle_layout.addWidget(self.present_cycle_value_label, alignment=Qt.AlignmentFlag.AlignHCenter)
    clicks_box = QWidget()
    clicks_layout = QVBoxLayout(clicks_box)
    clicks_layout.setContentsMargins(0, 0, 0, 0)
    clicks_layout.setSpacing(1)
    clicks_layout.addWidget(self.overall_clicks_title_label, alignment=Qt.AlignmentFlag.AlignHCenter)
    clicks_layout.addWidget(self.overall_clicks_value_label, alignment=Qt.AlignmentFlag.AlignHCenter)
    cycle_clicks_box = QWidget()
    cycle_clicks_layout = QHBoxLayout(cycle_clicks_box)
    cycle_clicks_layout.setContentsMargins(0, 0, 0, 0)
    cycle_clicks_layout.setSpacing(18)
    cycle_clicks_layout.addWidget(cycle_box)
    cycle_clicks_layout.addWidget(clicks_box)
    control_bar.addWidget(traffic_box)
    control_bar.addWidget(cycle_clicks_box)
    control_bar.addStretch()
    control_bar.addWidget(threads_label)
    control_bar.addWidget(self.threads_spin)
    control_bar.addWidget(cycles_label)
    control_bar.addWidget(self.cycles_spin)
    control_bar.addWidget(self.btn_start_auto)
    control_bar.addWidget(self.btn_stop_auto)
    layout.addWidget(self._card_panel(control_bar))

    bulk_bar = QHBoxLayout()
    bulk_bar.setSpacing(8)
    self.chk_select_all = QCheckBox("Select All")
    self.chk_select_all.setObjectName("selectAllCheck")
    self.btn_bulk_start = QPushButton("Start Selected")
    self.btn_bulk_start.setObjectName("btnBulkStart")
    self.btn_bulk_pause = QPushButton("Pause Selected")
    self.btn_bulk_pause.setObjectName("btnBulkPause")
    self.btn_bulk_kill = QPushButton("Stop Selected")
    self.btn_bulk_kill.setObjectName("btnBulkKill")
    self.btn_bulk_delete = QPushButton("Delete Selected")
    self.btn_bulk_delete.setObjectName("btnBulkDelete")
    bulk_bar.addWidget(self.chk_select_all)
    bulk_bar.addWidget(self.btn_bulk_start)
    bulk_bar.addWidget(self.btn_bulk_pause)
    bulk_bar.addWidget(self.btn_bulk_kill)
    bulk_bar.addWidget(self.btn_bulk_delete)
    bulk_bar.addStretch()
    layout.addWidget(self._card_panel(bulk_bar))

    self.profile_table = QTableWidget(0, len(self.COLUMNS))
    self.profile_table.setObjectName("profileTable")
    self.profile_table.setHorizontalHeaderLabels(self.COLUMNS)
    self.profile_table.setAlternatingRowColors(True)
    self.profile_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    self.profile_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    self.profile_table.verticalHeader().setVisible(False)
    self.profile_table.verticalHeader().setDefaultSectionSize(54)
    header = self.profile_table.horizontalHeader()
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(44)
    header.setSectionResizeMode(self.COL_CHECK, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_NO, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_ID, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_OS, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_BROWSER, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_PROXY, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_TRAFFIC, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(self.COL_ACTIONS, QHeaderView.ResizeMode.ResizeToContents)
    self.profile_table.setColumnWidth(self.COL_STATUS, 150)
    self.profile_table.setWordWrap(True)
    self.profile_table.setTextElideMode(Qt.TextElideMode.ElideNone)
    self.profile_table.itemChanged.connect(self._on_profile_table_item_changed)

    log_title = QLabel("ACTIVITY LOG")
    log_title.setObjectName("sectionTitle")
    self.log_view = QPlainTextEdit()
    self.log_view.setObjectName("logView")
    self.log_view.setReadOnly(True)
    self.log_view.setMaximumBlockCount(8000)
    log_font = QFont("Consolas", 9)
    self.log_view.setFont(log_font)

    left_panel = QWidget()
    left_layout = QVBoxLayout(left_panel)
    left_layout.setContentsMargins(0, 0, 0, 0)
    left_layout.setSpacing(0)
    left_layout.addWidget(self.profile_table)

    right_panel = QWidget()
    right_layout = QVBoxLayout(right_panel)
    right_layout.setContentsMargins(0, 0, 0, 0)
    right_layout.setSpacing(6)
    right_layout.addWidget(log_title)
    right_layout.addWidget(self.log_view, stretch=1)

    split_row = QHBoxLayout()
    split_row.setSpacing(12)
    split_row.addWidget(left_panel, stretch=2)
    split_row.addWidget(right_panel, stretch=1)
    layout.addLayout(split_row, stretch=1)
    return tab

  def _build_ai_fix_tab(self) -> QWidget:
    tab = QWidget()
    layout = QVBoxLayout(tab)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(12)

    title = QLabel("AI Fix — Cursor Agent")
    title.setObjectName("appTitle")
    layout.addWidget(title)

    subtitle = QLabel(
      "Chat with Cursor to edit this app (UI, SERP bot, AdsPower, workers). "
      "Changes apply to project files on disk — restart the app after Python/UI updates."
    )
    subtitle.setObjectName("hintLabel")
    subtitle.setWordWrap(True)
    layout.addWidget(subtitle)

    if not CURSOR_SDK_AVAILABLE:
      warn = QLabel("cursor-sdk is not installed. Run: pip install cursor-sdk")
      warn.setStyleSheet("color: #f87171; font-weight: 600;")
      warn.setWordWrap(True)
      layout.addWidget(warn)

    api_group = QGroupBox("Cursor API")
    api_form = QFormLayout(api_group)
    self.cursor_api_key = QLineEdit()
    self.cursor_api_key.setEchoMode(QLineEdit.EchoMode.Password)
    self.cursor_api_key.setPlaceholderText("cursor_... from Cursor Dashboard → Integrations")
    self.cursor_model = QComboBox()
    self.cursor_model.setEditable(True)
    for model_name in CURSOR_MODEL_CHOICES:
      self.cursor_model.addItem(model_name)
    self.cursor_model.setCurrentText(DEFAULT_CURSOR_MODEL)
    model_hint = QLabel(
      "Default gpt-5.3-codex uses API quota. composer-* models use First-party (Auto/Composer) quota."
    )
    model_hint.setObjectName("hintLabel")
    model_hint.setWordWrap(True)
    api_form.addRow("Cursor API Key:", self.cursor_api_key)
    api_form.addRow("Model:", self.cursor_model)
    api_form.addRow("", model_hint)
    layout.addWidget(api_group)

    chat_title = QLabel("CONVERSATION")
    chat_title.setObjectName("sectionTitle")
    layout.addWidget(chat_title)

    self.ai_fix_chat = QTextEdit()
    self.ai_fix_chat.setObjectName("aiFixChat")
    self.ai_fix_chat.setReadOnly(True)
    self.ai_fix_chat.setMinimumHeight(320)
    ai_chat_font = QFont("Segoe UI", 10)
    self.ai_fix_chat.setFont(ai_chat_font)
    layout.addWidget(self.ai_fix_chat, stretch=1)

    self.ai_fix_prompt = AiFixPromptEdit(self.on_send_ai_fix)
    self.ai_fix_prompt.setObjectName("aiFixPrompt")
    self.ai_fix_prompt.setPlaceholderText(
      "Describe what to change, e.g. Remove ... ellipsis on profile names in the profile table "
      "(Enter to send, Shift+Enter for new line)"
    )
    self.ai_fix_prompt.setMaximumHeight(90)
    layout.addWidget(self.ai_fix_prompt)

    btn_row = QHBoxLayout()
    self.btn_send_ai_fix = QPushButton("Send")
    self.btn_send_ai_fix.setObjectName("btnSendAiFix")
    self.btn_new_ai_session = QPushButton("New Session")
    self.btn_new_ai_session.setObjectName("btnRefresh")
    self.btn_clear_ai_chat = QPushButton("Clear Chat")
    self.btn_clear_ai_chat.setObjectName("btnRefresh")
    btn_row.addWidget(self.btn_send_ai_fix)
    btn_row.addWidget(self.btn_new_ai_session)
    btn_row.addWidget(self.btn_clear_ai_chat)
    btn_row.addStretch()
    layout.addLayout(btn_row)

    return tab

  def _build_settings_tab(self) -> QWidget:
    tab = QWidget()
    root = QVBoxLayout(tab)
    root.setContentsMargins(8, 8, 8, 8)
    root.setSpacing(16)
    outer = QHBoxLayout()
    outer.setSpacing(16)

    left_col = QVBoxLayout()
    right_col = QVBoxLayout()

    api_group = QGroupBox("API & Connection Settings")
    api_form = QFormLayout(api_group)
    self.capsolver_key = QLineEdit()
    self.capsolver_key.setEchoMode(QLineEdit.EchoMode.Password)
    self.capsolver_key.setPlaceholderText("Leave empty for manual captcha solving in browser")
    self.adspower_api_url = QLineEdit("http://local.adspower.com:50325")
    self.adspower_api_url.setPlaceholderText("http://local.adspower.com:50325")
    self.adspower_api_key = QLineEdit()
    self.adspower_api_key.setEchoMode(QLineEdit.EchoMode.Password)
    self.adspower_api_key.setPlaceholderText("AdsPower Bearer API key")
    api_form.addRow("CapSolver API Key:", self.capsolver_key)
    api_form.addRow("AdsPower API URL:", self.adspower_api_url)
    api_form.addRow("AdsPower API Key:", self.adspower_api_key)
    left_col.addWidget(api_group)

    target_group = QGroupBox("Targeting & Automation")
    target_form = QFormLayout(target_group)
    self.target_domain = QLineEdit()
    self.target_domain.setPlaceholderText("mysite.com")
    launch_row = self._min_max_row(QSpinBox, 1, 3600, 1, 4)
    dwell_row = self._min_max_row(QSpinBox, 10, 3600, 60, 120)
    warmup_dwell_row = self._min_max_row(QSpinBox, 1, 3600, 8, 16)
    warmup_count_row = self._min_max_row(QSpinBox, 1, 10, 1, 2)
    action_row = self._min_max_row(QDoubleSpinBox, 0.05, 5.0, 0.1, 0.3, step=0.05)
    self.launch_min, self.launch_max = launch_row
    self.dwell_min, self.dwell_max = dwell_row
    self.warmup_dwell_min, self.warmup_dwell_max = warmup_dwell_row
    self.warmup_count_min, self.warmup_count_max = warmup_count_row
    self.action_delay_min, self.action_delay_max = action_row
    self.max_search_pages = QSpinBox()
    self.max_search_pages.setRange(1, 50)
    self.max_search_pages.setValue(8)
    self.max_keywords_per_profile = QSpinBox()
    self.max_keywords_per_profile.setRange(1, 100)
    self.max_keywords_per_profile.setValue(3)
    self.max_keywords_per_profile.setToolTip(
      "Each profile runs this many keywords, then closes. "
      "Next profile continues from the next keyword batch."
    )
    target_form.addRow("Target Domain:", self.target_domain)
    target_form.addRow("Next Profile Launch Interval (sec):", self._wrap_row(launch_row))
    target_form.addRow("Target Site Dwell Time (sec):", self._wrap_row(dwell_row))
    target_form.addRow("Warm-up Dwell Time (sec):", self._wrap_row(warmup_dwell_row))
    warmup_count_row[0].setToolTip("Minimum number of warm-up Google searches per profile.")
    warmup_count_row[1].setToolTip("Maximum number of warm-up Google searches per profile.")
    target_form.addRow("Warm-up Queries / Profile:", self._wrap_row(warmup_count_row))
    target_form.addRow("Action Delay (sec):", self._wrap_row(action_row))
    target_form.addRow("Max Search Pages:", self.max_search_pages)
    target_form.addRow("Max Keywords / Profile:", self.max_keywords_per_profile)
    self.chk_ip_check_session = QCheckBox("At session start")
    self.chk_ip_check_session.setChecked(False)
    self.chk_ip_check_session.setToolTip(
      "Off (default): skip proxy IP lookup when a profile session begins (saves traffic).\n"
      "On: fetch egress IP once via lightweight browser fetch before opening Google."
    )
    self.chk_ip_check_keyword2 = QCheckBox("Before 2nd keyword")
    self.chk_ip_check_keyword2.setChecked(False)
    self.chk_ip_check_keyword2.setToolTip(
      "Off (default): do not compare IP again before the 2nd keyword.\n"
      "On: re-check IP before keyword 2 and stop the profile if it changed "
      "(requires session-start IP check to capture a baseline)."
    )
    ip_check_col = QVBoxLayout()
    ip_check_col.setContentsMargins(0, 0, 0, 0)
    ip_check_col.setSpacing(4)
    ip_check_col.addWidget(self.chk_ip_check_session)
    ip_check_col.addWidget(self.chk_ip_check_keyword2)
    target_form.addRow("IP Check:", self._wrap_col(ip_check_col))
    left_col.addWidget(target_group)
    left_col.addStretch()

    proxy_group = QGroupBox("Proxy Setup (HTTP ISP)")
    proxy_layout = QVBoxLayout(proxy_group)
    proxy_hint = QLabel("Format: username:password@ip:port — one per line, HTTP by default")
    proxy_hint.setObjectName("hintLabel")
    proxy_layout.addWidget(proxy_hint)
    self.proxies_edit = self._make_settings_multiline_edit(
      "myuser:mypass@123.45.67.89:10000",
      min_height=100,
    )
    proxy_layout.addWidget(self.proxies_edit)
    right_col.addWidget(proxy_group)

    kw_group = QGroupBox("Keyword List")
    kw_layout = QVBoxLayout(kw_group)
    self.keywords_edit = self._make_settings_multiline_edit("One keyword per line", min_height=100)
    kw_layout.addWidget(self.keywords_edit)
    right_col.addWidget(kw_group)

    warmup_group = QGroupBox("Warm-up Query List")
    warmup_layout = QVBoxLayout(warmup_group)
    self.warmup_edit = self._make_settings_multiline_edit("One warm-up query per line", min_height=100)
    warmup_layout.addWidget(self.warmup_edit)
    right_col.addWidget(warmup_group)
    right_col.addStretch()

    left_widget = QWidget()
    left_widget.setLayout(left_col)
    right_widget = QWidget()
    right_widget.setLayout(right_col)
    outer.addWidget(left_widget, stretch=1)
    outer.addWidget(right_widget, stretch=1)
    root.addLayout(outer, stretch=1)

    save_bar = QHBoxLayout()
    save_bar.addStretch()
    self.btn_save_settings = QPushButton("Save Settings")
    self.btn_save_settings.setObjectName("btnSaveSettings")
    save_bar.addWidget(self.btn_save_settings)
    save_bar.addStretch()
    root.addLayout(save_bar)
    return tab

  def _build_result_tab(self) -> QWidget:
    tab = QWidget()
    layout = QVBoxLayout(tab)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(10)

    title = QLabel("Session Results")
    title.setObjectName("appTitle")
    layout.addWidget(title)

    content = QVBoxLayout()
    content.setSpacing(8)

    header_row = QHBoxLayout()
    header_row.setSpacing(12)
    sessions_title = QLabel("SESSIONS")
    sessions_title.setObjectName("sectionTitle")

    stats_row = QHBoxLayout()
    stats_row.setSpacing(18)
    total_box = QVBoxLayout()
    total_box.setSpacing(1)
    total_title = QLabel("Total Clicks")
    total_title.setObjectName("trafficTotalTitle")
    self.result_total_clicks_value = QLabel("0")
    self.result_total_clicks_value.setObjectName("trafficTotalValue")
    total_box.addWidget(total_title, alignment=Qt.AlignmentFlag.AlignRight)
    total_box.addWidget(self.result_total_clicks_value, alignment=Qt.AlignmentFlag.AlignRight)

    failed_box = QVBoxLayout()
    failed_box.setSpacing(1)
    failed_title = QLabel("Not Found")
    failed_title.setObjectName("trafficTotalTitle")
    self.result_not_found_value = QLabel("0")
    self.result_not_found_value.setObjectName("trafficTotalValue")
    failed_box.addWidget(failed_title, alignment=Qt.AlignmentFlag.AlignRight)
    failed_box.addWidget(self.result_not_found_value, alignment=Qt.AlignmentFlag.AlignRight)

    stats_row.addStretch()
    stats_row.addLayout(total_box)
    stats_row.addLayout(failed_box)

    header_row.addWidget(sessions_title, stretch=1)
    header_row.addLayout(stats_row, stretch=3)

    body_row = QHBoxLayout()
    body_row.setSpacing(12)
    self.result_session_list = QListWidget()
    self.result_session_list.setObjectName("resultSessionList")

    table_panel = QWidget()
    table_panel_layout = QVBoxLayout(table_panel)
    table_panel_layout.setContentsMargins(0, 0, 0, 0)
    table_panel_layout.setSpacing(8)
    table_toolbar = QHBoxLayout()
    table_toolbar.setSpacing(8)
    self.btn_result_keyword_clicks = QPushButton("Keyword Clicks")
    self.btn_result_keyword_clicks.setObjectName("btnKeywordClicks")
    self.btn_result_keyword_clicks.setCheckable(True)
    self.btn_result_keyword_clicks.setToolTip(
      "Show per-keyword click totals (Windows / mobile), sorted by total clicks"
    )
    table_toolbar.addWidget(self.btn_result_keyword_clicks)
    table_toolbar.addStretch()

    self.result_table = QTableWidget(0, 0)
    self.result_table.setObjectName("resultTable")
    self.result_table.setAlternatingRowColors(True)
    self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    self.result_table.verticalHeader().setVisible(False)
    result_header = self.result_table.horizontalHeader()
    result_header.setStretchLastSection(True)

    table_panel_layout.addLayout(table_toolbar)
    table_panel_layout.addWidget(self.result_table, stretch=1)

    body_row.addWidget(self.result_session_list, stretch=1)
    body_row.addWidget(table_panel, stretch=3)

    footer_row = QHBoxLayout()
    footer_row.setSpacing(12)
    self.btn_refresh_results = QPushButton("Refresh")
    self.btn_refresh_results.setObjectName("btnRefresh")
    footer_row.addWidget(self.btn_refresh_results, stretch=1)
    footer_row.addStretch(3)

    content.addLayout(header_row)
    content.addLayout(body_row, stretch=1)
    content.addLayout(footer_row)
    layout.addLayout(content, stretch=1)

    self._refreshing_result_list = False
    self._result_session_files: list[Path] = []
    self._result_loaded_path: Path | None = None
    self._result_loaded_headers: list[str] = []
    self._result_loaded_rows: list[list[str]] = []
    self.result_session_list.currentItemChanged.connect(self._on_result_session_selected)
    self.btn_refresh_results.clicked.connect(self._refresh_result_file_list)
    self.btn_result_keyword_clicks.toggled.connect(self._on_result_keyword_clicks_toggled)
    QTimer.singleShot(0, self._refresh_result_file_list)
    return tab

  def _refresh_result_file_list(self) -> None:
    if getattr(self, "_refreshing_result_list", False):
      return
    self._refreshing_result_list = True
    try:
      selected_path = ""
      current = self.result_session_list.currentItem()
      if current is not None:
        selected_path = str(current.data(Qt.ItemDataRole.UserRole) or "")

      data_dir = self._project_root / "data"
      files = list_session_result_files(data_dir)
      self._result_session_files = files

      self.result_session_list.blockSignals(True)
      self.result_session_list.clear()
      select_row = 0
      for index, path in enumerate(files):
        item = QListWidgetItem(format_result_session_label(path))
        item.setData(Qt.ItemDataRole.UserRole, str(path.resolve()))
        item.setToolTip(path.name)
        self.result_session_list.addItem(item)
        if selected_path and str(path.resolve()) == selected_path:
          select_row = index

      self.result_session_list.blockSignals(False)

      if files:
        self.result_session_list.setCurrentRow(select_row)
        self._load_result_session(files[select_row])
      else:
        self._clear_result_session_view()
    finally:
      self._refreshing_result_list = False

  def _on_result_session_selected(
    self,
    current: QListWidgetItem | None,
    _previous: QListWidgetItem | None,
  ) -> None:
    if self._refreshing_result_list or current is None:
      return
    path_text = str(current.data(Qt.ItemDataRole.UserRole) or "").strip()
    if not path_text:
      return
    self._load_result_session(Path(path_text))

  def _clear_result_session_view(self) -> None:
    self.result_total_clicks_value.setText("0")
    self.result_not_found_value.setText("0")
    self._result_loaded_path = None
    self._result_loaded_headers = []
    self._result_loaded_rows = []
    self.result_table.clear()
    self.result_table.setRowCount(0)
    self.result_table.setColumnCount(0)

  def _load_result_session(self, path: Path) -> None:
    headers, rows = SessionClickCsvLogger.read_rows(path)
    self._result_loaded_path = path
    self._result_loaded_headers = headers
    self._result_loaded_rows = rows
    self.result_total_clicks_value.setText(str(len(rows)))

    session_start, session_end = session_result_window(path, self._result_session_files)
    not_found = 0
    if session_start is not None:
      not_found = count_target_not_found_in_session(
        session_start,
        session_end,
        session_log_path=self._project_root / "data" / "session.log",
      )
    self.result_not_found_value.setText(str(not_found))
    self._render_result_table()

  def _on_result_keyword_clicks_toggled(self, checked: bool) -> None:
    self.btn_result_keyword_clicks.setText("All Clicks" if checked else "Keyword Clicks")
    if self._result_loaded_path is not None:
      self._render_result_table()

  def _render_result_table(self) -> None:
    if self.btn_result_keyword_clicks.isChecked():
      self._render_result_keyword_table()
    else:
      self._render_result_detail_table()

  def _render_result_detail_table(self) -> None:
    headers = self._result_loaded_headers
    rows = self._result_loaded_rows
    display_headers = headers or [
      "datetime",
      "profile_name",
      "device",
      "keyword",
      "url",
      "page",
      "rank",
      "overall_rank",
    ]
    self.result_table.setColumnCount(len(display_headers))
    self.result_table.setHorizontalHeaderLabels(display_headers)
    self.result_table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
      for col_index, header_name in enumerate(display_headers):
        value = row[col_index] if col_index < len(row) else ""
        self.result_table.setItem(row_index, col_index, QTableWidgetItem(str(value)))
    self._resize_result_table_columns(len(display_headers))

  def _render_result_keyword_table(self) -> None:
    summaries = aggregate_keyword_clicks(self._result_loaded_headers, self._result_loaded_rows)
    display_headers = ("keyword", "total_clicks", "windows", "mobile")
    self.result_table.setColumnCount(len(display_headers))
    self.result_table.setHorizontalHeaderLabels(
      ["Keyword", "Total Clicks", "Windows", "Mobile"]
    )
    self.result_table.setRowCount(len(summaries))
    for row_index, summary in enumerate(summaries):
      values = (
        summary["keyword"],
        str(summary["total"]),
        str(summary["windows"]),
        str(summary["mobile"]),
      )
      for col_index, value in enumerate(values):
        self.result_table.setItem(row_index, col_index, QTableWidgetItem(value))
    self._resize_result_table_columns(len(display_headers))

  def _resize_result_table_columns(self, column_count: int) -> None:
    header = self.result_table.horizontalHeader()
    header.setStretchLastSection(True)
    for col_index in range(max(0, column_count - 1)):
      header.setSectionResizeMode(col_index, QHeaderView.ResizeMode.ResizeToContents)

  def _on_main_tab_changed(self, index: int) -> None:
    if index == getattr(self, "_result_tab_index", -1):
      self._refresh_result_file_list()

  def _make_settings_multiline_edit(self, placeholder: str, *, min_height: int = 100) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setObjectName("settingsMultilineEdit")
    edit.setPlaceholderText(placeholder)
    edit.setMinimumHeight(min_height)
    palette = edit.palette()
    palette.setColor(QPalette.ColorRole.Text, QColor("#e2e8f0"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0a0d14"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#64748b"))
    edit.setPalette(palette)
    return edit

  def _min_max_row(self, widget_cls, min_val, max_val, default_min, default_max, step=None):
    lo = widget_cls()
    hi = widget_cls()
    lo.setRange(min_val, max_val)
    hi.setRange(min_val, max_val)
    if step and isinstance(lo, QDoubleSpinBox):
      lo.setSingleStep(step)
      hi.setSingleStep(step)
    lo.setValue(default_min)
    hi.setValue(default_max)
    return lo, hi

  @staticmethod
  def _wrap_row(spinboxes: tuple) -> QWidget:
    row = QHBoxLayout()
    row.addWidget(QLabel("Min"))
    row.addWidget(spinboxes[0])
    row.addWidget(QLabel("Max"))
    row.addWidget(spinboxes[1])
    row.addStretch()
    container = QWidget()
    container.setLayout(row)
    return container

  @staticmethod
  def _wrap_col(layout: QVBoxLayout) -> QWidget:
    container = QWidget()
    container.setLayout(layout)
    return container

  def _wire_signals(self) -> None:
    self.btn_create.clicked.connect(self.on_create_profiles)
    self.btn_refresh.clicked.connect(self.on_refresh_profiles)
    self.btn_start_auto.clicked.connect(self.on_start_automated)
    self.btn_stop_auto.clicked.connect(self.on_stop_automated)
    self.btn_save_settings.clicked.connect(self.on_save_settings)
    self.btn_send_ai_fix.clicked.connect(self.on_send_ai_fix)
    self.btn_new_ai_session.clicked.connect(self.on_new_ai_session)
    self.btn_clear_ai_chat.clicked.connect(self.on_clear_ai_chat)
    self.chk_select_all.stateChanged.connect(self._on_select_all_changed)
    self.btn_bulk_start.clicked.connect(self.on_bulk_start)
    self.btn_bulk_pause.clicked.connect(self.on_bulk_pause)
    self.btn_bulk_kill.clicked.connect(self.on_bulk_kill)
    self.btn_bulk_delete.clicked.connect(self.on_bulk_delete)
    self.tabs.currentChanged.connect(self._on_main_tab_changed)
    self._controller.log.connect(self.append_log, Qt.ConnectionType.QueuedConnection)
    self._controller.profile_update.connect(self._on_profile_update, Qt.ConnectionType.QueuedConnection)
    self._controller.proxy_traffic_update.connect(
      self._on_proxy_traffic_update,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.profile_traffic_update.connect(
      self._on_profile_traffic_update,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.cycle_progress_update.connect(
      self._on_cycle_progress_update,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.profile_finished.connect(
      self._on_profile_finished,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.target_click_logged.connect(
      self._refresh_overall_clicks,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.target_click_logged.connect(
      self._on_result_click_logged,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.profile_created.connect(
      self._on_profile_created_event,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.keyword_excluded.connect(
      self._on_keyword_excluded_event,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.profile_deleted.connect(
      self._on_profile_deleted_event,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.profiles_sync_requested.connect(
      self._schedule_live_profile_refresh,
      Qt.ConnectionType.QueuedConnection,
    )
    self._controller.global_finished.connect(self._on_global_finished, Qt.ConnectionType.QueuedConnection)
    self._self_healer.log.connect(self.append_log, Qt.ConnectionType.QueuedConnection)
    self._self_healer.healing_started.connect(
      self._on_profile_healing_started, Qt.ConnectionType.QueuedConnection
    )
    self._self_healer.healing_finished.connect(
      self._on_profile_healing_finished, Qt.ConnectionType.QueuedConnection
    )

  def _scroll_ai_fix_chat(self) -> None:
    scrollbar = self.ai_fix_chat.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

  def _ai_fix_append_system(self, text: str) -> None:
    safe = escape(text).replace("\n", "<br>")
    self.ai_fix_chat.append(f'<p style="color:#64748b;margin:4px 0;">{safe}</p>')
    self._scroll_ai_fix_chat()
    self._schedule_ai_chat_persist()

  def _ai_fix_append_user(self, text: str) -> None:
    safe = escape(text).replace("\n", "<br>")
    self.ai_fix_chat.append(f'<p style="color:#93c5fd;margin:8px 0 4px 0;"><b>You:</b> {safe}</p>')
    self._scroll_ai_fix_chat()
    self._schedule_ai_chat_persist()

  def _ai_fix_begin_assistant(self) -> None:
    self.ai_fix_chat.append('<p style="color:#e2e8f0;margin:8px 0 4px 0;"><b>Cursor:</b> ')
    self._ai_fix_assistant_open = True
    self._scroll_ai_fix_chat()
    self._schedule_ai_chat_persist()

  def _ai_fix_append_assistant_text(self, text: str) -> None:
    if not text:
      return
    if not self._ai_fix_assistant_open:
      self._ai_fix_begin_assistant()
    cursor = QTextCursor(self.ai_fix_chat.document())
    cursor.movePosition(QTextCursor.MoveOperation.End)
    cursor.insertText(text)
    self._scroll_ai_fix_chat()
    self._schedule_ai_chat_persist()

  def _ai_fix_end_assistant(self) -> None:
    if self._ai_fix_assistant_open:
      self.ai_fix_chat.append("</p>")
      self._ai_fix_assistant_open = False
      self._scroll_ai_fix_chat()
      self._schedule_ai_chat_persist()

  def _ai_fix_append_status(self, text: str) -> None:
    safe = escape(text).replace("\n", "<br>")
    self.ai_fix_chat.append(f'<p style="color:#94a3b8;margin:2px 0;font-size:12px;">{safe}</p>')
    self._scroll_ai_fix_chat()
    self._schedule_ai_chat_persist()

  def _schedule_ai_chat_persist(self) -> None:
    if self._ai_chat_persist_timer.isActive():
      self._ai_chat_persist_timer.stop()
    self._ai_chat_persist_timer.start()

  def _initialize_ai_chat(self) -> None:
    self.ai_fix_chat.clear()
    self._ai_fix_assistant_open = False
    self._ai_fix_append_system(
      "Ready. Enter your Cursor API key, describe a fix, and press Send. "
      "Tool calls and edits stream here in real time."
    )

  def _persist_ai_chat_state(self) -> None:
    try:
      save_settings(self._collect_settings_dict())
    except Exception:
      pass

  def _set_ai_fix_busy(self, busy: bool) -> None:
    self.btn_send_ai_fix.setEnabled(not busy)
    self.ai_fix_prompt.setEnabled(not busy)

  def _cursor_model_value(self) -> str:
    if isinstance(self.cursor_model, QComboBox):
      return normalize_cursor_model(self.cursor_model.currentText())
    return normalize_cursor_model(self.cursor_model.text())

  def on_send_ai_fix(self) -> None:
    prompt = self.ai_fix_prompt.toPlainText().strip()
    if not prompt:
      self._ai_fix_append_system("Enter a prompt before sending.")
      return

    api_key = self.cursor_api_key.text().strip()
    if not api_key:
      self._ai_fix_append_system(
        "Cursor API Key is required. Get one at https://cursor.com/dashboard/integrations"
      )
      return

    if self._cursor_chat.is_running():
      self._ai_fix_append_system("Agent is still running — wait for the current request to finish.")
      return

    try:
      save_settings(self._collect_settings_dict())
    except Exception:
      pass

    self._ai_fix_append_user(prompt)
    self.ai_fix_prompt.clear()
    self._set_ai_fix_busy(True)

    worker = self._cursor_chat.start_run(
      api_key=api_key,
      model=self._cursor_model_value(),
      prompt=prompt,
      parent=self,
    )
    if worker is None:
      self._set_ai_fix_busy(False)
      self._ai_fix_append_system("Could not start Cursor agent.")
      return

    worker.status_line.connect(self._ai_fix_append_status, Qt.ConnectionType.QueuedConnection)
    worker.text_delta.connect(self._ai_fix_append_assistant_text, Qt.ConnectionType.QueuedConnection)
    worker.completed.connect(self._on_ai_fix_completed, Qt.ConnectionType.QueuedConnection)

  def _on_ai_fix_completed(self, success: bool, message: str, agent_id: str) -> None:
    self._ai_fix_end_assistant()
    self._set_ai_fix_busy(False)
    prefix = "✓" if success else "✗"
    self._ai_fix_append_status(f"{prefix} {message}")
    if agent_id:
      self._ai_fix_append_status(f"Session agent: {agent_id}")
    if success:
      self._ai_fix_append_system("Restart the app to load Python/UI changes.")
    self.append_log(f"[AI Fix] {'OK' if success else 'FAILED'}: {message}")
    self._schedule_ai_chat_persist()

  def on_new_ai_session(self) -> None:
    if self._cursor_chat.is_running():
      self._ai_fix_append_system("Wait for the current run to finish before starting a new session.")
      return
    self._cursor_chat.reset_session()
    self._ai_fix_append_system("Started a new Cursor agent session (conversation context cleared).")
    self._schedule_ai_chat_persist()

  def on_clear_ai_chat(self) -> None:
    if self._cursor_chat.is_running():
      self._ai_fix_append_system("Wait for the current run to finish before clearing chat.")
      return
    self._cursor_chat.reset_session()
    self._initialize_ai_chat()
    self._schedule_ai_chat_persist()

  def append_log(self, message: str) -> None:
    from utils.user_log import format_user_log

    try:
      log_path = Path("data/session.log")
      log_path.parent.mkdir(parents=True, exist_ok=True)
      stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
      with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")
    except Exception:
      pass

    user_line = format_user_log(message)
    if not user_line:
      return
    time_stamp = datetime.now().strftime("%H:%M:%S")
    self.log_view.appendPlainText(f"{time_stamp} {user_line}")
    scrollbar = self.log_view.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

  def _make_adspower_manager(self) -> AdsPowerManager:
    try:
      config = self._build_config(require_lists=False)
    except ValueError:
      config = BotConfig(
        capsolver_api_key=self.capsolver_key.text().strip(),
        adspower_api_url=self.adspower_api_url.text().strip(),
        adspower_api_key=self.adspower_api_key.text().strip(),
      )
    return AdsPowerManager(config.adspower_url, config.adspower_api_key, self.append_log)

  def _purge_offline_profiles(
    self,
    manager: AdsPowerManager,
    profiles: list[ProfileSpec],
    group_id: str,
  ) -> list[ProfileSpec]:
    offline_ids = [profile.profile_id for profile in profiles if not profile.is_active]
    if not offline_ids:
      return profiles

    self.append_log(f"[UI] Removing offline profiles: {len(offline_ids)}")
    for profile_id in offline_ids:
      try:
        manager.force_terminate_profile(profile_id)
      except Exception as exc:
        self.append_log(f"[UI] Offline force-stop warning for {profile_id}: {exc}")

    try:
      manager.delete_profiles(offline_ids)
    except Exception as exc:
      self.append_log(f"[UI] Offline delete batch warning: {exc}")

    remaining_ids: set[str] = set()
    try:
      remaining_ids = set(manager.verify_profile_ids(offline_ids))
    except Exception as exc:
      self.append_log(f"[UI] Offline delete verify warning: {exc}")

    removed = [profile_id for profile_id in offline_ids if profile_id not in remaining_ids]
    if removed:
      self._controller.remove_profiles(removed)
      for profile_id in removed:
        self._status_map.pop(profile_id, None)
      self.append_log(f"[UI] Offline profiles deleted: {len(removed)}")
    if remaining_ids:
      self.append_log(f"[UI] Offline profiles still present after delete: {len(remaining_ids)}")

    return manager.list_profiles_live(group_id=group_id)

  def _parse_proxies(self, required_count: int | None = None) -> list[tuple[str, int, str, str]]:
    lines = [line.strip() for line in self.proxies_edit.toPlainText().splitlines() if line.strip()]
    proxies: list[tuple[str, int, str, str]] = []
    for line in lines:
      at_idx = line.rfind("@")
      if at_idx <= 0:
        raise ValueError(
          f"Invalid proxy format: {line}\n"
          "Expected: username:password@ip:port"
        )
      credentials = line[:at_idx]
      hostport = line[at_idx + 1 :]
      user_pass = credentials.split(":", 1)
      host_port = hostport.rsplit(":", 1)
      if len(user_pass) != 2 or not host_port[0].strip() or not host_port[1].strip():
        raise ValueError(
          f"Invalid proxy format: {line}\n"
          "Expected: username:password@ip:port"
        )
      user, password = user_pass
      host, port_raw = host_port
      try:
        port = int(port_raw.strip())
      except ValueError as exc:
        raise ValueError(f"Invalid port number: {port_raw}") from exc
      proxies.append((host.strip(), port, user.strip(), password.strip()))
    if not proxies:
      raise ValueError("Enter at least one proxy. Format: username:password@ip:port")
    if required_count is not None and len(proxies) < required_count:
      raise ValueError(
        f"Creating {required_count} profiles requires at least {required_count} proxies "
        f"({len(proxies)} configured). Each proxy is used once per profile."
      )
    return proxies

  def _collect_settings_dict(self) -> dict:
    cursor_key = self.cursor_api_key.text().strip()
    cursor_model = self._cursor_model_value()
    return {
      "capsolver_api_key": self.capsolver_key.text().strip(),
      "adspower_api_url": self.adspower_api_url.text().strip(),
      "adspower_api_key": self.adspower_api_key.text().strip(),
      "cursor_api_key": cursor_key,
      "cursor_model": cursor_model,
      "llm_api_key": cursor_key,
      "llm_base_url": "https://api.cursor.com/v1",
      "llm_model": cursor_model,
      "target_domain": self.target_domain.text().strip(),
      "launch_interval_min": self.launch_min.value(),
      "launch_interval_max": self.launch_max.value(),
      "dwell_min": self.dwell_min.value(),
      "dwell_max": self.dwell_max.value(),
      "warmup_dwell_min": self.warmup_dwell_min.value(),
      "warmup_dwell_max": self.warmup_dwell_max.value(),
      "warmup_count_min": self.warmup_count_min.value(),
      "warmup_count_max": self.warmup_count_max.value(),
      "action_delay_min": self.action_delay_min.value(),
      "action_delay_max": self.action_delay_max.value(),
      "max_search_pages": self.max_search_pages.value(),
      "max_keywords_per_profile": self.max_keywords_per_profile.value(),
      "ip_check_session_start": self.chk_ip_check_session.isChecked(),
      "ip_check_enabled": self.chk_ip_check_keyword2.isChecked(),
      "profile_count": self.profile_count_spin.value(),
      "automation_threads": self.threads_spin.value(),
      "automation_cycles": self.cycles_spin.value(),
      "proxies_text": self.proxies_edit.toPlainText(),
      "keywords_text": self.keywords_edit.toPlainText(),
      "warmup_text": self.warmup_edit.toPlainText(),
      "ai_fix_chat_html": self.ai_fix_chat.toHtml(),
      "ai_fix_agent_id": self._cursor_chat.agent_id,
    }

  def _apply_settings_dict(self, data: dict) -> None:
    self.capsolver_key.setText(data.get("capsolver_api_key", ""))
    if data.get("adspower_api_url"):
      self.adspower_api_url.setText(data["adspower_api_url"])
    self.adspower_api_key.setText(data.get("adspower_api_key", ""))
    cursor_key = data.get("cursor_api_key", "").strip()
    if not cursor_key:
      legacy_llm = data.get("llm_api_key", "").strip()
      if legacy_llm.startswith(("cursor_", "crsr_")):
        cursor_key = legacy_llm
    self.cursor_api_key.setText(cursor_key)
    self.cursor_model.setCurrentText(
      normalize_cursor_model(data.get("cursor_model") or data.get("llm_model") or DEFAULT_CURSOR_MODEL)
    )
    self.target_domain.setText(data.get("target_domain", ""))
    self.launch_min.setValue(int(data.get("launch_interval_min", self.launch_min.value())))
    self.launch_max.setValue(int(data.get("launch_interval_max", self.launch_max.value())))
    self.dwell_min.setValue(int(data.get("dwell_min", self.dwell_min.value())))
    self.dwell_max.setValue(int(data.get("dwell_max", self.dwell_max.value())))
    self.warmup_dwell_min.setValue(int(data.get("warmup_dwell_min", self.warmup_dwell_min.value())))
    self.warmup_dwell_max.setValue(int(data.get("warmup_dwell_max", self.warmup_dwell_max.value())))
    self.warmup_count_min.setValue(int(data.get("warmup_count_min", self.warmup_count_min.value())))
    self.warmup_count_max.setValue(int(data.get("warmup_count_max", self.warmup_count_max.value())))
    self.action_delay_min.setValue(float(data.get("action_delay_min", self.action_delay_min.value())))
    self.action_delay_max.setValue(float(data.get("action_delay_max", self.action_delay_max.value())))
    self.max_search_pages.setValue(int(data.get("max_search_pages", self.max_search_pages.value())))
    self.max_keywords_per_profile.setValue(
      int(data.get("max_keywords_per_profile", self.max_keywords_per_profile.value()))
    )
    self.chk_ip_check_session.setChecked(bool(data.get("ip_check_session_start", False)))
    self.chk_ip_check_keyword2.setChecked(bool(data.get("ip_check_enabled", False)))
    self.profile_count_spin.setValue(int(data.get("profile_count", self.profile_count_spin.value())))
    self.threads_spin.setValue(int(data.get("automation_threads", self.threads_spin.value())))
    self.cycles_spin.setValue(int(data.get("automation_cycles", self.cycles_spin.value())))
    self.proxies_edit.setPlainText(data.get("proxies_text", ""))
    self.keywords_edit.setPlainText(data.get("keywords_text", ""))
    self.warmup_edit.setPlainText(data.get("warmup_text", ""))
    saved_chat_html = str(data.get("ai_fix_chat_html", "") or "").strip()
    if saved_chat_html:
      self.ai_fix_chat.setHtml(saved_chat_html)
      self._ai_fix_assistant_open = False
    else:
      self._initialize_ai_chat()
    self._cursor_chat.restore_session(str(data.get("ai_fix_agent_id", "")))

  def _load_saved_settings(self) -> None:
    data = load_settings()
    if not data:
      self._initialize_ai_chat()
      return
    try:
      self._apply_settings_dict(data)
      self.append_log("[UI] Loaded saved settings from data/settings.json")
    except Exception as exc:
      self.append_log(f"[UI] Failed to load saved settings: {exc}")

  def on_save_settings(self) -> None:
    try:
      save_settings(self._collect_settings_dict())
      self.append_log("[UI] Settings saved to data/settings.json")
    except Exception as exc:
      self.append_log(f"[UI] Failed to save settings: {exc}")

  def _on_profile_healing_started(self, profile_id: str, status_key: str, display_text: str) -> None:
    self._on_profile_update(profile_id, status_key, display_text)

  def _on_profile_healing_finished(self, profile_id: str, status_key: str, display_text: str) -> None:
    self._on_profile_update(profile_id, status_key, display_text)

  def _build_config(self, require_lists: bool = True) -> BotConfig:
    target = self.target_domain.text().strip()
    if require_lists and not target:
      raise ValueError("Target domain is required.")

    keywords = [k.strip() for k in self.keywords_edit.toPlainText().splitlines() if k.strip()]
    if require_lists and not keywords:
      raise ValueError("At least one keyword is required.")

    warmup = [w.strip() for w in self.warmup_edit.toPlainText().splitlines() if w.strip()]
    if require_lists and not warmup:
      raise ValueError("At least one warm-up query is required.")

    if self.launch_min.value() > self.launch_max.value():
      raise ValueError("Launch interval min cannot exceed max.")
    if self.dwell_min.value() > self.dwell_max.value():
      raise ValueError("Dwell time min cannot exceed max.")
    if self.warmup_dwell_min.value() > self.warmup_dwell_max.value():
      raise ValueError("Warm-up dwell time min cannot exceed max.")
    if self.warmup_count_min.value() > self.warmup_count_max.value():
      raise ValueError("Warm-up query count min cannot exceed max.")
    if self.action_delay_min.value() > self.action_delay_max.value():
      raise ValueError("Action delay min cannot exceed max.")
    if self.max_search_pages.value() < 1:
      raise ValueError("Max search pages must be at least 1.")
    if self.max_keywords_per_profile.value() < 1:
      raise ValueError("Max keywords per profile must be at least 1.")

    proxies: list[tuple[str, int, str, str]] = []
    if self.proxies_edit.toPlainText().strip():
      proxies = self._parse_proxies()

    cursor_key = self.cursor_api_key.text().strip()
    cursor_model = self._cursor_model_value()
    return BotConfig(
      capsolver_api_key=self.capsolver_key.text().strip(),
      adspower_api_url=self.adspower_api_url.text().strip(),
      adspower_api_key=self.adspower_api_key.text().strip(),
      target_domain=target,
      proxies=proxies,
      keywords=keywords,
      warmup_queries=warmup,
      launch_interval_min=self.launch_min.value(),
      launch_interval_max=self.launch_max.value(),
      dwell_min=self.dwell_min.value(),
      dwell_max=self.dwell_max.value(),
      warmup_dwell_min=self.warmup_dwell_min.value(),
      warmup_dwell_max=self.warmup_dwell_max.value(),
      warmup_count_min=self.warmup_count_min.value(),
      warmup_count_max=self.warmup_count_max.value(),
      action_delay_min=self.action_delay_min.value(),
      action_delay_max=self.action_delay_max.value(),
      max_search_pages=self.max_search_pages.value(),
      max_keywords_per_profile=self.max_keywords_per_profile.value(),
      ip_check_session_start=self.chk_ip_check_session.isChecked(),
      ip_check_enabled=self.chk_ip_check_keyword2.isChecked(),
      automation_threads=self.threads_spin.value(),
      automation_cycles=self.cycles_spin.value(),
      profile_count=self.profile_count_spin.value(),
      cursor_api_key=cursor_key,
      cursor_model=cursor_model,
      llm_api_key=cursor_key,
      llm_base_url="https://api.cursor.com/v1",
      llm_model=cursor_model,
    )

  @staticmethod
  def _proxy_label(profile: ProfileSpec) -> str:
    if profile.proxy_host in ("", "—"):
      return "—"
    if profile.proxy_port:
      return f"{profile.proxy_host}:{profile.proxy_port}"
    return profile.proxy_host

  @staticmethod
  def _device_os(profile: ProfileSpec) -> str:
    return profile.device_label

  def _populate_profile_table(self) -> None:
    self.profile_table.blockSignals(True)
    self.profile_table.setRowCount(0)
    self.chk_select_all.setChecked(False)
    for profile in self._profiles:
      row = self.profile_table.rowCount()
      self.profile_table.insertRow(row)

      check_item = QTableWidgetItem()
      check_item.setFlags(
        Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled
      )
      check_item.setCheckState(Qt.CheckState.Unchecked)
      check_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
      check_item.setBackground(QColor("#141a28"))
      self.profile_table.setItem(row, self.COL_CHECK, check_item)

      no_item = QTableWidgetItem(profile.profile_no or "—")
      no_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
      self.profile_table.setItem(row, self.COL_NO, no_item)

      id_item = QTableWidgetItem(profile.profile_id)
      id_item.setData(PROFILE_ID_ROLE, profile.profile_id)
      id_item.setToolTip(profile.profile_id)
      self.profile_table.setItem(row, self.COL_ID, id_item)

      name_item = QTableWidgetItem(profile.name)
      name_item.setToolTip(profile.name)
      self.profile_table.setItem(row, self.COL_NAME, name_item)
      self.profile_table.setItem(row, self.COL_OS, QTableWidgetItem(self._device_os(profile)))
      self.profile_table.setItem(row, self.COL_BROWSER, QTableWidgetItem("Chrome"))
      proxy_label = self._proxy_label(profile)
      self.profile_table.setItem(row, self.COL_PROXY, QTableWidgetItem(proxy_label))
      self.profile_table.setItem(
        row,
        self.COL_TRAFFIC,
        QTableWidgetItem(self._format_bytes(self._profile_traffic_totals.get(profile.profile_id, 0))),
      )

      saved_key, saved_text = self._status_map.get(profile.profile_id, ("", ""))
      if saved_key:
        initial_key = saved_key
        initial_text = saved_text
      else:
        initial_key = UiStatusKey.CLOSED.value
        initial_text = ui_label(UiStatusKey.CLOSED)
      status_item = QTableWidgetItem(initial_text)
      self._style_status_item(status_item, initial_key, initial_text)
      self.profile_table.setItem(row, self.COL_STATUS, status_item)
      self.profile_table.setCellWidget(row, self.COL_ACTIONS, self._build_action_buttons(profile.profile_id))
      self._status_map[profile.profile_id] = (initial_key, initial_text)

    self.profile_table.blockSignals(False)
    self.profile_table.resizeRowsToContents()
    self._controller.set_profiles(self._profiles)

  def _build_action_buttons(self, profile_id: str) -> QWidget:
    container = QWidget()
    row = QHBoxLayout(container)
    row.setContentsMargins(2, 1, 2, 1)
    row.setSpacing(3)

    btn_start = QPushButton("▶")
    btn_start.setObjectName("btnRowStart")
    btn_start.setToolTip("Start")
    btn_start.setFixedSize(28, 26)
    btn_pause = QPushButton("⏸")
    btn_pause.setObjectName("btnRowPause")
    btn_pause.setToolTip("Pause")
    btn_pause.setFixedSize(28, 26)
    btn_kill = QPushButton("■")
    btn_kill.setObjectName("btnRowKill")
    btn_kill.setToolTip("Stop")
    btn_kill.setFixedSize(28, 26)
    btn_delete = QPushButton("✕")
    btn_delete.setObjectName("btnRowDelete")
    btn_delete.setToolTip("Delete")
    btn_delete.setFixedSize(28, 26)

    btn_start.clicked.connect(lambda _checked=False, pid=profile_id: self._on_row_start(pid))
    btn_pause.clicked.connect(lambda _checked=False, pid=profile_id: self._on_row_pause(pid))
    btn_kill.clicked.connect(lambda _checked=False, pid=profile_id: self._on_row_kill(pid))
    btn_delete.clicked.connect(lambda _checked=False, pid=profile_id: self._on_row_delete(pid))

    row.addWidget(btn_start)
    row.addWidget(btn_pause)
    row.addWidget(btn_kill)
    row.addWidget(btn_delete)
    row.addStretch()
    return container

  def _get_profile_id_at_row(self, row: int) -> str | None:
    item = self.profile_table.item(row, self.COL_ID)
    if not item:
      return None
    profile_id = item.data(PROFILE_ID_ROLE)
    return str(profile_id) if profile_id else None

  def _get_selected_profile_ids(self) -> list[str]:
    selected: list[str] = []
    for row in range(self.profile_table.rowCount()):
      check_item = self.profile_table.item(row, self.COL_CHECK)
      if not check_item or check_item.checkState() != Qt.CheckState.Checked:
        continue
      profile_id = self._get_profile_id_at_row(row)
      if profile_id:
        selected.append(profile_id)
    return selected

  def _on_select_all_changed(self, _state: int) -> None:
    if self._syncing_select_all:
      return
    check_state = (
      Qt.CheckState.Checked
      if self.chk_select_all.checkState() != Qt.CheckState.Unchecked
      else Qt.CheckState.Unchecked
    )
    self.profile_table.blockSignals(True)
    for row in range(self.profile_table.rowCount()):
      item = self.profile_table.item(row, self.COL_CHECK)
      if item:
        item.setCheckState(check_state)
    self.profile_table.blockSignals(False)

  def _on_profile_table_item_changed(self, item: QTableWidgetItem) -> None:
    if item.column() != self.COL_CHECK:
      return
    if self._syncing_select_all:
      return

    total = self.profile_table.rowCount()
    if total == 0:
      return

    checked = 0
    for row in range(total):
      check_item = self.profile_table.item(row, self.COL_CHECK)
      if check_item and check_item.checkState() == Qt.CheckState.Checked:
        checked += 1

    self._syncing_select_all = True
    if checked == 0:
      self.chk_select_all.setCheckState(Qt.CheckState.Unchecked)
    elif checked == total:
      self.chk_select_all.setCheckState(Qt.CheckState.Checked)
    else:
      self.chk_select_all.setTristate(True)
      self.chk_select_all.setCheckState(Qt.CheckState.PartiallyChecked)
      self.chk_select_all.setTristate(False)
    self._syncing_select_all = False

  def _minimal_config(self) -> BotConfig:
    try:
      return self._build_config(require_lists=False)
    except ValueError:
      return BotConfig(
        capsolver_api_key=self.capsolver_key.text().strip(),
        adspower_api_url=self.adspower_api_url.text().strip(),
        adspower_api_key=self.adspower_api_key.text().strip(),
      )

  def _delete_profiles(self, profile_ids: list[str]) -> None:
    if not profile_ids:
      self.append_log("[UI] No profiles selected.")
      return

    config = self._minimal_config()
    if not config.adspower_api_key:
      self.append_log("[UI] Delete failed: AdsPower API Key is required.")
      return

    try:
      manager = AdsPowerManager(config.adspower_url, config.adspower_api_key, self.append_log)
      for profile_id in profile_ids:
        self._controller.force_terminate(profile_id, config)
      manager.delete_profiles(profile_ids)
      deleted_set = set(profile_ids)
      self._profiles = [p for p in self._profiles if p.profile_id not in deleted_set]
      for profile_id in profile_ids:
        self._status_map.pop(profile_id, None)
      self._controller.remove_profiles(profile_ids)
      self._populate_profile_table()
      self.append_log(f"[UI] Deleted {len(profile_ids)} profile(s) from AdsPower.")
    except Exception as exc:
      self.append_log(f"[UI] Profile delete failed: {exc}")

  def _begin_session_click_log(self, config: BotConfig) -> str:
    path = new_session_click_log_path("data")
    config.session_click_log_path = str(path)
    self._session_click_log_path = str(path)
    self.append_log(f"[UI] Click log file: {path}")
    if hasattr(self, "result_session_list"):
      QTimer.singleShot(0, self._refresh_result_file_list)
    return str(path)

  def _reset_overall_clicks(self) -> None:
    self._overall_clicks_session = 0
    self.overall_clicks_value_label.setText("0")

  def _refresh_overall_clicks(self) -> None:
    path = (self._session_click_log_path or "").strip()
    if not path:
      return
    total = SessionClickCsvLogger.count_rows(path)
    self._overall_clicks_session = total
    self.overall_clicks_value_label.setText(str(total))

  def _on_result_click_logged(self) -> None:
    if self.tabs.currentIndex() != getattr(self, "_result_tab_index", -1):
      return
    current = self.result_session_list.currentItem()
    active_path = (self._session_click_log_path or "").strip()
    if not current or not active_path:
      return
    if str(current.data(Qt.ItemDataRole.UserRole) or "") == str(Path(active_path).resolve()):
      self._load_result_session(Path(active_path))

  def on_bulk_start(self) -> None:
    profile_ids = self._get_selected_profile_ids()
    if not profile_ids:
      self.append_log("[UI] No profiles selected.")
      return
    try:
      config = self._build_config()
    except ValueError as exc:
      self.append_log(f"[UI] {exc}")
      return
    self._begin_session_click_log(config)
    self._reset_overall_clicks()
    started = 0
    for profile_id in profile_ids:
      if self._controller.start_profile_manual(profile_id, config):
        started += 1
    self.append_log(f"[UI] Bulk start: {started}/{len(profile_ids)} launched")

  def on_bulk_pause(self) -> None:
    profile_ids = self._get_selected_profile_ids()
    if not profile_ids:
      self.append_log("[UI] No profiles selected.")
      return
    for profile_id in profile_ids:
      self._controller.pause_profile(profile_id)
    self.append_log(f"[UI] Bulk pause: {len(profile_ids)} profile(s)")

  def on_bulk_kill(self) -> None:
    profile_ids = self._get_selected_profile_ids()
    if not profile_ids:
      self.append_log("[UI] No profiles selected.")
      return
    config = self._minimal_config()
    for profile_id in profile_ids:
      self._controller.force_terminate(profile_id, config)
    self.append_log(f"[UI] Bulk stop: {len(profile_ids)} profile(s)")

  def on_bulk_delete(self) -> None:
    self._delete_profiles(self._get_selected_profile_ids())

  def _find_row_for_profile(self, profile_id: str) -> int:
    for row in range(self.profile_table.rowCount()):
      item = self.profile_table.item(row, self.COL_ID)
      if item and item.data(PROFILE_ID_ROLE) == profile_id:
        return row
    return -1

  def _format_status_display(self, display_text: str) -> str:
    text = (display_text or "").strip()
    if not text:
      return text
    timer_match = re.match(r"^(.+?)\s+(\[\d{2,}:\d{2}\])$", text)
    if timer_match:
      return f"{timer_match.group(1).strip()}\n{timer_match.group(2)}"
    paren_match = re.match(r"^(.+?)\s+(\([^)]+\))$", text)
    if paren_match and len(text) > 14:
      return f"{paren_match.group(1).strip()}\n{paren_match.group(2)}"
    words = text.split()
    if len(words) >= 3 and len(text) > 16:
      mid = (len(words) + 1) // 2
      return "\n".join([" ".join(words[:mid]), " ".join(words[mid:])])
    return text

  def _style_status_item(self, item: QTableWidgetItem, status_key: str, display_text: str) -> None:
    formatted = self._format_status_display(display_text)
    item.setText(formatted)
    item.setToolTip(display_text)
    item.setTextAlignment(
      Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    )
    color = STATUS_COLORS.get(status_key, QColor("#808080"))
    item.setForeground(color)
    font = item.font()
    font.setBold(
      status_key in (
        UiStatusKey.CAPTCHA.value,
        UiStatusKey.CAPTCHA_MANUAL.value,
        UiStatusKey.SELF_HEALING.value,
      )
    )
    item.setFont(font)

  def _strip_timer_suffix(self, text: str) -> str:
    return _TIMER_SUFFIX_RE.sub("", text or "")

  def _format_status_with_elapsed(
    self,
    profile_id: str,
    status_key: str,
    display_text: str,
  ) -> str:
    if self._controller.get_cooldown_remaining(profile_id) > 0:
      return display_text
    if status_key in CAPTCHA_ELAPSED_KEYS:
      elapsed = self._controller.get_captcha_elapsed(profile_id)
    elif status_key in ERROR_ELAPSED_KEYS:
      elapsed = self._controller.get_error_elapsed(profile_id)
    elif status_key in SESSION_ELAPSED_KEYS:
      elapsed = self._controller.get_session_elapsed(profile_id)
    else:
      return self._strip_timer_suffix(display_text)
    if elapsed <= 0:
      return self._strip_timer_suffix(display_text)
    base = self._strip_timer_suffix(display_text) or status_key
    minutes, seconds = divmod(elapsed, 60)
    return f"{base} [{minutes:02d}:{seconds:02d}]"

  def _update_status_cell(self, profile_id: str, status_key: str, display_text: str) -> None:
    row = self._find_row_for_profile(profile_id)
    if row < 0:
      return
    item = self.profile_table.item(row, self.COL_STATUS)
    if item:
      self._style_status_item(item, status_key, display_text)
      self.profile_table.resizeRowToContents(row)
    self._status_map[profile_id] = (status_key, display_text)

  def _tick_cooldowns(self) -> None:
    for profile_id in list(self._status_map.keys()):
      remaining = self._controller.get_cooldown_remaining(profile_id)
      if remaining > 0:
        minutes, seconds = divmod(remaining, 60)
        text = f"{ui_label(UiStatusKey.CLOSED)} [{minutes:02d}:{seconds:02d}]"
        self._update_status_cell(profile_id, UiStatusKey.CLOSED.value, text)
        continue
      status_key, display_text = self._status_map[profile_id]
      if status_key not in SESSION_ELAPSED_KEYS | CAPTCHA_ELAPSED_KEYS | ERROR_ELAPSED_KEYS:
        continue
      text = self._format_status_with_elapsed(profile_id, status_key, display_text)
      if text != display_text:
        self._update_status_cell(profile_id, status_key, text)

  def _set_global_running(self, running: bool) -> None:
    self._global_running = running
    self.btn_start_auto.setEnabled(not running)
    self.btn_stop_auto.setEnabled(running)
    self.btn_create.setEnabled(not running)
    self.threads_spin.setEnabled(not running)
    self.cycles_spin.setEnabled(not running)

  def _schedule_live_profile_refresh(self) -> None:
    if self._global_running:
      self._pending_live_refresh = True
      return
    self._live_sync_queue_count = min(100, self._live_sync_queue_count + 1)
    if self._live_profile_refresh_timer.isActive():
      return
    self._live_profile_refresh_timer.start()

  def _refresh_profiles_live(self) -> None:
    if self._global_running:
      self._pending_live_refresh = True
      return
    if self._live_sync_inflight:
      self._live_profile_refresh_timer.start()
      return
    if self._live_sync_queue_count <= 0:
      return
    self._live_sync_queue_count -= 1
    self._live_sync_inflight = True
    try:
      self.append_log("[UI] Live refresh (queued) triggered by profile events.")
      self.on_refresh_profiles()
    finally:
      self._live_sync_inflight = False
      if self._live_sync_queue_count > 0:
        self._live_profile_refresh_timer.start()

  def _on_profile_created_event(self, profile: ProfileSpec) -> None:
    if any(existing.profile_id == profile.profile_id for existing in self._profiles):
      return
    self._profiles.append(profile)
    self.append_log(f"[UI] Profile created: {profile.name} ({profile.profile_id})")
    self._populate_profile_table()

  def _on_profile_deleted_event(self, profile_id: str) -> None:
    before = len(self._profiles)
    self._profiles = [profile for profile in self._profiles if profile.profile_id != profile_id]
    if len(self._profiles) == before:
      return
    self._status_map.pop(profile_id, None)
    self._profile_traffic_totals.pop(profile_id, None)
    self.append_log(f"[UI] Profile deleted: {profile_id}")
    self._populate_profile_table()

  def _on_keyword_excluded_event(self, keyword: str) -> None:
    needle = (keyword or "").strip()
    if not needle:
      return
    self.append_log(
      f"[UI] Keyword '{needle}' was not found after a full SERP scan. "
      "It remains in your keyword list."
    )

  @staticmethod
  def _format_bytes(total_bytes: int) -> str:
    value = float(max(0, int(total_bytes)))
    units = ("B", "KB", "MB", "GB", "TB")
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
      value /= 1024.0
      unit_index += 1
    if unit_index == 0:
      return f"{int(value)} {units[unit_index]}"
    return f"{value:.2f} {units[unit_index]}"

  def _on_proxy_traffic_update(self, proxy_key: str, total_bytes: int) -> None:
    _ = proxy_key
    _ = total_bytes

  def _on_profile_traffic_update(self, profile_id: str, total_bytes: int, total_all_bytes: int) -> None:
    self._profile_traffic_totals[profile_id] = int(total_bytes)
    self._session_traffic_total = max(self._session_traffic_total, int(total_all_bytes))
    self.proxy_traffic_total_label.setText(self._format_bytes(self._session_traffic_total))
    display = self._format_bytes(total_bytes)
    row = self._find_row_for_profile(profile_id)
    if row < 0:
      return
    traffic_item = self.profile_table.item(row, self.COL_TRAFFIC)
    if traffic_item:
      traffic_item.setText(display)

  def _on_cycle_progress_update(self, current_cycle: int, target_cycles: int) -> None:
    current = max(0, int(current_cycle))
    target = max(0, int(target_cycles))
    self.present_cycle_value_label.setText(f"{current} / {target}")

  def _on_profile_finished(self, profile_id: str, outcome: str) -> None:
    _ = profile_id
    if outcome == "not_found" and self.tabs.currentIndex() == getattr(self, "_result_tab_index", -1):
      current = self.result_session_list.currentItem()
      active_path = (self._session_click_log_path or "").strip()
      if current and active_path and str(current.data(Qt.ItemDataRole.UserRole) or "") == str(Path(active_path).resolve()):
        self._load_result_session(Path(active_path))

  def on_create_profiles(self) -> None:
    try:
      config = self._build_config(require_lists=False)
    except ValueError as exc:
      self.append_log(f"[UI] {exc}")
      return

    if not config.adspower_api_key:
      self.append_log(
        "[UI] Create skipped: enter AdsPower API Key in Settings → AdsPower API Key."
      )
      return

    if not config.proxies:
      self.append_log(
        "[UI] No proxies configured — creating in no_proxy test mode. "
        "Add proxies (username:password@ip:port) in Settings and click Save Settings before production runs."
      )
    elif len(config.proxies) < config.profile_count:
      self.append_log(
        f"[UI] Not enough proxies: need {config.profile_count} for {config.profile_count} profiles "
        f"({len(config.proxies)} configured). Each proxy is assigned once."
      )
      return

    self.btn_create.setEnabled(False)
    try:
      manager = AdsPowerManager(config.adspower_url, config.adspower_api_key, self.append_log)
      ok, conn_msg = manager.check_connection()
      self.append_log(f"[UI] {conn_msg}")
      if not ok:
        return

      manager.create_profiles_batch(
        config.proxies,
        config.adspower_group_id,
        total=config.profile_count,
      )
      self._profiles = manager.list_profiles_live(group_id=config.adspower_group_id)
      self._populate_profile_table()
      self.append_log(
        f"[UI] AdsPower sync complete: requested {config.profile_count}, "
        f"listing {len(self._profiles)} profile(s)."
      )
    except Exception as exc:
      self.append_log(f"[UI] Profile creation failed: {exc}")
    finally:
      if not self._global_running:
        self.btn_create.setEnabled(True)

  def on_refresh_profiles(self) -> None:
    self.btn_refresh.setEnabled(False)
    try:
      manager = self._make_adspower_manager()
      ok, conn_msg = manager.check_connection()
      self.append_log(f"[UI] {conn_msg}")
      if not ok:
        return

      if not self.adspower_api_key.text().strip():
        self.append_log(
          "[UI] Refresh skipped: enter AdsPower API Key in Settings → AdsPower API Key."
        )
        return

      try:
        config = self._build_config(require_lists=False)
        group_id = config.adspower_group_id
      except ValueError:
        group_id = "0"

      self._profiles = manager.list_profiles_live(group_id=group_id)
      self._populate_profile_table()
      self.append_log(f"[UI] Live refresh: {len(self._profiles)} profiles from AdsPower /api/v1/user/list.")
    except Exception as exc:
      self.append_log(f"[UI] Refresh failed: {exc}")
    finally:
      self.btn_refresh.setEnabled(True)

  def on_start_automated(self) -> None:
    try:
      config = self._build_config()
    except ValueError as exc:
      self.append_log(f"[UI] {exc}")
      return
    if not config.adspower_api_key:
      self.append_log("[UI] Enter AdsPower API Key before starting automated mode.")
      return
    config.auto_create_profiles = True
    config.automation_threads = self.threads_spin.value()
    config.automation_cycles = self.cycles_spin.value()
    self.present_cycle_value_label.setText(f"0 / {config.automation_cycles}")
    self._begin_session_click_log(config)
    self._reset_overall_clicks()
    cleared = self._controller.clear_keyword_exclusions(config.target_domain)
    if cleared:
      self.append_log(
        f"[UI] Cleared {cleared} previously auto-excluded keyword(s) for {config.target_domain}."
      )
    try:
      manager = AdsPowerManager(config.adspower_url, config.adspower_api_key, self.append_log)
      manager.reset_profile_name_counter(0)
      self.append_log("[UI] Automation run naming reset: next profile starts at s-001.")
    except Exception as exc:
      self.append_log(f"[UI] Failed to reset automation name counter: {exc}")
      return
    if self._controller.start_global(config, self._profiles):
      self._set_global_running(True)
      self.append_log(
        f"[UI] Auto-create automation started: threads={config.automation_threads}, "
        f"cycles={config.automation_cycles}, "
        f"launch interval {config.launch_interval_min}-{config.launch_interval_max}s."
      )
    else:
      self.append_log("[UI] Global bot is already running.")

  def on_stop_automated(self) -> None:
    self._controller.stop_global()

  def _on_global_finished(self) -> None:
    self._set_global_running(False)
    self.append_log("[UI] Automated bot stopped.")
    if self._pending_live_refresh:
      self._pending_live_refresh = False
    self._schedule_live_profile_refresh()

  def _on_profile_update(self, profile_id: str, status_key: str, display_text: str) -> None:
    display_text = self._format_status_with_elapsed(profile_id, status_key, display_text)
    self._update_status_cell(profile_id, status_key, display_text)

  def _on_row_start(self, profile_id: str) -> None:
    try:
      config = self._build_config()
    except ValueError as exc:
      self.append_log(f"[UI] {exc}")
      return
    self._begin_session_click_log(config)
    self._reset_overall_clicks()
    self._controller.start_profile_manual(profile_id, config)

  def _on_row_pause(self, profile_id: str) -> None:
    self._controller.pause_profile(profile_id)

  def _on_row_kill(self, profile_id: str) -> None:
    self._controller.force_terminate(profile_id, self._minimal_config())

  def _on_row_delete(self, profile_id: str) -> None:
    self._delete_profiles([profile_id])

  def closeEvent(self, event) -> None:
    try:
      self._persist_ai_chat_state()
    except Exception:
      pass
    try:
      self._cursor_chat.shutdown()
    except Exception:
      pass
    super().closeEvent(event)
