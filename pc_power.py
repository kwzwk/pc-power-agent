"""Tiny PC power agent for Home Assistant.

Adds two MQTT buttons (Sleep, Shut down) and an online/offline status to
Home Assistant via MQTT discovery. Settings live in config.json next to
the script or .exe; a template is written on first run.
"""

import ctypes
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
import psutil
import pystray
from PIL import Image, ImageDraw

if sys.platform == "win32":
    import winreg

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
    "idle_after_seconds": 300,
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
POLL_SECONDS = 5
IDLE_AFTER = int(CFG.get("idle_after_seconds", 300))

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
    for key, (label, icon, device_class) in SENSORS.items():
        config = {
            "name": label,
            "unique_id": f"pc_power_{SLUG}_{key}",
            "state_topic": f"{BASE}/{key}",
            "icon": icon,
            "availability": [availability],
            "device": device,
        }
        if device_class:
            config["device_class"] = device_class
        client.publish(f"{PREFIX}/binary_sensor/{SLUG}/{key}/config", json.dumps(config), retain=True)
    mac = {
        "name": "MAC address",
        "unique_id": f"pc_power_{SLUG}_mac",
        "state_topic": f"{BASE}/mac",
        "icon": "mdi:ethernet",
        "entity_category": "diagnostic",
        "device": device,
    }
    client.publish(f"{PREFIX}/sensor/{SLUG}/mac/config", json.dumps(mac), retain=True)


# Binary sensors polled from Windows: key -> (name, icon, device_class).
SENSORS = {
    "active": ("Active", "mdi:account-check", "occupancy"),
    "camera": ("Camera in use", "mdi:webcam", None),
    "microphone": ("Microphone in use", "mdi:microphone", None),
}


def idle_seconds():
    """Seconds since the last keyboard or mouse input."""

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO(ctypes.sizeof(LASTINPUTINFO), 0)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    # Both are 32-bit millisecond tick counts, so wraparound cancels out.
    now = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return ((now - info.dwTime) & 0xFFFFFFFF) / 1000


def device_in_use(capability):
    """True if any app is using the webcam/microphone right now.

    Windows records LastUsedTimeStop = 0 for an app while it holds the device.
    """
    root = rf"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{capability}"

    def scan(path):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return False
        with key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    return False
                i += 1
                if name == "NonPackaged":
                    if scan(rf"{path}\{name}"):
                        return True
                    continue
                try:
                    with winreg.OpenKey(key, name) as app:
                        stop, _ = winreg.QueryValueEx(app, "LastUsedTimeStop")
                        start, _ = winreg.QueryValueEx(app, "LastUsedTimeStart")
                except OSError:
                    continue
                if stop == 0 and start > 0:
                    return True

    return scan(root)


