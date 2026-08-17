# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['packaging/launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[('tiff_visualizer/assets', 'tiff_visualizer/assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TIFF Visualizer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['packaging/icon.icns'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TIFF Visualizer',
)
app = BUNDLE(
    coll,
    name='TIFF Visualizer.app',
    icon='packaging/icon.icns',
    bundle_identifier='com.swartzlab.tiffvisualizer',
)
