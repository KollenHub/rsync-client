from __future__ import annotations

import argparse
import os
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


def main() -> int:
    # Compiled SSH_ASKPASS must run before Qt or argument parsing is initialized.
    if len(sys.argv) > 1 and sys.argv[1] == '--ssh-askpass':
        from .askpass import main as askpass_main
        return askpass_main(sys.argv[2:])
    _pin_platform_for_wslg()
    parser = argparse.ArgumentParser(description='Rsync Desk · Python 桌面文件同步客户端')
    parser.add_argument('--config', type=Path, help='指定配置文件（密码库存放于同目录）')
    parser.add_argument('--version', action='version', version='Rsync Desk 1.0.0')
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
