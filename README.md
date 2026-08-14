# emb_fwtk — Embedded Firmware Development Toolkit

Dockerized development environment for STM32G4 firmware, with **two debug modes** (OpenOCD for Python scripting, J-Link Remote Server for Ozone GUI debugging) and a **USB arbitration manager** that cleanly switches between them.

## Quick Start

### 1. Prerequisites

- Docker on an **ARM64 Linux host** (Raspberry Pi 5 / Debian 13)
- One or more **SEGGER J-Link probes** connected via USB
- **J-Link ARM64 software pack** (download required, see Step 3)

### 2. Clone + configure

```bash
git clone <repo-url> emb_fwtk
cd emb_fwtk
```

Edit `probes.yaml` to match your hardware:

```yaml
port_offset: 0
probes:
  board-a:
    serial: "000770593783"
    role: "emitter"
    openocd_config: "openocd/board-a.cfg"
  board-b:
    serial: "000775604909"
    role: "receiver"
    openocd_config: "openocd/board-b.cfg"
```

### 3. Download J-Link software

SEGGER requires a click-through license agreement. Download the **Linux ARM64 .tgz** from:

https://www.segger.com/downloads/jlink/#J-LinkSoftwareAndDocumentationPack

Place it in the repo root:

```bash
mv ~/Downloads/JLink_Linux_V968_arm64.tgz emb_fwtk/JLink_Linux_arm64.tgz
file JLink_Linux_arm64.tgz   # should show: gzip compressed data
```

The tarball is excluded from git (`.gitignore`). You only need to download it once.

### 4. Build the container

```bash
docker build -t emb-fwtk:latest .
```

### 5. Run

```bash
# Option A: --network host (Pi, recommended)
docker run -d --rm --privileged --network host \
  -v /dev/bus/usb:/dev/bus/usb \
  -v /home/pi/main_ws:/workspace \
  -v $(pwd)/probes.yaml:/workspace/emb_fwtk/probes.yaml \
  --name emb-fwtk emb-fwtk:latest \
  tail -f /dev/null

# Option B: --network bridge (laptop, with port mappings)
docker run -d --rm --privileged \
  -v /dev/bus/usb:/dev/bus/usb \
  -v /home/user/main_ws:/workspace \
  -v $(pwd)/probes.yaml:/workspace/emb_fwtk/probes.yaml \
  -p 4444:4444 -p 4445:4445 \
  -p 3333:3333 -p 3334:3334 \
  -p 19020:19020 -p 19021:19021 \
  --name emb-fwtk emb-fwtk:latest \
  tail -f /dev/null
```

### 6. Verify probes

```bash
docker exec emb-fwtk manage_debug list
```

Expected output:

```
=== Probes ===

board-a:
  serial=000770593783
  telnet=4444  gdb=3333  remote=19020
  role=emitter

board-b:
  serial=000775604909
  telnet=4445  gdb=3334  remote=19021
  role=receiver

USB-detected: 2 J-Link probe(s)
```

## Usage

### OpenOCD Session (Python scripting)

```bash
# Start OpenOCD on all probes
docker exec emb-fwtk manage_debug mode ocd

# Read sensor data
docker exec emb-fwtk python3 /workspace/emb_fwtk/scripts/read_sensor.py --port 4444

# Collect calibration data
docker exec emb-fwtk python3 /workspace/emb_fwtk/scripts/collect_calibration.py \
  --port-emit 4444 --port-recv 4445

# Flash firmware
docker exec emb-fwtk manage_debug flash board-a /workspace/firmware.elf
```

### Ozone Session (GUI debugging from M3 Mac)

```bash
# On Pi: start J-Link Remote Server
docker exec emb-fwtk manage_debug mode remote

# On M3 Mac: Open Ozone
# Tools → J-Link Settings → Host Interface → Ethernet
# IP: <raspi5-03 Tailscale IP>
# Port: 19020 (board A) or 19021 (board B)
# Interface: SWD, Speed: 1 MHz
# Device: STM32G431CB
```

### Switch modes

