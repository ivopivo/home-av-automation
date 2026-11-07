import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from avhub.core.device import Device
from avhub.core.events import EventBus
from avhub.web import WebServer

JPEG = b"\xff\xd8fake-jpeg\xff\xd9"


class FakeCamera(Device):
    ACTIONS = frozenset({"move", "stop_moving", "pan_by"})

    def __init__(self, device_id: str, bus: EventBus) -> None:
        super().__init__(device_id, bus, name="Fake camera")
        self.calls: list[tuple[str, dict]] = []

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def move(self, direction: str) -> None:
        self.calls.append(("move", {"direction": direction}))
        await self.update_state("moving", True)

    async def stop_moving(self) -> None:
        self.calls.append(("stop_moving", {}))
        await self.update_state("moving", False)

    async def pan_by(self, degrees: float) -> None:
        if degrees > 360:
            raise ValueError("too far")
        self.calls.append(("pan_by", {"degrees": degrees}))


@pytest.fixture
async def setup():
    bus = EventBus()
    camera = FakeCamera("cam", bus)
    server = WebServer({"cam": camera}, bus)
    async with TestClient(TestServer(server.app)) as client:
        yield client, camera


async def test_index_and_static(setup):
    client, _ = setup
    response = await client.get("/")
    assert response.status == 200
    assert "Camera Control" in await response.text()
    assert (await client.get("/static/app.js")).status == 200


async def test_list_devices(setup):
    client, _ = setup
    response = await client.get("/api/devices")
    assert await response.json() == [{
        "id": "cam", "name": "Fake camera", "driver": "FakeCamera", "state": {},
        "actions": ["move", "pan_by", "stop_moving"],
    }]


async def test_post_action(setup):
    client, camera = setup
    response = await client.post("/api/devices/cam/actions/pan_by", json={"degrees": 15})
    assert response.status == 200
    assert camera.calls == [("pan_by", {"degrees": 15})]


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("/api/devices/nope/actions/pan_by", {"degrees": 15}, 404),
        ("/api/devices/cam/actions/start", {}, 400),  # lifecycle methods are not actions
        ("/api/devices/cam/actions/pan_by", {"degrees": 999}, 400),
        ("/api/devices/cam/actions/pan_by", {"speed": 1}, 400),
        ("/api/devices/cam/actions/pan_by", [1, 2], 400),
    ],
)
async def test_bad_requests(setup, path, body, status):
    client, camera = setup
    response = await client.post(path, json=body)
    assert response.status == status
    assert (await response.json())["ok"] is False
    assert camera.calls == []


async def test_websocket_calls_and_events(setup):
    client, camera = setup
    async with client.ws_connect("/api/ws") as ws:
        hello = await ws.receive_json()
        assert hello["type"] == "devices"
        assert hello["devices"][0]["id"] == "cam"

        await ws.send_json({"type": "call", "id": 1, "device": "cam", "action": "move",
                            "args": {"direction": "left"}})
        assert await ws.receive_json() == {"type": "state", "device": "cam", "name": "moving",
                                           "value": True}
        assert await ws.receive_json() == {"type": "result", "id": 1, "ok": True}

        await ws.send_json({"type": "call", "id": 2, "device": "cam", "action": "fly"})
        reply = await ws.receive_json()
        assert reply["ok"] is False and "fly" in reply["error"]


async def test_websocket_close_stops_movement(setup):
    client, camera = setup
    async with client.ws_connect("/api/ws") as ws:
        await ws.receive_json()
        await ws.send_json({"type": "call", "id": 1, "device": "cam", "action": "move",
                            "args": {"direction": "up"}})
        while (await ws.receive_json())["type"] != "result":
            pass
    async with asyncio.timeout(2):
        while camera.state["moving"]:  # noqa: ASYNC110 - simple polling is fine in tests
            await asyncio.sleep(0.01)
    assert camera.calls[-1] == ("stop_moving", {})


async def test_snapshot_proxy(setup):
    client, camera = setup
    assert (await client.get("/api/devices/cam/snapshot")).status == 404

    hits = 0

    async def snapshot(_request):
        nonlocal hits
        hits += 1
        return web.Response(body=JPEG, content_type="image/jpeg")

    app = web.Application()
    app.router.add_get("/snapshot.jpg", snapshot)
    async with TestServer(app) as cam_server:
        await camera.update_state("snapshot_url", str(cam_server.make_url("/snapshot.jpg")))
        for _ in range(3):
            response = await client.get("/api/devices/cam/snapshot")
            assert response.status == 200
            assert response.content_type == "image/jpeg"
            assert await response.read() == JPEG
    assert hits == 1  # served from the short cache


@pytest.mark.parametrize(("body", "status"), [(JPEG, 200), (JPEG[:-4], 502)])
async def test_snapshot_with_short_content_length(setup, body, status):
    """Some cameras announce more bytes than they send; accept it if the JPEG is complete."""
    client, camera = setup

    async def camera_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
                     b"Content-Length: %d\r\nConnection: close\r\n\r\n" % (len(body) + 20) + body)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(camera_http, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        await camera.update_state("snapshot_url", f"http://127.0.0.1:{port}/snapshot.jpg")
        response = await client.get("/api/devices/cam/snapshot")
        assert response.status == status
        if status == 200:
            assert await response.read() == JPEG
    finally:
        server.close()
        await server.wait_closed()
