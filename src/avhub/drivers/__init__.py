"""Device drivers. Register each driver here so it can be used from config.yaml."""

from avhub.core.device import Device
from avhub.drivers.denon_avr import DenonAvr
from avhub.drivers.qsys_camera import QsysCamera

DRIVERS: dict[str, type[Device]] = {
    "denon_avr": DenonAvr,
    "qsys_camera": QsysCamera,
}

__all__ = ["DRIVERS"]
