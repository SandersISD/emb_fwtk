#!/usr/bin/env python3
"""
manage_debug — USB arbitration & debug session manager for emb_fwtk.

Usage:
  manage_debug list [--json]
  manage_debug status [--json]
  manage_debug mode ocd [--force]
  manage_debug mode remote [--force]
  manage_debug flash <target> <elf> [--force]
  manage_debug stop [--force]
  manage_debug rescan [--json]

Modes are mutually exclusive (ocd-session or ozone-session).
A lockfile prevents concurrent operations.
Exit codes: 0=ok, 1=error, 2=busy (lock held).
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import yaml
from pathlib import Path
from typing import Any, Optional

# ─── Constants ───────────────────────────────────────────────────────────────

LOCKFILE = "/tmp/manage_debug.lock"
JLINK_DIR = "/opt/SEGGER/JLink"
OPENOCD_BIN = "/usr/bin/openocd"
JLINK_EXE = os.path.join(JLINK_DIR, "JLinkExe")
JLINK_REMOTE = os.path.join(JLINK_DIR, "JLinkRemoteServerCLExe")
JLINK_GDB = os.path.join(JLINK_DIR, "JLinkGDBServerExe")
DEFAULT_CONFIG = "probes.yaml"

# Port base values
BASE_TELNET = 4444
BASE_GDB = 3333
BASE_REMOTE = 19020

# paths for JLink tarball presence check
JLINK_TGZ = "/tmp/JLink_Linux_arm64.tgz"

# ─── Helpers ─────────────────────────────────────────────────────────────────


def eprint(*args: Any, **kwargs: Any) -> None:
    """Print to stderr (default output)."""
    print(*args, file=sys.stderr, **kwargs)


def json_or_text(data: dict, use_json: bool) -> str:
    """Return JSON or human-readable text for the given data dict."""
    if use_json:
        return json.dumps(data, indent=2, default=str)
    return data.get("_text", str(data))


def check_jlink_installed() -> bool:
    """Check if JLink binaries exist."""
    return os.path.isfile(JLINK_EXE) and os.path.isfile(JLINK_REMOTE)


def check_openocd_installed() -> bool:
    """Check if OpenOCD is installed."""
    return os.path.isfile(OPENOCD_BIN) and os.access(OPENOCD_BIN, os.X_OK)


def port_in_use(port: int) -> bool:
    """Check if a TCP port is already in use on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return False
        except OSError:
            return True


def detect_probes_via_usb() -> list[dict]:
    """Probe USB for J-Link devices. Returns list of {serial, path}."""
    probes = []
    # Try JLinkExe -USB for exact detection
    try:
        r = subprocess.run(
            [JLINK_EXE, "-USB", "-m"],
            capture_output=True, text=True, timeout=10,
            env={**os.environ, "JLINK_SILENT": "1"},
        )
        # Parse output for serial numbers
        for line in r.stdout.splitlines():
            if "Serial" in line or "SN" in line:
                parts = line.strip().split()
                for p in parts:
                    if p.isdigit() and len(p) > 6:
                        probes.append({"serial": p, "source": "JLinkExe"})
                        break
    except (FileNotFoundError, subprocess.TimeoutExpired, subprocess.CalledProcessError):
        pass

    # Fallback: lsusb
    if not probes:
        try:
            r = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=5)
            for line in r.stdout.splitlines():
                if "J-Link" in line or "SEGGER" in line:
                    probes.append({"serial": "unknown", "source": "lsusb", "raw": line.strip()})
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return probes


# ─── Config Loading ──────────────────────────────────────────────────────────


def load_config(path: str = DEFAULT_CONFIG) -> dict:
    """Load and validate probes.yaml."""
    if not os.path.isfile(path):
        return {"probes": {}, "port_offset": 0}

    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
    except (yaml.YAMLError, OSError) as e:
        eprint(f"ERROR: Failed to parse {path}: {e}")
        sys.exit(1)

    if not isinstance(cfg, dict):
        eprint(f"ERROR: {path} is not a valid YAML dict")
        sys.exit(1)

    cfg.setdefault("port_offset", 0)
    cfg.setdefault("probes", {})

    # Validate each probe
    for name, probe in cfg["probes"].items():
        if not isinstance(probe, dict):
            eprint(f"ERROR: probe '{name}' is not a dict")
            sys.exit(1)
        if "serial" not in probe:
            eprint(f"ERROR: probe '{name}' missing 'serial'")
            sys.exit(1)
        probe.setdefault("openocd_config", "")
        probe.setdefault("role", "unspecified")
        probe.setdefault("ports", {})

    return cfg


