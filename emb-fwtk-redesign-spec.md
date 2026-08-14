---
title: "emb_fwtk Container Redesign — Unified Debug Environment"
date: "2026-08-15"
project: "sca"
type: spec
status: accepted
tags: [spec, sca, emb_fwtk, docker, openocd, segger]
---

# emb_fwtk Container Redesign — Unified Debug Environment

## Problem Statement

The current `emb_fwtk` Docker setup requires two separate containers (one per debug probe) to run OpenOCD on the Pi. This causes two classes of bugs:

1. **`LIBUSB_ERROR_BUSY`** — both containers hold the same USB device node, and when switching between them the second container can't claim the probe.
2. **`tcl_port 6666` collision** — both containers bind the same port under `--network host`, causing OpenOCD startup failures.

Additionally, there is no support for SEGGER Ozone GUI debugging. The only debug path is OpenOCD telnet, which means the human operator cannot use Ozone's visual debugger (breakpoints, register inspection, RTT, flash programming GUI) from their M3 Mac.

The toolkit should also be reusable across future projects with arbitrary numbers of debug probes, not just the current 2-board SCA setup.

## Solution

A single Docker container that holds both OpenOCD and SEGGER J-Link software, with a `manage_debug` Python script that arbitrates between two mutually exclusive debug sessions:

- **`ocd-session`** — OpenOCD running on all probes, for Python sensor scripts (`read_sensor.py`, `collect_calibration.py`, `capture_check.py`) and command-line flash.
- **`ozone-session`** — J-Link Remote Server running on all probes, for Ozone GUI debugging from the M3 Mac over Tailscale.

A `probes.yaml` config file (bind-mounted at runtime) maps logical probe names to serial numbers, roles, and optional port overrides. Ports are auto-assigned by default with optional override.

## User Stories

1. As a developer, I want to run `manage_debug list` and see all connected J-Link probes with their serial numbers, USB state, and assigned ports, so I can verify the hardware is connected before starting a session.

2. As a developer, I want to run `manage_debug mode ocd` to start OpenOCD on all probes, so I can run Python sensor scripts via telnet.

3. As a developer, I want to run `manage_debug mode remote` to start J-Link Remote Server on all probes, so I can connect Ozone from my M3 Mac.

4. As a developer, I want `manage_debug mode ocd` and `mode remote` to be mutually exclusive — switching modes stops the other session first — so there is never a USB contention.

5. As a developer, I want to run `manage_debug flash <target> <firmware.elf>` to flash a single board without caring about which session is currently active, so I can flash and continue working.

6. As a developer, I want `manage_debug status` to show which session is active, which probes are running, and log output, so I can diagnose issues without SSHing into the container.

7. As an agent operator (Hermes/CI), I want `manage_debug <subcommand> --json` to return machine-parseable output, so I can script automated calibration and verification workflows.

8. As an agent operator, I want `manage_debug` to exit with code 2 when a lock is held, so I can handle the "busy" case gracefully in automation.

9. As a developer setting up a new project, I want to write a `probes.yaml` with just probe names and serial numbers, and have ports auto-assigned, so I can get started quickly.

10. As a developer with a complex multi-container setup, I want to set `port_offset` in `probes.yaml` to avoid port collisions, so I can run multiple containers under `--network host`.

11. As a developer on a laptop, I want to run the container with `--network bridge` and explicit `-p` port mappings, so I don't collide with local services.

12. As a developer, I want `manage_debug rescan` to re-probe USB devices after a hot-plug event, so I don't need to restart the container when a probe is reconnected.

13. As a developer, I want a session lockfile that prevents concurrent operations, so I can't accidentally corrupt a running calibration by switching modes.

14. As a developer, I want `--force` to break a stale lock, so I can recover from a crashed subagent without restarting the container.

15. As a developer, I want the Dockerfile to accept the J-Link tarball via `COPY` from the build context, so I can place the license-restricted file once and build repeatably.

16. As a developer, I want clear error messages when the J-Link tarball is missing, when OpenOCD configs are not found, or when USB probes are unreachable, so I can fix the problem without reading the source.

## Implementation Decisions

### Architecture

```
emb_fwtk/
├── Dockerfile                  ← arm64v8/debian:13-slim + OpenOCD + J-Link + manage_debug
├── probes.yaml                 ← User-edited, bind-mounted at runtime
├── bin/
│   └── manage_debug            ← Python script (single entry point)
├── openocd/
│   ├── board-a.cfg             ← Hand-written, user maintains
│   ├── board-b.cfg
│   └── jlink-swd-stm32g4.cfg  ← Shared SWD config
├── scripts/                    ← Python sensor scripts (unchanged)
├── viz/                        ← Three.js visualizer (unchanged)
├── .gitignore                  ← Excludes JLink tarball, __pycache__
└── README.md                   ← Updated for new workflow
```

### `manage_debug` — Python Entry Point

Language: Python 3 (already in container). One dependency: `pyyaml` for `probes.yaml` parsing.

Subcommands:

