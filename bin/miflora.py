#!/usr/bin/env python3
"""Poll MiFlora over BlueZ D-Bus and publish via MQTT and optional UDP."""

import argparse
import asyncio
import configparser
from dataclasses import dataclass
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
import time

from miflora.adapters import adapter_kind, detect_adapters, inventory, select_adapter
from miflora.session import poll_session

SCAN_RETRY_DELAY = 10
LEGACY_MQTT_KEYS = ("MQTTBROKER", "MQTTPORT", "MQTTUSERNAME", "MQTTPASSWORD")


def read_config(filename):
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(filename, encoding="utf-8"):
        raise ValueError(f"Plugin configuration file missing: {filename}")
    section = "FLOWERMON" if config.has_section("FLOWERMON") else "MIFLORA"
    if not config.has_section(section):
        raise ValueError(f"Plugin configuration section missing: FLOWERMON")
    settings = config[section]
    for key in LEGACY_MQTT_KEYS:
        settings.pop(key, None)
    # Config::Simple quotes and escapes punctuation when saving the web form.
    for key, value in settings.items():
        if value.startswith('"') and value.endswith('"'):
            settings[key] = json.loads(value)
    for key, default in (
        ("ENABLED", "1"), ("LOCALTIME", "1"),
        ("MQTTENABLED", "1"), ("UDPENABLED", "0"),
    ):
        if settings.get(key, default) not in {"0", "1"}:
            raise ValueError(f"{key} must be 0 or 1")
    if settings.getint("POLLFREQUENCY", 12) not in {1, 2, 4, 8, 12, 24}:
        raise ValueError("Invalid polling frequency")
    if settings.get("MQTTENABLED", "1") == "1":
        topic = settings.get("MQTTTOPIC", "flowermon")
        if not topic.strip("/ \t") or any(char in topic for char in "+#\0"):
            raise ValueError("MQTT topic must be nonempty and contain no wildcards")
    if settings.get("UDPENABLED", "0") == "1":
        check_port(settings.getint("UDPPORT", 5006))
    return settings


def migrate_config(filename):
    """Migrate the config section and persist adapter type before hci numbers change."""
    config = configparser.ConfigParser(interpolation=None)
    config.optionxform = str
    if not config.read(filename, encoding="utf-8"):
        raise ValueError(f"Plugin configuration file missing: {filename}")
    if not config.has_section("FLOWERMON"):
        if not config.has_section("MIFLORA"):
            raise ValueError("Plugin configuration section missing: FLOWERMON")
        config["FLOWERMON"] = dict(config["MIFLORA"])
        config.remove_section("MIFLORA")
    settings = config["FLOWERMON"]
    selection = settings.get("BTADAPTER", "serial")
    if selection not in {"default", "usb", "serial"}:
        settings["BTADAPTER"] = adapter_kind(selection, detect_adapters())
    for key in LEGACY_MQTT_KEYS:
        settings.pop(key, None)
    with Path(filename).open("w", encoding="utf-8") as handle:
        config.write(handle)


def check_port(port):
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")


@dataclass(frozen=True)
class MQTTConnection:
    host: str
    port: int
    username: str
    password: str
    tls: bool = False
    verify: bool = True
    cafile: str = ""


def mqtt_connectiondetails(directory=None):
    """Read the MQTT Basics schema used by LoxBerry::IO and loxberry.io."""
    directory = Path(directory or os.environ.get("LBSCONFIG", "/opt/loxberry/config/system"))
    try:
        with (directory / "general.json").open(encoding="utf-8") as handle:
            mqtt = json.load(handle)["Mqtt"]
        if not isinstance(mqtt, dict):
            raise ValueError("Mqtt must be an object")
        host = mqtt["Brokerhost"]
        if not isinstance(host, str) or not host.strip():
            raise ValueError("Brokerhost must be nonempty")
        port = int(mqtt["Brokerport"])
        check_port(port)
        username = mqtt.get("Brokeruser") or ""
        password = mqtt.get("Brokerpass") or ""
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError("Broker credentials must be strings")
        def enabled(value):
            return str(value).lower() in {"1", "true", "yes", "on"}

        local = enabled(mqtt.get("Uselocalbroker", "true"))
        tls = enabled(mqtt.get("Tlsenabled" if local else "TlsExternalEnabled", "false"))
        verify = not local and enabled(mqtt.get("TlsExternalValidatecert", "false"))
        cafile = ""
        if tls:
            if local:
                port = int(mqtt.get("Tlsport", 8883))
                check_port(port)
                cafile = "/etc/mosquitto/tls/ca.crt"
            else:
                cafile = str(directory / "mqtt_external_ca.crt")
        return MQTTConnection(host, port, username, password, tls, verify, cafile)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(
            "Cannot read LoxBerry MQTT Basics. Configure MQTT Basics in LoxBerry "
            "and check general.json and its permissions."
        ) from error


