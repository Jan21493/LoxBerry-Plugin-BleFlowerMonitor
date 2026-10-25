#!/usr/bin/env python3
"""Poll MiFlora over BlueZ D-Bus and publish via MQTT and optional UDP."""

import argparse
import asyncio
import configparser
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sys
import threading
import time

from miflora.adapters import detect_adapters, inventory, select_adapter


def read_config(filename):
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(filename):
        raise ValueError(f"Plugin configuration file missing: {filename}")
    settings = config["MIFLORA"]
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
        if not settings.get("MQTTBROKER", "").strip():
            raise ValueError("MQTT broker is required")
        topic = settings.get("MQTTTOPIC", "miflora")
        if not topic.strip("/ \t") or any(char in topic for char in "+#\0"):
            raise ValueError("MQTT topic must be nonempty and contain no wildcards")
        check_port(settings.getint("MQTTPORT", 1883))
    if settings.get("UDPENABLED", "0") == "1":
        check_port(settings.getint("UDPPORT", 5006))
    return settings


def check_port(port):
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")


def miniserver_address(settings, directory):
    """Only UDP requires a LoxBerry Miniserver configuration."""
    server_id = settings.get("MINISERVER", "MINISERVER1").removeprefix("MINISERVER")
    directory = Path(directory)
    if (directory / "general.json").exists():
        with (directory / "general.json").open() as handle:
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

    connected = threading.Event()
    connection_error = []

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            connection_error.append(str(reason_code))
        connected.set()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect_timeout = 10
    client.on_connect = on_connect
    if settings.get("MQTTUSERNAME", ""):
        client.username_pw_set(settings["MQTTUSERNAME"], settings.get("MQTTPASSWORD", ""))
    try:
        client.connect(settings["MQTTBROKER"], settings.getint("MQTTPORT", 1883))
        client.loop_start()
        if not connected.wait(10):
            raise TimeoutError("MQTT broker did not acknowledge connection")
        if connection_error:
            raise ConnectionError(f"MQTT connection rejected: {connection_error[0]}")
        for address, values in samples:
            for key, value in values.items():
                # Keep the topic format and lowercasing used by earlier releases.
                topic = f"{settings.get('MQTTTOPIC', 'miflora')}/{address}/{key}".lower()
                message = client.publish(topic, str(value), qos=1)
                if message.rc != mqtt.MQTT_ERR_SUCCESS:
                    raise ConnectionError(f"MQTT publish failed for {topic}: {message.rc}")
                message.wait_for_publish(timeout=10)
                if not message.is_published():
                    raise TimeoutError(f"MQTT publish acknowledgement missing for {topic}")
        logging.info("MQTT delivery acknowledged for %s sensor(s)", len(samples))
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


async def poll(settings, datafile):
    from bleak.exc import BleakError
    from miflora.miflora_poller import read_sample
    from miflora.miflora_scanner import scan

    adapter = select_adapter(settings.get("BTADAPTER", "default"), detect_adapters())
    logging.info("Scanning with %s using Bleak / BlueZ D-Bus", adapter)
    datafile.write_text(f"Polling via {adapter} (Bleak / BlueZ D-Bus)...\n")
    devices = await scan(adapter)
    samples = []
    errors = []
    for device in devices:
        try:
            values = await read_sample(device)
            values.update(timestamp_values(settings.get("LOCALTIME", "1") == "1"))
            samples.append((device.address.replace(":", "").upper(), values))
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
    datafile.write_text("\n".join(lines + errors) + "\n")
    for line in lines:
        logging.info(line)

    if samples and settings.get("MQTTENABLED", "1") == "1":
        try:
            await asyncio.to_thread(mqtt_publish, settings, samples)
        except (OSError, ValueError, RuntimeError) as error:
            logging.error("MQTT delivery failed: %s", error)
            errors.append(f"ERROR MQTT: {error}")
            with datafile.open("a") as handle:
                handle.write(errors[-1] + "\n")
    if samples and settings.get("UDPENABLED", "0") == "1":
        try:
            host = miniserver_address(settings, os.environ.get("LBSCONFIG", "."))
            await asyncio.to_thread(udp_publish, settings, samples, host)
        except (OSError, ValueError, KeyError, configparser.Error) as error:
            logging.error("UDP delivery failed: %s", error)
            errors.append(f"ERROR UDP: {error}")
            with datafile.open("a") as handle:
                handle.write(errors[-1] + "\n")
    return 1 if errors else 0


def run(args):
    settings = read_config(args.configfile)
    if settings.get("ENABLED", "1") != "1":
        logging.info("Plugin disabled")
        return 0
    if args.daemon and int(time.strftime("%H")) % settings.getint("POLLFREQUENCY", 12):
        return 0
    if settings.get("MQTTENABLED", "1") != "1" and settings.get("UDPENABLED", "0") != "1":
        raise ValueError("Enable at least one output: MQTT or UDP")
    configfile = Path(args.configfile).resolve()
    datafile = Path(args.datafile) if args.datafile else (
        Path(os.environ["LBPDATA"]) / configfile.parent.name / "lastdata.dat"
        if "LBPDATA" in os.environ else configfile.parent / "lastdata.dat"
    )
    datafile.parent.mkdir(parents=True, exist_ok=True)
    with (datafile.parent / ".poll.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.warning("A MiFlora poll is already running")
            return 0
        from bleak.exc import BleakError
        try:
            return asyncio.run(poll(settings, datafile))
        except (BleakError, OSError, ValueError, RuntimeError) as error:
            datafile.write_text(f"ERROR: {error}\n")
            logging.exception("Bluetooth polling failed: %s", error)
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configfile", default="miflora.cfg")
    parser.add_argument("--logfile", default="xiaomi-miflora.log")
    parser.add_argument("--datafile")
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--list-adapters", action="store_true", help="Print sysfs adapter inventory as JSON")
    parser.add_argument("--debug", action="store_true", help="Enable detailed logging")
    args = parser.parse_args()
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
            RotatingFileHandler(args.logfile, maxBytes=1024 * 1024, backupCount=1),
            logging.StreamHandler(),
        ],
    )
    try:
        return run(args)
    except (OSError, ValueError, KeyError, RuntimeError, configparser.Error, ImportError) as error:
        logging.exception("MiFlora poll failed: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
