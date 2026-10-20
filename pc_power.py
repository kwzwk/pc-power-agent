"""Tiny PC power agent for Home Assistant.

Adds two MQTT buttons (Sleep, Shut down) and an online/offline status to
Home Assistant via MQTT discovery. Settings live in config.json next to
the script or .exe; a template is written on first run.
"""

import ctypes
import json
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt

# Next to the .exe when built with PyInstaller, next to the script otherwise.
if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
else:
    HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
DEFAULT_CONFIG = {
    "host": "192.168.1.10",
    "port": 1883,
    "username": "mqtt-user",
    "password": "change-me",
    "device_name": socket.gethostname(),
    "discovery_prefix": "homeassistant",
    "shutdown_delay_seconds": 0,
}
if not CONFIG_PATH.exists():
    # First run: write a template to fill in, then stop.
    CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    sys.exit(0)
CFG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

NAME = CFG.get("device_name") or socket.gethostname()
SLUG = NAME.lower().replace(" ", "_")
PREFIX = CFG.get("discovery_prefix", "homeassistant")
BASE = f"pc_power/{SLUG}"
STATUS_TOPIC = f"{BASE}/status"
COMMAND_TOPIC = f"{BASE}/command"
HA_STATUS_TOPIC = f"{PREFIX}/status"

logging.basicConfig(
    filename=HERE / "pc_power.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("pc_power")

BUTTONS = {
    "sleep": ("Sleep", "mdi:power-sleep"),
    "shutdown": ("Shut down", "mdi:power"),
}


def publish_discovery(client):
    device = {
        "identifiers": [f"pc_power_{SLUG}"],
        "name": NAME,
        "manufacturer": "pc_power.py",
    }
    availability = {
        "topic": STATUS_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    for key, (label, icon) in BUTTONS.items():
        config = {
            "name": label,
            "unique_id": f"pc_power_{SLUG}_{key}",
            "command_topic": COMMAND_TOPIC,
            "payload_press": key,
            "icon": icon,
            "availability": [availability],
            "device": device,
        }
        client.publish(f"{PREFIX}/button/{SLUG}/{key}/config", json.dumps(config), retain=True)
    status = {
        "name": "Online",
        "unique_id": f"pc_power_{SLUG}_online",
        "state_topic": STATUS_TOPIC,
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "device": device,
    }
    client.publish(f"{PREFIX}/binary_sensor/{SLUG}/online/config", json.dumps(status), retain=True)


def do_sleep():
    log.info("Sleep requested")
    # SetSuspendState(hibernate=False, force=False, disable_wake_events=False)
    ctypes.windll.powrprof.SetSuspendState(False, False, False)


def do_shutdown():
    log.info("Shutdown requested")
    delay = int(CFG.get("shutdown_delay_seconds", 0))
    # No /f: Windows asks apps to close normally, same as Start > Shut down.
    subprocess.run(["shutdown", "/s", "/t", str(delay)], check=False)


ACTIONS = {"sleep": do_sleep, "shutdown": do_shutdown}


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log.error("MQTT connect failed: %s", reason_code)
        return
    log.info("Connected to MQTT")
    client.subscribe([(COMMAND_TOPIC, 0), (HA_STATUS_TOPIC, 0)])
    publish_discovery(client)
    client.publish(STATUS_TOPIC, "online", retain=True)


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="ignore").strip().lower()
    if msg.topic == HA_STATUS_TOPIC:
        if payload == "online":  # HA restarted, re-announce ourselves
            publish_discovery(client)
            client.publish(STATUS_TOPIC, "online", retain=True)
        return
    action = ACTIONS.get(payload)
    if action is None:
        log.warning("Ignoring unknown command %r", payload)
        return
    # Tell HA right away instead of waiting for the keepalive to time out,
    # then act from another thread so this message actually gets sent.
    client.publish(STATUS_TOPIC, "offline", retain=True)
    threading.Timer(1.0, action).start()


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"pc_power_{SLUG}")
    if CFG.get("username"):
        client.username_pw_set(CFG["username"], CFG.get("password"))
    client.will_set(STATUS_TOPIC, "offline", retain=True)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=2, max_delay=30)

    while True:
        try:
            client.connect(CFG["host"], int(CFG.get("port", 1883)), keepalive=30)
            break
        except OSError as err:  # network not up yet after boot or wake
            log.warning("Broker not reachable (%s), retrying", err)
            time.sleep(10)

    # Reconnects on its own after sleep or network drops.
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("pc_power crashed")
        sys.exit(1)
