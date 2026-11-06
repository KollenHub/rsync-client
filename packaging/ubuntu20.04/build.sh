#!/bin/bash
set -euo pipefail
cd /work
python3.11 -m unittest discover -v
python3.11 -m nuitka main.py \
    --mode=standalone --enable-plugin=pyside6 \
    --include-data-dir=rsync_client/assets=rsync_client/assets \
    --include-qt-plugins=platforms \
    --noinclude-qt-plugins=iconengines,imageformats,styles,platformthemes,tls,networkinformation,mediaservice,printsupport,egldeviceintegrations,xcbglintegrations,wayland-shell-integration,wayland-decoration-client,wayland-graphics-integration-client \
    --noinclude-qt-translations \
    --nofollow-import-to=PySide6.QtQml,PySide6.QtQuick,PySide6.QtQuickWidgets,PySide6.QtNetwork,PySide6.QtTest,PySide6.QtSvg,PySide6.QtPrintSupport,PySide6.QtOpenGL,PySide6.QtPdf,PySide6.QtWebEngineCore,PySide6.QtWebEngineWidgets \
    --nofollow-import-to=tkinter,unittest,pytest,tests,numpy,scipy,matplotlib,IPython,PIL,invoke \
    --output-dir=build/ubuntu20.04 --output-filename=rsync-desk \
    --report=build/ubuntu20.04/compilation-report.xml \
    --assume-yes-for-downloads --jobs=4
python3.11 packaging/ubuntu20.04/release.py
PYTHONPATH=/work python3.11 packaging/ubuntu20.04/smoke_ssh.py \
    /work/build/ubuntu20.04/rsync-desk-1.0.0-ubuntu20.04-x86_64/app/rsync-desk
