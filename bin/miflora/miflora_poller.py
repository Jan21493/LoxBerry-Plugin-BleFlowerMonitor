"""Read a complete MiFlora sample over BlueZ GATT using Bleak."""

import asyncio
import logging
import re
import struct

from bleak import BleakClient
from bleak.backends.bluezdbus.client import BleakClientBlueZDBus
from bleak.exc import BleakError

NAME_UUID = "00002a00-0000-1000-8000-00805f9b34fb"
MODE_UUID = "00001a00-0000-1000-8000-00805f9b34fb"
DATA_UUID = "00001a01-0000-1000-8000-00805f9b34fb"
FIRMWARE_UUID = "00001a02-0000-1000-8000-00805f9b34fb"


def decode_sample(name, firmware_data, sensor_data):
    """Validate the wire format before publishing any values."""
    if len(firmware_data) < 3 or firmware_data[0] > 100:
        raise ValueError("Invalid MiFlora battery/firmware response")
    firmware = bytes(firmware_data[2:]).decode("ascii").rstrip("\0")
    if not re.fullmatch(r"\d+\.\d+\.\d+", firmware):
        raise ValueError("Invalid MiFlora firmware version")
    version = tuple(map(int, firmware.split(".")))
    if (len(sensor_data) != 16 or not any(sensor_data)
            or sensor_data[7] > 100
            or (version >= (2, 6, 6) and not any(sensor_data[10:]))):
        raise ValueError("Invalid MiFlora sensor response")
    temperature, light, moisture, conductivity = struct.unpack(
        "<hxIBHxxxxxx", sensor_data
    )
    if not name:
        raise ValueError("Empty MiFlora device name")
    return {
        "Name": name,
        "Firmware": firmware,
        "Temperature": temperature / 10.0,
        "Moisture": moisture,
        "Light": light,
        "Conductivity": conductivity,
        "Battery": firmware_data[0],
    }


async def read_sample(device, retries=3):
    """Use the discovered BLEDevice so the client keeps the selected adapter."""
    for attempt in range(1, retries + 1):
        try:
            return await asyncio.wait_for(_read_sample(device), timeout=30)
        except (BleakError, OSError, asyncio.TimeoutError, ValueError) as error:
            if attempt == retries:
                raise
            logging.warning("Read %s failed (%s/%s): %s",
                            device.address, attempt, retries, error)
            await asyncio.sleep(1)


async def _read_sample(device):
    logging.info("Connecting to %s via BlueZ GATT", device.address)
    async with BleakClient(device, backend=BleakClientBlueZDBus, timeout=15) as client:
        logging.info("Connected to %s; reading firmware and battery", device.address)
        name = bytes(await client.read_gatt_char(NAME_UUID)).decode("utf-8").rstrip("\0")
        firmware_data = await client.read_gatt_char(FIRMWARE_UUID)
        if len(firmware_data) < 3:
            raise ValueError("Short MiFlora firmware response")
        firmware = bytes(firmware_data[2:]).decode("ascii").rstrip("\0")
        if not re.fullmatch(r"\d+\.\d+\.\d+", firmware):
            raise ValueError("Invalid MiFlora firmware version")
        if tuple(map(int, firmware.split("."))) >= (2, 6, 6):
            logging.info("Enabling live measurements for %s (firmware %s)", device.address, firmware)
            await client.write_gatt_char(MODE_UUID, b"\xa0\x1f", response=True)
            await asyncio.sleep(0.2)
        data = await client.read_gatt_char(DATA_UUID)
        logging.info("Measurements received from %s", device.address)
        return decode_sample(name, firmware_data, data)
