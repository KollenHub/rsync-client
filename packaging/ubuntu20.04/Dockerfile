FROM ubuntu:20.04
ARG DEBIAN_FRONTEND=noninteractive
ARG PYTHON_VERSION=3.11.18
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl pkg-config patchelf \
    libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev libffi-dev liblzma-dev \
    libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 \
    libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libdbus-1-3 libfontconfig1 \
    libglib2.0-0 libx11-xcb1 libsm6 libice6 libxrender1 libxi6 \
    rsync openssh-client xvfb xauth fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
RUN curl --fail --location --retry 3 \
    "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" -o /tmp/python.tar.xz \
    && mkdir /tmp/python-source && tar -xJf /tmp/python.tar.xz -C /tmp/python-source --strip-components=1 \
    && cd /tmp/python-source \
    && ./configure --prefix=/opt/python --enable-shared --with-ensurepip=install \
    && make -j4 && make install \
    && echo /opt/python/lib > /etc/ld.so.conf.d/rsync-desk-python.conf && ldconfig \
    && rm -rf /tmp/python-source /tmp/python.tar.xz
ENV PATH="/opt/python/bin:${PATH}" \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    QT_QPA_PLATFORM=offscreen
COPY packaging/ubuntu20.04/requirements.txt /tmp/build-requirements.txt
RUN python3.11 -m pip install --no-cache-dir -r /tmp/build-requirements.txt
ARG BUILD_UID=1000
ARG BUILD_GID=1000
RUN if ! getent group "${BUILD_GID}"; then groupadd --gid "${BUILD_GID}" builder; fi \
    && if ! getent passwd "${BUILD_UID}"; then useradd --uid "${BUILD_UID}" --gid "${BUILD_GID}" --create-home builder; fi
WORKDIR /work
# Required by the Qt xcb platform plugin (not pulled in by offscreen tests).
RUN apt-get update && apt-get install -y --no-install-recommends libxcb-randr0 && rm -rf /var/lib/apt/lists/*
