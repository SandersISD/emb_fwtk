#!/usr/bin/env python3
"""
Standalone joint state visualizer v2 — TRIPLE mode + global M.

v2 CHANGES:
  - Sets emitter to CAN_LED_TRIPLE (pair0=R, pair1=G, pair2=B; FW >=1.2).
  - Loads the GLOBAL system matrix M (calibrate_fit.py v2) and uses W = M⁻¹.
  - Per-pair distance: partner sensor i = pair_map[p] projects y = W·Sᵢ and
    reads y_p -> d̂_p = sqrt(255·cosθ / y_p).
  - Atomic sensor reads (frame-bracket burst on one connection).
  - Symbols resolved via nm (addresses move between builds).

Usage:
  python3 joint_visualizer.py --port-emit 4444 --port-recv 4445 \
      --m-matrix /workspace/emb_fwtk/data/M_matrix.csv --pair-map 1,0,2 \
      --http-port 8080
"""
import socket, subprocess, sys, time, math, json, struct, os, argparse
import threading, http.server, socketserver, asyncio
import numpy as np
import websockets

ELF_PATH = "/workspace/RGBSensingModule/Firmware/build/Firmware.elf"
VIZ_DIR = os.path.join(os.path.dirname(__file__), "..", "viz")

SENSOR_RADIUS_MM = 15.0
SENSOR_ANGLES = [0, 2*math.pi/3, 4*math.pi/3]

def get_symbols():
    r = subprocess.run(["arm-none-eabi-nm", "-n", ELF_PATH], capture_output=True, text=True)
    addrs = {}
    want = {"RGBSensorData", "IMUData_t", "g_can_led_mode", "g_sensor_frame"}
    for line in r.stdout.splitlines():
        p = line.strip().split()
        if len(p) >= 3 and p[2] in want:
            addrs[p[2]] = int(p[0], 16)
    return addrs

def send_burst(cmds, port, settle=0.3):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    s.connect(("127.0.0.1", port))
    try:
        while True:
            if not s.recv(4096): break
    except socket.timeout: pass
    s.sendall(("\n".join(cmds) + "\n").encode())
    time.sleep(settle)
    resp = b""
    s.settimeout(settle)
    try:
        while True:
            c = s.recv(4096)
            if not c: break
            resp += c
    except socket.timeout: pass
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

def read_sensors_atomic(syms, port, reps=6):
    sa, fa = syms["RGBSensorData"], syms["g_sensor_frame"]
    burst = [f"mdw 0x{fa:08x} 1", f"mdw 0x{sa:08x} 8", f"mdw 0x{fa:08x} 1"]
    for _ in range(reps):
        w = parse_words(send_burst(burst, port))
        if len(w) == 10 and w[0] == w[-1]:
            u16 = []
            for v in w[1:9]:
                u16.append(v & 0xFFFF); u16.append((v >> 16) & 0xFFFF)
            return [{"RGBCI"[j]: u16[i*5+j] for j in range(5)} for i in range(3)]
        time.sleep(0.05)
    return None

def read_imu(syms, port):
    if "IMUData_t" not in syms: return None
    w = parse_words(send_burst([f"mdw 0x{syms['IMUData_t']:08x} 12"], port))
    if len(w) < 12: return None
    return [struct.unpack("<f", struct.pack("<I", w[i]))[0] for i in (3, 4, 5)]

def load_m(path):
    if not path or not os.path.exists(path): return None
    return np.loadtxt(path, delimiter=",", skiprows=1)

def compute_theta_from_distances(d):
    r = SENSOR_RADIUS_MM
    pts = np.array([[r*math.cos(a), r*math.sin(a), d[i]] for i, a in enumerate(SENSOR_ANGLES)])
    n = np.cross(pts[1]-pts[0], pts[2]-pts[0])
    if np.linalg.norm(n) < 1e-10: return 0.0
    return math.acos(max(0.0, min(1.0, abs(n[2]/np.linalg.norm(n)))))

latest_state = {"status": "waiting", "d0": 0, "d1": 0, "d2": 0, "theta": 0}
ws_clients = set()

async def ws_handler(websocket, path):
    ws_clients.add(websocket)
    try:
        while True:
            await websocket.send(json.dumps(latest_state))
            await asyncio.sleep(0.1)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        ws_clients.discard(websocket)