def auto_assign_ports(cfg: dict) -> dict:
    """Assign ports to each probe, applying auto-assignment and overrides."""
    offset = cfg.get("port_offset", 0)
    probes = cfg.get("probes", {})
    assigned = {}

    for i, (name, probe) in enumerate(probes.items()):
        pcfg = probe.get("ports", {})
        telnet = pcfg.get("telnet", BASE_TELNET + i + offset)
        gdb = pcfg.get("gdb", BASE_GDB + i + offset)
        remote = pcfg.get("remote", BASE_REMOTE + i + offset)
        assigned[name] = {
            "serial": probe["serial"],
            "role": probe.get("role", "unspecified"),
            "openocd_config": probe.get("openocd_config", ""),
            "telnet": telnet,
            "gdb": gdb,
            "remote": remote,
        }

    return assigned


def check_port_collisions(assigned: dict) -> list[str]:
    """Check for port collisions. Returns list of warnings."""
    warnings = []
    seen = {}
    for name, info in assigned.items():
        for port_type in ("telnet", "gdb", "remote"):
            port = info[port_type]
            if port in seen:
                warnings.append(
                    f"WARNING: port {port} ({port_type}) assigned to both "
                    f"'{seen[port]}' and '{name}'"
                )
            else:
                seen[port] = name
        # Check if port is actually in use on the host
        for port_type in ("telnet", "gdb", "remote"):
            port = info[port_type]
            if port_in_use(port):
                warnings.append(
                    f"WARNING: port {port} ({port_type} for '{name}') is already in use. "
                    f"Consider increasing port_offset in probes.yaml."
                )
    return warnings


# ─── Lock Management ─────────────────────────────────────────────────────────


def _lock_pid_alive(lock_data: dict) -> bool:
    """Check if the PID holding the lock is still alive."""
    pid = lock_data.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(force: bool = False) -> bool:
    """Acquire the operation lock. Returns True if acquired.

    A lock held by a DEAD process is treated as stale and reclaimed
    automatically (docker exec processes are ephemeral). A lock held by a
    live process blocks, unless force=True.
    """
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}

        held_by_live_process = _lock_pid_alive(data)

        if force or not held_by_live_process:
            state = "live" if held_by_live_process else "stale"
            eprint(f"WARNING: Breaking {state} lock held by PID {data.get('pid', '?')}")
            release_lock()
        else:
            eprint(
                f"ERROR: Operation lock held by live PID {data.get('pid', '?')} "
                f"(command: {data.get('command', '?')}, "
                f"since: {data.get('timestamp', '?')})"
            )
            eprint("Use --force to break the lock.")
            return False

    lock_data = {
        "pid": os.getpid(),
        "command": " ".join(sys.argv),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "probes": [],
    }
    try:
        with open(LOCKFILE, "w") as f:
            json.dump(lock_data, f, indent=2)
    except OSError as e:
        eprint(f"ERROR: Cannot write lockfile: {e}")
        return False
    return True


def release_lock() -> None:
    """Release the session lock."""
    try:
        if os.path.exists(LOCKFILE):
            os.remove(LOCKFILE)
    except OSError:
        pass


def lock_held() -> bool:
    """Check if lock is held."""
    return os.path.exists(LOCKFILE)


# ─── Process Management ──────────────────────────────────────────────────────


