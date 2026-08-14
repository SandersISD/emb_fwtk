# emb_fwtk — Dockerized embedded firmware development toolkit
# Target: raspi5-03 (ARM64 / aarch64, Debian 13) — but platform-agnostic.
# Provides: ARM GCC cross toolchain + OpenOCD + make + build tools + Python3
# USB debug probe (J-Link/ST-Link) is passed through at container runtime.

FROM arm64v8/debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        make \
        cmake \
        git \
        ca-certificates \
        curl \
        wget \
        file \
        usbutils \
        netcat-openbsd \
        python3 \
        python3-numpy \
        python3-websockets \
        gcc-arm-none-eabi \
        binutils-arm-none-eabi \
        libnewlib-arm-none-eabi \
        openocd \
    && rm -rf /var/lib/apt/lists/*

# Non-root user so bind-mounted source dirs owned by host user still work
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} dev && useradd -m -u ${UID} -g dev -s /bin/bash dev
USER dev
WORKDIR /workspace

CMD ["/bin/bash"]