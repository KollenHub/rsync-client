#!/bin/sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"
mkdir -p build dist
docker build --progress=plain --build-arg BUILD_UID="$(id -u)" --build-arg BUILD_GID="$(id -g)" -t rsync-desk-build:ubuntu20.04 -f packaging/ubuntu20.04/Dockerfile .
docker run --rm --user "$(id -u):$(id -g)" \
    -e NUITKA_CACHE_DIR=/work/build/nuitka-cache \
    -v "$project_root:/work" -w /work \
    rsync-desk-build:ubuntu20.04 bash packaging/ubuntu20.04/build.sh
