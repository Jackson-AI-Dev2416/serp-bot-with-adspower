"""Left navigation sidebar for the main application shell."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QFrame, QLabel, QPushButton, QVBoxLayout


class AppSidebar(QFrame):
  """Fixed-width sidebar with exclusive page navigation buttons."""

  page_selected = pyqtSignal(int)

  def __init__(self, parent=None) -> None:
    super().__init__(parent)
    self.setObjectName("appSidebar")
    self.setFixedWidth(220)

    layout = QVBoxLayout(self)
    layout.setContentsMargins(16, 24, 16, 24)
    layout.setSpacing(6)

    logo = QLabel("SERP\nAutomation")
    logo.setObjectName("sidebarLogo")
    logo.setWordWrap(True)
    layout.addWidget(logo)

    subtitle = QLabel("AdsPower · Playwright")
    subtitle.setObjectName("sidebarSubtitle")
    layout.addWidget(subtitle)
    layout.addSpacing(20)

    self._group = QButtonGroup(self)
    self._group.setExclusive(True)
    self._buttons: list[QPushButton] = []
    for index, label in enumerate(("Dashboard", "Settings", "Result")):
      button = QPushButton(label)
      button.setObjectName("sidebarNavBtn")
      button.setCheckable(True)
      button.setCursor(Qt.CursorShape.PointingHandCursor)
      button.clicked.connect(lambda _checked=False, idx=index: self._on_nav(idx))
      self._group.addButton(button, index)
      self._buttons.append(button)
      layout.addWidget(button)

    self._buttons[0].setChecked(True)
    layout.addStretch()

  def set_current_index(self, index: int) -> None:
    if 0 <= index < len(self._buttons):
      self._buttons[index].setChecked(True)

  def _on_nav(self, index: int) -> None:
    self.set_current_index(index)
    self.page_selected.emit(index)
