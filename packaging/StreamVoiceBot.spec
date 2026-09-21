# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)


PROJECT_ROOT = Path.cwd().resolve()

datas = [
    (
        str(PROJECT_ROOT / "VERSION"),
        ".",
    ),
    (
        str(PROJECT_ROOT / "models" / "v5_ru.pt"),
        "models",
    ),
    (
        str(PROJECT_ROOT / "stream_voice_bot" / "web"),
        "stream_voice_bot/web",
    ),
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
hiddenimports = [
    # Silero V5 code inside the torch.package model imports wave dynamically;
    # PyInstaller cannot see imports embedded in the packaged .pt archive.
    "wave",
]

# Let PyInstaller's normal import analysis handle Python modules. Only add
# package data and native libraries that are not reliably found from imports.
# The build environment is intentionally CPU-only for PyTorch, so no CUDA
# runtime is pulled into the portable TTS package.
for package in (
    "torch",
    "faster_whisper",
    "ctranslate2",
    "argostranslate",
):
    datas.extend(collect_data_files(package, include_py_files=False))
    binaries.extend(collect_dynamic_libs(package))

# Silero V5 is loaded from the bundled .pt package via torch.package.
hiddenimports.extend(collect_submodules("torch.package"))

# These packages use dynamic imports.
hiddenimports.extend(collect_submodules("faster_whisper"))
hiddenimports.extend(collect_submodules("argostranslate"))
hiddenimports.extend(collect_submodules("uvicorn"))

a = Analysis(
    [str(PROJECT_ROOT / "scripts" / "exe_entry.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tensorboard",
        "torch.utils.tensorboard",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# PyInstaller 6.x normally places onedir support files in _internal.
# Our application code deliberately resolves resources relative to the
# directory containing the EXE, so restore the old onedir layout here.
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="StreamVoiceBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    contents_directory=".",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StreamVoiceBot",
)
