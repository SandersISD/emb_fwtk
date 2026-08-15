# Debug Topologies

emb_fwtk supports three ways to arrange **host** (where the human/debugger runs),
**slave** (the device with the probes), and **target** (the boards).
Pick by where the firmware *project* lives.

---

## Topology 1 — Probe server (ELF on host) ✅ recommended

```
┌──────────────┐         ┌──────────────────┐        ┌─────────┐
│ HOST (Mac/Win)│──TCP/IP──▶ slave: emb_fwtk │──SWD──▶│ TARGET  │
│ project+ELF   │          │ RemoteServer/OCD │        │ STM32   │
│ Ozone / gdb   │          └──────────────────┘        └─────────┘
└──────────────┘
```

- Build **and keep** the firmware on the host. Flash by pushing the ELF over
  (`manage_debug flash` after rsync, or a bind-mount).
- Ozone opens the **local** ELF and connects to the Remote Server by IP/tunnel.
- **Breakpoints fully work**: DWARF debug info carries build-time paths, and on
  the host those paths exist.

Use when: you develop on your machine, the slave is headless probe infrastructure.
This is the topology emb_fwtk is designed around.

## Topology 2 — Everything on slave

```
┌──────────────┐  mount   ┌──────────────────┐        ┌─────────┐
│ HOST          │◀────────│ slave: emb_fwtk  │──SWD──▶│ TARGET  │
│ Ozone only;   │ (source)│ project+ELF+all  │        │         │
│ no project    │         └──────────────────┘        └─────────┘
└──────────────┘
```

- Project and ELF live on the slave (built inside the container).
- Host mounts the **source** to view it: Samba (SMB) on the slave works natively
  with Windows and macOS; SSHFS-Win + WinFsp is the alternative on Windows.
- **The landmine is DWARF paths, not the mount.** The ELF embeds build-time
  absolute paths (`/workspace/...`). On the host those paths don't exist, so
  Ozone drops to disassembly-only breakpoints.
- Fix at build time: add to the firmware's CMake

  ```
  -ffile-prefix-map=/workspace=<host-visible mount point>
  ```

  The ELF then points wherever the debugger lives. Ozone does not have a
  GDB-style `set substitute-path` equivalent (as of V3.30 — verify if it
  matters to you), so build-time remapping is the reliable route.
- The mount only needs to cover *source*; the ELF itself is self-contained.

Use when: the toolchain must run where the probes are (no cross-build on host),
or the project is shared by several people through the slave.

## Topology 3 — Relayed / multi-tailnet

```
┌──────────┐   tailnet A   ┌─────────────┐   tailnet B   ┌──────────┐
│ HOST      │────direct────▶│ bridge      │────direct────▶│ slave    │
│ (Ozone)   │               │ (both nets) │               │ emb_fwtk │
└──────────┘               └─────────────┘               └──────────┘
```

- Host and slave are on different VPNs; no direct IP route. SSH jumps or a
  bridge node relays.
- Measured cost (real session): `InitTarget` ~20 s vs ~1 s on LAN, and idle
  tunnels drop, making Ozone reconnect-loop (each reconnect re-scans).
- **Improvement**: terminate the tunnel on the *bridge* with a persistent
  `autossh -L 19020:<slave-ip>:19020`. From the host this collapses topology 3
  into topology 1 — one hop, survives the host sleeping.

Use when: lab networking you don't control (e.g. robot on someone else's
tailnet). Keep `ssh -o ServerAliveInterval=10 -o TCPKeepAlive=yes` tunnels.

---

## Cross-cutting notes

- Where the ELF lives decides where `manage_debug flash` reads it from:
  bind-mounted `/workspace` (slave-side) or rsync first (host-side).
- All topologies share the same session lock: one debug mode at a time
  (`ocd` or `remote`), enforced in the container.
- J-Link Remote Server ports auto-assign with **stride 2** (19020, 19022, ...)
  because each server also binds `port+1` internally — see
  [Known Quirks](known-quirks.md).