class ProcessManager:
    """Manages background daemon processes for OpenOCD / JLinkRemoteServer.

    IMPORTANT DESIGN CONSTRAINT:
    manage_debug is invoked via `docker exec` — a NEW process each time. Popen
    handles held in memory die with that process. Therefore discovery and
    control of already-running daemons MUST go through the process table
    (pgrep), not in-memory handles. A new PM instance starts empty and
    discovers daemons by name + argument matching.

    Daemons are started detached (start_new_session=True) so they survive
    manage_debug's exit and are re-parented to PID 1 in the container.
    """

    def __init__(self):
        self.processes: dict[str, subprocess.Popen] = {}  # only Popen'd this run

    # ── discovery ────────────────────────────────────────────────────────────

    @staticmethod
    def _pgrep(pattern: str) -> list[int]:
        """Return PIDs matching a pgrep pattern (full command line)."""
        try:
            r = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=5,
            )
            return [int(p) for p in r.stdout.split() if p.strip().isdigit()]
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            return []

    def find_daemons(self) -> dict[str, dict]:
        """Find running openocd / JLinkRemoteServerCLExe daemons in the
        container's PID namespace, keyed by probe name when identifiable.

        Matching is by full command line:
          openocd ... -f /path/to/openocd/board-a.cfg ...
          JLinkRemoteServerCLExe -select usb=SERIAL -port N
        """
        daemons = {}

        # OpenOCD instances — identify probe by its config path
        for pid in self._pgrep("openocd"):
            try:
                with open(f"/proc/{pid}/cmdline") as f:
                    argv = f.read().split("\0")
            except OSError:
                continue
            if not argv or "openocd" not in os.path.basename(argv[0]):
                # pgrep -f matches manage_debug's own command line too; skip
                continue
            # Extract probe name from config path: openocd/board-a.cfg → board-a
            name = None
            for a in argv:
                if a.endswith(".cfg") and "board" in os.path.basename(a):
                    name = os.path.splitext(os.path.basename(a))[0]
                    break
            daemons[name or f"openocd-{pid}"] = {
                "pid": pid, "type": "openocd", "argv": argv,
            }

        # JLinkRemoteServer instances — identify probe by serial or port
        for pid in self._pgrep("JLinkRemoteServerCLExe"):
            try:
                with open(f"/proc/{pid}/cmdline") as f:
                    argv = f.read().split("\0")
            except OSError:
                continue
            serial = port = None
            for i, a in enumerate(argv):
                if a == "-select" and i + 1 < len(argv):
                    serial = argv[i + 1].replace("usb=", "")
                elif a == "-port" and i + 1 < len(argv):
                    port = argv[i + 1]
            name = f"remote-{serial or port or pid}"
            daemons[name] = {
                "pid": pid, "type": "jlink-remote",
                "argv": argv, "serial": serial, "port": port,
            }

        return daemons

    # ── start ────────────────────────────────────────────────────────────────

    def start_openocd(self, name: str, config_path: str, telnet_port: int,
                      gdb_port: int, serial: str) -> bool:
        """Start OpenOCD (detached) for one probe."""
        if not os.path.isfile(config_path):
            eprint(f"ERROR: OpenOCD config not found: {config_path}")
            return False

        cmd = [
            OPENOCD_BIN,
            "-f", config_path,
            "-c", f"telnet_port {telnet_port}",
            "-c", f"gdb_port {gdb_port}",
            "-c", "tcl_port disabled",
        ]
        if serial:
            cmd += ["-c", f"adapter serial {serial}"]

        log_path = f"/tmp/openocd-{name}.log"
        try:
            with open(log_path, "w") as log:
                subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # detach: survive parent exit
                )
            # Give OpenOCD a moment to fail on config errors before reporting OK
            time.sleep(1.0)
            running = bool(self._pgrep(" ".join(cmd[:4])))
            if not running:
                self._report_log(log_path, name)
                return False
            return True
        except FileNotFoundError:
            eprint(f"ERROR: OpenOCD not found at {OPENOCD_BIN}")
            return False
        except OSError as e:
            eprint(f"ERROR: Failed to start OpenOCD for '{name}': {e}")
            return False

    def start_remote(self, name: str, serial: str, port: int) -> bool:
        """Start JLinkRemoteServerCLExe (detached) for one probe."""
        if not os.path.isfile(JLINK_REMOTE):
            eprint(f"ERROR: JLinkRemoteServer not found at {JLINK_REMOTE}")
            return False

        cmd = [JLINK_REMOTE, "-select", f"usb={serial}", "-port", str(port)]
        log_path = f"/tmp/jlink-remote-{name}.log"

        try:
            with open(log_path, "w") as log:
                subprocess.Popen(
                    cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            time.sleep(1.5)
            running = bool(self._pgrep("JLinkRemoteServerCLExe"))
            if not running:
                self._report_log(log_path, name)
                return False
            return True
        except FileNotFoundError:
            eprint(f"ERROR: JLinkRemoteServer not found at {JLINK_REMOTE}")
            return False
        except OSError as e:
            eprint(f"ERROR: Failed to start JLinkRemoteServer for '{name}': {e}")
            return False

    @staticmethod
    def _report_log(log_path: str, name: str) -> None:
        """Print the tail of a daemon log after startup failure."""
        eprint(f"ERROR: daemon for '{name}' exited immediately. Log tail:")
        try:
            with open(log_path) as f:
                lines = f.read().splitlines()
            for line in lines[-15:]:
                eprint(f"  | {line}")
        except OSError:
            eprint(f"  (no log at {log_path})")

    # ── stop ─────────────────────────────────────────────────────────────────

    def stop_all(self) -> None:
        """Stop ALL openocd / JLinkRemoteServer daemons in this container
        (discovered via process table), plus any Popen'd this run."""
        killed = []
        # 1) daemons from the process table
        for name, info in self.find_daemons().items():
            pid = info["pid"]
            try:
                os.kill(pid, signal.SIGTERM)
                # wait up to 3s for graceful exit
                deadline = time.time() + 3
                while time.time() < deadline:
                    try:
                        os.kill(pid, 0)  # raises if gone
                        time.sleep(0.1)
                    except OSError:
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as e:
                eprint(f"WARNING: failed to kill {name} (pid {pid}): {e}")
                continue
            killed.append(f"{name}({info['type']})")

        # 2) any processes we Popen'd in this run (defensive)
        for name, proc in list(self.processes.items()):
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
            del self.processes[name]

        if killed:
            eprint(f"Stopped: {', '.join(killed)}")
            # USB device release is not instant — give the kernel 1.5s to
            # settle after the last daemon is killed before a new one claims it.
            time.sleep(1.5)

    # ── status ───────────────────────────────────────────────────────────────

    def get_status(self) -> dict[str, dict]:
        """Return discovered daemons: name -> {pid, type, status}."""
        status = {}
        for name, info in self.find_daemons().items():
            alive = True
            try:
                os.kill(info["pid"], 0)
            except OSError:
                alive = False
            status[name] = {
                "pid": info["pid"],
                "type": info["type"],
                "status": "running" if alive else "dead",
                **({"serial": info["serial"], "port": info["port"]}
                   if info.get("type") == "jlink-remote" else {}),
            }
        return status


# ─── Subcommand Implementations ──────────────────────────────────────────────


def cmd_list(args: argparse.Namespace, cfg: dict) -> dict:
    """List detected probes and their assigned ports."""
    assigned = auto_assign_ports(cfg)
    probes = detect_probes_via_usb()
    config_probes = list(assigned.keys())

    result = {
        "_text": "=== Probes ===\n",
        "detected_usb": probes,
        "configured": {},
    }

    for name, info in assigned.items():
        port_info = (
            f"  telnet={info['telnet']}  gdb={info['gdb']}  "
            f"remote={info['remote']}"
        )
        role_info = f"  role={info['role']}"
        result["_text"] += (
            f"\n{name}:\n"
            f"  serial={info['serial']}\n"
            f"{port_info}\n"
            f"{role_info}\n"
        )
        result["configured"][name] = info

    if probes:
        result["_text"] += f"\nUSB-detected: {len(probes)} J-Link probe(s)\n"
        for p in probes:
            result["_text"] += f"  {p}\n"
    else:
        result["_text"] += "\nNo J-Link probes detected via USB\n"

    if not config_probes:
        result["_text"] += "\nNo probes configured in probes.yaml\n"

    return result


def cmd_status(args: argparse.Namespace, cfg: dict, pm: ProcessManager) -> dict:
    """Show current session status."""
    assigned = auto_assign_ports(cfg)
    proc_status = pm.get_status()

    # Detect active session type from discovered daemons
    types = {s["type"] for s in proc_status.values() if s["status"] == "running"}
    if "openocd" in types:
        session_type = "ocd"
    elif "jlink-remote" in types:
        session_type = "remote"
    else:
        session_type = "none"

    lock_info = None
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE) as f:
                lock_info = json.load(f)
        except (json.JSONDecodeError, OSError):
            lock_info = {"pid": "?", "command": "?", "timestamp": "?"}

    result = {
        "_text": "=== Session Status ===\n",
        "session_type": session_type,
        "running_processes": proc_status,
        "lock": lock_info,
        "configured_probes": assigned,
    }

    result["_text"] += f"\nSession: {session_type}\n"

    if lock_info:
        result["_text"] += (
            f"\nLock held by PID {lock_info['pid']}\n"
            f"  command: {lock_info['command']}\n"
            f"  since: {lock_info['timestamp']}\n"
        )
    else:
        result["_text"] += "\nNo lock held\n"

    running = [n for n, s in proc_status.items() if s["status"] == "running"]
    result["_text"] += f"\nRunning daemons: {len(running)} active\n"
    for name, status in proc_status.items():
        extra = ""
        if status.get("type") == "jlink-remote":
            extra = f"  (serial={status.get('serial')}, port={status.get('port')})"
        result["_text"] += f"  {name}: {status['status']} [{status['type']}]{extra}\n"

    if not running:
        result["_text"] += "  (none)\n"

    return result


