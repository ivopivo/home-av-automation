import asyncio
import json

import pytest

from avhub.core.events import EventBus
from avhub.drivers.qsys_camera import QsysCamera, parse_change
from avhub.transports.qrc import QrcClient, QrcError

COMPONENT = "NC-12x80-1"


@pytest.mark.parametrize(
    ("control", "value", "string", "expected"),
    [
        ("pan.curr", 0.34702941, ".347", ("pan", 0.347)),
        ("is.moving", 1.0, "true", ("moving", True)),
        ("toggle.privacy", 0.0, "false", ("privacy", False)),
        ("camera.status", 0, "OK", ("status", "OK")),
        ("url.preview", 0, "http://cam/snapshot.jpg", ("snapshot_url", "http://cam/snapshot.jpg")),
        ("wb.hue", 7, "7", None),
    ],
)
def test_parse_change(control, value, string, expected):
    assert parse_change(control, value, string) == expected


class FakeCore:
    """A tiny QRC server with one camera component."""

    def __init__(self) -> None:
        self.controls: dict[str, object] = {
            "pan.curr": 0.0, "tilt.curr": 0.45, "zoom.curr": 0.35, "is.moving": False,
            "toggle.privacy": False, "camera.status": "OK",
            "url.preview": "http://127.0.0.1/snapshot.jpg",
        }
        self.sets: list[dict[str, object]] = []
        self.methods: list[str] = []

    def control(self, name: str) -> dict[str, object]:
        value = self.controls[name]
        return {"Component": COMPONENT, "Name": name,
                "Value": value if not isinstance(value, str) else 0, "String": str(value)}

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        group: list[str] = []
        poller: asyncio.Task | None = None

        async def send(message: dict) -> None:
            writer.write(json.dumps(message).encode() + b"\0")
            await writer.drain()

        async def poll(rate: float) -> None:
            last: dict[str, object] = {}
            while True:
                changes = [self.control(n) for n in group if last.get(n, ...) != self.controls[n]]
                last = {n: self.controls[n] for n in group}
                if changes:
                    await send({"jsonrpc": "2.0", "method": "ChangeGroup.Poll",
                                "params": {"Id": "avhub-cam", "Changes": changes}})
                await asyncio.sleep(rate)

        await send({"jsonrpc": "2.0", "method": "EngineStatus", "params": {"State": "Active"}})
        try:
            while True:
                try:
                    data = await reader.readuntil(b"\0")
                except asyncio.IncompleteReadError:
                    break
                msg = json.loads(data[:-1])
                params = msg.get("params") or {}
                self.methods.append(msg["method"])
                result: object = True
                if msg["method"] == "ChangeGroup.AddComponentControl":
                    group.extend(c["Name"] for c in params["Component"]["Controls"])
                elif msg["method"] == "ChangeGroup.AutoPoll":
                    poller = asyncio.create_task(poll(0.01))
                elif msg["method"] == "Component.Set":
                    if params["Name"] != COMPONENT:
                        await send({"jsonrpc": "2.0", "id": msg["id"],
                                    "error": {"code": 8, "message": "Unknown component"}})
                        continue
                    values = {c["Name"]: c["Value"] for c in params["Controls"]}
                    self.sets.append(values)
                    if "ptz.trigger" in values:
                        pan, tilt, zoom = map(float, values["ptz.preset"].split())
                        self.controls.update({"pan.curr": pan, "tilt.curr": tilt,
                                              "zoom.curr": zoom})
                    if "toggle.privacy" in values:
                        self.controls["toggle.privacy"] = values["toggle.privacy"]
                elif msg["method"] == "Component.Get":
                    result = {"Name": params["Name"],
                              "Controls": [self.control(c["Name"]) for c in params["Controls"]]}
                await send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        finally:
            if poller is not None:
                poller.cancel()
            writer.close()


async def wait_for(condition) -> None:
    """Poll until condition() is true; fail after 2 seconds."""
    async with asyncio.timeout(2.0):
        while not condition():  # noqa: ASYNC110 - simple polling is fine in tests
            await asyncio.sleep(0.01)


@pytest.fixture
async def core():
    fake = FakeCore()
    server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    fake.port = server.sockets[0].getsockname()[1]
    yield fake
    server.close()
    await server.wait_closed()


@pytest.fixture
async def camera(core):
    cam = QsysCamera("cam", EventBus(), host="127.0.0.1", port=core.port, component=COMPONENT,
                     poll_rate=0.01)
    await cam.start()
    await wait_for(lambda: cam.state.get("connected") and "snapshot_url" in cam.state)
    yield cam
    await cam.stop()


async def test_state_comes_from_change_group(core, camera):
    assert camera.state["pan"] == 0.0
    assert camera.state["tilt"] == 0.45
    assert camera.state["snapshot_url"] == "http://127.0.0.1/snapshot.jpg"
    assert core.methods[:2] == ["ChangeGroup.AddComponentControl", "ChangeGroup.AutoPoll"]

    core.controls["pan.curr"] = 0.5
    await wait_for(lambda: camera.state["pan"] == 0.5)
    assert camera.state["pan_deg"] == -85.0  # pan.curr goes up when turning left


async def test_move_releases_other_directions_and_stops(core, camera):
    await camera.call("move", direction="left")
    assert core.sets[-1]["pan.left"] is True
    assert core.sets[-1]["pan.right"] is False
    assert sum(core.sets[-1].values()) == 1

    await camera.call("stop_moving")
    assert not any(core.sets[-1].values())

    with pytest.raises(ValueError):
        await camera.call("move", direction="sideways")


async def test_move_stops_by_itself(core, camera):
    camera.max_move_seconds = 0.05
    await camera.call("move", direction="zoom_in")
    await wait_for(lambda: not any(core.sets[-1].values()))


async def test_pan_by_degrees(core, camera):
    await camera.call("pan_by", degrees=-17)  # 17 degrees to the left
    assert core.sets[-1] == {"ptz.preset": "0.1000 0.4500 0.3500", "ptz.trigger": 1}
    await wait_for(lambda: camera.state["pan_deg"] == -17.0)

    await camera.call("goto", pan=5)  # clamped to the end stop
    assert core.sets[-1]["ptz.preset"] == "1.0000 0.4500 0.3500"


async def test_presets_and_privacy(core, camera):
    await camera.call("recall_preset", preset="home")
    assert core.sets[-1] == {"preset.home.load.trigger": 1}
    await camera.call("save_preset", preset=2)
    assert core.sets[-1] == {"preset.2.save.trigger": 1}
    with pytest.raises(ValueError):
        await camera.call("recall_preset", preset="private")

    await camera.call("privacy", on=True)
    await wait_for(lambda: camera.state["privacy"] is True)


async def test_refresh_reads_all_controls(core, camera):
    core.controls["camera.status"] = "Fault"
    await camera.call("refresh")
    assert camera.state["status"] == "Fault"


async def test_unknown_component_raises_qrc_error(core):
    client = QrcClient("127.0.0.1", core.port)
    await client.start()
    try:
        async with asyncio.timeout(2):
            await client.wait_connected()
        with pytest.raises(QrcError):
            await client.call("Component.Set", {"Name": "nope", "Controls": []})
    finally:
        await client.stop()


async def test_actions_fail_while_disconnected():
    cam = QsysCamera("cam", EventBus(), host="127.0.0.1", port=1, component=COMPONENT)
    with pytest.raises(ConnectionError):
        await cam.call("stop_moving")
