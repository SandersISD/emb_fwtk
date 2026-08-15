#!/usr/bin/env python3
"""
Dual-board calibration collector v2 — single-emitter (PAIR) modes.

PROCEDURE (matches the physical workflow):
  1. Press joint flat -> boards parallel, all d_k = d_min (reference, theta~0)
  2. Release -> neutral
  3. Tilt to N positions; at each, measure the 3 pillar gaps with calipers
  4. Script sweeps each PAIR x color (9 single-emitter modes) + TRIPLE + OFF

v2 CHANGES vs v1:
  - LED modes use firmware >=1.2 PAIR modes (8..16): exactly ONE emitter lit.
    v1 lit all three LEDs the same color, which violates the single-emitter
    assumption behind proof Eq 17 (each C column must come from one emitter).
  - Symbols resolved from ELF via nm (addresses move between builds).
  - Atomic reads: frame/sensors/frame burst on ONE telnet connection
    (completes in ~tens of ms, well inside the ~330 ms MUX sweep).
  - Pair->sensor map auto-detected at startup and written into the CSV.
  - TRIPLE frame recorded at each position for end-to-end validation.

Usage:
  python3 collect_calibration.py --port-emit 4444 --port-recv 4445
"""
import socket, subprocess, sys, time, csv, os, math, struct, argparse
from datetime import datetime

ELF_PATH = "/workspace/RGBSensingModule/Firmware/build/Firmware.elf"
OUTPUT_DIR = "/workspace/emb_fwtk/data"

LED_OFF, LED_TRIPLE, LED_PAIR_BASE = 0, 5, 8
COLORS = ["R", "G", "B"]

SENSOR_RADIUS_MM = 15.0            # equilateral triangle vertex radius (KiCad)
SENSOR_ANGLES = [0, 2*math.pi/3, 4*math.pi/3]  # vertex angle per sensor index (ASSUMED)

def get_symbols():
    r = subprocess.run(["arm-none-eabi-nm", "-n", ELF_PATH], capture_output=True, text=True)
    addrs = {}
    want = {"RGBSensorData", "IMUData_t", "g_can_led_mode", "g_sensor_frame"}
    for line in r.stdout.splitlines():
        p = line.strip().split()
        if len(p) >= 3 and p[2] in want:
            addrs[p[2]] = int(p[0], 16)
    return addrs

def send_burst(cmds, port, settle=0.35):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    s.connect(("127.0.0.1", port))
    try:
        while True:
            if not s.recv(4096): break
    except socket.timeout:
        pass
    s.sendall(("\n".join(cmds) + "\n").encode())
    time.sleep(settle)
    resp = b""
    s.settimeout(settle)
    try:
        while True:
            c = s.recv(4096)
            if not c: break
            resp += c
    except socket.timeout:
        pass
    s.close()
    return resp.decode(errors="replace")

def parse_words(resp):
    out = []
    for line in resp.splitlines():
        line = line.strip().replace("\x00", "")
        if ":" not in line: continue
        for p in line.split(":", 1)[1].strip().split():
            try: out.append(int(p, 16))
            except ValueError: pass
    return out

class Boards:
    def __init__(self, syms, emit_port, recv_port):
        self.syms, self.emit, self.recv = syms, emit_port, recv_port

    def led(self, mode):
        send_burst([f"mww 0x{self.syms['g_can_led_mode']:08x} {mode}"], self.emit, settle=0.3)

    def read_atomic(self, reps=6):
        sa, fa = self.syms["RGBSensorData"], self.syms["g_sensor_frame"]
        burst = [f"mdw 0x{fa:08x} 1", f"mdw 0x{sa:08x} 8", f"mdw 0x{fa:08x} 1"]
        for _ in range(reps):
            w = parse_words(send_burst(burst, self.recv))
            if len(w) == 10 and w[0] == w[-1]:
                u16 = []
                for v in w[1:9]:
                    u16.append(v & 0xFFFF); u16.append((v >> 16) & 0xFFFF)
                return [u16[i*5+j] for i in range(3) for j in range(5)], w[0]
            time.sleep(0.05)
        return None, -1

    def read_imu(self, port):
        w = parse_words(send_burst([f"mdw 0x{self.syms['IMUData_t']:08x} 12"], port, settle=0.3))
        if len(w) < 12: return None
        return [struct.unpack("<f", struct.pack("<I", w[i]))[0] for i in (3, 4, 5)]

def detect_pair_map(b):
    """Light each pair in red; the sensor with the dominant R response is the partner.
    Returns list: pair_index -> sensor_index."""
    b.led(LED_OFF); time.sleep(0.5)
    amb, _ = b.read_atomic()
    if amb is None:
        sys.exit("ERROR: cannot read sensors (atomic read failed)")
    mapping = []
    for pair in range(3):
        b.led(LED_PAIR_BASE + 3*pair + 0)  # red on this pair
        time.sleep(0.5)
        cur, _ = b.read_atomic()
        if cur is None:
            sys.exit(f"ERROR: atomic read failed while probing pair {pair}")
        # dominant ambient-subtracted R channel
        deltas = [cur[s*5+0] - amb[s*5+0] for s in range(3)]
        partner = max(range(3), key=lambda s: deltas[s])
        mapping.append(partner)
        print(f"  pair {pair} (TIM2 ch{pair+2}) -> sensor {partner}   R-deltas={deltas}")
    b.led(LED_OFF)
    if sorted(mapping) != [0, 1, 2]:
        sys.exit(f"ERROR: pair->sensor map not a permutation: {mapping}")
    return mapping