def cmd_mode_ocd(args: argparse.Namespace, cfg: dict, pm: ProcessManager) -> dict:
    """Start OpenOCD on all configured probes."""
    assigned = auto_assign_ports(cfg)
    warnings = check_port_collisions(assigned)

    # Resolve config paths
    config_dir = os.path.dirname(os.path.abspath(args.config or DEFAULT_CONFIG))

    results = {}
    for name, info in assigned.items():
        cfg_path = info["openocd_config"]
        if not cfg_path:
            # Try default: openocd/{name}.cfg relative to config dir
            cfg_path = os.path.join(config_dir, "openocd", f"{name}.cfg")
            if not os.path.isfile(cfg_path):
                # Try the config dir itself
                alt = os.path.join(config_dir, f"{name}.cfg")
                if os.path.isfile(alt):
                    cfg_path = alt
                else:
                    results[name] = f"error: no openocd_config specified and no default found"
                    continue
        elif not os.path.isabs(cfg_path):
            cfg_path = os.path.join(config_dir, cfg_path)

        ok = pm.start_openocd(
            name=name,
            config_path=cfg_path,
            telnet_port=info["telnet"],
            gdb_port=info["gdb"],
            serial=info["serial"],
        )
        results[name] = "ok" if ok else "failed"

    result = {
        "_text": "=== OpenOCD Session ===\n",
        "mode": "ocd",
        "results": results,
        "warnings": warnings,
    }

    result["_text"] += f"\nResults:\n"
    for name, status in results.items():
        result["_text"] += f"  {name}: {status}\n"
    for w in warnings:
        result["_text"] += f"\n{w}\n"

    # Update lock with probe info
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE) as f:
                lock_data = json.load(f)
            lock_data["probes"] = list(assigned.keys())
            lock_data["mode"] = "ocd"
            with open(LOCKFILE, "w") as f:
                json.dump(lock_data, f, indent=2)
        except (OSError, json.JSONDecodeError):
            pass

    return result


