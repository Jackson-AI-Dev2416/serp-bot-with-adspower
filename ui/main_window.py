import re
import time
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QGuiApplication, QPalette
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
  QMessageBox,
  QPlainTextEdit,
  QPushButton,
  QButtonGroup,
  QSizePolicy,
  QSpinBox,
  QTabWidget,
  QTableWidget,
  QTableWidgetItem,
  QTextEdit,
  QVBoxLayout,
  QWidget,
)
from ui.cards import make_kpi_card
from ui.settings_widgets import (
  PasswordField,
  SETTINGS_COMBO_OS_WIDTH,
  ToggleSwitch,
  apply_spinbox_width,
  make_labeled_field_row,
  make_labeled_toggle_stack,
  make_range_row,
  make_settings_card,
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
from utils.app_paths import app_base_dir, data_dir
from utils.csv_logger import (
  SessionClickCsvLogger,
  aggregate_keyword_clicks,
  aggregate_keyword_clicks_for_domain,
  count_session_click_outcomes,
  filter_click_rows_by_domain,
  format_result_session_label,
  list_session_result_files,
  new_session_click_log_path,
  read_session_click_file,
  session_target_domains,
  should_auto_stop_on_failure_rate,
)

PROFILE_ID_ROLE = Qt.ItemDataRole.UserRole

def _load_theme_stylesheet() -> str:
  path = Path(__file__).resolve().parent / "theme.qss"
  try:
    return path.read_text(encoding="utf-8")
  except OSError:
    return ""


STYLESHEET = """
QMainWindow {
  background-color: #080B14;
}
QWidget {
  background-color: transparent;
  color: #F8FAFC;
  font-family: "Segoe UI", "Inter", sans-serif;
  font-size: 13px;
}
QWidget#contentArea {
  background-color: #080B14;
}
QFrame#appSidebar {
  background-color: #111827;
  border-right: 1px solid #263044;
}
QLabel#sidebarLogo {
  font-size: 20px;
  font-weight: 800;
  color: #F8FAFC;
  letter-spacing: -0.4px;
  line-height: 1.2;
}
QLabel#sidebarSubtitle {
  font-size: 11px;
  color: #64748b;
  margin-top: -2px;
}
QPushButton#sidebarNavBtn {
  background: transparent;
  color: #94A3B8;
  text-align: left;
  padding: 10px 14px;
  border-radius: 8px;
  font-weight: 600;
  min-height: 36px;
}
QPushButton#sidebarNavBtn:hover {
  background-color: #151B2B;
  color: #E2E8F0;
}
QPushButton#sidebarNavBtn:checked {
  background-color: #7C3AED;
  color: #F8FAFC;
}
QFrame#kpiCard {
  background-color: #151B2B;
  border: 1px solid #263044;
  border-radius: 12px;
}
QLabel#kpiTitle {
  font-size: 11px;
  font-weight: 600;
  color: #94A3B8;
  letter-spacing: 0.6px;
}
QLabel#kpiValue {
  font-size: 26px;
  font-weight: 700;
  color: #F8FAFC;
}
QFrame#panelCard {
  background-color: #151B2B;
  border: 1px solid #263044;
  border-radius: 12px;
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
QGroupBox#settingsGroup {
  margin-top: 8px;
  padding: 10px 12px 10px 12px;
}
QFrame#settingsCard {
  background-color: rgba(30, 30, 45, 0.92);
  border: 1px solid #2b2b40;
  border-radius: 14px;
}
QLabel#settingsCardTitle {
  font-size: 15px;
  font-weight: 700;
  color: #f1f5f9;
  letter-spacing: -0.2px;
}
QLabel#settingsCardSubtitle {
  font-size: 11px;
  color: #64748b;
  margin-top: 5px;
  padding-top: 2px;
}
QLabel#settingsFormLabel,
QLabel#settingsRangeLabel {
  color: #94a3b8;
  font-size: 12px;
  font-weight: 500;
}
QLabel#settingsRangeCaption {
  color: #64748b;
  font-size: 11px;
  font-weight: 600;
}
QLabel#settingsRangeSep {
  color: #6366f1;
  font-size: 14px;
  font-weight: 700;
}
QLabel#settingsRangeUnit {
  color: #64748b;
  font-size: 11px;
  font-weight: 600;
  min-width: 28px;
}
QWidget#passwordFieldWrap {
  background-color: #12121f;
  border: 1px solid #2b2b40;
  border-radius: 8px;
}
QWidget#passwordFieldWrap:focus-within {
  border: 2px solid #818cf8;
  background-color: #141428;
}
QPushButton#btnPasswordToggle {
  background: transparent;
  color: #94a3b8;
  border: none;
  border-left: 1px solid #2b2b40;
  border-radius: 0 8px 8px 0;
  padding: 0 10px;
  font-size: 11px;
  font-weight: 600;
}
QPushButton#btnPasswordToggle:hover {
  color: #e2e8f0;
  background-color: #1e1e2d;
}
QPushButton#btnPasswordToggle:checked {
  color: #c4b5fd;
}
QCheckBox#settingsToggle {
  spacing: 10px;
  color: #cbd5e1;
  font-size: 12px;
  font-weight: 500;
}
QCheckBox#settingsToggle::indicator {
  width: 46px;
  height: 24px;
  border-radius: 12px;
  border: 1px solid #3f3f5a;
  background: #252538;
}
QCheckBox#settingsToggle::indicator:hover {
  border-color: #6366f1;
}
QCheckBox#settingsToggle::indicator:checked {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366f1, stop:1 #a855f7);
  border: 1px solid #a78bfa;
}
QFrame#settingsFooter {
  background-color: rgba(13, 16, 23, 0.85);
  border-top: 1px solid #2b2b40;
  border-radius: 0 0 12px 12px;
  padding: 12px 20px;
  margin-top: 4px;
}
QGroupBox#settingsGroup::title {
  left: 10px;
}
QLineEdit#settingsApiField,
QComboBox#settingsComboField,
QSpinBox#settingsField,
QDoubleSpinBox#settingsField {
  min-height: 34px;
  max-height: 34px;
}
QLineEdit#settingsApiField {
  background-color: #12121f;
  border: 1px solid #2b2b40;
  border-radius: 8px;
  padding: 8px 12px;
}
QLineEdit#settingsApiField:focus {
  border: 2px solid #818cf8;
  background-color: #141428;
}
QWidget#passwordFieldWrap QLineEdit#settingsApiField {
  background: transparent;
  border: none;
}
QWidget#passwordFieldWrap QLineEdit#settingsApiField:focus {
  background: transparent;
  border: none;
}
QSpinBox#settingsField,
QDoubleSpinBox#settingsField,
QComboBox#settingsComboField {
  background-color: #12121f;
  color: #e2e8f0;
  border: 1px solid #2b2b40;
  border-radius: 8px;
  padding: 6px 10px;
}
QSpinBox#settingsField:focus,
QDoubleSpinBox#settingsField:focus,
QComboBox#settingsComboField:focus,
QComboBox#settingsComboField:on {
  border: 2px solid #818cf8;
  background-color: #141428;
}
QComboBox#settingsComboField {
  padding-right: 30px;
}
QComboBox#settingsComboField::drop-down {
  subcontrol-origin: padding;
  subcontrol-position: top right;
  width: 28px;
  border: none;
  border-top-right-radius: 8px;
  border-bottom-right-radius: 8px;
  background: #141925;
}
QComboBox#settingsComboField::down-arrow {
  width: 10px;
  height: 10px;
  image: none;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 6px solid #94a3b8;
  margin-right: 8px;
}
QComboBox#settingsComboField QAbstractItemView {
  background-color: #0d1017;
  color: #e2e8f0;
  border: 1px solid #252b38;
  selection-background-color: #6366f1;
  outline: none;
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
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus {
  border: 2px solid #818cf8;
  background-color: #141428;
}
QSpinBox:focus, QDoubleSpinBox:focus {
  border: 2px solid #818cf8;
  background-color: #141428;
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
QPushButton#btnCreate,
QPushButton#btnStartAuto,
QPushButton#btnStopAuto,
QPushButton#btnRefresh,
QPushButton#btnBulkStart,
QPushButton#btnBulkPause,
QPushButton#btnBulkKill,
QPushButton#btnBulkDelete {
  min-height: 36px;
  max-height: 36px;
  padding: 0 16px;
}
QPushButton#btnCreate {
  background-color: #7C3AED;
  color: #ffffff;
}
QPushButton#btnCreate:hover { background-color: #8B5CF6; }
QPushButton#btnCreate:pressed { background-color: #6D28D9; }
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
QPushButton#btnDeleteResult {
  background-color: #7f1d1d;
  color: #fecaca;
  border: 1px solid #b91c1c;
  padding: 7px 14px;
}
QPushButton#btnDeleteResult:hover {
  background-color: #991b1b;
  border-color: #f87171;
  color: #ffffff;
}
QPushButton#btnDeleteResult:disabled {
  background-color: #1e2430;
  color: #64748b;
  border-color: #334155;
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
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #6366f1, stop:1 #a855f7);
  color: #ffffff;
  min-width: 200px;
  min-height: 42px;
  padding: 12px 28px;
  font-size: 14px;
  font-weight: 700;
  border: 1px solid #7c3aed;
  border-radius: 10px;
}
QPushButton#btnSaveSettings:hover {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #818cf8, stop:1 #c084fc);
  border-color: #c4b5fd;
  padding: 13px 30px;
}
QPushButton#btnSaveSettings:pressed {
  background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #4f46e5, stop:1 #7e22ce);
  border-color: #5b21b6;
  padding: 11px 26px;
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
QPlainTextEdit#logView, QTextEdit#logView {
  background-color: #06080c;
  color: #e2e8f0;
  border: 1px solid #1e2430;
  border-radius: 12px;
  padding: 10px;
  font-family: "Cascadia Code", "Malgun Gothic", Consolas, monospace;
  font-size: 13px;
  line-height: 1.45;
  selection-background-color: #312e81;
  selection-color: #ffffff;
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
  UiStatusKey.CLOSED.value: QColor("#64748b"),
}

_TIMER_SUFFIX_RE = re.compile(r" \[\d{2,}:\d{2}\]$")


class SelectAllHeaderView(QHeaderView):
  """Checkbox embedded in the profile table Select column header."""

  def __init__(self, parent=None) -> None:
    super().__init__(Qt.Orientation.Horizontal, parent)
    self.chk_select_all = QCheckBox(self)
    self.chk_select_all.setObjectName("selectAllCheck")
    self.chk_select_all.setToolTip("Select All")
    self.sectionResized.connect(self._reposition_checkbox)
    self.geometriesChanged.connect(self._reposition_checkbox)

  def showEvent(self, event) -> None:
    super().showEvent(event)
    self._reposition_checkbox()

  def resizeEvent(self, event) -> None:
    super().resizeEvent(event)
    self._reposition_checkbox()

  def _reposition_checkbox(self) -> None:
    if self.count() < 1:
      return
    section_x = self.sectionPosition(0)
    section_w = self.sectionSize(0)
    checkbox = self.chk_select_all
    checkbox_w = checkbox.sizeHint().width()
    checkbox_h = checkbox.sizeHint().height()
    checkbox.move(
      section_x + max(0, (section_w - checkbox_w) // 2),
      max(0, (self.height() - checkbox_h) // 2),
    )


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
    self.setWindowTitle("SERP Automation")
    theme = _load_theme_stylesheet()
    self.setStyleSheet(theme or STYLESHEET)

    self._profiles: list[ProfileSpec] = []
    self._controller = ProfileController(self)
    self._project_root = app_base_dir()
    self._data_dir = data_dir()
    self._status_map: dict[str, tuple[str, str]] = {}
    self._profile_traffic_totals: dict[str, int] = {}
    self._session_traffic_total: int = 0
    self._session_target_traffic: int = 0
    self._session_other_traffic: int = 0
    self._overall_clicks_session: int = 0
    self._session_captcha_auto: int = 0
    self._session_captcha_total: int = 0
    self._session_click_log_path: str = ""
    self._global_running = False
    self._failure_rate_auto_stop_triggered = False
    self._syncing_select_all = False
    self._live_profile_refresh_timer = QTimer(self)
    self._live_profile_refresh_timer.setSingleShot(True)
    self._live_profile_refresh_timer.setInterval(500)
    self._live_profile_refresh_timer.timeout.connect(self._refresh_profiles_live)
    self._pending_live_refresh = False
    self._live_sync_queue_count = 0
    self._live_sync_inflight = False
    self._elapsed_seconds = 0
    self._elapsed_timer = QTimer(self)
    self._elapsed_timer.setInterval(1000)
    self._elapsed_timer.timeout.connect(self._tick_elapsed_time)

    self._build_ui()
    self._fit_window_to_screen()
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
    self.tabs.setDocumentMode(True)
    self.setCentralWidget(self.tabs)
    self.tabs.addTab(self._build_dashboard_tab(), "Dashboard")
    self.tabs.addTab(self._build_settings_tab(), "Settings")
    self.tabs.addTab(self._build_result_tab(), "Result")
    self._result_tab_index = self.tabs.count() - 1
    self.tabs.currentChanged.connect(self._on_main_tab_changed)

  def _current_page_index(self) -> int:
    return self.tabs.currentIndex()

  def _fit_window_to_screen(self) -> None:
    """Keep the control panel within the monitor work area (DPI / taskbar safe)."""
    screen = QGuiApplication.primaryScreen()
    if screen is None:
      self.setMinimumSize(1100, 640)
      self.resize(1480, 900)
      return

    available = screen.availableGeometry()
    min_w = min(1100, max(960, available.width() // 2))
    min_h = min(640, max(520, available.height() // 2))
    self.setMinimumSize(min_w, min_h)

    margin = 16
    target_w = max(min_w, available.width() - margin)
    target_h = max(min_h, int(available.height() * 0.92))
    self.resize(target_w, target_h)

    frame = self.frameGeometry()
    frame.moveCenter(available.center())
    self.move(frame.topLeft())

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
    layout.setSpacing(8)
    layout.setContentsMargins(4, 4, 4, 4)

    kpi_row = QHBoxLayout()
    kpi_row.setSpacing(12)
    elapsed_card, self.elapsed_time_value_label = make_kpi_card(
      "Elapsed time",
      tooltip="Automation run duration since Start Automation (MM:SS).",
    )
    self.elapsed_time_value_label.setText("00:00")
    traffic_card, self.proxy_traffic_total_label = make_kpi_card(
      "Proxy Traffic",
      tooltip=(
        "CDP wire estimate (upload+download through proxy tunnel). "
        "Format: total (site / other). "
        "Before target open = other; after successful click/touch = site."
      ),
    )
    self.proxy_traffic_total_label.setText("0 B")
    cycle_card, self.present_cycle_value_label = make_kpi_card("Current Session")
    self.present_cycle_value_label.setText("0 / 0")
    clicks_card, self.overall_clicks_value_label = make_kpi_card("Total Clicks")
    self.overall_clicks_value_label.setText("0 / 0")
    active_card, self.captcha_occurs_value_label = make_kpi_card("Captcha Occurs")
    self.captcha_occurs_value_label.setText("0 / 0")
    for card in (elapsed_card, traffic_card, cycle_card, clicks_card, active_card):
      kpi_row.addWidget(card, stretch=1)
    layout.addLayout(kpi_row)

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

    self.btn_bulk_start = QPushButton("Start Selected")
    self.btn_bulk_start.setObjectName("btnBulkStart")
    self.btn_bulk_pause = QPushButton("Pause Selected")
    self.btn_bulk_pause.setObjectName("btnBulkPause")
    self.btn_bulk_kill = QPushButton("Stop Selected")
    self.btn_bulk_kill.setObjectName("btnBulkKill")
    self.btn_bulk_delete = QPushButton("Delete Selected")
    self.btn_bulk_delete.setObjectName("btnBulkDelete")

    control_bar.addWidget(count_label)
    control_bar.addWidget(self.profile_count_spin)
    control_bar.addWidget(self.btn_create)
    control_bar.addWidget(self.btn_refresh)
    control_bar.addSpacing(30)
    control_bar.addWidget(self.btn_bulk_start)
    control_bar.addWidget(self.btn_bulk_pause)
    control_bar.addWidget(self.btn_bulk_kill)
    control_bar.addWidget(self.btn_bulk_delete)
    control_bar.addStretch()
    control_bar.addWidget(threads_label)
    control_bar.addWidget(self.threads_spin)
    control_bar.addWidget(cycles_label)
    control_bar.addWidget(self.cycles_spin)
    control_bar.addWidget(self.btn_start_auto)
    control_bar.addWidget(self.btn_stop_auto)
    layout.addWidget(self._card_panel(control_bar))

    self.profile_table = QTableWidget(0, len(self.COLUMNS))
    self.profile_table.setObjectName("profileTable")
    select_header = SelectAllHeaderView(self.profile_table)
    self.profile_table.setHorizontalHeader(select_header)
    self.chk_select_all = select_header.chk_select_all
    self.profile_table.setHorizontalHeaderLabels(list(self.COLUMNS))
    header_item = self.profile_table.horizontalHeaderItem(self.COL_CHECK)
    if header_item is not None:
      header_item.setText("")
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
    header.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Interactive)
    header.setSectionResizeMode(self.COL_OS, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_BROWSER, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_PROXY, QHeaderView.ResizeMode.Interactive)
    header.setSectionResizeMode(self.COL_TRAFFIC, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(self.COL_STATUS, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(self.COL_ACTIONS, QHeaderView.ResizeMode.ResizeToContents)
    self.profile_table.setColumnWidth(self.COL_NAME, 80)
    self.profile_table.setColumnWidth(self.COL_PROXY, 140)
    self.profile_table.setColumnWidth(self.COL_STATUS, 140)
    self.profile_table.setWordWrap(True)
    self.profile_table.setTextElideMode(Qt.TextElideMode.ElideNone)
    self.profile_table.itemChanged.connect(self._on_profile_table_item_changed)

    log_title = QLabel("ACTIVITY LOG")
    log_title.setObjectName("sectionTitle")
    self.log_view = QTextEdit()
    self.log_view.setObjectName("logView")
    self.log_view.setReadOnly(True)
    self.log_view.document().setMaximumBlockCount(8000)
    log_font = QFont("Malgun Gothic", 11)
    log_font.setStyleHint(QFont.StyleHint.Monospace)
    self.log_view.setFont(log_font)

    left_panel = QWidget()
    left_layout = QVBoxLayout(left_panel)
    left_layout.setContentsMargins(0, 0, 0, 0)
    left_layout.setSpacing(0)
    left_layout.addWidget(self.profile_table)

    right_panel = QWidget()
    right_panel.setMinimumWidth(338)
    right_layout = QVBoxLayout(right_panel)
    right_layout.setContentsMargins(0, 0, 0, 0)
    right_layout.setSpacing(6)
    right_layout.addWidget(log_title)
    right_layout.addWidget(self.log_view, stretch=1)

    split_row = QHBoxLayout()
    split_row.setSpacing(12)
    split_row.addWidget(left_panel, stretch=5)
    split_row.addWidget(right_panel, stretch=3)
    layout.addLayout(split_row, stretch=1)
    return tab

  @staticmethod
  def _configure_settings_line_edit(edit: QLineEdit) -> None:
    edit.setObjectName("settingsApiField")
    edit.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Fixed,
    )
    edit.setMinimumWidth(280)
    edit.setFixedHeight(36)

  @staticmethod
  def _configure_settings_combo(combo: QComboBox) -> None:
    combo.setObjectName("settingsComboField")
    combo.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Fixed,
    )
    combo.setMinimumWidth(120)
    combo.setFixedHeight(36)

  @staticmethod
  def _configure_settings_spinbox(spinbox) -> None:
    spinbox.setObjectName("settingsField")
    spinbox.setFixedHeight(36)
    spinbox.setAlignment(Qt.AlignmentFlag.AlignCenter)
    apply_spinbox_width(spinbox)

  def _password_field(self, line_edit: QLineEdit) -> PasswordField:
    self._configure_settings_line_edit(line_edit)
    return PasswordField(line_edit)

  def _build_settings_tab(self) -> QWidget:
    tab = QWidget()
    root = QVBoxLayout(tab)
    root.setContentsMargins(8, 4, 8, 8)
    root.setSpacing(4)

    outer = QHBoxLayout()
    outer.setSpacing(14)

    left_col = QVBoxLayout()
    left_col.setSpacing(0)
    left_col.setContentsMargins(0, 0, 0, 0)

    api_card, api_body = make_settings_card(
      "API & Connection Settings",
      "CapSolver and AdsPower credentials for automation.",
    )
    api_card.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Maximum,
    )
    self.capsolver_key = QLineEdit()
    self.capsolver_key.setPlaceholderText("Leave empty for manual captcha solving in browser")
    capsolver_field = self._password_field(self.capsolver_key)
    self.adspower_api_url = QLineEdit("http://local.adspower.com:50325")
    self.adspower_api_url.setPlaceholderText("http://local.adspower.com:50325")
    self._configure_settings_line_edit(self.adspower_api_url)
    self.adspower_api_key = QLineEdit()
    self.adspower_api_key.setPlaceholderText("AdsPower Bearer API key")
    adspower_key_field = self._password_field(self.adspower_api_key)
    api_body.addWidget(make_labeled_field_row("CapSolver API Key:", capsolver_field, full_width=True))
    api_body.addWidget(make_labeled_field_row("AdsPower API URL:", self.adspower_api_url, full_width=True))
    api_body.addWidget(make_labeled_field_row("AdsPower API Key:", adspower_key_field, full_width=True))

    launch_row = self._min_max_row(QSpinBox, 1, 3600, 1, 4)
    start_delay_row = self._min_max_row(QSpinBox, 0, 3600, 10, 30)
    dwell_row = self._min_max_row(QSpinBox, 10, 3600, 60, 120)
    internal_link_row = self._min_max_row(QSpinBox, 0, 5, 1, 1)
    warmup_dwell_row = self._min_max_row(QSpinBox, 1, 3600, 8, 16)
    warmup_count_row = self._min_max_row(QSpinBox, 1, 10, 1, 2)
    action_row = self._min_max_row(QDoubleSpinBox, 0.05, 5.0, 0.1, 0.3, step=0.05)
    for spinboxes in (
      launch_row, start_delay_row, dwell_row, internal_link_row,
      warmup_dwell_row, warmup_count_row, action_row,
    ):
      self._configure_settings_spinbox(spinboxes[0])
      self._configure_settings_spinbox(spinboxes[1])
    self.launch_min, self.launch_max = launch_row
    self.session_start_delay_min, self.session_start_delay_max = start_delay_row
    self.dwell_min, self.dwell_max = dwell_row
    self.internal_link_min, self.internal_link_max = internal_link_row
    self.warmup_dwell_min, self.warmup_dwell_max = warmup_dwell_row
    self.warmup_count_min, self.warmup_count_max = warmup_count_row
    self.action_delay_min, self.action_delay_max = action_row

    self.max_search_pages = QSpinBox()
    self.max_search_pages.setRange(1, 50)
    self.max_search_pages.setValue(8)
    self._configure_settings_spinbox(self.max_search_pages)
    self.max_keywords_per_profile = QSpinBox()
    self.max_keywords_per_profile.setRange(1, 100)
    self.max_keywords_per_profile.setValue(3)
    self._configure_settings_spinbox(self.max_keywords_per_profile)
    self.max_keywords_per_profile.setToolTip(
      "When the assigned keyword/domain pair is not found, retry up to this many times "
      "per profile. Retries try other target sites first; after all sites are tried, "
      "the keyword changes (fresh search)."
    )
    self.failure_rate_auto_stop_percent = QSpinBox()
    self.failure_rate_auto_stop_percent.setRange(0, 100)
    self.failure_rate_auto_stop_percent.setValue(20)
    self.failure_rate_auto_stop_percent.setSuffix("%")
    self._configure_settings_spinbox(self.failure_rate_auto_stop_percent)
    self.failure_rate_auto_stop_percent.setToolTip(
      "Auto-stop automation when session failure rate exceeds this value.\n"
      "0 = disabled. Default 20%."
    )
    self.failure_rate_auto_stop_min_attempts = QSpinBox()
    self.failure_rate_auto_stop_min_attempts.setRange(1, 1000)
    self.failure_rate_auto_stop_min_attempts.setValue(20)
    self._configure_settings_spinbox(self.failure_rate_auto_stop_min_attempts)
    self.failure_rate_auto_stop_min_attempts.setToolTip(
      "Minimum completed session outcomes before failure-rate auto-stop is evaluated."
    )
    self.profile_os_mode_combo = QComboBox()
    self.profile_os_mode_combo.addItem("Mixed (Windows + Android)", "mixed")
    self.profile_os_mode_combo.addItem("Windows", "windows_only")
    self.profile_os_mode_combo.addItem("Android", "android_only")
    self._configure_settings_combo(self.profile_os_mode_combo)
    self.profile_os_mode_combo.setFixedWidth(SETTINGS_COMBO_OS_WIDTH)
    self.profile_os_mode_combo.setToolTip(
      "AdsPower profile OS for Create Profiles and Start Automation.\n"
      "Mixed: Android when result.csv shows mobile SERP page 1–2, or Windows page 1 "
      "with no mobile history for that keyword+site; otherwise Windows.\n"
      "Windows / Android: create only that device type."
    )
    start_delay_row[0].setToolTip(
      "Minimum idle time on Google after profile launch, before warm-up search."
    )
    start_delay_row[1].setToolTip(
      "Maximum idle time on Google after profile launch, before warm-up search."
    )
    internal_link_row[0].setToolTip(
      "Minimum internal link clicks on the target site during dwell (0 = scroll/select only)."
    )
    internal_link_row[1].setToolTip(
      "Maximum internal link clicks during dwell (e.g. 1–1 = always once, 1–2 = once or twice)."
    )
    warmup_count_row[0].setToolTip("Minimum number of warm-up Google searches per profile.")
    warmup_count_row[1].setToolTip("Maximum number of warm-up Google searches per profile.")

    session_card, session_body = make_settings_card(
      "Session & Dwell",
      "Launch timing, dwell duration, warm-up, and human-like delays.",
    )
    session_body.addWidget(make_range_row("Launch Interval", launch_row[0], launch_row[1]))
    session_body.addWidget(make_range_row("Start Delay", start_delay_row[0], start_delay_row[1]))
    session_body.addWidget(make_range_row("Dwell Time", dwell_row[0], dwell_row[1]))
    session_body.addWidget(
      make_range_row("Internal Links", internal_link_row[0], internal_link_row[1], unit=""),
    )
    session_body.addWidget(
      make_range_row("Warm-up Dwell", warmup_dwell_row[0], warmup_dwell_row[1]),
    )
    session_body.addWidget(
      make_range_row("Warm-up Queries", warmup_count_row[0], warmup_count_row[1], unit=""),
    )
    session_body.addWidget(make_range_row("Action Delay", action_row[0], action_row[1]))
    session_body.addStretch(1)

    automation_card, automation_body = make_settings_card(
      "Search & Automation",
      "SERP depth, retries, device OS, and traffic-saving options.",
    )
    session_card.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Expanding,
    )
    automation_card.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Expanding,
    )
    automation_body.addWidget(
      make_labeled_field_row("Max SERP Pages:", self.max_search_pages),
    )
    automation_body.addWidget(
      make_labeled_field_row("Max Retries / Profile:", self.max_keywords_per_profile),
    )
    automation_body.addWidget(
      make_labeled_field_row("Failure Rate Auto-Stop:", self.failure_rate_auto_stop_percent),
    )
    automation_body.addWidget(
      make_labeled_field_row("Auto-Stop Min Attempts:", self.failure_rate_auto_stop_min_attempts),
    )
    automation_body.addWidget(
      make_labeled_field_row("Profile OS Mode:", self.profile_os_mode_combo),
    )

    self.chk_skip_exhausted_pairs = ToggleSwitch("Skip not-found pairs")
    self.chk_skip_exhausted_pairs.setChecked(False)
    self.chk_skip_exhausted_pairs.setToolTip(
      "Off (default): if a keyword+site is not found after a full SERP scan, "
      "it can be assigned again in the next cycle.\n"
      "On: exclude that pair for the rest of this Start~Stop session."
    )
    automation_body.addWidget(
      make_labeled_toggle_stack("Session Pair Skip:", [self.chk_skip_exhausted_pairs]),
    )

    self.chk_ip_check_session = ToggleSwitch("At session start")
    self.chk_ip_check_session.setChecked(False)
    self.chk_ip_check_session.setToolTip(
      "Off (default): skip proxy IP lookup when a profile session begins (saves traffic).\n"
      "On: fetch egress IP via browser fetch before warm-up (retries up to 10s)."
    )
    self.chk_ip_check_keyword2 = ToggleSwitch("Before 2nd keyword")
    self.chk_ip_check_keyword2.setChecked(False)
    self.chk_ip_check_keyword2.setToolTip(
      "Off (default): do not compare IP again before the 2nd keyword.\n"
      "On: re-check IP before keyword 2 and stop the profile if it changed "
      "(requires session-start IP check to capture a baseline)."
    )
    automation_body.addWidget(
      make_labeled_toggle_stack("IP Check:", [
        self.chk_ip_check_session,
        self.chk_ip_check_keyword2,
      ]),
    )

    self.chk_resource_blocking = ToggleSwitch("")
    self.chk_resource_blocking.setChecked(True)
    self.chk_resource_blocking.setToolTip(
      "On (default): block images, video, fonts, Google ads, and FB/NAVER/DAUM/KAKAO "
      "tracker scripts on SERP and target sites (saves residential proxy traffic).\n"
      "Off: allow all resources — use with ISP or unlimited bandwidth proxies."
    )
    automation_body.addWidget(
      make_labeled_toggle_stack("Resource Block:", [self.chk_resource_blocking]),
    )
    automation_body.addStretch(1)

    targeting_panel = QWidget()
    targeting_panel.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Expanding,
    )
    targeting_row = QHBoxLayout(targeting_panel)
    targeting_row.setContentsMargins(0, 0, 0, 0)
    targeting_row.setSpacing(14)
    targeting_row.setAlignment(Qt.AlignmentFlag.AlignTop)
    targeting_row.addWidget(session_card, stretch=1)
    targeting_row.addWidget(automation_card, stretch=1)

    domains_card, domains_body = make_settings_card(
      "Target Domains (max 5)",
      "One domain per line — all are searched on each SERP page.",
    )
    domains_card.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Preferred,
    )
    self.target_domains_edit = QPlainTextEdit()
    self.target_domains_edit.setObjectName("settingsMultilineEdit")
    self.target_domains_edit.setPlaceholderText("mysite.com\nothersite.com")
    self.target_domains_edit.setToolTip(
      "One target domain per line (max 5). All are searched on each SERP page."
    )
    self.target_domains_edit.setMinimumHeight(108)
    self.target_domains_edit.setMaximumHeight(108)
    domains_body.addWidget(self.target_domains_edit)

    top_row_panel = QWidget()
    top_row_panel.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Maximum,
    )
    top_row = QHBoxLayout(top_row_panel)
    top_row.setContentsMargins(0, 0, 0, 0)
    top_row.setSpacing(14)
    top_row.setAlignment(Qt.AlignmentFlag.AlignTop)
    top_row.addWidget(api_card, stretch=2)
    top_row.addWidget(domains_card, stretch=1)

    left_col.addWidget(targeting_panel, stretch=1)

    self.btn_save_settings = QPushButton("Save Settings")
    self.btn_save_settings.setObjectName("btnSaveSettings")
    self.btn_save_settings.setFixedSize(180, 42)
    save_row = QHBoxLayout()
    save_row.setContentsMargins(0, 10, 0, 6)
    save_row.addStretch()
    save_row.addWidget(self.btn_save_settings)
    save_row.addStretch()
    left_col.addLayout(save_row)

    right_col = QVBoxLayout()
    right_col.setSpacing(6)
    right_col.setContentsMargins(0, 0, 0, 0)

    proxy_card, proxy_body = make_settings_card(
      "Proxy Setup",
      "Format: username:password@ip:port — one per line.",
    )
    self.proxies_edit = self._make_settings_multiline_edit(
      "myuser:mypass@123.45.67.89:10000",
    )
    proxy_body.addWidget(self.proxies_edit, stretch=1)
    right_col.addWidget(proxy_card, stretch=1)

    kw_card, kw_body = make_settings_card(
      "Keyword List",
      "Target search keywords — one per line.",
    )
    self.keywords_edit = self._make_settings_multiline_edit("One keyword per line")
    kw_body.addWidget(self.keywords_edit, stretch=1)
    right_col.addWidget(kw_card, stretch=1)

    warmup_card, warmup_body = make_settings_card(
      "Warm-up Query List",
      "Neutral queries used before target keyword searches.",
    )
    self.warmup_edit = self._make_settings_multiline_edit("One warm-up query per line")
    warmup_body.addWidget(self.warmup_edit, stretch=1)
    right_col.addWidget(warmup_card, stretch=1)

    left_widget = QWidget()
    left_widget.setLayout(left_col)
    left_widget.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Expanding,
    )
    right_widget = QWidget()
    right_widget.setLayout(right_col)
    right_widget.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Expanding,
    )
    outer.addWidget(left_widget, stretch=2)
    outer.addWidget(right_widget, stretch=1)

    content = QWidget()
    content_layout = QVBoxLayout(content)
    content_layout.setContentsMargins(0, 0, 0, 0)
    content_layout.setSpacing(10)
    content_layout.addWidget(top_row_panel, 0)
    content_layout.addLayout(outer, stretch=1)

    root.addWidget(content, stretch=1)
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

    traffic_box = QVBoxLayout()
    traffic_box.setSpacing(1)
    traffic_title = QLabel("Proxy Traffic")
    traffic_title.setObjectName("trafficTotalTitle")
    traffic_title.setToolTip(
      "CDP wire estimate (upload+download). Matches residential proxy billing more closely than body-only meters."
    )
    self.result_traffic_value = QLabel("0 B")
    self.result_traffic_value.setObjectName("trafficTotalValue")
    traffic_box.addWidget(traffic_title, alignment=Qt.AlignmentFlag.AlignRight)
    traffic_box.addWidget(self.result_traffic_value, alignment=Qt.AlignmentFlag.AlignRight)

    self.result_not_found_value = QLabel("0")
    self.result_not_found_value.setObjectName("trafficTotalValue")
    self.result_not_found_value.hide()

    stats_row.addStretch()
    stats_row.addLayout(total_box)
    stats_row.addLayout(traffic_box)

    header_row.addWidget(sessions_title, stretch=1)
    header_row.addLayout(stats_row, stretch=3)

    body_row = QHBoxLayout()
    body_row.setSpacing(12)

    sessions_panel = QWidget()
    sessions_panel_layout = QVBoxLayout(sessions_panel)
    sessions_panel_layout.setContentsMargins(0, 0, 0, 0)
    sessions_panel_layout.setSpacing(8)
    self.result_session_list = QListWidget()
    self.result_session_list.setObjectName("resultSessionList")
    sessions_panel_layout.addWidget(self.result_session_list, stretch=1)

    table_panel = QWidget()
    table_panel_layout = QVBoxLayout(table_panel)
    table_panel_layout.setContentsMargins(0, 0, 0, 0)
    table_panel_layout.setSpacing(8)
    self.result_domain_toolbar = QWidget()
    self.result_domain_toolbar_layout = QHBoxLayout(self.result_domain_toolbar)
    self.result_domain_toolbar_layout.setContentsMargins(0, 0, 0, 0)
    self.result_domain_toolbar_layout.setSpacing(8)
    self.result_domain_button_group = QButtonGroup(self.result_domain_toolbar)
    self.result_domain_button_group.setExclusive(True)
    self.btn_result_all_clicks = QPushButton("All Clicks")
    self.btn_result_all_clicks.setObjectName("btnKeywordClicks")
    self.btn_result_all_clicks.setCheckable(True)
    self.btn_result_all_clicks.setChecked(True)
    self.btn_result_all_clicks.setToolTip("Show every click row for this session")
    self.result_domain_button_group.addButton(self.btn_result_all_clicks)
    self.result_domain_toolbar_layout.addWidget(self.btn_result_all_clicks)
    self.result_click_domain_label = QLabel("Site:")
    self.result_click_domain_label.setObjectName("resultClickDomainLabel")
    self.result_domain_toolbar_layout.addWidget(self.result_click_domain_label)
    self.result_click_domain_combo = QComboBox()
    self.result_click_domain_combo.setObjectName("resultClickDomainCombo")
    self.result_click_domain_combo.setMinimumWidth(180)
    self.result_click_domain_combo.setToolTip("Filter All Clicks rows by target site")
    self.result_domain_toolbar_layout.addWidget(self.result_click_domain_combo)
    self.result_domain_toolbar_layout.addStretch()

    self.result_table = QTableWidget(0, 0)
    self.result_table.setObjectName("resultTable")
    self.result_table.setAlternatingRowColors(True)
    self.result_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    self.result_table.verticalHeader().setVisible(False)
    result_header = self.result_table.horizontalHeader()
    result_header.setStretchLastSection(True)

    table_panel_layout.addWidget(self.result_domain_toolbar)
    table_panel_layout.addWidget(self.result_table, stretch=1)

    body_row.addWidget(sessions_panel, stretch=1)
    body_row.addWidget(table_panel, stretch=3)

    footer_row = QHBoxLayout()
    footer_row.setSpacing(12)
    result_action_size = (110, 36)
    self.btn_delete_result_session = QPushButton("Delete")
    self.btn_delete_result_session.setObjectName("btnDeleteResult")
    self.btn_delete_result_session.setToolTip("Delete the selected session and its CSV file")
    self.btn_delete_result_session.setFixedSize(*result_action_size)
    self.btn_refresh_results = QPushButton("Refresh")
    self.btn_refresh_results.setObjectName("btnRefreshResults")
    self.btn_refresh_results.setFixedSize(*result_action_size)
    footer_row.addWidget(self.btn_delete_result_session)
    footer_row.addStretch()
    footer_row.addWidget(self.btn_refresh_results)

    content.addLayout(header_row)
    content.addLayout(body_row, stretch=1)
    content.addLayout(footer_row)
    layout.addLayout(content, stretch=1)

    self._refreshing_result_list = False
    self._result_session_files: list[Path] = []
    self._result_loaded_path: Path | None = None
    self._result_loaded_headers: list[str] = []
    self._result_loaded_rows: list[list[str]] = []
    self._result_loaded_meta: dict[str, str] = {}
    self._result_session_domains: list[str] = []
    self._result_table_filter: str = "all"
    self._result_click_domain_filter: str = "all"
    self._result_domain_buttons: list[QPushButton] = []
    self._result_list_loaded = False
    self._result_files_signature = ""
    self.result_session_list.currentItemChanged.connect(self._on_result_session_selected)
    self.btn_refresh_results.clicked.connect(lambda: self._refresh_result_file_list(force=True))
    self.btn_delete_result_session.clicked.connect(self._delete_selected_result_session)
    self.result_domain_button_group.buttonClicked.connect(self._on_result_domain_filter_clicked)
    self.result_click_domain_combo.currentIndexChanged.connect(self._on_result_click_domain_combo_changed)
    QTimer.singleShot(0, lambda: self._refresh_result_file_list(force=True))
    return tab

  @staticmethod
  def _compute_result_files_signature(files: list[Path]) -> str:
    parts: list[str] = []
    for path in files:
      try:
        stat = path.stat()
        parts.append(f"{path.name}:{stat.st_mtime_ns}:{stat.st_size}")
      except OSError:
        parts.append(path.name)
    return "|".join(parts)

  def _refresh_result_file_list(self, *, force: bool = False) -> None:
    if getattr(self, "_refreshing_result_list", False):
      return
    self._refreshing_result_list = True
    try:
      files = list_session_result_files(self._data_dir)
      signature = self._compute_result_files_signature(files)
      if (
        not force
        and self._result_list_loaded
        and signature == self._result_files_signature
        and self._result_loaded_path is not None
      ):
        return

      selected_path = ""
      current = self.result_session_list.currentItem()
      if current is not None:
        selected_path = str(current.data(Qt.ItemDataRole.UserRole) or "")

      self._result_session_files = files
      self._result_files_signature = signature

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
      self._result_list_loaded = True
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
    self.result_traffic_value.setText("0 B")
    self.result_not_found_value.setText("0")
    self._result_loaded_path = None
    self._result_loaded_headers = []
    self._result_loaded_rows = []
    self._result_loaded_meta = {}
    self._result_session_domains = []
    self._result_table_filter = "all"
    self._result_click_domain_filter = "all"
    self._rebuild_result_domain_buttons()
    self.result_table.clear()
    self.result_table.setRowCount(0)
    self.result_table.setColumnCount(0)

  def _load_result_session(self, path: Path) -> None:
    headers, rows, meta = read_session_click_file(path)
    self._result_loaded_path = path
    self._result_loaded_headers = headers
    self._result_loaded_rows = rows
    self._result_loaded_meta = meta
    self._result_session_domains = session_target_domains(headers, rows, meta)
    self._result_table_filter = "all"
    self._result_click_domain_filter = "all"
    successes, _failures = count_session_click_outcomes(path)
    self.result_total_clicks_value.setText(str(successes))

    traffic_raw = (meta.get("traffic_bytes") or "").strip()
    try:
      traffic_bytes = int(traffic_raw) if traffic_raw else 0
    except ValueError:
      traffic_bytes = 0
    active_path = (self._session_click_log_path or "").strip()
    if active_path and Path(active_path).resolve() == path.resolve():
      traffic_bytes = max(traffic_bytes, self._controller.get_session_traffic_total())
    self.result_traffic_value.setText(self._format_bytes(traffic_bytes))

    self.result_not_found_value.setText("0")
    self._rebuild_result_domain_buttons()
    self._render_result_table()

  def _rebuild_result_domain_buttons(self) -> None:
    self.result_domain_button_group.blockSignals(True)
    try:
      for button in list(self._result_domain_buttons):
        self.result_domain_button_group.removeButton(button)
        self.result_domain_toolbar_layout.removeWidget(button)
        button.deleteLater()
      self._result_domain_buttons.clear()

      self.btn_result_all_clicks.setChecked(self._result_table_filter == "all")
      for domain in self._result_session_domains:
        button = QPushButton(domain)
        button.setObjectName("btnKeywordClicks")
        button.setCheckable(True)
        button.setProperty("result_domain", domain)
        button.setToolTip(f"Keyword click totals for {domain}")
        if self._result_table_filter == domain:
          button.setChecked(True)
          self.btn_result_all_clicks.setChecked(False)
        self.result_domain_button_group.addButton(button)
        self.result_domain_toolbar_layout.insertWidget(
          self.result_domain_toolbar_layout.count() - 1,
          button,
        )
        self._result_domain_buttons.append(button)
      self._rebuild_result_click_domain_combo()
    finally:
      self.result_domain_button_group.blockSignals(False)

  def _rebuild_result_click_domain_combo(self) -> None:
    self.result_click_domain_combo.blockSignals(True)
    try:
      current = self._result_click_domain_filter
      self.result_click_domain_combo.clear()
      self.result_click_domain_combo.addItem("All", "all")
      for domain in self._result_session_domains:
        self.result_click_domain_combo.addItem(domain, domain)
      index = self.result_click_domain_combo.findData(current)
      if index < 0:
        index = 0
        self._result_click_domain_filter = "all"
      self.result_click_domain_combo.setCurrentIndex(index)
    finally:
      self.result_click_domain_combo.blockSignals(False)

  def _on_result_click_domain_combo_changed(self, _index: int) -> None:
    value = str(self.result_click_domain_combo.currentData() or "all").strip() or "all"
    self._result_click_domain_filter = value
    if self._result_table_filter == "all":
      self._render_result_table()

  def _on_result_domain_filter_clicked(self, button) -> None:
    if button is self.btn_result_all_clicks:
      self._result_table_filter = "all"
      self.result_click_domain_combo.setEnabled(True)
      self.result_click_domain_label.setEnabled(True)
    else:
      domain = str(button.property("result_domain") or button.text() or "").strip()
      self._result_table_filter = domain or "all"
      self.result_click_domain_combo.setEnabled(False)
      self.result_click_domain_label.setEnabled(False)
    self._render_result_table()

  def _delete_selected_result_session(self) -> None:
    current = self.result_session_list.currentItem()
    if current is None:
      self.append_log("[UI] No session selected to delete.")
      return
    path_text = str(current.data(Qt.ItemDataRole.UserRole) or "").strip()
    if not path_text:
      return
    path = Path(path_text)
    label = format_result_session_label(path)
    answer = QMessageBox.question(
      self,
      "Delete Session",
      f"Delete session '{label}' and remove its CSV file?\n\n{path.name}",
      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
      QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
      return
    active_path = (self._session_click_log_path or "").strip()
    if active_path and str(Path(active_path).resolve()) == str(path.resolve()):
      self.append_log("[UI] Cannot delete the active in-progress session log.")
      return
    try:
      if path.exists():
        path.unlink()
      self.append_log(f"[UI] Deleted session file: {path.name}")
    except OSError as exc:
      self.append_log(f"[UI] Failed to delete session file: {exc}")
      return
    self._refresh_result_file_list(force=True)

  def _render_result_table(self) -> None:
    if self._result_table_filter != "all":
      self._render_result_keyword_table(self._result_table_filter)
    else:
      self._render_result_detail_table()

  def _render_result_detail_table(self) -> None:
    headers = self._result_loaded_headers
    rows = self._result_loaded_rows
    if self._result_click_domain_filter != "all":
      rows = filter_click_rows_by_domain(
        headers,
        rows,
        self._result_click_domain_filter,
        meta=self._result_loaded_meta,
      )
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
    self.result_table.setUpdatesEnabled(False)
    self.result_table.blockSignals(True)
    try:
      self.result_table.setColumnCount(len(display_headers))
      self.result_table.setHorizontalHeaderLabels(display_headers)
      self.result_table.setRowCount(len(rows))
      for row_index, row in enumerate(rows):
        for col_index, header_name in enumerate(display_headers):
          value = row[col_index] if col_index < len(row) else ""
          self.result_table.setItem(row_index, col_index, QTableWidgetItem(str(value)))
      self._resize_result_table_columns(len(display_headers))
    finally:
      self.result_table.blockSignals(False)
      self.result_table.setUpdatesEnabled(True)

  def _render_result_keyword_table(self, domain: str) -> None:
    summaries = aggregate_keyword_clicks_for_domain(
      self._result_loaded_headers,
      self._result_loaded_rows,
      domain,
      meta=self._result_loaded_meta,
    )
    display_headers = ("keyword", "total_clicks", "windows", "mobile", "not_found")
    self.result_table.setColumnCount(len(display_headers))
    self.result_table.setHorizontalHeaderLabels(
      ["Keyword", "Total Clicks", "Windows", "Mobile", "Not Found"]
    )
    total_clicks = sum(summary["total"] for summary in summaries)
    total_windows = sum(summary["windows"] for summary in summaries)
    total_mobile = sum(summary["mobile"] for summary in summaries)
    total_not_found = sum(summary["not_found"] for summary in summaries)
    self.result_table.setRowCount(len(summaries) + 1)
    for row_index, summary in enumerate(summaries):
      values = (
        summary["keyword"],
        str(summary["total"]),
        str(summary["windows"]),
        str(summary["mobile"]),
        str(summary["not_found"]),
      )
      for col_index, value in enumerate(values):
        self.result_table.setItem(row_index, col_index, QTableWidgetItem(value))

    footer_row = len(summaries)
    footer_font = QFont()
    footer_font.setBold(True)
    footer_values = (
      "Total",
      str(total_clicks),
      str(total_windows),
      str(total_mobile),
      str(total_not_found),
    )
    for col_index, value in enumerate(footer_values):
      item = QTableWidgetItem(value)
      item.setFont(footer_font)
      self.result_table.setItem(footer_row, col_index, item)
    self._resize_result_table_columns(len(display_headers))

  def _resize_result_table_columns(self, column_count: int) -> None:
    header = self.result_table.horizontalHeader()
    header.setStretchLastSection(True)
    for col_index in range(max(0, column_count - 1)):
      header.setSectionResizeMode(col_index, QHeaderView.ResizeMode.ResizeToContents)

  def _on_main_tab_changed(self, index: int) -> None:
    if index != getattr(self, "_result_tab_index", -1):
      return
    if not self._result_list_loaded:
      self._refresh_result_file_list()
      return
    files = list_session_result_files(self._data_dir)
    if self._compute_result_files_signature(files) != self._result_files_signature:
      self._refresh_result_file_list(force=True)

  def _make_settings_multiline_edit(self, placeholder: str, *, min_height: int = 48) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setObjectName("settingsMultilineEdit")
    edit.setPlaceholderText(placeholder)
    edit.setMinimumHeight(min_height)
    edit.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Expanding,
    )
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
  def _wrap_min_max(spinboxes: tuple) -> QWidget:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    min_label = QLabel("Min")
    min_label.setObjectName("hintLabel")
    max_label = QLabel("Max")
    max_label.setObjectName("hintLabel")
    row.addWidget(min_label, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(spinboxes[0], 0, Qt.AlignmentFlag.AlignVCenter)
    row.addSpacing(6)
    row.addWidget(max_label, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(spinboxes[1], 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch()
    container = QWidget()
    container.setFixedHeight(32)
    container.setLayout(row)
    return container

  @staticmethod
  def _wrap_row(spinboxes: tuple) -> QWidget:
    return UiMainWindow._wrap_min_max(spinboxes)

  @staticmethod
  def _wrap_row_layout(layout: QHBoxLayout | QVBoxLayout) -> QWidget:
    container = QWidget()
    container.setLayout(layout)
    return container

  @staticmethod
  def _wrap_col(layout: QVBoxLayout) -> QWidget:
    return UiMainWindow._wrap_row_layout(layout)

  def _wire_signals(self) -> None:
    self.btn_create.clicked.connect(self.on_create_profiles)
    self.btn_refresh.clicked.connect(self.on_refresh_profiles)
    self.btn_start_auto.clicked.connect(self.on_start_automated)
    self.btn_stop_auto.clicked.connect(self.on_stop_automated)
    self.btn_save_settings.clicked.connect(self.on_save_settings)
    self.chk_select_all.stateChanged.connect(self._on_select_all_changed)
    self.btn_bulk_start.clicked.connect(self.on_bulk_start)
    self.btn_bulk_pause.clicked.connect(self.on_bulk_pause)
    self.btn_bulk_kill.clicked.connect(self.on_bulk_kill)
    self.btn_bulk_delete.clicked.connect(self.on_bulk_delete)
    self._controller.log.connect(
      lambda msg: self.append_log(msg, persist=False),
      Qt.ConnectionType.QueuedConnection,
    )
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
    self._controller.captcha_stat.connect(
      self._on_captcha_stat,
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

  def append_log(self, message: str, *, persist: bool = True) -> None:
    from utils.session_log import append_session_log
    from utils.user_log import format_user_log, format_user_log_html

    if persist:
      append_session_log(message)

    user_line = format_user_log(message)
    if not user_line:
      return
    time_stamp = datetime.now().strftime("%H:%M:%S")
    html_line = format_user_log_html(user_line, time_stamp)
    self.log_view.append(html_line)
    scrollbar = self.log_view.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

  def _warn_capsolver_key_missing(self) -> None:
    if self.capsolver_key.text().strip():
      return
    message = "CapSolver API 키가 없으므로 캡쳐가 나오면 프로필을 삭제합니다."
    from utils.session_log import append_session_log

    append_session_log(f"[UI] {message}")
    time_stamp = datetime.now().strftime("%H:%M:%S")
    self.log_view.append(
      f'<span style="color:#f87171">[{time_stamp}] {message}</span>'
    )
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

  @staticmethod
  def _normalize_profile_os_mode(value: str) -> str:
    normalized = (value or "mixed").strip().lower()
    if normalized in ("android", "android_only"):
      return "android_only"
    if normalized in ("windows", "windows_only", "win"):
      return "windows_only"
    return "mixed"

  def _set_profile_os_mode_combo(self, value: str) -> None:
    mode = self._normalize_profile_os_mode(value)
    index = self.profile_os_mode_combo.findData(mode)
    if index >= 0:
      self.profile_os_mode_combo.setCurrentIndex(index)
    else:
      self.profile_os_mode_combo.setCurrentIndex(0)

  def _profile_os_mode_from_ui(self) -> str:
    data = self.profile_os_mode_combo.currentData()
    return self._normalize_profile_os_mode(str(data or "mixed"))

  def _collect_settings_dict(self) -> dict:
    return {
      "capsolver_api_key": self.capsolver_key.text().strip(),
      "adspower_api_url": self.adspower_api_url.text().strip(),
      "adspower_api_key": self.adspower_api_key.text().strip(),
      "target_domains_text": self.target_domains_edit.toPlainText(),
      "target_domain": self._primary_target_domain_from_ui(),
      "launch_interval_min": self.launch_min.value(),
      "launch_interval_max": self.launch_max.value(),
      "session_start_delay_min": self.session_start_delay_min.value(),
      "session_start_delay_max": self.session_start_delay_max.value(),
      "dwell_min": self.dwell_min.value(),
      "dwell_max": self.dwell_max.value(),
      "internal_link_min": self.internal_link_min.value(),
      "internal_link_max": self.internal_link_max.value(),
      "warmup_dwell_min": self.warmup_dwell_min.value(),
      "warmup_dwell_max": self.warmup_dwell_max.value(),
      "warmup_count_min": self.warmup_count_min.value(),
      "warmup_count_max": self.warmup_count_max.value(),
      "action_delay_min": self.action_delay_min.value(),
      "action_delay_max": self.action_delay_max.value(),
      "max_search_pages": self.max_search_pages.value(),
      "max_keywords_per_profile": self.max_keywords_per_profile.value(),
      "failure_rate_auto_stop_percent": self.failure_rate_auto_stop_percent.value(),
      "failure_rate_auto_stop_min_attempts": self.failure_rate_auto_stop_min_attempts.value(),
      "ip_check_session_start": self.chk_ip_check_session.isChecked(),
      "ip_check_enabled": self.chk_ip_check_keyword2.isChecked(),
      "skip_exhausted_pairs_in_session": self.chk_skip_exhausted_pairs.isChecked(),
      "resource_blocking_enabled": self.chk_resource_blocking.isChecked(),
      "profile_count": self.profile_count_spin.value(),
      "profile_os_mode": self._profile_os_mode_from_ui(),
      "automation_threads": self.threads_spin.value(),
      "automation_cycles": self.cycles_spin.value(),
      "proxies_text": self.proxies_edit.toPlainText(),
      "keywords_text": self.keywords_edit.toPlainText(),
      "warmup_text": self.warmup_edit.toPlainText(),
    }

  def _apply_settings_dict(self, data: dict) -> None:
    self.capsolver_key.setText(data.get("capsolver_api_key", ""))
    if data.get("adspower_api_url"):
      self.adspower_api_url.setText(data["adspower_api_url"])
    self.adspower_api_key.setText(data.get("adspower_api_key", ""))
    legacy_target = data.get("target_domain", "").strip()
    domains_text = data.get("target_domains_text", "").strip()
    if not domains_text and legacy_target:
      domains_text = legacy_target
    self.target_domains_edit.setPlainText(domains_text)
    self.launch_min.setValue(int(data.get("launch_interval_min", self.launch_min.value())))
    self.launch_max.setValue(int(data.get("launch_interval_max", self.launch_max.value())))
    self.session_start_delay_min.setValue(
      int(data.get("session_start_delay_min", self.session_start_delay_min.value()))
    )
    self.session_start_delay_max.setValue(
      int(data.get("session_start_delay_max", self.session_start_delay_max.value()))
    )
    self.dwell_min.setValue(int(data.get("dwell_min", self.dwell_min.value())))
    self.dwell_max.setValue(int(data.get("dwell_max", self.dwell_max.value())))
    self.internal_link_min.setValue(
      int(data.get("internal_link_min", self.internal_link_min.value()))
    )
    self.internal_link_max.setValue(
      int(data.get("internal_link_max", self.internal_link_max.value()))
    )
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
    self.failure_rate_auto_stop_percent.setValue(
      int(data.get("failure_rate_auto_stop_percent", self.failure_rate_auto_stop_percent.value()))
    )
    self.failure_rate_auto_stop_min_attempts.setValue(
      int(
        data.get(
          "failure_rate_auto_stop_min_attempts",
          self.failure_rate_auto_stop_min_attempts.value(),
        )
      )
    )
    self.chk_ip_check_session.setChecked(bool(data.get("ip_check_session_start", False)))
    self.chk_ip_check_keyword2.setChecked(bool(data.get("ip_check_enabled", False)))
    self.chk_skip_exhausted_pairs.setChecked(
      bool(data.get("skip_exhausted_pairs_in_session", False))
    )
    self.chk_resource_blocking.setChecked(bool(data.get("resource_blocking_enabled", True)))
    self.profile_count_spin.setValue(int(data.get("profile_count", self.profile_count_spin.value())))
    self._set_profile_os_mode_combo(str(data.get("profile_os_mode", "mixed")))
    self.threads_spin.setValue(int(data.get("automation_threads", self.threads_spin.value())))
    self.cycles_spin.setValue(int(data.get("automation_cycles", self.cycles_spin.value())))
    self.proxies_edit.setPlainText(data.get("proxies_text", ""))
    self.keywords_edit.setPlainText(data.get("keywords_text", ""))
    self.warmup_edit.setPlainText(data.get("warmup_text", ""))

  def _load_saved_settings(self) -> None:
    data = load_settings()
    if not data:
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

  def _parse_target_domains_from_ui(self) -> list[str]:
    lines = [line.strip() for line in self.target_domains_edit.toPlainText().splitlines() if line.strip()]
    seen: set[str] = set()
    domains: list[str] = []
    for line in lines:
      key = line.lower().removeprefix("www.")
      if key in seen:
        continue
      seen.add(key)
      domains.append(line)
      if len(domains) >= 5:
        break
    return domains

  def _primary_target_domain_from_ui(self) -> str:
    domains = self._parse_target_domains_from_ui()
    return domains[0] if domains else ""

  def _build_config(self, require_lists: bool = True) -> BotConfig:
    target_domains = self._parse_target_domains_from_ui()
    if require_lists and not target_domains:
      raise ValueError("At least one target domain is required (max 5).")
    if len([line for line in self.target_domains_edit.toPlainText().splitlines() if line.strip()]) > 5:
      raise ValueError("At most 5 target domains are allowed.")

    keywords = [k.strip() for k in self.keywords_edit.toPlainText().splitlines() if k.strip()]
    if require_lists and not keywords:
      raise ValueError("At least one keyword is required.")

    warmup = [w.strip() for w in self.warmup_edit.toPlainText().splitlines() if w.strip()]
    if require_lists and not warmup:
      raise ValueError("At least one warm-up query is required.")

    if self.launch_min.value() > self.launch_max.value():
      raise ValueError("Launch interval min cannot exceed max.")
    if self.session_start_delay_min.value() > self.session_start_delay_max.value():
      raise ValueError("Session start delay min cannot exceed max.")
    if self.dwell_min.value() > self.dwell_max.value():
      raise ValueError("Dwell time min cannot exceed max.")
    if self.internal_link_min.value() > self.internal_link_max.value():
      raise ValueError("Internal link click min cannot exceed max.")
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
    if self.failure_rate_auto_stop_percent.value() < 0:
      raise ValueError("Failure rate auto-stop percent cannot be negative.")
    if self.failure_rate_auto_stop_min_attempts.value() < 1:
      raise ValueError("Failure rate auto-stop min attempts must be at least 1.")

    proxies: list[tuple[str, int, str, str]] = []
    if self.proxies_edit.toPlainText().strip():
      proxies = self._parse_proxies()

    return BotConfig(
      capsolver_api_key=self.capsolver_key.text().strip(),
      adspower_api_url=self.adspower_api_url.text().strip(),
      adspower_api_key=self.adspower_api_key.text().strip(),
      target_domain=target_domains[0] if target_domains else "",
      target_domains=target_domains,
      proxies=proxies,
      keywords=keywords,
      warmup_queries=warmup,
      launch_interval_min=self.launch_min.value(),
      launch_interval_max=self.launch_max.value(),
      session_start_delay_min=self.session_start_delay_min.value(),
      session_start_delay_max=self.session_start_delay_max.value(),
      dwell_min=self.dwell_min.value(),
      dwell_max=self.dwell_max.value(),
      internal_link_min=self.internal_link_min.value(),
      internal_link_max=self.internal_link_max.value(),
      warmup_dwell_min=self.warmup_dwell_min.value(),
      warmup_dwell_max=self.warmup_dwell_max.value(),
      warmup_count_min=self.warmup_count_min.value(),
      warmup_count_max=self.warmup_count_max.value(),
      action_delay_min=self.action_delay_min.value(),
      action_delay_max=self.action_delay_max.value(),
      max_search_pages=self.max_search_pages.value(),
      max_keywords_per_profile=self.max_keywords_per_profile.value(),
      failure_rate_auto_stop_percent=self.failure_rate_auto_stop_percent.value(),
      failure_rate_auto_stop_min_attempts=self.failure_rate_auto_stop_min_attempts.value(),
      ip_check_session_start=self.chk_ip_check_session.isChecked(),
      ip_check_enabled=self.chk_ip_check_keyword2.isChecked(),
      skip_exhausted_pairs_in_session=self.chk_skip_exhausted_pairs.isChecked(),
      resource_blocking_enabled=self.chk_resource_blocking.isChecked(),
      automation_threads=self.threads_spin.value(),
      automation_cycles=self.cycles_spin.value(),
      profile_count=self.profile_count_spin.value(),
      profile_os_mode=self._profile_os_mode_from_ui(),
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
      time.sleep(4.0)
      last_exc: Exception | None = None
      for attempt in range(1, 5):
        try:
          manager.delete_profiles(profile_ids)
          last_exc = None
          break
        except Exception as exc:
          last_exc = exc
          message = str(exc).lower()
          if attempt < 4 and (
            "being used by other users" in message
            or "cannot be deleted" in message
          ):
            wait_seconds = min(30.0, 4.0 * attempt)
            self.append_log(
              f"[UI] Delete retry {attempt}/4 for {len(profile_ids)} profile(s) "
              f"in {wait_seconds:.0f}s..."
            )
            time.sleep(wait_seconds)
            continue
          raise
      if last_exc is not None:
        raise last_exc
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
    path = new_session_click_log_path(self._data_dir)
    config.session_click_log_path = str(path)
    self._session_click_log_path = str(path)
    self._failure_rate_auto_stop_triggered = False
    SessionClickCsvLogger(path, target_domains=config.get_target_domains())
    self.append_log(f"[UI] Click log file: {path}")
    if hasattr(self, "result_session_list"):
      QTimer.singleShot(0, lambda: self._refresh_result_file_list(force=True))
    return str(path)

  def _reset_overall_clicks(self) -> None:
    self._overall_clicks_session = 0
    self.overall_clicks_value_label.setText("0 / 0")

  def _refresh_overall_clicks_kpi(self) -> None:
    path = (self._session_click_log_path or "").strip()
    if not path:
      self.overall_clicks_value_label.setText("0 / 0")
      return
    successes, failures = count_session_click_outcomes(path)
    self._overall_clicks_session = successes
    self.overall_clicks_value_label.setText(f"{successes} / {failures}")

  def _refresh_overall_clicks(self) -> None:
    self._refresh_overall_clicks_kpi()

  def _reset_captcha_stats(self) -> None:
    self._session_captcha_auto = 0
    self._session_captcha_total = 0
    if hasattr(self, "captcha_occurs_value_label"):
      self.captcha_occurs_value_label.setText("0 / 0")

  def _refresh_captcha_kpi(self) -> None:
    if hasattr(self, "captcha_occurs_value_label"):
      self.captcha_occurs_value_label.setText(
        f"{self._session_captcha_auto} / {self._session_captcha_total}"
      )

  def _on_captcha_stat(self, event: str) -> None:
    if event == "detected":
      self._session_captcha_total += 1
    elif event == "auto_solved":
      self._session_captcha_auto += 1
    else:
      return
    self._refresh_captcha_kpi()

  def _reset_session_traffic(self) -> None:
    self._session_traffic_total = 0
    self._session_target_traffic = 0
    self._session_other_traffic = 0
    self.proxy_traffic_total_label.setText(self._format_traffic_total_display())
    self._controller.reset_session_traffic()
    self._profile_traffic_totals.clear()

  def _reset_session_log(self) -> None:
    """Truncate on-disk session logs and clear the live log panel for a fresh automation run."""
    self._data_dir.mkdir(parents=True, exist_ok=True)
    for name in ("session.log", "traffic_sessions.jsonl"):
      try:
        (self._data_dir / name).write_text("", encoding="utf-8")
      except OSError as exc:
        self.log_view.append(f'<span style="color:#fca5a5">[UI] Could not reset {name}: {exc}</span>')
        continue
    self.log_view.clear()

  def _finalize_session_click_log(self) -> None:
    path = (self._session_click_log_path or "").strip()
    if not path:
      return
    traffic_bytes = self._controller.get_session_traffic_total()
    try:
      SessionClickCsvLogger.finalize_session(path, traffic_bytes=traffic_bytes)
      self.append_log(
        f"[UI] Session log finalized: traffic={self._format_bytes(traffic_bytes)}"
      )
    except OSError as exc:
      self.append_log(f"[UI] Failed to finalize session log: {exc}")
    active_path = Path(path)
    if (
      self._result_loaded_path is not None
      and self._result_loaded_path.resolve() == active_path.resolve()
    ):
      self._load_result_session(active_path)
    elif self._current_page_index() == getattr(self, "_result_tab_index", -1):
      self._refresh_result_file_list(force=True)

  def _on_result_click_logged(self) -> None:
    if self._current_page_index() != getattr(self, "_result_tab_index", -1):
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
    self._reset_captcha_stats()
    self._reset_session_traffic()
    self._warn_capsolver_key_missing()
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
    if running:
      self._start_elapsed_timer()
    else:
      self._stop_elapsed_timer()

  @staticmethod
  def _format_elapsed_time(seconds: int) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"

  def _refresh_elapsed_time_label(self) -> None:
    if hasattr(self, "elapsed_time_value_label"):
      self.elapsed_time_value_label.setText(self._format_elapsed_time(self._elapsed_seconds))

  def _reset_elapsed_time(self) -> None:
    self._elapsed_seconds = 0
    self._refresh_elapsed_time_label()

  def _start_elapsed_timer(self) -> None:
    if not self._elapsed_timer.isActive():
      self._elapsed_timer.start()

  def _stop_elapsed_timer(self) -> None:
    if self._elapsed_timer.isActive():
      self._elapsed_timer.stop()

  def _tick_elapsed_time(self) -> None:
    if not self._global_running:
      return
    self._elapsed_seconds += 1
    self._refresh_elapsed_time_label()

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
  def _format_bytes(total_bytes: int, *, integer_units: bool = False) -> str:
    value = max(0, int(total_bytes))
    if value < 1024:
      return f"{value} B"
    kb = value / 1024.0
    if kb < 1024.0:
      if integer_units:
        return f"{int(round(kb))} KB"
      return f"{kb:.2f} KB"
    mb = value / (1024.0 * 1024.0)
    if integer_units:
      return f"{int(round(mb))} MB"
    return f"{mb:.2f} MB"

  def _format_traffic_total_display(self) -> str:
    total = self._format_bytes(self._session_traffic_total, integer_units=True)
    target = self._format_bytes(self._session_target_traffic, integer_units=True)
    other = self._format_bytes(self._session_other_traffic, integer_units=True)
    return f"{total} ({target} / {other})"

  def _on_proxy_traffic_update(self, proxy_key: str, total_bytes: int) -> None:
    _ = proxy_key
    _ = total_bytes

  def _on_profile_traffic_update(
    self,
    profile_id: str,
    total_bytes: int,
    total_all_bytes: int,
    target_all_bytes: int,
    other_all_bytes: int,
  ) -> None:
    self._profile_traffic_totals[profile_id] = int(total_bytes)
    self._session_traffic_total = max(self._session_traffic_total, int(total_all_bytes))
    self._session_target_traffic = int(target_all_bytes)
    self._session_other_traffic = int(other_all_bytes)
    self.proxy_traffic_total_label.setText(self._format_traffic_total_display())
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
    _ = outcome
    self._refresh_overall_clicks_kpi()
    self._check_auto_stop_on_failure_rate()
    if self._current_page_index() != getattr(self, "_result_tab_index", -1):
      return
    current = self.result_session_list.currentItem()
    active_path = (self._session_click_log_path or "").strip()
    if current and active_path and str(current.data(Qt.ItemDataRole.UserRole) or "") == str(Path(active_path).resolve()):
      self._load_result_session(Path(active_path))

  def _check_auto_stop_on_failure_rate(self) -> None:
    if not self._global_running or self._failure_rate_auto_stop_triggered:
      return
    threshold_percent = int(self.failure_rate_auto_stop_percent.value())
    if threshold_percent <= 0:
      return
    min_attempts = int(self.failure_rate_auto_stop_min_attempts.value())
    path = (self._session_click_log_path or "").strip()
    if not path:
      return
    successes, failures = count_session_click_outcomes(path)
    total = successes + failures
    if not should_auto_stop_on_failure_rate(
      successes,
      failures,
      threshold_percent=threshold_percent,
      min_attempts=min_attempts,
    ):
      return
    failure_rate = failures / total if total else 0.0
    self._failure_rate_auto_stop_triggered = True
    self.append_log(
      f"[UI] Session failure rate {failures}/{total} ({failure_rate * 100:.0f}%) "
      f"exceeds {threshold_percent}% with {total} attempts — stopping automation."
    )
    self.on_stop_automated()

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
        profile_os_mode=config.profile_os_mode,
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
    self._reset_elapsed_time()
    self._reset_session_log()
    self._begin_session_click_log(config)
    self._reset_overall_clicks()
    self._reset_captcha_stats()
    self._reset_session_traffic()
    self._warn_capsolver_key_missing()
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
        f"launch interval {config.launch_interval_min}-{config.launch_interval_max}s, "
        f"session start delay {config.session_start_delay_min}-{config.session_start_delay_max}s, "
        f"profile OS mode={config.profile_os_mode}."
      )
    else:
      self.append_log("[UI] Global bot is already running.")

  def on_stop_automated(self) -> None:
    self._stop_elapsed_timer()
    self._controller.stop_global()

  def _on_global_finished(self) -> None:
    self._finalize_session_click_log()
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
    self._reset_captcha_stats()
    self._reset_session_traffic()
    self._warn_capsolver_key_missing()
    self._controller.start_profile_manual(profile_id, config)

  def _on_row_pause(self, profile_id: str) -> None:
    self._controller.pause_profile(profile_id)

  def _on_row_kill(self, profile_id: str) -> None:
    self._controller.force_terminate(profile_id, self._minimal_config())

  def _on_row_delete(self, profile_id: str) -> None:
    self._delete_profiles([profile_id])

  def closeEvent(self, event) -> None:
    super().closeEvent(event)
