import sys
from pathlib import Path

from utils.cursor_sdk_windows_patch import apply_cursor_sdk_windows_patch

apply_cursor_sdk_windows_patch()

from PyQt6.QtWidgets import QApplication

from ui.main_window import UiMainWindow


def main() -> None:
  Path("data").mkdir(exist_ok=True)
  app = QApplication(sys.argv)
  app.setApplicationName("SERP Bot")
  window = UiMainWindow()
  window.show()
  sys.exit(app.exec())


if __name__ == "__main__":
  main()
