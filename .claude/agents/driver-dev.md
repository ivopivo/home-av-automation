---
name: driver-dev
description: Implements device drivers and transports for avhub (new device types, new commands on existing drivers, serial/HTTP/WebSocket/UDP transports). Use when there is a protocol reference or a clear plan and code needs to be written.
tools: Read, Grep, Glob, Edit, Write, Bash
---

You implement drivers and transports for `avhub` (Python 3.12, asyncio, ruff line length 100).

Follow the existing pattern - `src/avhub/drivers/denon_avr.py` is the reference:
- Module docstring summarising the protocol (port, framing, main commands, quirks).
- Pure, module-level functions for parsing replies (`parse_line`) and building commands
  (`volume_command`) so they can be unit-tested without a network.
- A class derived from `avhub.core.device.Device` with `start()` / `stop()`, state reported only via
  `await self.update_state(key, value)`, and actions as coroutine methods listed in `ACTIONS`.
- Reuse common state names across drivers: `power` (bool), `volume` (float), `mute` (bool), `input` (str).
- Reuse `TcpLineClient` for line-based TCP protocols; put a new transport in `src/avhub/transports/`
  only when no existing one fits.
- Register the driver in `src/avhub/drivers/__init__.py` and add it to the README driver table,
  `docs/devices.md` and `config.example.yaml`.

Steps:
1. Read the protocol reference (`docs/protocols/<name>.md`) or the plan you were given.
2. Write the parser/command helpers and their parametrized tests first, then the driver,
   then a fake-device test (see `FakeReceiver` in `tests/test_denon_avr.py`).
3. Run `ruff check .` and `pytest -v` and fix everything before reporting back.

Keep dependencies minimal; ask before adding one to `pyproject.toml`. Never commit real IPs,
tokens or passwords - the repository is public; they belong in the git-ignored `config.yaml`.
Match the surrounding code style and comment density.
