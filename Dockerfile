# emb_fwtk — Dockerized embedded firmware development toolkit
# Target: ARM64 (aarch64) — Raspberry Pi 5 / Debian 13
# Holds: ARM GCC toolchain + OpenOCD + Python3 + SEGGER J-Link software
# Provides: manage_debug USB arbitration manager for ocd/ozone sessions

FROM arm64v8/debian:13-slim

ENV DEBIAN_FRONTEND=noninteractive

# ─── Base packages ───────────────────────────────────────────────────────────
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
        python3-pip \
        python3-websockets \
        gcc-arm-none-eabi \
        binutils-arm-none-eabi \
        libnewlib-arm-none-eabi \
        openocd \
        libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ─── Python packages ────────────────────────────────────────────────────────
RUN pip3 install --no-cache-dir pyyaml

# ─── SEGGER J-Link Software ──────────────────────────────────────────────────
# The J-Link tarball must be downloaded separately (SEGGER license click-through).
# Place JLink_Linux_arm64.tgz in the build context directory.
# If missing, this step will fail — see README for download instructions.

COPY JLink_Linux_arm64.tgz /tmp/JLink_Linux_arm64.tgz

RUN tar xzf /tmp/JLink_Linux_arm64.tgz -C /tmp/ && \
    JLINK_DIR=$(ls -d /tmp/JLink_Linux_V*_arm64 2>/dev/null | head -1) && \
    if [ -z "$JLINK_DIR" ]; then \
        echo "ERROR: Could not find JLink directory in tarball" && exit 1; \
    fi && \
    mkdir -p /opt/SEGGER && \
    mv "$JLINK_DIR" /opt/SEGGER/JLink && \
    rm -f /tmp/JLink_Linux_arm64.tgz && \
    # Symlink key binaries into PATH \
    for bin in JLinkExe JLinkRemoteServerCLExe JLinkGDBServerExe JLinkConnServerExe JLinkConfigExe; do \
        if [ -f "/opt/SEGGER/JLink/$bin" ]; then \
            ln -s "/opt/SEGGER/JLink/$bin" "/usr/local/bin/$bin"; \
        fi; \
    done && \
    # udev rules (for host-side setup, not critical inside container) \
    cp /opt/SEGGER/JLink/99-jlink.rules /etc/udev/rules.d/ 2>/dev/null || true

# ─── manage_debug ────────────────────────────────────────────────────────────
COPY bin/manage_debug /usr/local/bin/manage_debug
COPY bin/manage_debug.py /usr/local/bin/manage_debug.py
RUN chmod +x /usr/local/bin/manage_debug /usr/local/bin/manage_debug.py

# ─── Non-root user ──────────────────────────────────────────────────────────
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} dev && \
    useradd -m -u ${UID} -g dev -s /bin/bash dev && \
    # Allow non-root to access USB devices (dev group or plugdev)
    groupadd -f plugdev && \
    usermod -aG plugdev dev

USER dev
WORKDIR /workspace

CMD ["/bin/bash"]