# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the macOS bundle. The Info.plist document types make
# Finder/Dock accept dropped TIFFs and offer "Open With > TIFF Visualizer";
# the app handles them via Qt FileOpen events.

import os

from PyInstaller.utils.hooks import collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# imagecodecs loads every codec through importlib from a lookup table
# (imagecodecs/imagecodecs.py __getattr__), so PyInstaller's static analysis
# sees none of its ~70 extension modules and bundles only _shared. Without
# these, opening an LZW- or Deflate-compressed TIFF - what most non-Fiji
# tools write - failed with "cannot import name 'lzw_decode' from
# 'imagecodecs'" (lzw_decode lives in imagecodecs._imcd).
hidden = collect_submodules("imagecodecs")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[root],
    binaries=[],
    datas=[(os.path.join(root, "tiff_visualizer", "assets"), "tiff_visualizer/assets")],
    hiddenimports=hidden,
    hookspath=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TIFF Visualizer",
    console=False,
    icon=os.path.join(SPECPATH, "icon.icns"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="TIFF Visualizer")
app = BUNDLE(
    coll,
    name="TIFF Visualizer.app",
    icon=os.path.join(SPECPATH, "icon.icns"),
    bundle_identifier="com.swartzlab.tiffvisualizer",
    version="1.4.0",
    info_plist={
        "NSHighResolutionCapable": True,
        "CFBundleShortVersionString": "1.4.0",
        "CFBundleDocumentTypes": [
            {
                "CFBundleTypeName": "TIFF image",
                "CFBundleTypeRole": "Viewer",
                "LSHandlerRank": "Alternate",
                "LSItemContentTypes": ["public.tiff"],
                "CFBundleTypeExtensions": ["tif", "tiff"],
            }
        ],
    },
)
