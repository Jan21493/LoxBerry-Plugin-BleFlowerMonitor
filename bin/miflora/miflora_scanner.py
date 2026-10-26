"""Discover MiFlora devices on one BlueZ adapter."""

import asyncio
import logging

from bleak import BleakScanner
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus

VALID_DEVICE_NAMES = {"flower mate", "flower care"}
DEVICE_PREFIX = "C4:7C:8D:"
RSSI_AT_ONE_METER = -59


def estimate_distance_m(rssi):
    """Estimate free-space distance with an uncalibrated -59 dBm 1 m reference."""
    if rssi is None:
        return None
    return max(1, round(10 ** ((RSSI_AT_ONE_METER - rssi) / 20)))


async def scan(adapter, timeout=10):
    devices = {}

    def detected(device, advertisement):
        name = advertisement.local_name or device.name or ""
        if (name.lower() in VALID_DEVICE_NAMES
                or device.address.upper().startswith(DEVICE_PREFIX)):
            if device.address.upper() not in devices:
                distance = estimate_distance_m(advertisement.rssi)
                distance_text = (f", approx. {distance} m line of sight without walls"
                                 if distance is not None else "")
                logging.info("Discovered %s (%s), RSSI %s dBm%s",
                             device.address, name, advertisement.rssi, distance_text)
            devices[device.address.upper()] = device

    # adapter is needed by Bleak <= 2.0; newer releases use bluez["adapter"].
    async with BleakScanner(
        detection_callback=detected,
        backend=BleakScannerBlueZDBus,
        adapter=adapter,
        bluez={"adapter": adapter},
    ):
        await asyncio.sleep(timeout)
    return [devices[address] for address in sorted(devices)]
