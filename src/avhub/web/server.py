"""Web UI and HTTP API for the hub.

    GET  /                                  control page (static/index.html)
    GET  /api/devices                       every device with its state and actions
    POST /api/devices/{id}/actions/{action} run an action, JSON body = arguments
    GET  /api/devices/{id}/snapshot         JPEG from the device's `snapshot_url` state
    GET  /api/ws                            WebSocket: state events out, action calls in

WebSocket messages:
    server -> {"type": "devices", "devices": [...]}           once, after connecting
    server -> {"type": "state", "device", "name", "value"}     on every state change
    client -> {"type": "call", "id", "device", "action", "args"}
    server -> {"type": "result", "id", "ok", "error"?}

A camera that a client started moving is stopped when that client's WebSocket
closes, so a dropped phone connection never leaves the camera turning.

There is no login: bind to 127.0.0.1 (the default) or a trusted network only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import (
    ClientError,
    ClientPayloadError,
    ClientSession,
    ClientTimeout,
    WSMsgType,
    web,
)

from avhub.core.device import Device
from avhub.core.events import Event, EventBus
from avhub.transports.qrc import QrcError

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SNAPSHOT_MAX_AGE = 0.4  # seconds; several browsers share one camera request
JPEG_END = b"\xff\xd9"


class ActionError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def describe(device: Device) -> dict[str, Any]:
    return {
        "id": device.id,
        "name": device.name,
        "driver": type(device).__name__,
        "state": device.state,
        "actions": sorted(device.ACTIONS),
    }


class WebServer:
    def __init__(
        self,
        devices: dict[str, Device],
        bus: EventBus,
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        self.devices = devices
        self.bus = bus
        self.host = host
        self.port = port
        self._runner: web.AppRunner | None = None
        self._http: ClientSession | None = None
        self._snapshots: dict[str, tuple[float, bytes]] = {}
        self._snapshot_locks: dict[str, asyncio.Lock] = {}
        self.app = web.Application()
        self.app.router.add_get("/", self._index)
        self.app.router.add_static("/static", STATIC_DIR)
        self.app.router.add_get("/api/devices", self._list_devices)
        self.app.router.add_post("/api/devices/{device}/actions/{action}", self._post_action)
        self.app.router.add_get("/api/devices/{device}/snapshot", self._snapshot)
        self.app.router.add_get("/api/ws", self._websocket)
        self.app.on_cleanup.append(self._close_http)

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("Web UI on http://%s:%s/", self.host, self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _close_http(self, _app: web.Application) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    # ---- actions -------------------------------------------------------

    async def call(self, device_id: str, action: str, args: Any) -> None:
        device = self.devices.get(device_id)
        if device is None:
            raise ActionError(404, f"Unknown device '{device_id}'")
        if not isinstance(args, dict):
            raise ActionError(400, "Arguments must be a JSON object")
        try:
            await device.call(action, **args)
        except (ValueError, TypeError) as exc:
            raise ActionError(400, str(exc)) from exc
        except (ConnectionError, TimeoutError) as exc:
            raise ActionError(503, f"{device_id} is not reachable: {exc}") from exc
        except QrcError as exc:
            raise ActionError(502, str(exc)) from exc

    # ---- HTTP handlers -------------------------------------------------

    async def _index(self, _request: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def _list_devices(self, _request: web.Request) -> web.Response:
        return web.json_response([describe(d) for d in self.devices.values()])

    async def _post_action(self, request: web.Request) -> web.Response:
        try:
            args = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Body is not JSON"}, status=400)
        try:
            await self.call(request.match_info["device"], request.match_info["action"], args)
        except ActionError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=exc.status)
        return web.json_response({"ok": True})

    async def _snapshot(self, request: web.Request) -> web.Response:
        device = self.devices.get(request.match_info["device"])
        if device is None:
            raise web.HTTPNotFound(text="Unknown device")
        url = device.state.get("snapshot_url")
        if not url:
            raise web.HTTPNotFound(text="This device has no snapshot")
        lock = self._snapshot_locks.setdefault(device.id, asyncio.Lock())
        async with lock:
            cached = self._snapshots.get(device.id)
            if cached is None or time.monotonic() - cached[0] > SNAPSHOT_MAX_AGE:
                try:
                    cached = (time.monotonic(), await self._fetch(url))
                except (ClientError, TimeoutError) as exc:
                    raise web.HTTPBadGateway(text=f"Snapshot failed: {exc}") from exc
                self._snapshots[device.id] = cached
        return web.Response(
            body=cached[1], content_type="image/jpeg", headers={"Cache-Control": "no-store"}
        )

    async def _fetch(self, url: str) -> bytes:
        if self._http is None:
            self._http = ClientSession(timeout=ClientTimeout(total=5))
        # Cameras use self-signed certificates.
        async with self._http.get(url, ssl=False) as response:
            response.raise_for_status()
            data = bytearray()
            try:
                async for chunk in response.content.iter_any():
                    data += chunk
            except ClientPayloadError:
                # QSC NC cameras sometimes announce a Content-Length a few bytes longer than
                # they send. The picture is still complete if it ends with the JPEG end marker.
                if not data.endswith(JPEG_END):
                    raise
            return bytes(data)

    # ---- WebSocket -----------------------------------------------------

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        moved: set[str] = set()  # devices this client started moving

        async def forward(event: Event) -> None:
            if not ws.closed:
                await ws.send_json(
                    {"type": "state", "device": event.source, "name": event.name,
                     "value": event.value}
                )

        unsubscribe = self.bus.subscribe("*", forward)
        try:
            await ws.send_json(
                {"type": "devices", "devices": [describe(d) for d in self.devices.values()]}
            )
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(message.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "call":
                    await self._ws_call(ws, data, moved)
        finally:
            unsubscribe()
            for device_id in moved:
                try:
                    await self.call(device_id, "stop_moving", {})
                except ActionError as exc:
                    log.warning("Could not stop %s after client left: %s", device_id, exc)
        return ws

    async def _ws_call(
        self, ws: web.WebSocketResponse, data: dict[str, Any], moved: set[str]
    ) -> None:
        device_id = str(data.get("device"))
        action = str(data.get("action"))
        reply: dict[str, Any] = {"type": "result", "id": data.get("id"), "ok": True}
        try:
            await self.call(device_id, action, data.get("args") or {})
        except ActionError as exc:
            reply.update(ok=False, error=str(exc))
        else:
            if action == "move":
                moved.add(device_id)
            elif action == "stop_moving":
                moved.discard(device_id)
        if not ws.closed:
            await ws.send_json(reply)
