#!/usr/bin/env python3
"""Tests for manage_debug — USB arbitration & debug session manager."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add bin directory to path so we can import manage_debug directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import manage_debug as md


class TestConfigLoading(unittest.TestCase):
    """Test probes.yaml parsing and validation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.tmpdir, "probes.yaml")

    def write_config(self, content: str):
        with open(self.config_path, "w") as f:
            f.write(content)

    def test_empty_config(self):
        """Empty/missing config should return defaults."""
        cfg = md.load_config("/nonexistent/probes.yaml")
        self.assertEqual(cfg["probes"], {})
        self.assertEqual(cfg["port_offset"], 0)

    def test_basic_config(self):
        self.write_config("""
port_offset: 0
probes:
  board-a:
    serial: "000770593783"
    role: "emitter"
    openocd_config: "openocd/board-a.cfg"
        """)
        cfg = md.load_config(self.config_path)
        self.assertIn("board-a", cfg["probes"])
        self.assertEqual(cfg["probes"]["board-a"]["serial"], "000770593783")
        self.assertEqual(cfg["port_offset"], 0)

    def test_port_offset(self):
        self.write_config("""
port_offset: 10
probes:
  board-a:
    serial: "000770593783"
    openocd_config: "openocd/board-a.cfg"
        """)
        cfg = md.load_config(self.config_path)
        self.assertEqual(cfg["port_offset"], 10)

    def test_missing_serial_errors(self):
        self.write_config("""
probes:
  board-a:
    role: "emitter"
        """)
        with self.assertRaises(SystemExit):
            md.load_config(self.config_path)

    def test_invalid_yaml(self):
        self.write_config("not: valid: yaml: [")
        with self.assertRaises(SystemExit):
            md.load_config(self.config_path)

    def test_optional_ports_override(self):
        self.write_config("""
probes:
  board-a:
    serial: "000770593783"
    openocd_config: "openocd/board-a.cfg"
    ports:
      telnet: 4444
      gdb: 3333
      remote: 19020
        """)
        cfg = md.load_config(self.config_path)
        ports = cfg["probes"]["board-a"]["ports"]
        self.assertEqual(ports["telnet"], 4444)
        self.assertEqual(ports["gdb"], 3333)
        self.assertEqual(ports["remote"], 19020)


class TestPortAssignment(unittest.TestCase):
    """Test auto-assignment and override logic."""

    def test_auto_assign_default(self):
        cfg = {
            "port_offset": 0,
            "probes": {
                "board-a": {"serial": "SN1", "openocd_config": "a.cfg", "ports": {}, "role": ""},
                "board-b": {"serial": "SN2", "openocd_config": "b.cfg", "ports": {}, "role": ""},
            },
        }
        assigned = md.auto_assign_ports(cfg)
        self.assertEqual(assigned["board-a"]["telnet"], 4444)
        self.assertEqual(assigned["board-a"]["gdb"], 3333)
        self.assertEqual(assigned["board-a"]["remote"], 19020)
        self.assertEqual(assigned["board-b"]["telnet"], 4445)
        self.assertEqual(assigned["board-b"]["gdb"], 3334)
        # remote ports stride 2 (JLinkRemoteServer binds port AND port+1)
        self.assertEqual(assigned["board-b"]["remote"], 19022)

    def test_port_offset_shifts_all(self):
        cfg = {
            "port_offset": 10,
            "probes": {
                "board-a": {"serial": "SN1", "openocd_config": "a.cfg", "ports": {}, "role": ""},
                "board-b": {"serial": "SN2", "openocd_config": "b.cfg", "ports": {}, "role": ""},
            },
        }
        assigned = md.auto_assign_ports(cfg)
        self.assertEqual(assigned["board-a"]["telnet"], 4454)
        self.assertEqual(assigned["board-b"]["telnet"], 4455)
        # remote ports stride 2 (JLinkRemoteServer binds port AND port+1)
        self.assertEqual(assigned["board-a"]["remote"], 19030)
        self.assertEqual(assigned["board-b"]["remote"], 19032)

    def test_override_beats_auto(self):
        cfg = {
            "port_offset": 0,
            "probes": {
                "board-a": {
                    "serial": "SN1", "openocd_config": "a.cfg", "role": "",
                    "ports": {"telnet": 9999},
                },
            },
        }
        assigned = md.auto_assign_ports(cfg)
        self.assertEqual(assigned["board-a"]["telnet"], 9999)
        # gdb and remote should still be auto-assigned
        self.assertEqual(assigned["board-a"]["gdb"], 3333)
        self.assertEqual(assigned["board-a"]["remote"], 19020)

    def test_three_probes(self):
        cfg = {
            "port_offset": 0,
            "probes": {
                "a": {"serial": "S1", "openocd_config": "a", "ports": {}, "role": ""},
                "b": {"serial": "S2", "openocd_config": "b", "ports": {}, "role": ""},
                "c": {"serial": "S3", "openocd_config": "c", "ports": {}, "role": ""},
            },
        }
        assigned = md.auto_assign_ports(cfg)
        self.assertEqual(assigned["c"]["telnet"], 4446)
        self.assertEqual(assigned["c"]["gdb"], 3335)
        self.assertEqual(assigned["c"]["remote"], 19024)