| Command | Description | Locking |
|---------|-------------|---------|
| `list [--json]` | Show detected probes, USB state, assigned ports | No |
| `status [--json]` | Show active session, running services, logs | No |
| `mode ocd` | Start OpenOCD on all probes | Yes |
| `mode remote` | Start J-Link Remote Server on all probes | Yes |
| `flash <target> <elf>` | Flash firmware (transient, no session restore) | Yes |
| `stop` | Kill all debug services | Yes |
| `rescan` | Re-probe USB for new device nodes | No |

All commands support `--json` for machine-parsable output.

Exit codes: 0 = ok, 1 = error, 2 = busy (lock held).

### `probes.yaml` Schema

```yaml
# Optional: shift all ports by this offset (default 0)
# Use when multiple containers run under --network host
port_offset: 0

probes:
  # Logical name — used as target arg in `flash` and `mode` commands
  board-a:
    serial: "000770593783"
    role: "emitter"           # Free-text, for human reference
    openocd_config: "openocd/board-a.cfg"  # Relative to repo root, or absolute
    # Optional port overrides — omit for auto-assignment
    ports:
      telnet: 4444
      gdb: 3333
      remote: 19020

  board-b:
    serial: "000775604909"
    role: "receiver"
    openocd_config: "openocd/board-b.cfg"
    ports:
      telnet: 4445
      gdb: 3334
      remote: 19021
```

Auto-assignment formula (when `ports` block is omitted):
- `telnet = 4444 + probe_index + port_offset`
- `gdb = 3333 + probe_index + port_offset`
- `remote = 19020 + probe_index + port_offset`

### Session Arbitration

- **Global lockfile** at `/tmp/manage_debug.lock` inside the container.
- Locking operations: `mode ocd`, `mode remote`, `flash`.
- Non-locking operations: `list`, `status`, `rescan`.
- `stop` is locking (it writes the lock then releases it).
- `--force` flag on locking commands breaks a stale lock.
- Lock contains: PID, command, timestamp, probe list.
- Exit code 2 when lock is held and `--force` not given.

### Flash Semantics

- `flash` is a transient operation — it does NOT save or restore session state.
- Steps: stop any running services → run OpenOCD flash sequence → stop.
- The user must re-enter their desired mode after flashing.
- Flash sequence: `init` → `halt` → `program <elf> verify 0x08000000` → `reset run` → `shutdown`.

### USB Rescan

- `manage_debug rescan` re-runs `lsusb` and `JLinkExe -USB` to detect all connected probes.
- If a probe is visible via `lsusb` but OpenOCD/JLink can't open the device node, print a clear error: "Probe detected but device node is stale — run `docker restart emb-fwtk` to re-bind USB."
- This is a diagnostic command, not a full fix — Docker doesn't support live USB re-binding.

### Dual Network Mode

- `--network host` (default, Pi-optimized): ports are directly on the host's network stack.
- `--network bridge` with `-p` mappings: supported via `docker-compose.yml` or documented `docker run` command.
- The README documents both patterns with examples.

### Port Collision Detection

- On startup of any mode, `manage_debug` probes all assigned ports with `socket.socket().bind()` to check for conflicts.
- If a port is already in use, print a warning with the port number and the probe name.
- Suggestion: increase `port_offset` in `probes.yaml` or manually override the conflicting port.

### J-Link Software Installation

- The SEGGER J-Link ARM64 `.tgz` must be downloaded manually by the user from segger.com (license click-through required).
- Placed at `emb_fwtk/JLink_Linux_arm64.tgz` (or `.tgz`).
- Dockerfile `COPY`s it into the image and extracts it to `/opt/SEGGER/JLink/`.
- `.gitignore` excludes it from version control.
- If the tarball is missing at build time, the Docker build fails with a clear error.
- The README provides: download URL, expected filename, verification steps.

### OpenOCD Configs

- Hand-written, same format as today. The `probes.yaml` references them by path.
- No auto-generation. The user maintains them per project.
- `manage_debug` passes `-f <config>` to OpenOCD for each probe.
- `-c "tcl_port disabled"` is mandatory for all OpenOCD instances (solves the 6666 collision bug).

### Dockerfile Changes

Base: `arm64v8/debian:13-slim` (unchanged).

Additions:
- `COPY JLink_Linux_arm64.tgz /tmp/` and extract + install to `/opt/SEGGER/JLink/`
- `RUN pip install pyyaml` (for Python YAML parsing)
- `COPY bin/manage_debug /usr/local/bin/`
- Symlink key J-Link binaries: `JLinkExe`, `JLinkRemoteServerCLExe`, `JLinkGDBServerExe`
- Existing ARM GCC toolchain, OpenOCD, Python3 packages remain unchanged
- Non-root user setup unchanged

## Testing Decisions

### Seam 1: Unit Tests (no hardware, runs on Mac or in container)

`pytest` suite in `emb_fwtk/tests/test_manage_debug.py` covering:

- YAML parsing: valid config, missing fields, optional overrides, port_offset
- Port auto-assignment: formula correctness, override priority
- Lock file logic: acquire, release, contention, `--force` break
- Argument parsing: all subcommands, `--json` flag, missing required args
- Port collision detection: mock occupied ports, verify warning
- No actual USB or OpenOCD calls — all mocked via `unittest.mock`

