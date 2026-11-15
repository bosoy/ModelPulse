# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one windowed ModelPulse.exe that carries its dashboard.

Layout once frozen (see core.RES_DIR / core.APP_DIR):
  inside the exe : ui/ (dashboard + lang packs) -> unpacked to a temp dir
  next to the exe: config.json, data/            -> written at runtime

Importing modules lazily (discover, tomllib, winreg, ctypes, webbrowser) can
hide them from the static analysis, so they are listed explicitly.
"""

a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    datas=[('ui', 'ui')],
    hiddenimports=[
        'discover', 'locate', 'report', 'storage',
        'tomllib', 'winreg', 'ctypes', 'webbrowser', 'sqlite3',
        'email.utils', 'datetime', 'concurrent.futures',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'test', 'pydoc_data'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ModelPulse',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
)
