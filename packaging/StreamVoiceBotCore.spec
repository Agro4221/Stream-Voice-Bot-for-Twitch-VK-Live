# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules


PROJECT_ROOT = Path(SPECPATH).parent.parent


datas = [
    (
        str(PROJECT_ROOT / "stream_voice_bot" / "vk_bridge"),
        "stream_voice_bot/vk_bridge",
    ),
    (
        str(PROJECT_ROOT / ".runtime" / "node"),
        ".runtime/node",
    ),
]

binaries = []
hiddenimports = []

for package in (
    "torch",
    "torchaudio",
    "faster_whisper",
    "ctranslate2",
    "argostranslate",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas.extend(package_datas)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_hiddenimports)

hiddenimports.extend(collect_submodules("uvicorn"))


a = Analysis(
    [str(PROJECT_ROOT / "stream_voice_bot" / "__main__.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="StreamVoiceBotCore",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StreamVoiceBotCore",
)