### Seam 2: Probe Detection (needs Pi + J-Link, no target board)

- `manage_debug list` shows both J-Link serials
- `manage_debug list --json` returns valid JSON with serials
- `manage_debug rescan` after physical replug
- Stale USB node: `lsusb` shows probe but old device node → clear error message

### Seam 3: OpenOCD Mode (needs Pi + J-Link + target boards)

- `manage_debug mode ocd` → both telnet ports respond (`nc -z localhost 4444 && nc -z localhost 4445`)
- `read_sensor.py` works on both ports
- `capture_check.py` reports correct LED modes
- `manage_debug flash board-a firmware.elf` → "Verified OK"
- `manage_debug flash board-b firmware.elf` → "Verified OK"

### Seam 4: Mode Switching (needs full hardware)

- `mode ocd → mode remote → mode ocd` cycle, no `LIBUSB_ERROR_BUSY`
- `mode remote → flash board-a → mode remote` — flash is transient, leaves clean
- `mode ocd` while lock held → exit code 2
- `mode ocd --force` while lock held → succeeds

### Seam 5: Ozone Remote (needs M3 Mac + Tailscale)

- Ozone connects to `raspi5-03 Tailscale IP:19020`
- Halt, step, breakpoint, register read, RTT all work
- Ozone connects to port 19021 for board B

### Seam 6: Bridge Mode (laptop or second machine)

- `docker run --network bridge -p 4444:4444 -p ...` works identically
- No functional difference from host mode

## Out of Scope

- CANbus protocol, ROS2 integration
- IMUData_t.accel population (firmware change, not debug toolkit)
- Full M-matrix calibration with calipers (separate workflow)
- Auto-generation of OpenOCD configs from YAML
- GUI for `manage_debug` (CLI-only, SSH-friendly)
- CI/CD pipeline for the container (might add later, not in scope)
- Windows support for the container host (Linux-only, Docker-on-Pi)

## Further Notes

### Risks

1. **SEGGER license tarball** — Requires manual download. The Dockerfile will fail to build without it. Mitigation: README provides clear download instructions and verification step. The tarball is excluded from git but lives in the repo directory.

2. **JLinkRemoteServer on ARM64** — Confirmed present in V968 tarball (`JLinkRemoteServerCLExe` exists). The Qt GUI dependencies are not needed (we use the CLI version).

3. **Two JLinkRemoteServer instances** — The CLI supports `-select usb=<SN> -port <port>`. Verified by SEGGER documentation.

4. **Non-root USB access** — `--privileged` at `docker run` handles this. The `dev` user (UID 1000) can access `/dev/bus/usb/*` through the `--device` flag.

5. **OpenOCD vs JLink on same USB** — The session arbitration (lockfile + stop-before-start) prevents simultaneous access. This is the core of the fix.

### Dependencies

- `pyyaml` (Python package, added to Dockerfile)
- SEGGER J-Link Software Pack V968 for ARM64 (manual download, 73MB)
- Python 3.12+ (stdlib `tomllib` available if we switch to TOML, but YAML is more familiar for config)

## Glossary

| Term | Definition |
|------|------------|
| **emb_fwtk** | Embedded Firmware Development Toolkit — Docker container + scripts for SCA firmware development |
| **OpenOCD** | Open On-Chip Debugger — open-source debug tool for ARM microcontrollers |
| **J-Link Remote Server** | SEGGER utility that makes a USB J-Link probe accessible over TCP/IP |
| **Ozone** | SEGGER's GUI debugger for J-Link probes |
| **ocd-session** | Container mode where OpenOCD is running on all probes |
| **ozone-session** | Container mode where J-Link Remote Server is running on all probes |
| **manage_debug** | Python script that manages debug sessions, USB arbitration, and flashing |
| **probes.yaml** | YAML config file mapping probe names to serial numbers, roles, and ports |
| **port_offset** | YAML field to shift all auto-assigned ports by a fixed amount (avoids collisions) |
| **session lock** | Lockfile at `/tmp/manage_debug.lock` preventing concurrent operations |
| **transient flash** | Flash operation that stops any running session, flashes, and stops — no session restore |

## ADRs

See ADR-01 through ADR-08 in `plan.md` (embedded in the plan file at `.hermes/plans/2026-08-14_emb_fwtk-redesign.md`). Key decisions:

- **ADR-01**: Session-based arbitration (ocd-session or ozone-session, globally exclusive)
- **ADR-02**: Agent-friendly interface (`--json` flag for machine parsing)
- **ADR-03**: Flash is transient (stop → flash → stop, no session restore)
- **ADR-04**: Port auto-assignment with optional override in YAML
- **ADR-05**: Dual network mode support (`--network host` or `--network bridge`)
- **ADR-06**: Port collision via `port_offset` in YAML
- **ADR-07**: USB rescan command for hot-plug recovery
- **ADR-08**: Session lockfile for concurrent access safety