def miniserver_address(settings, directory):
    """Only UDP requires a LoxBerry Miniserver configuration."""
    server_id = settings.get("MINISERVER", "MINISERVER1").removeprefix("MINISERVER")
    directory = Path(directory)
    if (directory / "general.json").exists():
        with (directory / "general.json").open(encoding="utf-8") as handle:
            servers = json.load(handle)["Miniserver"]
        if server_id not in servers:
            raise ValueError(f"Miniserver {server_id} not found in general.json")
        return servers[server_id]["Ipaddress"]
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(directory / "general.cfg"):
        raise ValueError("UDP enabled but no LoxBerry Miniserver configuration found")
    return config.get(f"MINISERVER{server_id}", "IPADDRESS")


def timestamp_values(localtime):
    now = time.time()
    offset = time.localtime(now).tm_gmtoff if localtime else 0
    return {
        "PollTime": int(now) + offset - 1230768000,
        "PollTimeString": time.strftime("%d.%m.%Y %H:%M:%S", time.localtime(now)),
    }


def mqtt_publish(settings, samples):
    """Wait for CONNACK and every QoS 1 acknowledgement before disconnecting."""
    from paho.mqtt import client as mqtt

    connection = mqtt_connectiondetails()
    connected = threading.Event()
    connection_error = []

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            connection_error.append(str(reason_code))
        connected.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect_timeout = 10
    client.on_connect = on_connect
    if connection.username:
        client.username_pw_set(connection.username, connection.password)
    if connection.tls:
        context = ssl.create_default_context(cafile=connection.cafile if connection.verify else None)
        if not connection.verify:
            logging.warning("MQTT Basics disables TLS certificate verification")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        client.tls_set_context(context)
    try:
        client.connect(connection.host, connection.port)
        client.loop_start()
        if not connected.wait(10):
            raise TimeoutError("MQTT broker did not acknowledge connection")
        if connection_error:
            raise ConnectionError(f"MQTT connection rejected: {connection_error[0]}")
        for address, values in samples:
            for key, value in values.items():
                # Keep the topic format and lowercasing used by earlier releases.
                topic = f"{settings.get('MQTTTOPIC', 'flowermon')}/{address}/{key}".lower()
                message = client.publish(topic, str(value), qos=1)
                if message.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise ConnectionError(f"MQTT publish failed for {topic}: {message.rc}")
                message.wait_for_publish(timeout=10)
                if not message.is_published():
                    raise TimeoutError(f"MQTT publish acknowledgement missing for {topic}")
        logging.info("MQTT delivery acknowledged for %s sensor(s)", len(samples), extra={"success": True})
    finally:
        client.disconnect()
        client.loop_stop()


def udp_publish(settings, samples, host):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(10)
        for address, values in samples:
            for key, value in values.items():
                payload = f"{address}.{key}={value}".encode()
                if connection.sendto(payload, (host, settings.getint("UDPPORT", 5006))) != len(payload):
                    raise OSError("Incomplete UDP datagram")
    logging.info("UDP sent for %s sensor(s) to %s:%s (no acknowledgement)",
                 len(samples), host, settings.getint("UDPPORT", 5006))


async def poll(settings, datafile):
    from bleak.exc import BleakError
    from miflora.miflora_poller import read_sample
    from miflora.miflora_scanner import scan

    selection = settings.get("BTADAPTER", "serial")
    adapters = detect_adapters()
    adapter = select_adapter(selection, adapters)
    selected = next(item for item in adapters if item.id == adapter)
    logging.info("Adapter selection: %s - %s",
                 {"serial": "Onboard", "usb": "USB", "default": "Default - hci0"}.get(selection, selection),
                 selected.label)
    logging.info("Scanning with %s using Bleak / BlueZ D-Bus", adapter)
    datafile.write_text(f"Polling via {adapter} (Bleak / BlueZ D-Bus)...\n", encoding="utf-8")
    try:
        devices = await scan(adapter)
    except BleakError as error:
        # Some USB adapters are reset by the kernel after a scan timeout and get a new hci number.
        logging.warning("Scan with %s failed (%s); retrying once", adapter, error)
        await asyncio.sleep(SCAN_RETRY_DELAY)
        adapters = detect_adapters()
        adapter = select_adapter(selection, adapters)
        selected = next(item for item in adapters if item.id == adapter)
        logging.info("Adapter after retry (%s): %s",
                     {"serial": "Onboard", "usb": "USB", "default": "Default - hci0"}.get(selection, selection),
                     selected.label)
        logging.info("Retrying scan with %s", adapter)
        datafile.write_text(f"Polling via {adapter} (Bleak / BlueZ D-Bus)...\n", encoding="utf-8")
        devices = await scan(adapter)
    logging.info("Scan finished: %s MiFlora sensor(s) found", len(devices))
    samples = []
    errors = []
    for device in devices:
        try:
            logging.info("Reading sensor %s", device.address)
            values = await read_sample(device)
            values.update(timestamp_values(settings.get("LOCALTIME", "1") == "1"))
            samples.append((device.address.replace(":", "").upper(), values))
            with datafile.open("a", encoding="utf-8") as handle:
                for key, value in values.items():
                    handle.write(f"{device.address.replace(':', '').upper()}.{key}={value}\n")
            logging.info("Sensor %s read successfully", device.address, extra={"success": True})
        except (BleakError, OSError, asyncio.TimeoutError, ValueError) as error:
            logging.error("Cannot poll %s: %s", device.address, error)
            errors.append(f"ERROR {device.address}: {error}")

    lines = [
        f"{address}.{key}={value}"
        for address, values in samples for key, value in values.items()
    ]
    if not devices:
        lines.append("No MiFlora sensors found. Check range, batteries and adapter selection.")
        logging.warning(lines[-1])
    datafile.write_text("\n".join(lines + errors) + "\n", encoding="utf-8")
    for line in lines:
        logging.info(line)

    if samples and settings.get("MQTTENABLED", "1") == "1":
        try:
            logging.info("Sending values via MQTT")
            await asyncio.to_thread(mqtt_publish, settings, samples)
        except (OSError, ValueError, RuntimeError) as error:
            logging.error("MQTT delivery failed: %s", error)
            errors.append(f"ERROR MQTT: {error}")
            with datafile.open("a", encoding="utf-8") as handle:
                handle.write(errors[-1] + "\n")
    if samples and settings.get("UDPENABLED", "0") == "1":
        try:
            logging.info("Sending values via UDP")
            host = miniserver_address(settings, os.environ.get("LBSCONFIG", "."))
            await asyncio.to_thread(udp_publish, settings, samples, host)
        except (OSError, ValueError, KeyError, configparser.Error) as error:
            logging.error("UDP delivery failed: %s", error)
            errors.append(f"ERROR UDP: {error}")
            with datafile.open("a", encoding="utf-8") as handle:
                handle.write(errors[-1] + "\n")
    return 1 if errors else 0


