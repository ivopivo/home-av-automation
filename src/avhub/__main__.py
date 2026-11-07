"""Run the hub: `python -m avhub --config config.yaml`."""

from __future__ import annotations

import argparse
import asyncio
import logging

from avhub.automation.rules import AutomationEngine
from avhub.config import build_devices, build_rules, load_config
from avhub.core.events import Event, EventBus
from avhub.web import WebServer

log = logging.getLogger("avhub")


async def run(config_path: str) -> None:
    config = load_config(config_path)
    bus = EventBus()
    devices = build_devices(config, bus)
    engine = AutomationEngine(bus, devices, build_rules(config))

    async def log_event(event: Event) -> None:
        log.info("%s = %r", event.topic, event.value)

    bus.subscribe("*", log_event)
    engine.start()
    for device in devices.values():
        await device.start()
    web = None
    if config.get("web") is not None:
        web = WebServer(devices, bus, **config["web"])
        await web.start()
    log.info("Running with %d device(s), %d automation(s). Ctrl+C to stop.",
             len(devices), len(engine.rules))
    try:
        await asyncio.Event().wait()
    finally:
        if web is not None:
            await web.stop()
        engine.stop()
        for device in devices.values():
            await device.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="avhub", description=__doc__)
    parser.add_argument("--config", default="config.yaml", help="path to config file")
    parser.add_argument("-v", "--verbose", action="store_true", help="show raw protocol traffic")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args.config))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