def cmd_mode_remote(args: argparse.Namespace, cfg: dict, pm: ProcessManager) -> dict:
    """Start JLinkRemoteServer on all configured probes."""
    assigned = auto_assign_ports(cfg)
    warnings = check_port_collisions(assigned)

    if not check_jlink_installed():
        return {
            "_text": "ERROR: J-Link software not installed.\n"
                     "Download from https://www.segger.com/downloads/jlink/ and\n"
                     "place JLink_Linux_arm64.tgz in the repo root, then rebuild.\n",
            "mode": "remote",
            "error": "JLink not installed",
            "results": {},
        }

    results = {}
    for name, info in assigned.items():
        ok = pm.start_remote(
            name=name,
            serial=info["serial"],
            port=info["remote"],
        )
        results[name] = "ok" if ok else "failed"

    result = {
        "_text": "=== Ozone Session ===\n",
        "mode": "remote",
        "results": results,
        "warnings": warnings,
    }

    result["_text"] += f"\nResults:\n"
    for name, status in results.items():
        result["_text"] += f"  {name}: {status}\n"
        if status == "ok":
            result["_text"] += (
                f"    Connect Ozone to: <pi-ip>:{info['remote']}\n"
            )
    for w in warnings:
        result["_text"] += f"\n{w}\n"

    # Update lock
    if os.path.exists(LOCKFILE):
        try:
            with open(LOCKFILE) as f:
                lock_data = json.load(f)
            lock_data["probes"] = list(assigned.keys())
            lock_data["mode"] = "remote"
            with open(LOCKFILE, "w") as f:
                json.dump(lock_data, f, indent=2)
        except (OSError, json.JSONDecodeError):
            pass

    return result


