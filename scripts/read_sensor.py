#!/usr/bin/env python3
"""
Read sensor + IMU data from STM32 via OpenOCD telnet.
Zero firmware changes needed for RGB sensor reading.
IMU reading requires firmware to populate IMUData_t (see notes below).

FIRMWARE IMU STATUS:
  ImuGyroTask currently only reads raw gyro → CAN. It does NOT write
  to IMUData_t. To enable IMU reading via OpenOCD, add to ImuGyroTask:

    int16_t accel[3] = {0,0,0};
    icm42688p.getRawAccel(accel);
    // Convert to float (m/s² or g)
    IMUData_t.rawaccel[0] = accel[0]; ... etc
    IMUData_t.accel[0] = accel[0] / 2048.0; // LSB sensitivity

  Until this is done, IMU fields will read as zero.

MEMORY LAYOUT:
  RGBSensorData[3] — 3 sensors × {R,G,B,C,IR} uint16 = 30 bytes
  IMUData_t — struct with rawaccel[3], rawgyro[3], accel[3], gyro[3], etc.

Usage:
  python3 read_sensor.py --port 4444           # single read
  python3 read_sensor.py --port 4444 --poll 1  # poll every 1s
  python3 read_sensor.py --port 4444 --led 1   # set LED RED + read
"""
import socket
import subprocess
import sys
import time
import math
import struct
import argparse

ELF_PATH = "/workspace/RGBSensingModule/Firmware/build/Firmware.elf"

def get_symbol_addrs(elf_path, symbols):
    result = subprocess.run(
        ["arm-none-eabi-nm", "-n", elf_path],
        capture_output=True, text=True
    )
    addrs = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) >= 3:
            for sym in symbols:
                if parts[2] == sym:
                    addrs[sym] = int(parts[0], 16)
    return addrs

