# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — freezes the offline Gradio backend for Mi-Ripple Studio.

Build from the repository root (what .github/workflows/build-windows.yml does):

    pyinstaller build/server.spec --clean --noconfirm

Produces a **one-dir** bundle at ``dist/MiRippleServer/``
(``MiRippleServer.exe`` on Windows; the heavy files live in
``dist/MiRippleServer/_internal/``). The workflow moves that folder to
``python-dist/MiRippleServer`` and electron-builder ships it under
``resources/server/MiRippleServer`` (see electron/electron-builder.yml).

Design notes
------------
* ``console=False``: the Electron shell spawns the exe with piped stdio and
  reads the ``MI_RIPPLE_READY <port>`` line from stdout, so no console window
  is needed (and on Windows none will flash).
* ``pathex=["app"]`` makes the vendored ``mi_ripple`` package importable from
  ``app/gradio_app.py`` exactly like in a dev venv.
* ``collect_all`` for the heavy packages: gradio serves its prebuilt frontend
  from package data (``gradio/templates/frontend``), gradio_client loads
  ``types.json``, and groovy/safehttpx load ``version.txt`` at import time,
  uvicorn lazy-loads its protocol/lifespan submodules by name at runtime, and
  cv2 ships Haar-cascade XML data used by the optional face-protection step.
  Plain static analysis misses those; collecting each package wholesale is
  the robust route.
* The spec is cross-platform so the same file can be tested on any OS.
"""

import os

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

datas = []
binaries = []
hiddenimports = [
    "mi_ripple",
    "mi_ripple.pipeline",
]

# Packages with lazy imports and/or bundled data. Each is collected wholesale;
# optional ones (cv2) are skipped if not installed so the build still works
# with a minimal environment.
for _pkg in ("gradio", "gradio_client", "groovy", "safehttpx", "uvicorn", "scipy", "skimage", "cv2"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except ImportError:
        pass

a = Analysis(
    [os.path.join(ROOT, "app", "gradio_app.py")],
    pathex=[os.path.join(ROOT, "app")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MiRippleServer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MiRippleServer",
)
