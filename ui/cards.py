"""Dashboard KPI card widgets."""

from __future__ import annotations

from PyQt6.QtWidgets import QFrame, QLabel, QVBoxLayout


def make_kpi_card(title: str, *, tooltip: str = "") -> tuple[QFrame, QLabel]:
  """Return a styled KPI card and its value label."""
  card = QFrame()
  card.setObjectName("kpiCard")
  layout = QVBoxLayout(card)
  layout.setContentsMargins(20, 18, 20, 18)
  layout.setSpacing(4)

  title_label = QLabel(title)
  title_label.setObjectName("kpiTitle")
  if tooltip:
    title_label.setToolTip(tooltip)
    card.setToolTip(tooltip)

  value_label = QLabel("0")
  value_label.setObjectName("kpiValue")

  layout.addWidget(title_label)
  layout.addWidget(value_label)
  return card, value_label