def sensor_loop(syms, ports, W, pair_map, ambient):
    global latest_state
    inv_pair = {s: p for p, s in enumerate(pair_map)}
    while True:
        try:
            sensors = read_sensors_atomic(syms, ports["recv"])
            if sensors is None:
                latest_state = {"status": "no_sensor_data", "d0": 0, "d1": 0, "d2": 0, "theta": 0}
                time.sleep(0.5); continue

            if W is not None:
                d = [0.0, 0.0, 0.0]
                yvals = [0.0, 0.0, 0.0]
                for p in range(3):
                    si = pair_map[p]
                    S = np.array([sensors[si][c] - (ambient[si][c] if ambient else 0)
                                  for c in "RGB"])
                    y = W @ S
                    yvals[p] = float(y[p])
                    d[p] = math.sqrt(255.0 / y[p]) if y[p] > 0 else 0.0
                theta = compute_theta_from_distances(d)
                # IMU theta would refine cosθ; geometric used until firmware populates it
                latest_state = {
                    "status": "ok", "d0": d[0], "d1": d[1], "d2": d[2],
                    "theta": theta,
                    "y0": yvals[0], "y1": yvals[1], "y2": yvals[2],
                    "sensor_positions": [
                        {"x": SENSOR_RADIUS_MM*math.cos(a), "y": SENSOR_RADIUS_MM*math.sin(a)}
                        for a in SENSOR_ANGLES],
                    "raw": sensors,
                }
            else:
                latest_state = {"status": "raw", "raw": sensors, "d0": 0, "d1": 0, "d2": 0, "theta": 0}
        except Exception as e:
            latest_state = {"status": f"error: {e}", "d0": 0, "d1": 0, "d2": 0, "theta": 0}
        time.sleep(0.1)

class VizHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=VIZ_DIR, **kw)
    def log_message(self, *a): pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port-emit", type=int, default=4444)
    ap.add_argument("--port-recv", type=int, default=4445)
    ap.add_argument("--m-matrix", "--c-matrix", default=None, help="M matrix CSV (calibrate_fit v2)")
    ap.add_argument("--pair-map", default="1,0,2", help="pair->sensor map from collector")
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--ws-port", type=int, default=8081)
    args = ap.parse_args()
    pair_map = [int(x) for x in args.pair_map.split(",")]
    ports = {"emit": args.port_emit, "recv": args.port_recv}

    M = load_m(args.m_matrix)
    W = None
    if M is not None:
        W = np.linalg.inv(M)
        print(f"Loaded M from {args.m_matrix}; W=M⁻¹:\n{W}")
    else:
        print("No M matrix — raw mode (distances meaningless until calibrated)")

    syms = get_symbols()
    if "RGBSensorData" not in syms:
        sys.exit("ERROR: RGBSensorData not found in ELF")
    print(f"Symbols: {syms}")

    # Emitter -> TRIPLE runtime mode
    send_burst([f"mww 0x{syms['g_can_led_mode']:08x} 5"], ports["emit"])
    print("Emitter set to TRIPLE (pair0=R pair1=G pair2=B)")

    # Ambient baseline (LED off momentarily)
    send_burst([f"mww 0x{syms['g_can_led_mode']:08x} 0"], ports["emit"])
    time.sleep(0.6)
    ambient = read_sensors_atomic(syms, ports["recv"])
    send_burst([f"mww 0x{syms['g_can_led_mode']:08x} 5"], ports["emit"])
    print(f"Ambient: {ambient}")

    threading.Thread(target=sensor_loop, args=(syms, ports, W, pair_map, ambient), daemon=True).start()

    httpd = socketserver.TCPServer(("", args.http_port), VizHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def main_async():
        await websockets.serve(ws_handler, "0.0.0.0", args.ws_port)
        print(f"\nJoint Visualizer v2 ready: http://<pi-ip>:{args.http_port}  ws:{args.ws_port}")
        print(f"  M: {'loaded' if W is not None else 'none (raw)'}  pair_map={pair_map}  10 Hz")
        await asyncio.Future()

    try:
        loop.run_until_complete(main_async())
    except KeyboardInterrupt:
        print("\nShutting down; LED off...")
        send_burst([f"mww 0x{syms['g_can_led_mode']:08x} 0"], ports["emit"])
        httpd.shutdown()

if __name__ == "__main__":
    main()
