"""README용 문서 화면을 실제 PySide6 MainWindow에서 생성합니다.

실제 네트워크에 접속하지 않으며 RFC 5737 문서용 IP와 가상 장비명만 사용합니다.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidgetItem

from aruba2930f_backup.gui import MainWindow


def _prepare_window() -> tuple[QApplication, MainWindow]:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Aruba 2930F 설정 백업")
    app.setStyle("Fusion")

    window = MainWindow(service=None)
    window.resize(1040, 760)
    window.ip_input.setPlainText("192.0.2.10\n192.0.2.11\n192.0.2.12")
    window.port_input.setValue(22)
    window.username_input.setText("netops-demo")
    window.password_input.setText("documentation-only")
    window.enable_password_input.clear()
    window.concurrency_input.setValue(10)
    window.output_input.setText(r"C:\NetworkBackup\Aruba2930F")
    window.show()
    app.processEvents()
    return app, window


def _save_main_window(output: Path) -> None:
    app, window = _prepare_window()
    window.status_label.setText("대기 중 — 문서용 가상 데이터")
    window.progress_bar.setValue(0)
    app.processEvents()
    if not window.grab().save(str(output / "main-window.png"), "PNG"):
        raise RuntimeError("메인 화면 PNG 저장에 실패했습니다.")
    window.close()
    app.processEvents()


def _save_result_example(output: Path) -> None:
    app, window = _prepare_window()
    rows = (
        ("192.0.2.10", "LAB-2930F-01", "Aruba 2930F / JL255A", "성공", "지문 1/4 · 백업 1/4", ""),
        ("192.0.2.11", "LAB-2930F-02", "Aruba 2930F / JL256A", "성공", "지문 1/4 · 백업 1/4", ""),
        ("192.0.2.12", "LAB-2930F-03", "Aruba 2930F", "성공", "지문 1/4 · 백업 2/4", ""),
    )
    window.result_table.setRowCount(len(rows))
    for row_index, values in enumerate(rows):
        for column_index, value in enumerate(values):
            window.result_table.setItem(row_index, column_index, QTableWidgetItem(value))
    window.status_label.setText("완료 — 성공 3대 / 실패 0대 · 문서용 가상 결과")
    window.progress_bar.setValue(100)
    window.open_result_button.setEnabled(True)
    app.processEvents()
    if not window.grab().save(str(output / "result-example.png"), "PNG"):
        raise RuntimeError("결과 화면 PNG 저장에 실패했습니다.")
    window.close()
    app.processEvents()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    _save_main_window(output)
    _save_result_example(output)

    expected = (output / "main-window.png", output / "result-example.png")
    for path in expected:
        if not path.is_file() or path.stat().st_size < 10_000:
            raise RuntimeError(f"문서 화면이 정상적으로 생성되지 않았습니다: {path}")
        print(f"created: {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
