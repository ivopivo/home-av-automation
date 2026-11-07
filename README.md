# home-av-automation

A small Python hub that talks to audio/video devices at home (receivers, TVs,
projectors, HDMI matrices, media players) over their native control protocols,
and runs automations across them.

## How it is built

```
            config.yaml (devices + automations)
                         |
   +---------------------+----------------------+
   |                                            |
 drivers/  (one per device type)          automation/  (rules)
   |  publish state changes                     ^  "when tv.power == true
   v                                            |   -> receiver.power_on"
 core/events.py  EventBus  ---------------------+
   ^
 transports/  (TCP line client, Q-SYS QRC; later: serial, HTTP, UDP)

 web/  browser UI + HTTP/WebSocket API on top of the devices and the EventBus
```

- **Transport** - how bytes get to the device (TCP, RS-232, HTTP, ...).
- **Driver** - speaks one device's protocol, keeps its `state` (power, volume, input, ...)
  and offers `ACTIONS` (power_on, set_volume, ...).
- **EventBus** - every state change becomes an event like `receiver.power = True`.
- **Automations** - rules in `config.yaml` that react to events and call actions.
- **Web UI** - optional, see below.

## Getting started

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

pytest                       # run tests (uses a fake receiver, no hardware needed)
copy config.example.yaml config.yaml   # then edit IPs
python -m avhub --config config.yaml -v
```

`config.yaml` is git-ignored. **This repository is public - keep IPs, tokens and passwords in `config.yaml`, never in committed files.**

## Drivers

| Driver | Devices | Protocol |
|---|---|---|
| `denon_avr` | Denon / Marantz AV receivers | Telnet, TCP 23 |
| `qsys_camera` | QSC NC-series PTZ cameras in a Q-SYS design | QRC (JSON-RPC) to the Core, TCP 1710 |

See [docs/devices.md](docs/devices.md) for the device inventory, common protocols and how to add a new device.

## Web UI

Add a `web:` section to `config.yaml` (see `config.example.yaml`) and open
http://127.0.0.1:8080/. The page currently controls cameras: live picture (JPEG
snapshots, about 2-3 per second), hold-to-move with mouse, touch or arrow keys, zoom,
turn by degrees, presets and privacy mode.

The same things are available as an API:

```powershell
curl http://127.0.0.1:8080/api/devices
curl -X POST http://127.0.0.1:8080/api/devices/camera/actions/pan_by -H "Content-Type: application/json" -d '{"degrees": -15}'
```

There is no login. Keep `host: 127.0.0.1`, or use `0.0.0.0` only on a network you trust.

## Adding a driver

1. Create `src/avhub/drivers/<name>.py` with a class derived from `Device`.
2. Parse incoming data into `await self.update_state(key, value)`.
3. Add action methods and list them in `ACTIONS`.
4. Register the class in `src/avhub/drivers/__init__.py`.
5. Write a test with a fake device (see `tests/test_denon_avr.py`).