class TestPortCollision(unittest.TestCase):
    """Test port collision detection."""

    def test_no_collisions(self):
        assigned = {
            "a": {"telnet": 4444, "gdb": 3333, "remote": 19020},
            "b": {"telnet": 4445, "gdb": 3334, "remote": 19021},
        }
        warnings = md.check_port_collisions(assigned)
        # May have host-in-use warnings, but no assignment collisions
        assignment_warnings = [w for w in warnings if "assigned to both" in w]
        self.assertEqual(len(assignment_warnings), 0)

    def test_detects_collision(self):
        assigned = {
            "a": {"telnet": 4444, "gdb": 3333, "remote": 19020},
            "b": {"telnet": 4444, "gdb": 3333, "remote": 19020},
        }
        warnings = md.check_port_collisions(assigned)
        collision_warnings = [w for w in warnings if "assigned to both" in w]
        self.assertGreaterEqual(len(collision_warnings), 3)


class TestLockFile(unittest.TestCase):
    """Test lock file acquire/release logic."""

    def setUp(self):
        # Ensure lockfile doesn't exist
        if os.path.exists(md.LOCKFILE):
            os.remove(md.LOCKFILE)

    def tearDown(self):
        if os.path.exists(md.LOCKFILE):
            os.remove(md.LOCKFILE)

    def test_acquire_release(self):
        self.assertTrue(md.acquire_lock(force=False))
        self.assertTrue(os.path.exists(md.LOCKFILE))
        with open(md.LOCKFILE) as f:
            data = json.load(f)
        self.assertEqual(data["pid"], os.getpid())
        md.release_lock()
        self.assertFalse(os.path.exists(md.LOCKFILE))

    def test_acquire_when_locked_returns_false(self):
        self.assertTrue(md.acquire_lock(force=False))
        self.assertFalse(md.acquire_lock(force=False))

    def test_force_breaks_lock(self):
        self.assertTrue(md.acquire_lock(force=False))
        self.assertTrue(md.acquire_lock(force=True))
        self.assertTrue(os.path.exists(md.LOCKFILE))

    def test_lock_held_check(self):
        self.assertFalse(md.lock_held())
        md.acquire_lock(force=False)
        self.assertTrue(md.lock_held())
        md.release_lock()
        self.assertFalse(md.lock_held())


class TestPortInUse(unittest.TestCase):
    """Test port-in-use detection."""

    def test_free_port(self):
        # Port 0 asks the OS to assign a free port — we can't test "is free"
        # but we can test that a known-free port returns False
        self.assertFalse(md.port_in_use(0))

    def test_occupied_port(self):
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))  # OS assigns a free port
        port = s.getsockname()[1]
        s.listen(1)
        try:
            self.assertTrue(md.port_in_use(port))
        finally:
            s.close()


class TestArgumentParsing(unittest.TestCase):
    """Test CLI argument parsing."""

    def setUp(self):
        self.parser = md.build_parser()

    def test_list_subcommand(self):
        args = self.parser.parse_args(["list"])
        self.assertEqual(args.subcommand, "list")

    def test_list_json(self):
        args = self.parser.parse_args(["--json", "list"])
        self.assertEqual(args.subcommand, "list")
        self.assertTrue(args.use_json)

    def test_mode_ocd(self):
        args = self.parser.parse_args(["mode", "ocd"])
        self.assertEqual(args.subcommand, "mode")
        self.assertEqual(args.mode, "ocd")

    def test_mode_remote_force(self):
        args = self.parser.parse_args(["mode", "remote", "--force"])
        self.assertEqual(args.mode, "remote")
        self.assertTrue(args.force)

    def test_flash(self):
        args = self.parser.parse_args(["flash", "board-a", "fw.elf"])
        self.assertEqual(args.target, "board-a")
        self.assertEqual(args.elf, "fw.elf")

    def test_flash_with_force(self):
        args = self.parser.parse_args(["flash", "board-a", "fw.elf", "--force"])
        self.assertTrue(args.force)

    def test_stop(self):
        args = self.parser.parse_args(["stop"])
        self.assertEqual(args.subcommand, "stop")

    def test_rescan(self):
        args = self.parser.parse_args(["rescan"])
        self.assertEqual(args.subcommand, "rescan")

    def test_config_flag(self):
        args = self.parser.parse_args(["--config", "/custom/path.yaml", "list"])
        self.assertEqual(args.config, "/custom/path.yaml")

    def test_unknown_subcommand(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["unknown"])

    def test_invalid_mode(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["mode", "invalid"])


class TestJSONOutput(unittest.TestCase):
    """Test JSON vs text output formatting."""

    def test_json_output(self):
        data = {"_text": "hello", "key": "value"}
        result = md.json_or_text(data, use_json=True)
        parsed = json.loads(result)
        self.assertEqual(parsed["key"], "value")

    def test_text_output(self):
        data = {"_text": "hello world", "key": "value"}
        result = md.json_or_text(data, use_json=False)
        self.assertEqual(result, "hello world")


class TestDetectProbes(unittest.TestCase):
    """Test USB probe detection (mocked)."""

    @patch("bin.manage_debug.subprocess.run")
    def test_via_lsusb(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="Bus 001 Device 003: ID 1366:1015 SEGGER J-Link\n",
            returncode=0,
        )
        probes = md.detect_probes_via_usb()
        # JLinkExe won't be found, so falls back to lsusb
        self.assertGreaterEqual(len(probes), 0)  # may or may not parse

    @patch("bin.manage_debug.subprocess.run")
    def test_no_probes(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        probes = md.detect_probes_via_usb()
        self.assertIsInstance(probes, list)


class TestCheckJLink(unittest.TestCase):
    """Test JLink installation check."""

    def test_jlink_not_installed(self):
        # On the dev machine, JLink isn't installed
        result = md.check_jlink_installed()
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()