def local_mac():
    """MAC of the network adapter that reaches the MQTT broker (for Wake-on-LAN)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect((CFG["host"], int(CFG.get("port", 1883))))
        ip = s.getsockname()[0]
    for addrs in psutil.net_if_addrs().values():
        if any(a.family == socket.AF_INET and a.address == ip for a in addrs):
            for a in addrs:
                if a.family == psutil.AF_LINK:
                    mac = a.address.replace("-", ":").upper()
                    return None if mac == "00:00:00:00:00:00" else mac
    return None


def read_sensors():
    return {
        "active": idle_seconds() < IDLE_AFTER,
        "camera": device_in_use("webcam"),
        "microphone": device_in_use("microphone"),
    }


class Poller:
    """Publishes sensor states every few seconds, but only when they change."""

    def __init__(self, client):
        self.client = client
        self.last = {}

    def reset(self):
        self.last = {}  # republish everything after a (re)connect

    def run(self):
        while True:
            if self.client.is_connected():
                try:
                    states = read_sensors()
                    if "mac" not in self.last:
                        states["mac"] = local_mac()
                except Exception:
                    log.exception("Reading sensors failed")
                    states = {}
                for key, value in states.items():
                    if value is None or self.last.get(key) == value:
                        continue
                    payload = value if key == "mac" else ("ON" if value else "OFF")
                    self.client.publish(f"{BASE}/{key}", payload, retain=True)
                    self.last[key] = value
            time.sleep(POLL_SECONDS)


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
    set_tray_state("connected")
    client.subscribe([(COMMAND_TOPIC, 0), (HA_STATUS_TOPIC, 0)])
    publish_discovery(client)
    client.publish(STATUS_TOPIC, "online", retain=True)
    userdata.reset()


def on_message(client, userdata, msg):
    payload = msg.payload.decode(errors="ignore").strip().lower()
    if msg.topic == HA_STATUS_TOPIC:
        if payload == "online":  # HA restarted, re-announce ourselves
            publish_discovery(client)
            client.publish(STATUS_TOPIC, "online", retain=True)
            userdata.reset()
        return
    if msg.topic != COMMAND_TOPIC:
        return
    action = ACTIONS.get(payload)
    if action is None:
        log.warning("Ignoring unknown command %r", payload)
        return
    # Tell HA right away instead of waiting for the keepalive to time out,
    # then act from another thread so this message actually gets sent.
    client.publish(STATUS_TOPIC, "offline", retain=True)
    threading.Timer(1.0, action).start()


def on_disconnect(client, userdata, flags, reason_code, properties):
    log.info("Disconnected from MQTT: %s", reason_code)
    set_tray_state("connecting" if agent.running else "stopped")


class Agent:
    """Owns the MQTT connection so the tray can start and stop it."""

    def __init__(self):
        self.running = False
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"pc_power_{SLUG}")
        if CFG.get("username"):
            self.client.username_pw_set(CFG["username"], CFG.get("password"))
        self.client.will_set(STATUS_TOPIC, "offline", retain=True)
        self.client.on_connect = on_connect
        self.client.on_disconnect = on_disconnect
        self.client.on_message = on_message
        self.client.reconnect_delay_set(min_delay=2, max_delay=30)
        poller = Poller(self.client)
        self.client.user_data_set(poller)
        threading.Thread(target=poller.run, daemon=True).start()

    def start(self):
        if self.running:
            return
        log.info("Starting")
        self.running = True
        set_tray_state("connecting")
        # The loop thread keeps retrying until the broker is reachable and
        # reconnects on its own after sleep or network drops.
        self.client.connect_async(CFG["host"], int(CFG.get("port", 1883)), keepalive=30)
        self.client.loop_start()

    def stop(self):
        if not self.running:
            return
        log.info("Stopping")
        self.running = False
        if self.client.is_connected():
            # A clean disconnect skips the last will, so say offline ourselves.
            self.client.publish(STATUS_TOPIC, "offline", retain=True).wait_for_publish(timeout=3)
        self.client.disconnect()
        self.client.loop_stop()
        set_tray_state("stopped")


# Tray icon: a monitor whose screen shows the connection state.
TRAY_COLORS = {"connected": "#2e9d48", "connecting": "#e0a020", "stopped": "#8a8a8a"}
TRAY_TEXT = {"connected": "Connected", "connecting": "Connecting…", "stopped": "Stopped"}
tray = None
tray_state = "stopped"
agent = None


def tray_image(state):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((4, 8, 60, 46), radius=6, fill="#303030")
    d.rounded_rectangle((9, 13, 55, 41), radius=3, fill=TRAY_COLORS[state])
    d.rectangle((28, 46, 36, 54), fill="#303030")
    d.rounded_rectangle((16, 52, 48, 58), radius=3, fill="#303030")
    return img


def set_tray_state(state):
    global tray_state
    tray_state = state
    if tray is not None:
        tray.icon = tray_image(state)
        tray.title = f"PC power agent ({NAME}): {TRAY_TEXT[state]}"
        tray.update_menu()


def run_tray():
    global tray

    def open_folder(icon, item):
        os.startfile(HERE)

    def exit_app(icon, item):
        agent.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda item: f"{NAME}: {TRAY_TEXT[tray_state]}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start", lambda icon, item: agent.start(), enabled=lambda item: not agent.running),
        pystray.MenuItem("Stop", lambda icon, item: agent.stop(), enabled=lambda item: agent.running),
        pystray.MenuItem("Open folder", open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit", exit_app),
    )
    tray = pystray.Icon("pc_power", tray_image(tray_state), "PC power agent", menu)
    # setup runs once the icon is visible; start the agent from there.
    tray.run(setup=lambda icon: (setattr(icon, "visible", True), agent.start()))


def main():
    global agent
    agent = Agent()
    if "--no-tray" in sys.argv:
        agent.start()
        while True:
            time.sleep(3600)
    run_tray()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("pc_power crashed")
        sys.exit(1)