def telnet_cmd(cmd, host="localhost", port=4444, timeout=3.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    time.sleep(0.2)
    try:
        while True:
            s.recv(4096)
    except socket.timeout:
        pass
    s.sendall((cmd + "\n").encode())
    time.sleep(0.3)
    resp = b""
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
    except socket.timeout:
        pass
    s.close()
    return resp.decode(errors='replace')

def parse_mdws(resp):
    words = []
    for line in resp.splitlines():
        line = line.strip().replace('\x00', '')  # strip null bytes from OpenOCD telnet
        if ":" not in line:
            continue
        parts = line.split(":", 1)[1].strip().split()
        for p in parts:
            # OpenOCD mdw outputs values WITHOUT 0x prefix (e.g. "0075004a")
            # The address has 0x but the data words don't
            try:
                words.append(int(p, 16))
            except ValueError:
                pass
    return words

def read_block(addr, count, port=4444):
    resp = telnet_cmd(f"mdw 0x{addr:08x} {count}", port=port)
    return parse_mdws(resp)[:count]

def read_rgb_sensor_data(base_addr, port=4444):
    """Read RGBSensorData[3] — 3 sensors × 5 uint16 = 30 bytes = 8 u32 words."""
    words = read_block(base_addr, 8, port=port)
    if len(words) < 8:
        return None
    u16s = []
    for w in words:
        u16s.append(w & 0xFFFF)
        u16s.append((w >> 16) & 0xFFFF)
    sensors = []
    for i in range(3):
        base = i * 5
        sensors.append({
            'R': u16s[base], 'G': u16s[base+1], 'B': u16s[base+2],
            'C': u16s[base+3], 'IR': u16s[base+4],
        })
    return sensors

def read_imu_data(addr, port=4444):
    """Read IMUData_t from RAM.

    struct IMUData {
        int16_t rawaccel[3];      // offset 0,  6 bytes
        int16_t rawgyro[3];       // offset 6,  6 bytes
        float accel[3];           // offset 12, 12 bytes
        float gyro[3];            // offset 24, 12 bytes
        int16_t rawtemperature[2]; // offset 36, 4 bytes
        float temperature[2];     // offset 40, 8 bytes
    };                           // total: 48 bytes = 12 u32 words
    """
    words = read_block(addr, 12, port=port)
    if len(words) < 12:
        return None

    # Extract float accel[3] at offset 12 bytes = word[3], word[4], word[5]
    accel_floats = []
    for i in range(3, 6):
        # Convert u32 to float (IEEE 754 little-endian)
        raw = struct.pack('<I', words[i])
        accel_floats.append(struct.unpack('<f', raw)[0])

    # Extract raw accel (int16) from word[0] and word[1]
    raw_accel = []
    for i in range(3):
        word_idx = i // 2
        if i % 2 == 0:
            val = words[word_idx] & 0xFFFF
        else:
            val = (words[word_idx] >> 16) & 0xFFFF
        # Sign extend int16
        if val >= 32768:
            val -= 65536
        raw_accel.append(val)

    return {
        'raw_accel': raw_accel,
        'accel': accel_floats,
        'words': words,
    }

def accel_to_pitch_roll(accel):
    """Compute pitch/roll from accelerometer (g units).

    pitch = atan2(ax, sqrt(ay² + az²))
    roll  = atan2(-ay, az)

    Returns (pitch_rad, roll_rad) or None if accel is all zeros.
    """
    ax, ay, az = accel
    mag = math.sqrt(ax**2 + ay**2 + az**2)
    if mag < 0.01:
        return None  # IMU not populated or in freefall
    pitch = math.atan2(ax, math.sqrt(ay**2 + az**2))
    roll = math.atan2(-ay, az)
    return pitch, roll

def compute_theta(pitch_a, roll_a, pitch_b, roll_b):
    """Compute relative rotation θ between two boards from IMU.

    θ = arccos(cos(pitch_diff) · cos(roll_diff))

    where pitch_diff = pitch_b - pitch_a, roll_diff = roll_b - roll_a
    """
    pd = pitch_b - pitch_a
    rd = roll_b - roll_a
    cos_theta = math.cos(pd) * math.cos(rd)
    # Clamp for numerical safety
    cos_theta = max(-1.0, min(1.0, cos_theta))
    return math.acos(cos_theta), cos_theta

def main():
    parser = argparse.ArgumentParser(description="Read sensor + IMU from STM32 via OpenOCD")
    parser.add_argument("--port", type=int, default=4444, help="OpenOCD telnet port")
    parser.add_argument("--poll", type=float, default=None, help="Poll interval (seconds)")
    parser.add_argument("--led", type=int, default=None, help="Set LED mode (0=off,1=R,2=G,3=B)")
    args = parser.parse_args()

    syms = get_symbol_addrs(ELF_PATH, [
        "RGBSensorData", "IMUData_t", "g_can_led_mode", "g_can_rx_ok",
    ])
    if not syms:
        print("ERROR: No symbols found in ELF")
        sys.exit(1)

    print("Symbols:")
    for name, addr in syms.items():
        print(f"  {name:30s} = 0x{addr:08x}")

    # Set LED mode if requested
    if args.led is not None and "g_can_led_mode" in syms:
        telnet_cmd(f"mww 0x{syms['g_can_led_mode']:08x} {args.led}", port=args.port)
        led_names = {0: "OFF", 1: "RED", 2: "GREEN", 3: "BLUE", 4: "RESUME"}
        print(f"\nLED → {led_names.get(args.led, '?')}")

    def read_once():
        ts = time.time()
        # RGB sensors
        if "RGBSensorData" in syms:
            sensors = read_rgb_sensor_data(syms["RGBSensorData"], port=args.port)
            if sensors:
                print(f"\n[{ts:.3f}] VEML3328 readings:")
                print(f"  {'Sensor':>8} {'R':>7} {'G':>7} {'B':>7} {'C':>7} {'IR':>7}")
                for i, s in enumerate(sensors):
                    print(f"  {i:>8} {s['R']:>7} {s['G']:>7} {s['B']:>7} {s['C']:>7} {s['IR']:>7}")

        # IMU
        if "IMUData_t" in syms:
            imu = read_imu_data(syms["IMUData_t"], port=args.port)
            if imu:
                print(f"\n  IMU raw accel: {imu['raw_accel']}")
                print(f"  IMU accel (g): [{imu['accel'][0]:.3f}, {imu['accel'][1]:.3f}, {imu['accel'][2]:.3f}]")
                pr = accel_to_pitch_roll(imu['accel'])
                if pr:
                    print(f"  Pitch: {math.degrees(pr[0]):.2f}°  Roll: {math.degrees(pr[1]):.2f}°")
                else:
                    print(f"  Pitch/Roll: N/A (accel all zeros — IMU not populated in firmware)")

    if args.poll:
        print(f"\nPolling every {args.poll}s (Ctrl-C to stop)")
        try:
            while True:
                read_once()
                time.sleep(args.poll)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        read_once()

if __name__ == "__main__":
    main()