def cmd_flash(args: argparse.Namespace, cfg: dict, pm: ProcessManager) -> dict:
    """Flash firmware to a single target board."""
    target = args.target
    elf_path = args.elf

    assigned = auto_assign_ports(cfg)

    if target not in assigned:
        return {
            "_text": f"ERROR: Unknown target '{target}'. "
                     f"Configured targets: {', '.join(assigned.keys())}\n",
            "error": f"unknown target: {target}",
        }

    info = assigned[target]
    config_dir = os.path.dirname(os.path.abspath(args.config or DEFAULT_CONFIG))
    cfg_path = info["openocd_config"]
    if not cfg_path:
        cfg_path = os.path.join(config_dir, "openocd", f"{target}.cfg")
    elif not os.path.isabs(cfg_path):
        cfg_path = os.path.join(config_dir, cfg_path)

    if not os.path.isfile(elf_path):
        return {
            "_text": f"ERROR: ELF file not found: {elf_path}\n",
            "error": f"file not found: {elf_path}",
        }

    # Stop any running services first
    pm.stop_all()

    # Flash sequence: init -> halt -> program -> verify -> reset -> shutdown
    flash_cmd = [
        OPENOCD_BIN,
        "-f", cfg_path,
        "-c", "tcl_port disabled",
        "-c", "gdb_port disabled",
        "-c", "telnet_port disabled",
        "-c", "init",
        "-c", "halt",
        "-c", f"program {elf_path} verify 0x08000000",
        "-c", "reset run",
        "-c", "shutdown",
    ]
    if info["serial"]:
        flash_cmd.insert(3, "-c")
        flash_cmd.insert(4, f"adapter serial {info['serial']}")

    try:
        eprint(f"Flashing {target} ({elf_path})...")
        r = subprocess.run(
            flash_cmd,
            capture_output=True, text=True, timeout=120,
        )
        output = r.stdout + r.stderr
        success = "Verified OK" in output or r.returncode == 0

        result = {
            "_text": f"=== Flash {target} ===\n\n",
            "target": target,
            "elf": elf_path,
            "success": success,
            "returncode": r.returncode,
            "output": output,
        }

        if success:
            result["_text"] += f"SUCCESS: {target} flashed and verified\n"
        else:
            result["_text"] += f"FAILED (exit {r.returncode}):\n{output}\n"

        return result

    except subprocess.TimeoutExpired:
        return {
            "_text": f"ERROR: Flash timed out (120s) for {target}\n",
            "error": "timeout",
            "target": target,
        }
    except FileNotFoundError:
        return {
            "_text": f"ERROR: OpenOCD not found at {OPENOCD_BIN}\n",
            "error": "OpenOCD not found",
        }


def cmd_stop(args: argparse.Namespace, cfg: dict, pm: ProcessManager) -> dict:
    """Stop all debug services."""
    pm.stop_all()
    release_lock()

    result = {
        "_text": "=== Stopped ===\n\nAll debug services stopped. Lock released.\n",
        "status": "stopped",
    }
    return result


