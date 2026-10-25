"""Discover MiFlora devices on one BlueZ adapter."""

import asyncio

from bleak import BleakScanner
from bleak.backends.bluezdbus.scanner import BleakScannerBlueZDBus

VALID_DEVICE_NAMES = {"flower mate", "flower care"}
DEVICE_PREFIX = "C4:7C:8D:"


async def scan(adapter, timeout=10):
    devices = {}

    def detected(device, advertisement):
        name = advertisement.local_name or device.name or ""
        if (name.lower() in VALID_DEVICE_NAMES
                or device.address.upper().startswith(DEVICE_PREFIX)):
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
