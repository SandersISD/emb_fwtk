# emb_fwtk — Embedded Firmware Debug Toolkit

One Docker container, two debug modes, one USB arbiter. Run OpenOCD for
scripted/agent access **or** SEGGER J-Link Remote Server for GUI debugging
(Ozone), and switch between them without ever seeing `LIBUSB_ERROR_BUSY`.

Works with any J-Link-compatible probe and SWD target (built and battle-tested
with STM32G4 + two probes on a Raspberry Pi 5; see `examples/sca`).

## Why

Multi-probe embedded setups hit the same three walls:

1. **USB contention** — two debug servers, one USB device each, constant
   `LIBUSB_ERROR_BUSY` when both grab the same probe.
2. **Port collisions** — under `--network host`, every OpenOCD defaults to
   telnet 4444 / tcl 6666 and they stamp on each other.
3. **Human vs agent** — you want Ozone; your scripts/agents want telnet.
   Same probes, different tools.

`manage_debug` arbitrates: one session type at a time, per-probe unique
ports (`tcl_port disabled` always), stop-before-start with USB settle time,
lockfile against concurrent operators, JSON output for automation.

## Quick start

```bash
# 1. Get the J-Link pack (license click-through, one time)
#    https://www.segger.com/downloads/jlink/  → JLink_Linux_arm64.tgz
cp ~/Downloads/JLink_Linux_V*_arm64.tgz ./JLink_Linux_arm64.tgz

# 2. Describe your probes
cp probes.example.yaml probes.yaml      # edit serials
cp openocd/probe.example.cfg openocd/my-board.cfg

# 3. Build & run (ARM64 host: Pi, Jetson, ARM server)
docker build -t emb-fwtk .
docker run -d --rm --privileged --network host --init \
  -v /dev/bus/usb:/dev/bus/usb \
  -v $PWD:/cfg -v ~/myproject:/workspace \
  --name emb-fwtk emb-fwtk tail -f /dev/null

# 4. Debug
docker exec emb-fwtk manage_debug --config /cfg/probes.yaml mode ocd
docker exec emb-fwtk manage_debug --config /cfg/probes.yaml mode remote
```

## manage_debug reference

| Command | What it does | Locks |
|---|---|---|
| `list [--json]` | probes in config + USB-detected, assigned ports | no |
| `status [--json]` | active session type, daemons (pgrep-discovered), lock holder | no |
| `mode ocd` | stop all → start OpenOCD per probe (unique telnet/gdb, tcl disabled) | yes |
| `mode remote` | stop all → start JLinkRemoteServer per probe (stride-2 ports) | yes |
| `flash <probe> <elf>` | transient: stop → init/halt/program/verify/reset → stop | yes |
| `stop` | kill all daemons, release lock | yes |
| `rescan` | re-probe USB after hot-plug | no |

Exit codes: `0` ok, `1` error, `2` busy (lock held by a live process — dead
PIDs are auto-reclaimed; `--force` breaks live locks).

Ports auto-assign: telnet `4444+i`, gdb `3333+i`, remote `19020+2i`
(JLinkRemoteServer binds port **and** port+1 — stride 2 is not optional;
see `docs/known-quirks.md`). Override per probe or shift everything with
`port_offset`.

## Debugging from another machine (Ozone)

Three topologies, one container — full write-up in
[docs/debug-topologies.md](docs/debug-topologies.md):

1. **Probe server** (recommended): project + ELF on your machine, container
   on the probe host. Open the ELF locally, connect Ozone via
   `Project.SetHostIF("IP", "host:19020")` — through an SSH tunnel if not
   same-LAN.
2. **Everything on the probe host**: sources there, mount them (SMB /
   SSHFS-Win), and remap DWARF paths — either `-ffile-prefix-map` at build
   time or `Project.AddPathSubstitute()` in the `.jdebug` (Ozone's
   `set substitute-path` equivalent).
3. **Relayed / multi-VPN**: tunnel terminates on a bridge node with autossh;
   from your machine it's topology 1 again.

`examples/sca/boardA.jdebug` is a working project file: remote host, SVD,
FreeRTOS kernel awareness, path substitution for a container-built ELF.

## Repository layout

```
├── Dockerfile               ARM64 Debian 13 + OpenOCD + ARM GCC + J-Link
├── bin/manage_debug[.py]    session manager (std lib + PyYAML only)
├── probes.example.yaml      probe inventory template
├── openocd/probe.example.cfg  per-probe OpenOCD config template
├── docs/                    wiki: topologies, known quirks, design spec
├── examples/sca/            first user: RGB-sensing boards, scripts, .jdebug
└── tests/                   unit tests (no hardware needed)
```

## Testing

```bash
PYTHONPATH=bin python3 -m pytest tests/ -v     # 34 tests, hardware-free
```

## Requirements

- ARM64 Linux host (Docker), `--privileged` + `/dev/bus/usb` passthrough
- J-Link-compatible probes (ST-Link clones flashed with J-Link firmware work)
- SEGGER J-Link Software Pack — you download it; it is never committed
  (gitignored) and never redistributed here

## License

MIT — see [LICENSE](LICENSE). SEGGER's own license governs their software;
this repo ships none of it.
