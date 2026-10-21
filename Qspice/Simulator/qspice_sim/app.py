"""Application entry point: discover models, create the main window."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PyQt6 import QtWidgets

from . import model_loader

SIMULATOR_DIR = Path(__file__).resolve().parents[1]
QSPICE_DIR = SIMULATOR_DIR.parent


def model_search_roots(extra: list[str] | None = None) -> list[Path]:
    roots = [QSPICE_DIR]
    for entry in (os.environ.get("QSPICE_MODEL_PATH", "").split(os.pathsep) + list(extra or [])):
        if entry.strip():
            roots.append(Path(entry.strip()))
    return roots


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qspice Simulator - GUI for QSPICE circuit models")
    ap.add_argument("--models", action="append", default=[], metavar="DIR",
                    help="extra folder whose sub-folders are models (each with simulator_model.json); may be repeated")
    ap.add_argument("--model", metavar="NAME", help="model folder name to open first")
    ap.add_argument("--qspice", metavar="EXE", help="path to QSPICE64.exe")
    args = ap.parse_args(argv)

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    models = model_loader.discover(model_search_roots(args.models))
    if args.model:
        models.sort(key=lambda m: m.id != args.model)
    from .gui.main_window import MainWindow  # imported late so that --help works without a display

    if not models:
        QtWidgets.QMessageBox.critical(None, "Qspice Simulator",
                                       f"No models found.\n\nA model is a folder with simulator_model.json inside {QSPICE_DIR} "
                                       "or in a folder given with --models / QSPICE_MODEL_PATH.")
        return 1
    win = MainWindow(models, args.qspice)
    win.show()
    return app.exec()
