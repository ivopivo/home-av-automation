# Device inventory

Fill in one row per device. This is the input for deciding which driver to write next.
Keep real IP addresses and passwords out of this file - put them in `config.yaml` (git-ignored).

| Device | Brand / model | Room | Connection options (LAN / RS-232 / IR / CEC) | Known protocol / port | Driver status |
|---|---|---|---|---|---|
| AV receiver | Denon / Marantz ... | Living room | LAN | Telnet, TCP 23 | `denon_avr` done |
| TV | | | | | todo |
| Projector | | | | | todo |
| HDMI matrix | | | | | todo |
| Media player | | | | | todo |
| PTZ camera | QSC NC-12x80 (via Q-SYS Core) | Meeting room | LAN | QRC to the Core, TCP 1710; RTSP 554 and `/snapshot.jpg` on the camera | `qsys_camera` done |
| Ceiling microphone | Sennheiser TeamConnect Ceiling 2 | Meeting room | LAN | SSC (JSON over UDP/TCP 45), also via Q-SYS plugin | todo |

## Typical protocols per device type

| Device type | Usual control options | Useful references / libraries |
|---|---|---|
| AV receivers | Telnet/TCP (Denon/Marantz 23, Onkyo eISCP 60128, Yamaha YNCA 50000), HTTP API (Yamaha MusicCast) | `denonavr`, `onkyo-eiscp`, `aiomusiccast` |
| TVs | Samsung WebSocket 8001/8002, LG webOS WebSocket 3000/3001, Sony Bravia REST + IRCC, Wake-on-LAN for power on | `samsungtvws`, `aiowebostv`, `pybravia` |
| Projectors | PJLink (TCP 4352, standard across Epson/Sony/NEC/Panasonic...), RS-232 | PJLink spec (JBMIA) |
| HDMI matrices | RS-232 or Telnet with a vendor command set (check the manual) | vendor manual |
| Media players | Apple TV (MRP/Companion), Chromecast (Cast v2), Sonos (UPnP/SOAP), Kodi (JSON-RPC) | `pyatv`, `pychromecast`, `soco` |
| Anything else | IR via a network IR blaster (e.g. Broadlink, Global Caché iTach), HDMI-CEC via a Raspberry Pi (`cec-client`) | `broadlink`, `libcec` |

## How to explore an unknown device

1. Find it on the network: router DHCP list, or `nmap -sV 192.168.1.0/24`.
2. Check open ports: `nmap -p- <ip>`.
3. Search the manual / vendor site for "control protocol", "RS-232 command list", "IP control", "API".
4. Try commands by hand: `telnet <ip> <port>` or PuTTY (raw mode), or watch the vendor app's traffic with Wireshark.
5. Write the parser + a fake-device test first (see `tests/test_denon_avr.py`), then try it on the real device.
