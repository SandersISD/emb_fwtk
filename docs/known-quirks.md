# Known Quirks

Empirically paid-for lessons from deploying this on real hardware
(raspi5-03, two ST-Link V2 clones flashed as J-Link, FW V968, Ozone V3.30).
Each entry: symptom → root cause → fix that worked.

---

## JLinkRemoteServer binds port AND port+1

**Symptom:** second probe's Remote Server fails "Failed to put socket into
listening state" in a ~1 Hz retry loop; the first server works fine.

**Root cause:** each `JLinkRemoteServerCLExe` opens the configured port on
`0.0.0.0` **and** `port+1` on `127.0.0.1` (internal helper). Consecutive
per-probe ports collide: server-A@19020 silently grabs 19021, so
server-B@19021 can never bind.

**Fix:** space remote ports by 2. `manage_debug` auto-assigns
`19020 + 2*i` (`BASE_REMOTE + 2*i + port_offset`). Explicit config should
do the same (e.g. 19020 / 19022).

## USB claim is not instant after killing a daemon

**Symptom:** switching `mode remote → mode ocd` fails on the first probe with
`LIBUSB_ERROR_BUSY`, even though the previous daemon was just killed.

**Root cause:** the kernel releases the USB interface asynchronously; a
sub-second race between kill and re-claim.

**Fix:** `stop_all()` sleeps 1.5 s after killing daemons before starting the
new session's daemons.

## Old containers silently hold the probes

**Symptom:** fresh container sees probes in `lsusb` but JLinkExe/OpenOCD
can't connect ("Cannot connect to J-Link").

**Root cause:** another container (or host process) still holds the USB
device. `lsusb` shows the *bus topology*, not claim state.

**Fix:** `docker stop` every container that ever touched the probes before
starting a session. `manage_debug status` inside one container cannot see
other containers' processes — check `docker ps` on the host.

## No PID 1 reaper → zombie flood

**Symptom:** thousands of `[JLinkGUIServerExe] <defunct>` zombies on the host,
PID 1 in-container is `tail -f /dev/null`.

**Root cause:** SEGGER tools fork a GUI-server helper; in headless containers
it dies instantly and nothing reaps it. Each failed bind (see quirk 1) spawned
another. Basic `docker run` without an init does not reap orphans.

**Fix:** always run with `--init` (tini). Harmless zombies from ordinary
tool runs still occur occasionally; `--init` keeps them reaped.

## Non-root can't claim USB even with --privileged

**Symptom:** as `dev` (uid 1000, in `plugdev`), OpenOCD/JLink fail to open
`/dev/bus/usb/...`; as root in the same container it works.

**Root cause:** host udev rules set device nodes to `root:plugdev 0660`;
container groupadd'd plugdev gid may differ from the host's gid.

**Fix (quick):** `docker exec -u root` for mode switches. **Fix (proper):**
build with `--build-arg GID=$(getent group plugdev | cut -d: -f3)` matching
the host, or chmod the nodes on the host.

## OpenOCD mdw data words have no 0x prefix

**Symptom:** telnet `mdw` output parses to zero words in scripts.

**Root cause:** OpenOCD ≥0.12 prints the address with `0x` but data words as
bare hex (`0x20001f78: 0075004a ...`).

**Fix:** `int(tok, 16)` on every token after the colon; don't require `0x`.

## telnet_port must differ per probe; tcl_port must be disabled

**Symptom:** second OpenOCD instance dies at startup under `--network host`;
or Python telnet reads get empty responses through `-p` mapped ports.

**Root cause:** both instances default-bind 6666 (tcl) and 4444 (telnet) on
the shared host network. Port mapping (`-p`) also NATs the loopback bind,
which OpenOCD telnet clients don't tolerate well.

**Fix:** every OpenOCD gets `-c "tcl_port disabled" -c "telnet_port <unique>"`.
Prefer `--network host` for the container.

## Symbols move every build — resolve at runtime

**Symptom:** sensor reads garbage / LEDs don't toggle after a rebuild, with
no code change on the script side.

**Root cause:** `RGBSensorData`, `g_can_led_mode` etc. are BSS symbols whose
addresses shift with any firmware change (they even moved from `.data` to
`.bss` once when an init value changed).

**Fix:** scripts always `arm-none-eabi-nm -n Firmware.elf | grep <symbol>`
at runtime. Never hardcode.

## Relayed networks make Ozone reconnect-loop

**Symptom:** Ozone connects, works, then silently detaches; every reconnect
re-runs `InitTarget` (~20 s through a DERP relay vs ~1 s LAN).

**Root cause:** idle SSH tunnel / relay drops; Ozone auto-reconnects.

**Fix:** `ssh -o ServerAliveInterval=10 -o TCPKeepAlive=yes` on the tunnel,
or terminate the tunnel on a bridge host with `autossh` (see
[Debug Topologies](debug-topologies.md), topology 3).
