# SPDX-License-Identifier: Apache-2.0
#
# Linux target for rez-in-a-box. Not a rez image: it contains the OS, git and
# uv. `rezbox ... --target linux` runs rezbox.py in here (the repo is mounted
# read-only at /rezbox), so rez installs, Pythons and test workdirs live in
# the per-base-image volume mounted at /home/rezbox.
#
#   docker build --build-arg BASE_IMAGE=ubuntu:24.04 docker/rez-linux
ARG BASE_IMAGE=rockylinux:9
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.9

FROM ${UV_IMAGE} AS uv

FROM ${BASE_IMAGE}
ARG BASE_IMAGE

# rez needs: git (rez-release), tar/gzip (sources), a shell, procps (ps, used
# by rez's shell detection), which, findutils. Distro-agnostic on purpose.
RUN set -eux; \
    if command -v dnf >/dev/null 2>&1; then \
        dnf -y install --setopt=install_weak_deps=False \
            git tar gzip ca-certificates procps-ng which findutils shadow-utils; \
        dnf clean all; \
    elif command -v yum >/dev/null 2>&1; then \
        yum -y install git tar gzip ca-certificates procps-ng which findutils shadow-utils; \
        yum clean all; \
    elif command -v apt-get >/dev/null 2>&1; then \
        apt-get update; \
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            git tar gzip ca-certificates procps findutils passwd; \
        rm -rf /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
        apk add --no-cache git tar gzip ca-certificates procps findutils bash shadow; \
    else \
        echo "unsupported base image ${BASE_IMAGE}: no dnf/yum/apt-get/apk" >&2; exit 1; \
    fi

COPY --from=uv /uv /uvx /usr/local/bin/

RUN useradd --create-home --uid 1000 --shell /bin/bash rezbox 2>/dev/null \
    || useradd --create-home --shell /bin/bash rezbox

COPY --chmod=755 entrypoint.sh /usr/local/bin/rezbox

USER rezbox
ENV HOME=/home/rezbox \
    SHELL=/bin/bash \
    REZBOX_HOME=/home/rezbox/.rezbox \
    REZBOX_TARGET=linux \
    UV_CACHE_DIR=/home/rezbox/.cache/uv \
    UV_PYTHON_INSTALL_DIR=/home/rezbox/.local/share/uv/python \
    UV_LINK_MODE=copy \
    REZBOX_BASE_IMAGE=${BASE_IMAGE}
WORKDIR /home/rezbox

ENTRYPOINT ["/usr/local/bin/rezbox"]
CMD ["--help"]
