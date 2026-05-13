# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

rasterio_hiddenimports = collect_submodules('rasterio')
rasterio_datas = collect_data_files('rasterio')

# Include the application icon so Qt can load it at runtime from the bundled package.
app_icon = [('GPXEditor_icon.ico', '.')]
custom_script_datas = [
    (os.path.join('custom_scripts', name), 'custom_scripts')
    for name in os.listdir('custom_scripts')
    if name.lower().endswith('.py') or name == 'README.md'
]


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=app_icon + rasterio_datas + custom_script_datas,
    hiddenimports=rasterio_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='GPXEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='GPXEditor_icon.ico',
)
