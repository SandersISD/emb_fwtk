# Extending emb_fwtk to other targets

The session-arbitration layer (`manage_debug` stop-before-start, USB settle,
lockfile, per-probe ports, `--init` zombie reaping) is target-agnostic —
it's "one server per USB device, don't let two claim it." That carries over
unchanged to any debug probe. What diverges when you leave STM32+J-Link+Ozone
is three layers: **toolchain**, **flash/probe model**, and **debug GUI**.

This document is a map for the next target (worked example: ESP32-S3 + Arduino),
not a tested implementation. Add the real support when hardware is in hand and
each step can be verified — speculative toolchain installs bloat the image and
rot fast.

---

## 1. Container toolchain

STM32: `gcc-arm-none-eabi` + apt `openocd`. ESP32-S3 needs a different stack:

| Concern | STM32 (current) | ESP32-S3 (new) |
|---|---|---|
| Compiler | `gcc-arm-none-eabi` (apt) | `xtensa-esp32-elf-gcc` — installed by `arduino-cli core install esp32:esp32`, or standalone |
| GDB | `arm-none-eabi-gdb` | `xtensa-esp32-elf-gdb` (also bundled in the Arduino core) |
| Flash tool | OpenOCD `program` command | `esptool` (`pip install esptool`) — talks to the ROM bootloader over USB-UART, not the debug port |
| OpenOCD | apt `openocd` 0.12 (has STM32G4) | espressif's `openocd-esp32` fork — apt openocd's ESP32 target support is weak |
| Build system | Make/CMake | `arduino-cli compile` (or ESP-IDF `idf.py`) |

Dockerfile additions (sketch):

```dockerfile
RUN pip3 install --no-cache-dir esptool
RUN curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh \
    | BINDIR=/usr/local/bin sh
RUN arduino-cli core install esp32:esp32
# openocd-esp32: build from espressif/openocd-esp32, or download a release
```

Consider making the toolchain a build arg or a separate stage so a pure-ARM
user doesn't carry the Xtensa toolchain. Multi-stage + `--target` build arg is
the clean pattern.

## 2. Probe model + flash command

This is the real schema break.

**Today** (J-Link + OpenOCD flash):
- Probe identity = USB device with a **serial number**
- `manage_debug flash` runs `openocd ... program <elf> verify 0x08000000` —
  one ELF, one address, via the debug port

**ESP32-S3** (built-in USB-JTAG + esptool flash):
- Probe identity = a `/dev/ttyACMx` CDC serial **port path** (the ESP32-S3's
  built-in USB-JTAG is a USB-CDC device, not a J-Link USB-class device)
- Flash is multi-blob at different offsets via esptool over USB-UART (the ROM
  bootloader, not the debug port):

  ```
  esptool.py --chip esp32s3 -p /dev/ttyACM0 -b 921600 \
    write_flash 0x0 bootloader.bin 0x8000 partition-table.bin 0x10000 firmware.bin
  ```

### `probes.yaml` schema additions

```yaml
probes:
  esp-board-1:
    type: esp_usb_jtag          # NEW — was implicit "jlink"
    port: "/dev/ttyACM0"        # NEW — replaces "serial" for non-J-Link probes
    flash_method: esptool       # NEW — was implicit "openocd"
    flash_offsets:              # NEW — esptool writes multiple blobs
      bootloader: 0x0
      partition: 0x8000
      app: 0x10000
    openocd_config: "openocd/esp32s3.cfg"
    # ports: ... (telnet/gdb/remote auto-assigned as today)
```

Back-compat: `type` defaults to `jlink`, `flash_method` defaults to `openocd`,
so existing `probes.yaml` files keep working unchanged.

### `manage_debug` flash dispatch

```python
flash_method = probe.get("flash_method", "openocd")
if flash_method == "openocd":
    # current path: openocd init/halt/program/verify/reset/shutdown
elif flash_method == "esptool":
    # esptool.py --chip <chip> -p <port> write_flash <offsets> <files>
else:
    error("unknown flash_method")
```

A stub branch is wired in `manage_debug.py` today — it errors with a pointer
to this document so the extension point is visible without faking an untested
implementation.

### Probe discovery

`manage_debug list` today uses `lsusb | grep SEGGER` + `JLinkExe -USB`. For
ESP32's built-in USB-JTAG, add a second branch: enumerate `/dev/ttyACM*` +
match by USB serial (via `lsusb -v` or `udevadm info`) so the right port maps
to the right logical probe name. The discovery function should key off the
`type` field in `probes.yaml` and only run the matching enumerator.

## 3. Debug GUI — Ozone doesn't apply to ESP32

SEGGER Ozone has no Xtensa core support (ARM/RISC-V/V850 only). The ESP32
debug GUI is **GDB-based**:

- VS Code + the **Cortex-Debug** extension, or
- VS Code + the **ESP-IDF** extension's debugger, or
- Eclipse + GDB Hardware Debugging

All of them talk to `openocd-esp32`'s GDB server on `gdb_port 3333+i` —
which `manage_debug mode ocd` already exposes. So topologies 1/2/3 from
[debug-topologies.md](debug-topologies.md) still hold, but the client side
swaps Ozone for VS Code, and the connection is to `gdb_port` (not
`remote_port`).

`mode remote` (JLinkRemoteServer + Ozone) only works if you use an
**external J-Link probe** on the ESP32 — possible but uncommon; most ESP32-S3
setups use the built-in USB-JTAG and skip `mode remote` entirely.

The `examples/sca/boardA.jdebug` is STM32+Ozone-specific. An ESP32 example
would be a `.vscode/launch.json` with `cortex-debug` pointing at
`localhost:3333` through an SSH tunnel:

```json
{
  "type": "cortex-debug",
  "servertype": "external",
  "gdbTarget": "localhost:3333",
  "device": "esp32s3",
  "executable": "firmware.elf"
}
```

## 4. What stays the same

- Session arbitration (ocd ↔ remote, stop-before-start, 1.5s USB settle)
- Lockfile + `--force` semantics
- Port auto-assignment (telnet/gdb/remote, stride-2 for remote)
- `--init` zombie reaping
- `status` / `list` / `rescan` / `stop` commands (target-agnostic)
- The three debug topologies (probe-server / all-on-slave / relayed) —
  only the client software changes

## 5. Implementation checklist for a real ESP32-S3 addition

1. Get an ESP32-S3 board with USB-JTAG; verify `lsusb` + `/dev/ttyACMx`
2. Add the Dockerfile toolchain block; rebuild; verify `arduino-cli compile`
   + `esptool.py` run inside the container
3. Add `type`/`port`/`flash_method`/`flash_offsets` to `probes.yaml` schema;
   update `load_config` validation
4. Implement `manage_debug flash` esptool branch; test on hardware
5. Add ESP32 discovery branch to `manage_debug list`
6. Install `openocd-esp32`; write `openocd/esp32s3.cfg`; test `mode ocd`
   (GDB server on 3333)
7. Write `examples/esp32/` with a `.vscode/launch.json` + a working
   `probes.yaml` + `openocd/esp32s3.cfg`
8. Add unit tests for the new schema fields + flash dispatch
9. Update this doc with what actually worked