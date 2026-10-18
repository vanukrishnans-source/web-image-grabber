# -*- mode: python ; coding: utf-8 -*-
# PyInstaller onedir build for Web Image Grabber (Windows x64; the same spec also builds on Linux for testing).
from pathlib import Path

block_cipher = None
root = Path(SPECPATH)
datas = [
    (str(root / "packaging" / "icon.ico"), "."),
    (str(root / "THIRD_PARTY.md"), "."),
    (str(root / "LICENSE"), "."),
    (str(root / "README.md"), "."),
]
hidden = ["PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtSvg", "PySide6.QtTest",
          "websocket", "requests", "charset_normalizer"]

a = Analysis(
    [str(root / "WebImageGrabber.py")],
    pathex=[str(root)], binaries=[], datas=datas, hiddenimports=hidden,
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "notebook", "pytest", "numpy", "scipy", "pandas", "cv2", "onnxruntime",
              "PIL", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL", "PySide6.QtPdf", "PySide6.QtDBus",
              "PySide6.QtMultimedia", "PySide6.QtWebEngineCore", "PySide6.QtNetwork", "PySide6.Qt3DCore"],
    cipher=block_cipher, noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
icon = str(root / "packaging" / "icon.ico")
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="WebImageGrabber", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False, argv_emulation=False, icon=icon)
exe_console = EXE(pyz, a.scripts, [], exclude_binaries=True, name="WebImageGrabber_cli", debug=False,
                  bootloader_ignore_signals=False, strip=False, upx=False, console=True, icon=icon)
coll = COLLECT(exe, exe_console, a.binaries, a.zipfiles, a.datas, strip=False, upx=False, upx_exclude=[],
               name="WebImageGrabber")
