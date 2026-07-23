"""Reusable widgets for the Settings tab."""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
  QButtonGroup,
  QFrame,
  QGraphicsDropShadowEffect,
  QHBoxLayout,
  QLabel,
  QLineEdit,
  QPushButton,
  QSizePolicy,
  QVBoxLayout,
  QWidget,
)

SETTINGS_ROW_HEIGHT = 42
SETTINGS_BODY_SPACING = 18
SETTINGS_SPINBOX_WIDTH = 152
SETTINGS_COMBO_OS_WIDTH = int(SETTINGS_SPINBOX_WIDTH * 2.5)
SETTINGS_TOGGLE_STACK_SPACING = 10


class OnOffSwitch(QWidget):
  """Caption + explicit OFF / ON segmented control."""

  toggled = pyqtSignal(bool)

  def __init__(self, caption: str = "", parent: QWidget | None = None) -> None:
    super().__init__(parent)
    self._checked = False
    self.setCursor(Qt.CursorShape.PointingHandCursor)

    row = QHBoxLayout(self)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)

    if caption:
      caption_label = QLabel(caption)
      caption_label.setObjectName("settingsToggleCaption")
      row.addWidget(caption_label, 0, Qt.AlignmentFlag.AlignVCenter)

    track = QFrame()
    track.setObjectName("onOffSwitchTrack")
    track_layout = QHBoxLayout(track)
    track_layout.setContentsMargins(3, 3, 3, 3)
    track_layout.setSpacing(0)

    self._btn_off = QPushButton("OFF")
    self._btn_off.setObjectName("btnToggleOff")
    self._btn_on = QPushButton("ON")
    self._btn_on.setObjectName("btnToggleOn")
    for button in (self._btn_off, self._btn_on):
      button.setCheckable(True)
      button.setCursor(Qt.CursorShape.PointingHandCursor)
      button.setFixedHeight(24)
      button.setFixedWidth(40)

    self._group = QButtonGroup(self)
    self._group.setExclusive(True)
    self._group.addButton(self._btn_off, 0)
    self._group.addButton(self._btn_on, 1)
    self._group.idClicked.connect(self._on_segment_clicked)

    track_layout.addWidget(self._btn_off)
    track_layout.addWidget(self._btn_on)
    row.addWidget(track, 0, Qt.AlignmentFlag.AlignVCenter)
    self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    self.setChecked(False)

  def isChecked(self) -> bool:
    return self._checked

  def setChecked(self, checked: bool) -> None:
    checked = bool(checked)
    if self._checked == checked:
      self._sync_buttons()
      return
    self._checked = checked
    self._sync_buttons()

  def _sync_buttons(self) -> None:
    self._btn_off.setChecked(not self._checked)
    self._btn_on.setChecked(self._checked)

  def _on_segment_clicked(self, button_id: int) -> None:
    new_value = button_id == 1
    if self._checked == new_value:
      return
    self._checked = new_value
    self._sync_buttons()
    self.toggled.emit(self._checked)


ToggleSwitch = OnOffSwitch


class PasswordField(QWidget):
  """Full-width API-style field with show/hide toggle."""

  def __init__(
    self,
    line_edit: QLineEdit,
    parent: QWidget | None = None,
  ) -> None:
    super().__init__(parent)
    self._edit = line_edit
    self._edit.setEchoMode(QLineEdit.EchoMode.Password)
    self.setObjectName("passwordFieldWrap")
    self.setFixedHeight(36)
    self.setSizePolicy(
      QSizePolicy.Policy.Expanding,
      QSizePolicy.Policy.Fixed,
    )

    row = QHBoxLayout(self)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    row.addWidget(self._edit, stretch=1)

    self._toggle = QPushButton("Show")
    self._toggle.setObjectName("btnPasswordToggle")
    self._toggle.setCheckable(True)
    self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
    self._toggle.setFixedWidth(52)
    self._toggle.setFixedHeight(34)
    self._toggle.toggled.connect(self._on_toggle)
    row.addWidget(self._toggle)

  def line_edit(self) -> QLineEdit:
    return self._edit

  def _on_toggle(self, visible: bool) -> None:
    if visible:
      self._edit.setEchoMode(QLineEdit.EchoMode.Normal)
      self._toggle.setText("Hide")
    else:
      self._edit.setEchoMode(QLineEdit.EchoMode.Password)
      self._toggle.setText("Show")


