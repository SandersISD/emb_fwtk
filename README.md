# emb_fwtk — Embedded Firmware Development Toolkit

Dockerized development environment for SCA RGBSensingModule firmware.
Runs on raspi5-03 (ARM64) but is platform-agnostic.

## Quick Start (tomorrow)

### 1. Start OpenOCD server (container, persistent)

```bash
# On raspi5-03, in ~/main_ws/emb_fwtk
docker run -d --rm --privileged \
  -v /dev/bus/usb:/dev/bus/usb \
  -v ~/main_ws:/workspace \
  -p 4444:4444 -p 3333:3333 \
  --name ocd \
  emb-fwtk:latest \
  openocd -f /workspace/emb_fwtk/openocd/board-a.cfg \
  -c "gdb_port 3333" -c "telnet_port 4444" -c "tcl_port 6666"
```

### 2. Flash firmware

```bash
docker exec ocd openocd -f /workspace/emb_fwtk/openocd/board-a.cfg \
  -c init -c "program /workspace/RGBSensingModule/Firmware/build/Firmware.elf verify reset exit"
```

### 3. Read sensor data (zero firmware changes!)

```bash
docker exec ocd python3 /workspace/emb_fwtk/scripts/read_sensor.py
docker exec ocd python3 /workspace/emb_fwtk/scripts/read_sensor.py --poll 1.0
```

### 4. Collect calibration data

```bash
docker exec -it ocd python3 /workspace/emb_fwtk/scripts/collect_calibration.py --d0 35
```

### 5. Fit C matrix

```bash
docker exec ocd python3 /workspace/emb_fwtk/scripts/calibrate_fit.py \
  /workspace/emb_fwtk/data/calibration_*.csv
```

---

## How it works (no firmware changes)

The firmware already reads 3× VEML3328 sensors continuously and stores them in:

```c
// UserTask.cpp (global, in BSS)
struct RGBSensorDataStruct {
    uint16_t rawRed;    // offset +0
    uint16_t rawGreen;  // offset +2
    uint16_t rawBlue;   // offset +4
    uint16_t rawClear;  // offset +6
    uint16_t rawIR;     // offset +8
} RGBSensorData[3];
```

Address found via: `arm-none-eabi-nm -n Firmware.elf | grep RGBSensorData`

We read this via OpenOCD telnet `mdw` (memory display words) — the J-Link
reads RAM through the AHB-AP bus without stopping the CPU.

LED color is controlled by writing to `g_can_led_mode` in RAM:
  `mww <addr> <0|1|2|3>` → OFF, RED, GREEN, BLUE

---

## Option B: Spectral Separation Check (5 min)

Goal: verify VEML3328 has enough spectral separation for zero-forcing.

```bash
# Start OpenOCD server (step 1 above)

# Read ambient (LED off)
docker exec ocd python3 /workspace/emb_fwtk/scripts/read_sensor.py --led 0

# Read with RED LED
docker exec ocd python3 /workspace/emb_fwtk/scripts/read_sensor.py --led 1

# Read with GREEN LED
docker exec ocd python3 /workspace/emb_fwtk/scripts/read_sensor.py --led 2

# Read with BLUE LED
docker exec ocd python3 /workspace/emb_fwtk/scripts/read_sensor.py --led 3
```

Then manually check diagonal dominance:
```
Sensor_R(RED_LED) >> Sensor_R(GREEN_LED) and Sensor_R(BLUE_LED)?
Sensor_G(GREEN_LED) >> Sensor_G(RED_LED) and Sensor_G(BLUE_LED)?
```

If yes → proceed to full calibration. If no → need optical shielding.

---

## Option A: Full Calibration (30 min)

```bash
# Collect at d0=35mm only first (for C fitting)
docker exec -it ocd python3 /workspace/emb_fwtk/scripts/collect_calibration.py --d0 35 --sweep-dist 35

# Then collect at all distances for validation
docker exec -it ocd python3 /workspace/emb_fwtk/scripts/collect_calibration.py --sweep-dist 5,15,25,35,45,55

# Fit C matrix and validate
docker exec ocd python3 /workspace/emb_fwtk/scripts/calibrate_fit.py \
  /workspace/emb_fwtk/data/calibration_*.csv --d0 35
```

---

## Layout

```
emb_fwtk/
├── README.md                          ← this file
├── Dockerfile                         ← ARM64 Debian 13 + GCC 14.2 + OpenOCD + Python3
├── build.sh                           ← docker build
├── run.sh                             ← interactive container with USB passthrough
├── flash.sh                           ← flash firmware to board-a or board-b
├── gdb-server.sh                      ← start OpenOCD as GDB/telnet server
├── openocd/
│   ├── board-a.cfg                    ← J-Link SN 000770593783, SWD 1MHz
│   ├── board-b.cfg                    ← J-Link SN 000775604909, SWD 1MHz
│   └── jlink-swd-stm32g4.cfg          ← generic config (no serial)
├── scripts/
│   ├── read_sensor.py                 ← read RGBSensorData from RAM
│   ├── collect_calibration.py         ← RGB sweep + distance sweep
│   └── calibrate_fit.py               ← fit C matrix, validate distances
└── data/                              ← calibration CSV output
```

---

## Board map

| Board | J-Link SN | CAN Node ID | Role |
|-------|-----------|-------------|------|
| A | 000770593783 | 0x21 | Sensor board (sensing + IMU) |
| B | 000775604909 | 0x22 | Sensor board (sensing + IMU) |

---

## Firmware memory map (RGBSensingModule, canbus-sanders-dev)

| Symbol | Section | Address (example) | Size |
|--------|---------|-------------------|------|
| RGBSensorData | BSS | 0x200001f78 | 30 bytes (3×10) |
| g_can_led_mode | BSS | varies | 1 byte (uint8) |
| g_can_rx_ok | BSS | varies | 4 bytes (uint32) |
| IMUData_t | BSS | varies | ~40 bytes |

Addresses change each build — always resolve via `arm-none-eabi-nm` or let
read_sensor.py do it automatically.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| "Multiple devices found" | Add `adapter serial <SN>` to OpenOCD cfg |
| OpenOCD exits at 4MHz | Use 1MHz (J-Link clone limit) |
| Sensor reads all zero | Check mux channel init in firmware log; allow 100ms settle |
| LED doesn't change | Verify g_can_led_mode address via nm; check RGBControlTask priority |
| telnet connection refused | OpenOCD server not running; restart container |