def cmd_rescan(args: argparse.Namespace, cfg: dict) -> dict:
    """Re-probe USB for J-Link devices."""
    probes = detect_probes_via_usb()

    result = {
        "_text": "=== USB Rescan ===\n\n",
        "detected_probes": probes,
    }

    if probes:
        result["_text"] += f"Found {len(probes)} J-Link probe(s):\n"
        for p in probes:
            result["_text"] += f"  {p}\n"
    else:
        result["_text"] += "No J-Link probes detected.\n"
        result["_text"] += (
            "\nIf probes are physically connected but not detected:\n"
            "  1. Check USB cable\n"
            "  2. Run `lsusb` to verify the device is visible\n"
            "  3. If visible but stale, run `docker restart emb-fwtk` to re-bind USB\n"
        )

    return result


# ─── Main ────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="emb_fwtk Debug Session Manager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  manage_debug list
  manage_debug mode ocd
  manage_debug mode remote --force
  manage_debug flash board-a /workspace/firmware.elf
  manage_debug stop
  manage_debug list --json
        """,
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="Path to probes.yaml (default: probes.yaml)")
    parser.add_argument("--json", action="store_true", dest="use_json",
                        help="Output JSON instead of human-readable text")

    sub = parser.add_subparsers(dest="subcommand", required=True)

    # list
    p_list = sub.add_parser("list", help="List detected probes and ports")

    # status
    p_status = sub.add_parser("status", help="Show session status")

    # mode
    p_mode = sub.add_parser("mode", help="Set debug mode (ocd or remote)")
    p_mode.add_argument("mode", choices=["ocd", "remote"],
                        help="ocd = OpenOCD, remote = JLinkRemoteServer")
    p_mode.add_argument("--force", action="store_true",
                        help="Break any existing lock")

    # flash
    p_flash = sub.add_parser("flash", help="Flash firmware to a target board")
    p_flash.add_argument("target", help="Probe name (from probes.yaml)")
    p_flash.add_argument("elf", help="Path to firmware .elf file")
    p_flash.add_argument("--force", action="store_true",
                         help="Break any existing lock")

    # stop
    p_stop = sub.add_parser("stop", help="Stop all debug services")
    p_stop.add_argument("--force", action="store_true",
                        help="Break any existing lock")

    # rescan
    p_rescan = sub.add_parser("rescan", help="Re-probe USB for J-Link devices")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # Load config
    cfg = load_config(args.config)

    # Determine if this command needs the lock
    locking_commands = {"mode", "flash", "stop"}
    needs_lock = args.subcommand in locking_commands

    # Acquire lock if needed
    if needs_lock:
        force = getattr(args, "force", False)
        if not acquire_lock(force):
            return 2  # busy

    try:
        # Create process manager
        pm = ProcessManager()

        # Dispatch
        if args.subcommand == "list":
            result = cmd_list(args, cfg)
        elif args.subcommand == "status":
            result = cmd_status(args, cfg, pm)
        elif args.subcommand == "mode":
            if args.mode == "ocd":
                # Stop any existing processes first
                pm.stop_all()
                result = cmd_mode_ocd(args, cfg, pm)
            else:
                pm.stop_all()
                result = cmd_mode_remote(args, cfg, pm)
        elif args.subcommand == "flash":
            result = cmd_flash(args, cfg, pm)
        elif args.subcommand == "stop":
            result = cmd_stop(args, cfg, pm)
        elif args.subcommand == "rescan":
            result = cmd_rescan(args, cfg)
        else:
            eprint(f"ERROR: Unknown subcommand: {args.subcommand}")
            return 1

        # Output
        output = json_or_text(result, args.use_json)
        # For JSON, write to stdout; for text, write to stderr (so stdout is clean for agents)
        if args.use_json:
            print(output)
        else:
            eprint(output)

        # Check for errors in result
        if result.get("error") or any(
            v == "failed" for v in result.get("results", {}).values()
        ):
            return 1

        return 0

    except KeyboardInterrupt:
        eprint("\nInterrupted.")
        return 1
    except Exception as e:
        eprint(f"ERROR: {e}")
        return 1
    finally:
        # Lock semantics:
        # - mode ocd / mode remote: the lockfile IS the session state — keep it
        #   (daemons keep running after this process exits). The lock records
        #   the active mode; `stop` clears it.
        # - flash / stop: transient operations — always release on exit.
        if needs_lock and args.subcommand in ("flash", "stop"):
            release_lock()


if __name__ == "__main__":
    sys.exit(main())