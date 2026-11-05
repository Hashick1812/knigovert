"""Пути к встроенным файлам — и при запуске из исходников, и из собранного .exe."""

from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    PACKAGE_DIR = Path(getattr(sys, "_MEIPASS")) / "knigovert"
else:
    PACKAGE_DIR = Path(__file__).resolve().parent

ASSETS_DIR = PACKAGE_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
WEB_DIR = PACKAGE_DIR / "web"
