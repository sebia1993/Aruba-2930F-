"""PyInstaller onedir definition for the unsigned Windows x64 package."""

from pathlib import Path

repo_root = Path(SPECPATH).resolve().parent
src_root = repo_root / "src"

analysis = Analysis(
    [str(repo_root / "packaging" / "entrypoint.py")],
    pathex=[str(src_root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=1,
)
pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="Aruba2930FConfigBackup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
)

bundle = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="Aruba2930FConfigBackup",
)
