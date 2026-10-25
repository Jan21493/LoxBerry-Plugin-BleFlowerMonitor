"""Shared adapter inventory and selection for the web UI and poller.

Ported and adapted from Jan21493/LoxBerry-Plugin-TeslaCmd,
webfrontend/htmlauth/tesla_inc.php (Apache-2.0), commit
49cc2c3598a3b3e5d7c6f7b8c24b214170671d51.
Uses sysfs for selection and hciconfig only for read-only HCI diagnostics.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import logging
import os
import shutil
import subprocess

KNOWN_USB_ADAPTERS = {
    "0bda:a725": "Realtek Bluetooth USB-Adapter (RTL8761B)",
    "0bda:a728": "Realtek Bluetooth 5.4 USB-Adapter (RTL8761BU or newer)",
    "0a12:0001": "CSR8510 A10 Bluetooth USB-Adapter",
    "0a5c:21e8": "Broadcom BCM20702A0 Bluetooth USB-Adapter",
    "8087:0026": "Intel Wireless Bluetooth adapter",
    "8087:0032": "Intel Wireless Bluetooth adapter",
    "8087:0033": "Intel Wireless Bluetooth adapter",
}


@dataclass(frozen=True)
class Adapter:
    id: str
    kind: str
    label: str
    address: str
    selection_label: str = ""


def _read(path):
    if not path.exists():
        return ""
    return path.read_text().replace("\0", "").strip()


def hci_details():
    """Read version and revision as in TeslaCmd; never change controller state."""
    command = shutil.which("hciconfig")
    if not command:
        logging.warning("hciconfig unavailable; HCI version/revision cannot be displayed")
        return {}
    try:
        result = subprocess.run(
            [command, "-a"], capture_output=True, text=True, timeout=5,
            env={**os.environ, "LC_ALL": "C"}, check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logging.warning("Cannot read HCI version/revision: %s", error)
        return {}
    return parse_hci_details(result.stdout)


def parse_hci_details(output):
    details = {}
    current = None
    for line in output.splitlines():
        match = re.match(r"^(hci\d+):", line)
        if match:
            current = match[1]
        match = re.match(r"\s*HCI Version:\s*(.+)", line)
        if current and match:
            details[current] = "HCI " + match[1].strip()
    return details


def detect_adapters(sys_root=Path("/sys")):
    """Match TeslaCmd's natural hci ordering and USB/UART sysfs detection."""
    adapters = []
    paths = [
        path for path in (sys_root / "class/bluetooth").glob("hci*")
        if re.fullmatch(r"hci\d+", path.name)
    ]
    model = _read(sys_root / "firmware/devicetree/base/model")
    versions = hci_details() if paths else {}
    for path in sorted(paths, key=lambda entry: int(entry.name[3:])):
        target = path.resolve()
        device = (path / "device").resolve()
        parts = target.parts + device.parts
        usb = next(
            (parent for parent in (device, *device.parents)
             if (parent / "idVendor").exists() and (parent / "idProduct").exists()),
            None,
        )
        is_usb = usb is not None or any(part.startswith("usb") for part in parts)
        is_serial = any(
            part == "tty" or "serial" in part or part.startswith("uart")
            for part in parts
        )
        subsystem = (path / "device/subsystem").resolve().name
        kind = "usb" if is_usb else (
            "serial" if is_serial or subsystem in {"platform", "amba", "sdio"} else "other"
        )
        bus = {"usb": "USB", "serial": "Onboard - UART", "other": subsystem}[kind]
        chip = ""
        if usb:
            chip = f"{_read(usb / 'idVendor')}:{_read(usb / 'idProduct')}".lower()
            description = KNOWN_USB_ADAPTERS.get(chip) or _read(usb / "product") or "USB Bluetooth adapter"
            selection_label = f"USB - currently mapped to {path.name} - {description}"
        else:
            description = f"{model} Bluetooth" if kind == "serial" and model else bus
            selection_label = f"Onboard - {description}"
        address = _read(path / "address")
        details = [description, address, versions.get(path.name), chip.upper()]
        separator = " - " if kind == "serial" else " / "
        label = separator.join(dict.fromkeys(detail for detail in details if detail))
        adapters.append(Adapter(path.name, kind, f"{path.name} - {label}", address, selection_label))
    return adapters


def adapter_kind(selection, adapters):
    """Keep supported choices and resolve legacy explicit hci selections."""
    if selection in {"default", "usb", "serial"}:
        return selection
    for adapter in adapters:
        if adapter.id == selection and adapter.kind in {"usb", "serial"}:
            return adapter.kind
    raise ValueError(
        f"Cannot migrate Bluetooth selection '{selection}'. "
        "Select Default, USB or Onboard in the plugin settings."
    )


def select_adapter(selection, adapters):
    if selection not in {"default", "usb", "serial"}:
        raise ValueError("Select Default, USB or Onboard in the plugin settings and save.")
    for adapter in sorted(adapters, key=lambda item: int(item.id[3:])):
        if (selection == "default" and adapter.id == "hci0") or adapter.kind == selection:
            return adapter.id
    raise ValueError(
        f"Bluetooth adapter '{selection}' not detected. "
        "Check the connection, BlueZ and the adapter setting; no fallback was used."
    )


def inventory():
    return [asdict(adapter) for adapter in detect_adapters()]