def run(args):
    startup_error = None
    try:
        settings = read_config(args.configfile)
    except (OSError, ValueError, KeyError, configparser.Error) as error:
        startup_error = error
        settings = None
    if args.daemon and settings is not None:
        if settings.get("ENABLED", "1") != "1":
            return 0
        if int(time.strftime("%H")) % settings.getint("POLLFREQUENCY", 12):
            return 0
    configfile = Path(args.configfile).resolve()
    datafile = Path(args.datafile) if args.datafile else (
        Path(os.environ["LBPDATA"]) / configfile.parent.name / "lastdata.dat"
        if "LBPDATA" in os.environ else configfile.parent / "lastdata.dat"
    )
    datafile.parent.mkdir(parents=True, exist_ok=True)
    inherited_fd = getattr(args, "lock_fd", None)
    lockfile = (os.fdopen(inherited_fd, "a") if inherited_fd is not None
                else (datafile.parent / ".poll.lock").open("a"))
    with lockfile as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.warning("A MiFlora poll is already running")
            return 0
        logfile = getattr(args, "logfile", str(datafile.parent / "xiaomi-miflora.log"))
        level = logging.DEBUG if getattr(args, "debug", False) else None
        with poll_session(datafile, logfile, level) as outcome:
            from bleak.exc import BleakError
            try:
                if startup_error is not None:
                    raise startup_error
                if settings.get("ENABLED", "1") != "1":
                    logging.warning("Plugin disabled; enable it in Settings before polling")
                    datafile.write_text("Plugin disabled; no scan performed.\n", encoding="utf-8")
                    outcome["status"] = "DISABLED"
                    return 0
                if settings.get("MQTTENABLED", "1") != "1" and settings.get("UDPENABLED", "0") != "1":
                    raise ValueError("Enable at least one output: MQTT or UDP")
                result = asyncio.run(poll(settings, datafile))
                outcome["status"] = "ERROR" if result else "OK"
                return result
            except (BleakError, OSError, ValueError, RuntimeError, KeyError, configparser.Error) as error:
                with datafile.open("a", encoding="utf-8") as handle:
                    handle.write(f"ERROR: {error}\n")
                logging.exception("Bluetooth polling failed: %s", error)
                return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configfile", default="miflora.cfg")
    parser.add_argument("--logfile", default="xiaomi-miflora.log")
    parser.add_argument("--datafile")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--list-adapters", action="store_true", help="Print sysfs adapter inventory as JSON")
    parser.add_argument("--migrate-config", action="store_true", help="Migrate legacy adapter selection to USB/Onboard")
    parser.add_argument("--debug", action="store_true", help="Enable detailed logging")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.migrate_config:
        try:
            migrate_config(args.configfile)
            return 0
        except (OSError, ValueError, KeyError, configparser.Error) as error:
            print(f"Cannot migrate MiFlora settings: {error}", file=sys.stderr)
            return 1
    if args.list_adapters:
        try:
            print(json.dumps(inventory()))
            return 0
        except OSError as error:
            print(f"Cannot enumerate Bluetooth adapters: {error}", file=sys.stderr)
            return 1
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(args.logfile, maxBytes=1024 * 1024, backupCount=1, encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    for handler in logging.getLogger().handlers:
        handler.setLevel(logging.DEBUG if args.debug else logging.INFO)
    try:
        return run(args)
    except (OSError, ValueError, KeyError, RuntimeError, configparser.Error, ImportError) as error:
        logging.exception("MiFlora poll failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