```bash
# Stop current session, start another
docker exec emb-fwtk manage_debug mode ocd       # switch to OpenOCD
docker exec emb-fwtk manage_debug mode remote     # switch to Remote Server
docker exec emb-fwtk manage_debug stop            # stop everything
```

Modes are **mutually exclusive**. Switching kills the other session first.

### Agent-friendly output

```bash
docker exec emb-fwtk manage_debug list --json
docker exec emb-fwtk manage_debug status --json
```

### Lock management

```bash
# If a lock is stale (e.g. subagent crashed), use --force
docker exec emb-fwtk manage_debug mode ocd --force
```

## File Layout

```
emb_fwtk/
├── README.md                           ← this file
├── Dockerfile                          ← ARM64 Debian 13 + OpenOCD + J-Link + Python
├── probes.yaml                         ← YOUR probe config (bind-mounted at runtime)
├── .gitignore                          ← excludes JLink tarball, __pycache__
├── bin/
│   ├── manage_debug                    ← thin wrapper
│   └── manage_debug.py                 ← USB arbitration manager (Python)
├── openocd/
│   ├── board-a.cfg                     ← J-Link SN 000770593783, SWD 1MHz
│   ├── board-b.cfg                     ← J-Link SN 000775604909, SWD 1MHz
│   └── jlink-swd-stm32g4.cfg           ← shared SWD config (no serial)
├── scripts/
│   ├── read_sensor.py                  ← read RGBSensorData from RAM
│   ├── collect_calibration.py          ← RGB sweep + distance sweep
│   ├── calibrate_fit.py                ← fit M matrix, validate distances
│   └── joint_visualizer.py             ← WebSocket + HTTP server for viz
├── viz/
│   └── index.html                      ← Three.js browser visualization
├── tests/
│   └── test_manage_debug.py            ← unit tests (34 tests, no hardware)
└── emb-fwtk-redesign-spec.md           ← formal spec
```

## Port Assignment

Ports are **auto-assigned** by probe index in `probes.yaml`:

| Probe Index | Telnet  | GDB     | Remote  |
|-------------|---------|---------|---------|
| 0 (board-a) | 4444    | 3333    | 19020   |
| 1 (board-b) | 4445    | 3334    | 19021   |
| 2 (board-c) | 4446    | 3335    | 19022   |
| ...         | 4444+N  | 3333+N  | 19020+N |

Override any port in `probes.yaml`:

```yaml
probes:
  board-a:
    serial: "..."
    ports:
      telnet: 4444    # explicit
      # gdb and remote still auto-assigned
```

### Port offset for multi-container setups

If running multiple containers under `--network host`, set `port_offset`:

```yaml
# Container 2
port_offset: 10
probes:
  board-c:  # gets telnet=4454, gdb=3343, remote=19030
    ...
```

## manage_debug Reference

| Command | Description | Locks? |
|---------|-------------|--------|
| `list [--json]` | Show detected probes, ports, USB state | No |
| `status [--json]` | Show active session, running services, lock | No |
| `mode ocd [--force]` | Start OpenOCD on all probes | Yes |
| `mode remote [--force]` | Start J-Link Remote Server | Yes |
| `flash <target> <elf> [--force]` | Flash firmware (transient, no session restore) | Yes |
| `stop [--force]` | Stop all debug services, release lock | Yes |
| `rescan [--json]` | Re-probe USB for new device nodes | No |

Exit codes: 0 = ok, 1 = error, 2 = busy (lock held).

## USB Rescan

If a J-Link is unplugged and re-plugged, the container's USB device node may be stale:

```bash
docker exec emb-fwtk manage_debug rescan
```

This re-probes USB. If the probe is detected but the device node is stale, run:

```bash
docker restart emb-fwtk
```

## Testing

```bash
# Unit tests (no hardware needed)
cd emb_fwtk
PYTHONPATH=bin python3 -m pytest tests/ -v
```

## Project History

This is a redesign of the original two-container setup (separate `ocd-a` and `ocd-b` containers) that suffered from `LIBUSB_ERROR_BUSY` and `tcl_port` collisions. The unified container with `manage_debug` arbitration resolves both.

## License

The SEGGER J-Link software is subject to SEGGER's license terms. The J-Link tarball is excluded from this repository — download it directly from SEGGER.