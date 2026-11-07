from __future__ import annotations

import argparse
import faulthandler
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _pin_platform_for_wslg() -> None:
    """Run through XWayland on WSLg: its Wayland compositor mishandles popup
    input grabs (combobox lists stay open after a click) and child-window
    stacking; the X11 path handles both correctly."""
    if os.environ.get('QT_QPA_PLATFORM') or not os.environ.get('DISPLAY'):
        return
    try:
        release = Path('/proc/sys/kernel/osrelease').read_text(encoding='utf-8').lower()
    except OSError:
        return
    if 'microsoft' in release:
        os.environ['QT_QPA_PLATFORM'] = 'xcb'


def _activate_input_method() -> None:
    """Hook Qt onto a DBus input method (ibus/fcitx) when one is installed.

    Qt6 has no XIM support and WSLg does not bridge the Windows IME into Linux,
    so CJK input needs a running input-method daemon. PySide6 ships the ibus
    plugin, so activating ibus here is enough once the distro packages exist.
    """
    if os.environ.get('QT_IM_MODULE') or os.environ.get('QT_IM_MODULES'):
        return  # the user chose an input method explicitly
    if not shutil.which('ibus-daemon') or not os.environ.get('DBUS_SESSION_BUS_ADDRESS'):
        return
    os.environ['QT_IM_MODULE'] = 'ibus'
    os.environ.setdefault('XMODIFIERS', '@im=ibus')
    if not shutil.which('busctl'):
        return
    try:  # already on the session bus? then only the env vars were missing
        running = subprocess.run(['busctl', '--user', 'list', '--no-pager'],
                                 check=False, capture_output=True, timeout=10,
                                 env={**os.environ, 'LC_ALL': 'C'}).stdout
        if b'org.freedesktop.IBus' in running:
            return
    except OSError:
        pass
    subprocess.Popen(['ibus-daemon', '-d', '-x', '-r'], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def main() -> int:
    # Print a Python traceback on hard crashes (segfault) to make them diagnosable.
    faulthandler.enable()
    # Compiled SSH_ASKPASS must run before Qt or argument parsing is initialized.
    if len(sys.argv) > 1 and sys.argv[1] == '--ssh-askpass':
        from .askpass import main as askpass_main
        return askpass_main(sys.argv[2:])
    _pin_platform_for_wslg()
    _activate_input_method()
    from . import __version__
    parser = argparse.ArgumentParser(description='Rsync Desk · Python 桌面文件同步客户端')
    parser.add_argument('--config', type=Path, help='指定配置文件（密码库存放于同目录）')
    parser.add_argument('--version', action='version', version=f'Rsync Desk {__version__}')
    parser.add_argument('--self-test', action='store_true', help='检查界面、同步、密码库及编译后密码助手')
    args = parser.parse_args()
    if args.self_test:
        from .selftest import run
        return run()
    if sys.platform == 'win32':
        parser.error('当前同步引擎请在 Linux、macOS 或带图形界面的 WSL2 中运行（需要 rsync 和 OpenSSH）')
    try:
        from PySide6.QtWidgets import QApplication

        from .theme import setup_style
        from .ui import App
    except ImportError as exc:
        print(f'缺少组件：{exc}。请先 pip install -r requirements.txt。', file=sys.stderr)
        return 1
    application = QApplication(sys.argv[:1])
    application.setApplicationName('rsync-desk')
    application.setOrganizationName('Rsync Desk')
    setup_style(application)
    window = App(args.config)
    window.show()
    return application.exec()


if __name__ == '__main__':
    raise SystemExit(main())
