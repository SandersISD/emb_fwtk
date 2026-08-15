# emb_fwtk Wiki

Documentation for using and extending the toolkit.

## Pages

- [Debug Topologies](debug-topologies.md) — the three ways to wire up host / slave / target, and when to use each
- [Known Quirks](known-quirks.md) — empirically discovered gotchas (J-Link port+1, USB settle, zombies, relay latency)
- [Extending to Other Targets](EXTENDING.md) — what changes for non-STM32/non-J-Link (ESP32-S3 worked example)

## Quick reference

```
manage_debug list                 # probes + assigned ports
manage_debug mode ocd             # OpenOCD session (Python scripts, flash)
manage_debug mode remote          # J-Link Remote Server session (Ozone)
manage_debug flash <board> <elf>  # transient flash — stops everything, flashes, stops
manage_debug status               # what's running, who holds the lock
manage_debug stop                 # kill all daemons, release lock
```

Ports auto-assign per probe: telnet `4444+i`, gdb `3333+i`, remote `19020+2i`.
Override in `probes.yaml`. Multi-container: set `port_offset`.

## The 30-second version

1. Download [J-Link ARM64 tgz](https://www.segger.com/downloads/jlink/) (license click-through) into the repo root as `JLink_Linux_arm64.tgz`
2. Edit `probes.yaml` — probe serials and OpenOCD configs
3. `docker build -t emb-fwtk .`
4. `docker run -d --rm --privileged --network host --init -v /dev/bus/usb:/dev/bus/usb -v $HOME/main_ws:/workspace --name emb-fwtk emb-fwtk tail -f /dev/null`
5. `docker exec emb-fwtk manage_debug mode ocd` — you're debugging
