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
]

binaries = [
    (
        str(PROJECT_ROOT / ".runtime" / "node" / "node.exe"),
        ".runtime/node",
    ),
]
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
    "sherpa_onnx",
    "argostranslate",
):
    if package == "torch":
        # The torch wheel ships C/C++ header trees for building native extensions.
        # They are not needed by the frozen runtime and can account for thousands
        # of individual files in an otherwise runnable EXE bundle.
        datas.extend(
            collect_data_files(
                package,
                include_py_files=False,
                excludes=["include/**"],
            )
        )
    else:
        datas.extend(collect_data_files(package, include_py_files=False))
    binaries.extend(collect_dynamic_libs(package))

# Silero V5 is loaded from the bundled .pt package via torch.package.
hiddenimports.extend(collect_submodules("torch.package"))

# sherpa-onnx exposes its native backend through dynamic package imports and
# ships the CUDA-capable ONNX Runtime libraries inside the wheel.
hiddenimports.extend(collect_submodules("sherpa_onnx"))
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

# Keep the user-facing folder tidy: PyInstaller onedir runtime support files
# live under _internal, while the EXE itself remains at the top level.
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="StreamVoiceBot",
    icon=str(PROJECT_ROOT / "packaging" / "StreamVoiceBot.ico"),
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=True,
    contents_directory="_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="StreamVoiceBot",
)