def make_settings_card(title: str, subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
  card = QFrame()
  card.setObjectName("settingsCard")
  shadow = QGraphicsDropShadowEffect(card)
  shadow.setBlurRadius(16)
  shadow.setOffset(0, 3)
  shadow.setColor(QColor(0, 0, 0, 70))
  card.setGraphicsEffect(shadow)
  card_layout = QVBoxLayout(card)
  card_layout.setContentsMargins(14, 10, 14, 10)
  card_layout.setSpacing(4)

  header = QHBoxLayout()
  header.setContentsMargins(0, 0, 0, 0)
  header.setSpacing(8)
  title_label = QLabel(title)
  title_label.setObjectName("settingsCardTitle")
  header.addWidget(title_label, 0, Qt.AlignmentFlag.AlignVCenter)
  if subtitle:
    subtitle_label = QLabel(subtitle)
    subtitle_label.setObjectName("settingsCardSubtitle")
    subtitle_label.setWordWrap(True)
    subtitle_label.setContentsMargins(0, 5, 0, 0)
    header.addWidget(subtitle_label, 1, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
  card_layout.addLayout(header)

  card_layout.addSpacing(4)
  body = QVBoxLayout()
  body.setSpacing(SETTINGS_BODY_SPACING)
  body.setContentsMargins(0, 0, 0, 0)
  card_layout.addLayout(body, stretch=1)
  card.setSizePolicy(
    QSizePolicy.Policy.Expanding,
    QSizePolicy.Policy.Expanding,
  )
  return card, body


def apply_spinbox_width(spinbox: QWidget) -> None:
  spinbox.setFixedWidth(SETTINGS_SPINBOX_WIDTH)
  spinbox.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)


def make_range_row(
  label: str,
  min_widget: QWidget,
  max_widget: QWidget,
  *,
  unit: str = "sec",
) -> QWidget:
  """Label — Min — ~ — Max — unit on one balanced row."""
  apply_spinbox_width(min_widget)
  apply_spinbox_width(max_widget)
  row_widget = QWidget()
  row = QHBoxLayout(row_widget)
  row.setContentsMargins(0, 0, 0, 0)
  row.setSpacing(10)

  label_widget = QLabel(label)
  label_widget.setObjectName("settingsRangeLabel")
  label_widget.setMinimumWidth(148)
  label_widget.setSizePolicy(
    QSizePolicy.Policy.Fixed,
    QSizePolicy.Policy.Preferred,
  )
  row.addWidget(label_widget, 0, Qt.AlignmentFlag.AlignVCenter)

  min_caption = QLabel("Min")
  min_caption.setObjectName("settingsRangeCaption")
  row.addWidget(min_caption, 0, Qt.AlignmentFlag.AlignVCenter)
  row.addWidget(min_widget, 0, Qt.AlignmentFlag.AlignVCenter)

  sep = QLabel("~")
  sep.setObjectName("settingsRangeSep")
  sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
  sep.setFixedWidth(16)
  row.addWidget(sep, 0, Qt.AlignmentFlag.AlignVCenter)

  max_caption = QLabel("Max")
  max_caption.setObjectName("settingsRangeCaption")
  row.addWidget(max_caption, 0, Qt.AlignmentFlag.AlignVCenter)
  row.addWidget(max_widget, 0, Qt.AlignmentFlag.AlignVCenter)

  if unit:
    unit_label = QLabel(unit)
    unit_label.setObjectName("settingsRangeUnit")
    row.addWidget(unit_label, 0, Qt.AlignmentFlag.AlignVCenter)

  row.addStretch()
  row_widget.setFixedHeight(SETTINGS_ROW_HEIGHT)
  return row_widget


def make_labeled_field_row(
  label: str,
  field: QWidget,
  *,
  full_width: bool = False,
) -> QWidget:
  row_widget = QWidget()
  row = QHBoxLayout(row_widget)
  row.setContentsMargins(0, 0, 0, 0)
  row.setSpacing(16)
  label_widget = QLabel(label)
  label_widget.setObjectName("settingsFormLabel")
  label_widget.setMinimumWidth(148)
  label_widget.setSizePolicy(
    QSizePolicy.Policy.Fixed,
    QSizePolicy.Policy.Preferred,
  )
  row.addWidget(label_widget, 0, Qt.AlignmentFlag.AlignVCenter)
  if full_width:
    row.addWidget(field, 1, Qt.AlignmentFlag.AlignVCenter)
  else:
    row.addWidget(field, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch()
  row_widget.setFixedHeight(SETTINGS_ROW_HEIGHT)
  return row_widget


def make_labeled_toggle_stack(label: str, toggles: list[OnOffSwitch]) -> QWidget:
  """Label on the left; toggles stacked vertically and left-aligned."""
  row_widget = QWidget()
  outer = QHBoxLayout(row_widget)
  outer.setContentsMargins(0, 0, 0, 0)
  outer.setSpacing(16)

  label_widget = QLabel(label)
  label_widget.setObjectName("settingsFormLabel")
  label_widget.setMinimumWidth(148)
  label_widget.setAlignment(
    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
  )
  label_widget.setContentsMargins(0, 4, 0, 0)

  stack = QVBoxLayout()
  stack.setContentsMargins(0, 0, 0, 0)
  stack.setSpacing(SETTINGS_TOGGLE_STACK_SPACING)
  for toggle in toggles:
    stack.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)

  outer.addWidget(label_widget, 0, Qt.AlignmentFlag.AlignTop)
  outer.addLayout(stack, 0)
  outer.addStretch()
  return row_widget
