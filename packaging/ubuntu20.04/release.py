"""Assemble, audit and smoke-test an Ubuntu 20.04 Nuitka distribution."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path('/work')
BUILD = ROOT / 'build' / 'ubuntu20.04'
DIST = ROOT / 'dist'
NAME = 'rsync-desk-1.0.0-ubuntu20.04-x86_64'
DEB_VERSION = '1.0.0-3ubuntu20.04'
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)


def command(args, **kwargs):
    try:
        return subprocess.run([str(arg) for arg in args], check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, flush=True)
        if exc.stderr:
            print(exc.stderr, flush=True)
        raise


def elf(path):
    return path.is_file() and path.open('rb').read(4) == b'\x7fELF'


def dependencies(path):
    dynamic = command(['readelf', '-d', path], capture_output=True, text=True).stdout
    return re.findall(r'\(NEEDED\).*?\[(.*?)\]', dynamic)


def run():
    source = BUILD / 'main.dist'
    if not (source / 'rsync-desk').is_file():
        raise RuntimeError('Nuitka output is missing')
    staging = BUILD / NAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    shutil.copytree(source, staging / 'app', symlinks=True)
    app = staging / 'app'
    removed = []
    for path in list(app.rglob('qt-plugins/*/*')):
        if path.is_file() and path.name not in {'libqxcb.so', 'libqoffscreen.so'}:
            removed.append(str(path.relative_to(app)))
            path.unlink()
    binaries = [path for path in app.rglob('*') if elf(path)]
    lookup = {path.name: path for path in binaries}
    needed = {path: dependencies(path) for path in binaries}
    roots = [path for path in binaries if not path.name.startswith('lib') or path.name in {'libqxcb.so', 'libqoffscreen.so'}]
    reachable = set(roots)
    pending = roots.copy()
    while pending:
        for dependency in needed[pending.pop()]:
            path = lookup.get(dependency)
            if path and path not in reachable:
                reachable.add(path)
                pending.append(path)
    for path in binaries:
        if path.name.startswith('libQt6') and path not in reachable:
            removed.append(str(path.relative_to(app)))
            path.unlink()
    qt_modules = sorted(path.name.split('.')[0] for path in (app / 'PySide6').glob('Qt*.so'))
    if set(qt_modules) != {'QtCore', 'QtGui', 'QtWidgets'}:
        raise RuntimeError(f'Unexpected Qt Python modules: {qt_modules}')
    maximum = (0,)
    for path in app.rglob('*'):
        if not elf(path):
            continue
        versions = command(['readelf', '--version-info', path], capture_output=True, text=True).stdout
        for version in re.findall(r'GLIBC_(\d+(?:\.\d+)+)', versions):
            parsed = tuple(map(int, version.split('.')))
            maximum = max(maximum, parsed)
            if parsed > (2, 31):
                raise RuntimeError(f'{path.name} requires GLIBC_{version}')
        output = command(['ldd', path], capture_output=True, text=True).stdout
        if 'not found' in output:
            raise RuntimeError(f'Missing dependency for {path}:\n{output}')
    launcher = staging / 'rsync-desk'
    launcher.write_text('#!/bin/sh\nset -eu\napp_root=$(CDPATH= cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)\nexport QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"\nexec "$app_root/app/rsync-desk" "$@"\n')
    launcher.chmod(0o755)
    (staging / 'README.txt').write_text(
        'Rsync Desk 1.0.0 — Ubuntu 20.04 x86_64\n\n'
        '便携版：完整解压后执行 ./rsync-desk，不需要安装 Python。不要单独移动 app 内的可执行文件。\n'
        '系统依赖：sudo apt install rsync openssh-client libgl1 libegl1 libglib2.0-0 libxkbcommon-x11-0 libxcb-cursor0 libxcb-randr0 libxcb-icccm4 libxcb-keysyms1 libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-sync1 libxcb-xkb1 libx11-xcb1 libfontconfig1 libdbus-1-3\n'
        '中文字体建议：sudo apt install fonts-noto-cjk\n'
        '诊断：QT_QPA_PLATFORM=offscreen ./rsync-desk --self-test\n'
        '配置：~/.config/rsync-desk/，原有任务和加密密码库继续使用。\n'
        '查看完整使用说明：README.md\n', encoding='utf-8')
    shutil.copy2(ROOT / 'README.md', staging / 'README.md')
    shutil.copytree(ROOT / 'docs', staging / 'docs')
    licenses = staging / 'licenses'
    licenses.mkdir()
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata['Name']
        if name.lower() in {'nuitka', 'pip', 'setuptools', 'wheel', 'ordered-set', 'zstandard'}:
            continue
        for file in distribution.files or ():
            if not any(part.lower().startswith(('license', 'copying')) for part in file.parts):
                continue
            original = Path(distribution.locate_file(file))
            if original.is_file():
                target = licenses / name / Path(*file.parts[1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
    manifest = {
        'name': NAME, 'os': 'Ubuntu 20.04', 'architecture': 'x86_64',
        'debian_version': DEB_VERSION, 'application_icon_sizes': ICON_SIZES,
        'maximum_glibc': '.'.join(map(str, maximum)), 'qt_python_modules': qt_modules,
        'qt_plugins': sorted(str(path.relative_to(app)) for path in app.rglob('qt-plugins/**/*.so')),
        'qt_removed': sorted(removed),
        'packages': {name: importlib.metadata.version(name) for name in ('Nuitka', 'PySide6-Essentials', 'shiboken6', 'cryptography', 'paramiko', 'bcrypt', 'PyNaCl')},
    }
    # A relocated path containing spaces verifies launcher and SSH_ASKPASS quoting.
    relocated = BUILD / 'release smoke test'
    if relocated.exists():
        shutil.rmtree(relocated)
    shutil.copytree(staging, relocated, symlinks=True)
    smoke = command([relocated / 'rsync-desk', '--self-test'], env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'}, capture_output=True, text=True)
    manifest['standalone_smoke'] = json.loads(smoke.stdout.strip().splitlines()[-1])
    command(['xvfb-run', '-a', str(relocated / 'rsync-desk'), '--self-test'], env={**os.environ, 'QT_QPA_PLATFORM': 'xcb'})
    (staging / 'build-info.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    DIST.mkdir(exist_ok=True)
    tar_path = DIST / f'{NAME}.tar.gz'
    with tarfile.open(tar_path, 'w:gz', compresslevel=9) as archive:
        archive.add(staging, arcname=NAME)
    package = BUILD / 'debian-package'
    if package.exists():
        shutil.rmtree(package)
    target = package / 'opt' / 'rsync-desk'
    target.parent.mkdir(parents=True)
    shutil.copytree(staging, target, symlinks=True)
    executable = package / 'usr' / 'bin' / 'rsync-desk'
    executable.parent.mkdir(parents=True)
    executable.symlink_to('/opt/rsync-desk/rsync-desk')
    desktop = package / 'usr' / 'share' / 'applications' / 'rsync-desk.desktop'
    desktop.parent.mkdir(parents=True)
    desktop.write_text('[Desktop Entry]\nType=Application\nName=Rsync Desk\nName[zh_CN]=Rsync 文件同步\nComment=Batch file synchronization over SSH\nExec=/usr/bin/rsync-desk\nIcon=rsync-desk\nTerminal=false\nCategories=Utility;FileTransfer;Network;\nStartupNotify=true\nStartupWMClass=rsync-desk\n')
    assets = ROOT / 'rsync_client' / 'assets'
    for size in ICON_SIZES:
        filename = 'rsync-desk.png' if size == 512 else f'rsync-desk-{size}.png'
        icon = package / 'usr' / 'share' / 'icons' / 'hicolor' / f'{size}x{size}' / 'apps' / 'rsync-desk.png'
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assets / filename, icon)
    scalable = package / 'usr' / 'share' / 'icons' / 'hicolor' / 'scalable' / 'apps' / 'rsync-desk.svg'
    scalable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(assets / 'rsync-desk.svg', scalable)
    pixmap = package / 'usr' / 'share' / 'pixmaps' / 'rsync-desk.png'
    pixmap.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(assets / 'rsync-desk.png', pixmap)
    control = package / 'DEBIAN' / 'control'
    control.parent.mkdir()
    refresh_icons = (
        '#!/bin/sh\nset -e\n'
        'if command -v gtk-update-icon-cache >/dev/null 2>&1 && [ -f /usr/share/icons/hicolor/index.theme ]; then\n'
        '  gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true\nfi\n'
        'if command -v update-desktop-database >/dev/null 2>&1; then\n'
        '  update-desktop-database -q /usr/share/applications || true\nfi\n'
    )
    for name in ('postinst', 'postrm'):
        script = control.parent / name
        script.write_text(refresh_icons)
        script.chmod(0o755)
    installed_kib = sum(path.stat().st_size for path in package.rglob('*') if path.is_file()) // 1024
    control.write_text(
        f'Package: rsync-desk\nVersion: {DEB_VERSION}\nArchitecture: amd64\n'
        'Maintainer: Rsync Desk <noreply@localhost>\nSection: utils\nPriority: optional\n'
        f'Installed-Size: {installed_kib}\n'
        'Depends: libc6 (>= 2.31), rsync, openssh-client, libgl1, libegl1, libglib2.0-0, libxkbcommon-x11-0, libxcb-cursor0, libxcb-randr0, libxcb-icccm4, libxcb-keysyms1, libxcb-render-util0, libxcb-shape0, libxcb-xfixes0, libxcb-sync1, libxcb-xkb1, libx11-xcb1, libfontconfig1, libdbus-1-3\n'
        'Recommends: fonts-noto-cjk\n'
        'Description: Rsync desktop client with SSH server management\n'
        ' Batch transfers, encrypted credentials, jump hosts, progress and terminal logs.\n')
    deb = DIST / f'rsync-desk_{DEB_VERSION}_amd64.deb'
    command(['dpkg-deb', '--root-owner-group', '-Zxz', '--build', package, deb])
    shutil.copy2(staging / 'build-info.json', DIST / f'{NAME}.build-info.json')
    with (DIST / 'SHA256SUMS').open('w') as stream:
        for path in (deb, tar_path):
            digest = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
            stream.write(f'{digest}  {path.name}\n')
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print('Artifacts:', deb.name, tar_path.name)


if __name__ == '__main__':
    run()
