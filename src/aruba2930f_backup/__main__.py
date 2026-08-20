"""Executable entry point for the packaged Windows application."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence

from . import __version__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="Aruba2930FConfigBackup",
        description="Aruba 2930F running-config backup GUI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def _smoke_test() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from .developer_inspector import DeveloperInspectorController
    from .gui import MainWindow

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    if not isinstance(app, QApplication):
        raise RuntimeError("The existing Qt application is not a QApplication.")
    developer_inspector = DeveloperInspectorController(app, f"v{__version__}", app)
    window = MainWindow(developer_inspector=developer_inspector)
    try:
        window.show()
        app.processEvents()
        window.close()
    finally:
        developer_inspector.close()
    print(json.dumps({"application": "Aruba2930FConfigBackup", "version": __version__, "ok": True}))
    return 0


def _show_fatal_diagnostic(code: str) -> int:
    message = (
        f"프로그램을 시작하지 못했습니다.\n\n진단 코드: {code}\n\n이 코드만 Codex에 전달하세요."
    )
    print(message, file=sys.stderr)
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        app = QApplication.instance() or QApplication([])
        QMessageBox.critical(None, "Aruba 2930F 백업 오류", message)
        app.processEvents()
    except Exception:
        if sys.platform == "win32":
            try:
                import ctypes

                vars(ctypes)["windll"].user32.MessageBoxW(
                    None,
                    message,
                    "Aruba 2930F 백업 오류",
                    0x10,
                )
            except Exception:
                pass
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.smoke_test:
            return _smoke_test()

        from .gui import run_gui

        return run_gui()
    except Exception as exc:
        from .diagnostics import diagnostic_code_for_exception

        return _show_fatal_diagnostic(diagnostic_code_for_exception(exc))


if __name__ == "__main__":
    sys.exit(main())
