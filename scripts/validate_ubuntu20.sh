#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
docker build --progress=plain -t rsync-desk-runtime:ubuntu20.04 -f packaging/ubuntu20.04/Dockerfile.runtime .
docker run --rm -v "$project_root/dist:/release:ro" rsync-desk-runtime:ubuntu20.04 bash -eu -c '
    if command -v python3; then echo "Runtime must not contain Python" >&2; exit 1; fi
    ldd --version | head -1
    dpkg -i /release/rsync-desk_1.0.1-1ubuntu20.04_amd64.deb
    grep -qx "Icon=rsync-desk" /usr/share/applications/rsync-desk.desktop
    test -s /usr/share/icons/hicolor/48x48/apps/rsync-desk.png
    test -s /usr/share/icons/hicolor/256x256/apps/rsync-desk.png
    test -s /usr/share/icons/hicolor/scalable/apps/rsync-desk.svg
    rsync-desk --version
    QT_QPA_PLATFORM=offscreen rsync-desk --self-test
    QT_QPA_PLATFORM=xcb xvfb-run -a rsync-desk --self-test
    mkdir -p "/tmp/portable package"
    tar -xzf /release/rsync-desk-1.0.1-ubuntu20.04-x86_64.tar.gz -C "/tmp/portable package"
    QT_QPA_PLATFORM=offscreen "/tmp/portable package/rsync-desk-1.0.1-ubuntu20.04-x86_64/rsync-desk" --self-test
    cd /release
    sha256sum -c SHA256SUMS
'