def compute_theta_from_distances(d):
    r = SENSOR_RADIUS_MM
    pts = [[r*math.cos(a), r*math.sin(a), d[i]] for i, a in enumerate(SENSOR_ANGLES)]
    v1 = [pts[1][j]-pts[0][j] for j in range(3)]
    v2 = [pts[2][j]-pts[0][j] for j in range(3)]
    n = [v1[1]*v2[2]-v1[2]*v2[1], v1[2]*v2[0]-v1[0]*v2[2], v1[0]*v2[1]-v1[1]*v2[0]]
    ln = math.sqrt(sum(x*x for x in n))
    if ln < 1e-10: return 0.0, 1.0
    ct = max(0.0, min(1.0, abs(n[2]/ln)))
    return math.acos(ct), ct

def collect_position(pos, syms, b, pair_map, settle, reps):
    print(f"\n  --- Position {pos} ---")
    print("  Measure the gap at each of the 3 SENSOR pillars with calipers.")
    d = []
    for i in range(3):
        while True:
            try:
                d.append(float(input(f"  Caliper gap at sensor pillar {i} (mm): ").strip())); break
            except ValueError:
                print("  Invalid number, try again.")
    theta, cos_t = compute_theta_from_distances(d)
    print(f"  d0={d[0]:.1f} d1={d[1]:.1f} d2={d[2]:.1f} mm | theta_geom={math.degrees(theta):.2f} deg cos={cos_t:.4f}")

    rows = []
    base = dict(position=pos, theta_rad=theta, cos_theta=cos_t, d0=d[0], d1=d[1], d2=d[2])

    def snap(mode_label, extra):
        for rep in range(reps):
            vals, frame = b.read_atomic()
            if vals is None: continue
            row = dict(base); row.update(extra); row["rep"] = rep; row["mode"] = mode_label; row["frame"] = frame
            for i in range(3):
                for j, ch in enumerate("RGBCI"):
                    row[f"S{i}_{ch}"] = vals[i*5+j]
            rows.append(row)
            time.sleep(0.1)

    b.led(LED_OFF); time.sleep(settle/1000.0)
    snap("OFF", dict(pair=-1, color="", B_R=0, B_G=0, B_B=0))

    for pair in range(3):
        for ci, cname in enumerate(COLORS):
            mode = LED_PAIR_BASE + 3*pair + ci
            print(f"  PAIR{pair} {cname} (mode {mode})...")
            b.led(mode); time.sleep(settle/1000.0)
            bv = [255 if k == ci else 0 for k in range(3)]
            snap(f"PAIR{pair}{cname}", dict(pair=pair, color=cname, B_R=bv[0], B_G=bv[1], B_B=bv[2]))
    print("  TRIPLE (runtime mode)...")
    b.led(LED_TRIPLE); time.sleep(settle/1000.0)
    snap("TRIPLE", dict(pair=-2, color="TRIPLE", B_R=255, B_G=255, B_B=255))

    b.led(LED_OFF)
    return rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-emit", type=int, default=4444, help="OpenOCD telnet: emitter board")
    ap.add_argument("--port-recv", type=int, default=4445, help="OpenOCD telnet: receiver board")
    ap.add_argument("--positions", type=int, default=6)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--settle", type=int, default=400, help="LED settle time ms")
    args = ap.parse_args()

    syms = get_symbols()
    for k in ("RGBSensorData", "g_can_led_mode", "g_sensor_frame"):
        if k not in syms: sys.exit(f"ERROR: {k} not in ELF")
    print(f"Symbols: " + "  ".join(f"{k}=0x{v:08x}" for k, v in syms.items()))

    b = Boards(syms, args.port_emit, args.port_recv)
    print("\nAuto-detecting pair->sensor map (each pair lit red, one at a time):")
    pair_map = detect_pair_map(b)
    print(f"  pair_map (pair -> sensor): {pair_map}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    print(f"""
{'='*60}
JOINT MECHANICS CALIBRATION v2 (single-emitter PAIR modes)
  Emitter OpenOCD port {args.port_emit} | Receiver port {args.port_recv}
  Positions: {args.positions} x reps {args.reps}
  Position 1: joint pressed FLAT (reference, theta~0)
  Position 2: released NEUTRAL
  Positions 3..{args.positions}: manual tilts
{'='*60}""")

    all_rows = []
    for pos in range(1, args.positions+1):
        if pos == 1:   input("\nPOSITION 1: press joint FLAT, then Enter...")
        elif pos == 2: input("\nPOSITION 2: RELEASE to neutral, then Enter...")
        else:          input(f"\nPOSITION {pos}: tilt to a new angle, then Enter...")
        all_rows.extend(collect_position(pos, syms, b, pair_map, args.settle, args.reps))

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(OUTPUT_DIR, f"calibration_{ts}.csv")
    if all_rows:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
            w.writeheader(); w.writerows(all_rows)
        meta = os.path.join(OUTPUT_DIR, f"calibration_{ts}_meta.txt")
        with open(meta, "w") as f:
            f.write(f"pair_map={pair_map}\ntimestamp={ts}\nelf={ELF_PATH}\n")
        print(f"\nSaved {len(all_rows)} rows -> {path}\nMeta -> {meta}")
        print(f"Next: python3 calibrate_fit.py {path} --pair-map {','.join(map(str,pair_map))}")

if __name__ == "__main__":
    main()
