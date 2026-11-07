"""Q-SYS PTZ cameras (QSC NC-series) - controlled through the Q-SYS Core over QRC.

The camera is a component in the Q-SYS design (e.g. "NC-12x80_NC-12x80-1");
`component` must be that name, as listed by `Component.GetComponents`.
The design must make the component scriptable (it is by default for cameras).

Controls used:
    pan.curr / tilt.curr / zoom.curr   position, -1..1 / -1..1 / 0..1
    pan.left, tilt.up, zoom.in, ...    continuous move while true
    ptz.preset + ptz.trigger           absolute move to "<pan> <tilt> <zoom>"
    preset.<home|1|2|3>.load.trigger   recall / .save.trigger to store
    toggle.privacy                     privacy mode (camera turns away)
    url.preview                        JPEG snapshot URL served by the camera

Note: on the Core, `pan.left` makes `pan.curr` go UP. The driver hides this:
`pan_deg` and `pan_by()` use "positive = right".
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from avhub.core.device import Device
from avhub.core.events import EventBus
from avhub.transports.qrc import QrcClient

log = logging.getLogger(__name__)

# Q-SYS control -> (state name, kind)
WATCHED: dict[str, tuple[str, str]] = {
    "pan.curr": ("pan", "float"),
    "tilt.curr": ("tilt", "float"),
    "zoom.curr": ("zoom", "float"),
    "is.moving": ("moving", "bool"),
    "toggle.privacy": ("privacy", "bool"),
    "camera.status": ("status", "string"),
    "url.preview": ("snapshot_url", "string"),
}

MOVES: dict[str, str] = {
    "left": "pan.left",
    "right": "pan.right",
    "up": "tilt.up",
    "down": "tilt.down",
    "up_left": "pan.left.tilt.up",
    "up_right": "pan.right.tilt.up",
    "down_left": "pan.left.tilt.down",
    "down_right": "pan.right.tilt.down",
    "zoom_in": "zoom.in",
    "zoom_out": "zoom.out",
}

PRESETS = ("home", "1", "2", "3")


def parse_change(control: str, value: Any, string: str | None) -> tuple[str, Any] | None:
    """Turn a Q-SYS control value into (state name, value), or None if not watched."""
    watched = WATCHED.get(control)
    if watched is None:
        return None
    key, kind = watched
    if kind == "float":
        return key, round(float(value), 4)
    if kind == "bool":
        return key, bool(value)
    return key, string if string is not None else str(value)


class QsysCamera(Device):
    ACTIONS = frozenset(
        {"move", "stop_moving", "goto", "pan_by", "recall_preset", "save_preset", "privacy",
         "refresh"}
    )

    def __init__(
        self,
        device_id: str,
        bus: EventBus,
        host: str,
        component: str,
        port: int = 1710,
        name: str | None = None,
        user: str | None = None,
        password: str | None = None,
        pan_range: float = 170.0,
        max_move_seconds: float = 10.0,
        poll_rate: float = 0.2,
    ) -> None:
        super().__init__(device_id, bus, name)
        self.component = component
        self.pan_range = pan_range  # degrees from centre to either end (pan.curr = +-1)
        self.max_move_seconds = max_move_seconds  # safety stop for a forgotten move()
        self.poll_rate = poll_rate
        self._group = f"avhub-{device_id}"
        self._auto_stop: asyncio.Task[None] | None = None
        self.client = QrcClient(
            host,
            port,
            user=user,
            password=password,
            on_notification=self._on_notification,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )

    async def start(self) -> None:
        await self.client.start()

    async def stop(self) -> None:
        self._cancel_auto_stop()
        await self.client.stop()

    async def _set(self, controls: dict[str, Any]) -> None:
        await self.client.call(
            "Component.Set",
            {"Name": self.component,
             "Controls": [{"Name": k, "Value": v} for k, v in controls.items()]},
        )

    async def _on_connect(self) -> None:
        await self.client.call(
            "ChangeGroup.AddComponentControl",
            {"Id": self._group,
             "Component": {"Name": self.component,
                           "Controls": [{"Name": c} for c in WATCHED]}},
        )
        # The first poll reports every control, later ones only what changed.
        await self.client.call("ChangeGroup.AutoPoll", {"Id": self._group, "Rate": self.poll_rate})
        await self.update_state("connected", True)

    async def _on_disconnect(self) -> None:
        await self.update_state("connected", False)

    async def _on_notification(self, method: str, params: Any) -> None:
        if method != "ChangeGroup.Poll" or params.get("Id") != self._group:
            return
        for change in params.get("Changes", []):
            if change.get("Component") == self.component:
                await self._apply(change["Name"], change.get("Value"), change.get("String"))

    async def _apply(self, control: str, value: Any, string: str | None) -> None:
        parsed = parse_change(control, value, string)
        if parsed is None:
            return
        await self.update_state(*parsed)
        if parsed[0] == "pan":
            await self.update_state("pan_deg", round(-parsed[1] * self.pan_range, 1))

    def _cancel_auto_stop(self) -> None:
        if self._auto_stop is not None:
            self._auto_stop.cancel()
            self._auto_stop = None

    async def _stop_later(self) -> None:
        await asyncio.sleep(self.max_move_seconds)
        self._auto_stop = None
        log.warning("%s: still moving after %ss, stopping", self.id, self.max_move_seconds)
        await self.stop_moving()

    # ---- actions -------------------------------------------------------

    async def refresh(self) -> None:
        result = await self.client.call(
            "Component.Get",
            {"Name": self.component, "Controls": [{"Name": c} for c in WATCHED]},
        )
        for control in result["Controls"]:
            await self._apply(control["Name"], control.get("Value"), control.get("String"))

    async def move(self, direction: str) -> None:
        """Start moving (or zooming) until stop_moving() - or max_move_seconds - is reached."""
        if direction not in MOVES:
            raise ValueError(f"Unknown direction '{direction}' (use one of {sorted(MOVES)})")
        self._cancel_auto_stop()
        # Release every other move control so directions never add up.
        await self._set({c: c == MOVES[direction] for c in MOVES.values()})
        self._auto_stop = asyncio.create_task(self._stop_later())

    async def stop_moving(self) -> None:
        self._cancel_auto_stop()
        await self._set(dict.fromkeys(MOVES.values(), False))

    async def goto(
        self, pan: float | None = None, tilt: float | None = None, zoom: float | None = None
    ) -> None:
        """Move to an absolute position in Q-SYS units; omitted axes stay where they are."""
        pan = self.state.get("pan", 0.0) if pan is None else pan
        tilt = self.state.get("tilt", 0.0) if tilt is None else tilt
        zoom = self.state.get("zoom", 0.0) if zoom is None else zoom
        pan = max(-1.0, min(1.0, pan))
        tilt = max(-1.0, min(1.0, tilt))
        zoom = max(0.0, min(1.0, zoom))
        await self._set({"ptz.preset": f"{pan:.4f} {tilt:.4f} {zoom:.4f}", "ptz.trigger": 1})

    async def pan_by(self, degrees: float) -> None:
        """Turn by `degrees`: positive = right, negative = left."""
        await self.goto(pan=self.state.get("pan", 0.0) - degrees / self.pan_range)

    async def recall_preset(self, preset: str | int) -> None:
        await self._set({f"preset.{_preset(preset)}.load.trigger": 1})

    async def save_preset(self, preset: str | int) -> None:
        await self._set({f"preset.{_preset(preset)}.save.trigger": 1})

    async def privacy(self, on: bool = True) -> None:
        await self._set({"toggle.privacy": on})


def _preset(preset: str | int) -> str:
    preset = str(preset).lower()
    if preset not in PRESETS:
        raise ValueError(f"Unknown preset '{preset}' (use one of {list(PRESETS)})")
    return preset
