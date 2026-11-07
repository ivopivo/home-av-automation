"""Q-SYS Remote Control (QRC) client with automatic reconnect.

QRC is JSON-RPC 2.0 over TCP port 1710 on a Q-SYS Core. Every message, in
both directions, is one JSON object terminated by a NUL byte (b"\\0").
The Core answers requests by "id" and also sends notifications without an
"id" (e.g. "EngineStatus", "ChangeGroup.Poll").

The Core drops connections that stay silent for 60 s, so a "NoOp" is sent
as a keep-alive. If the design has access control enabled, pass `user` and
`password` and the client logs on after every connect.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

TERMINATOR = b"\0"


class QrcError(Exception):
    """The Core answered a request with a JSON-RPC error."""

    def __init__(self, method: str, error: dict[str, Any]) -> None:
        self.code = error.get("code")
        super().__init__(f"{method}: {error.get('message', error)} (code {self.code})")


class QrcClient:
    def __init__(
        self,
        host: str,
        port: int = 1710,
        *,
        on_notification: Callable[[str, Any], Awaitable[None]] | None = None,
        on_connect: Callable[[], Awaitable[None]] | None = None,
        on_disconnect: Callable[[], Awaitable[None]] | None = None,
        user: str | None = None,
        password: str | None = None,
        keepalive: float = 20.0,
        timeout: float = 5.0,
        reconnect_delay: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.on_notification = on_notification
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self.user = user
        self.password = password
        self.keepalive = keepalive
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name=f"qrc:{self.host}:{self.port}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def wait_connected(self) -> None:
        """Wait until connected; wrap in `asyncio.timeout(...)` to limit the wait."""
        await self._connected.wait()

    async def call(self, method: str, params: Any = None) -> Any:
        """Send a request and return its "result"; raises QrcError on a JSON-RPC error."""
        if self._writer is None:
            raise ConnectionError(f"Not connected to Q-SYS Core {self.host}:{self.port}")
        request_id = next(self._ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            if method != "NoOp":
                log.debug("-> %s %s %s", self.host, method, params)
            self._writer.write(json.dumps(message).encode() + TERMINATOR)
            await self._writer.drain()
            async with asyncio.timeout(self.timeout):
                response = await future
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            raise QrcError(method, response["error"])
        return response.get("result")

    async def _run(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection(
                    self.host, self.port, limit=1024 * 1024
                )
                self._writer = writer
                self._connected.set()
                log.info("Connected to Q-SYS Core %s:%s", self.host, self.port)
                await self._session(reader)
            except (OSError, TimeoutError, QrcError, asyncio.IncompleteReadError,
                    asyncio.LimitOverrunError, json.JSONDecodeError) as exc:
                log.warning(
                    "Connection to Q-SYS Core %s:%s lost (%s); retrying in %ss",
                    self.host,
                    self.port,
                    exc or type(exc).__name__,
                    self.reconnect_delay,
                )
            finally:
                was_connected = self._writer is not None
                self._connected.clear()
                if self._writer is not None:
                    self._writer.close()
                    self._writer = None
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("Connection to Q-SYS Core lost"))
                self._pending.clear()
                if was_connected and self.on_disconnect is not None:
                    await self.on_disconnect()
            await asyncio.sleep(self.reconnect_delay)

    async def _session(self, reader: asyncio.StreamReader) -> None:
        # Responses are read by a separate task so that on_connect() can await calls.
        reader_task = asyncio.create_task(self._read_loop(reader))
        try:
            if self.user is not None:
                await self.call("Logon", {"User": self.user, "Password": self.password or ""})
            if self.on_connect is not None:
                await self.on_connect()
            while True:
                done, _ = await asyncio.wait({reader_task}, timeout=self.keepalive)
                if done:
                    reader_task.result()  # re-raises the read error
                    raise ConnectionError("closed by the Core")
                await self.call("NoOp", {})
        finally:
            reader_task.cancel()

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            data = await reader.readuntil(TERMINATOR)
            message = json.loads(data[:-1])
            request_id = message.get("id")
            if request_id is not None:
                future = self._pending.get(request_id)
                if future is not None and not future.done():
                    future.set_result(message)
            elif "method" in message and self.on_notification is not None:
                try:
                    await self.on_notification(message["method"], message.get("params"))
                except Exception:
                    log.exception("Handling QRC notification %s failed", message["method"])
