import os
import sys

from utils.app_paths import app_base_dir, ensure_data_dir

os.chdir(app_base_dir())
ensure_data_dir()

from PyQt6.QtWidgets import QApplication

from ui.main_window import UiMainWindow


def main() -> None:
  app = QApplication(sys.argv)
  app.setApplicationName("SERP Bot")
  window = UiMainWindow()
  window.show()
  sys.exit(app.exec())


if __name__ == "__main__":
  main()
