"""Shared adapter inventory and selection for the web UI and poller.

Ported and adapted from Jan21493/LoxBerry-Plugin-TeslaCmd,
webfrontend/htmlauth/tesla_inc.php (Apache-2.0), commit
49cc2c3598a3b3e5d7c6f7b8c24b214170671d51.
Uses sysfs instead of the deprecated hciconfig command.
"""

from dataclasses import asdict, dataclass
from pathlib import Path
import re

KNOWN_USB_ADAPTERS = {
    "0bda:a725": "Realtek RTL8761B",
    "0bda:a728": "Realtek RTL8761BU or newer",
    "0a12:0001": "CSR8510 A10",
    "0a5c:21e8": "Broadcom BCM20702A0",
    "8087:0026": "Intel Wireless Bluetooth",
    "8087:0032": "Intel Wireless Bluetooth",
    "8087:0033": "Intel Wireless Bluetooth",
}


@dataclass(frozen=True)
class Adapter:
    id: str
    kind: str
    label: str
    address: str


def _read(path):
    if not path.exists():
        return ""
    return path.read_text().replace("\0", "").strip()


def detect_adapters(sys_root=Path("/sys")):
    """Match TeslaCmd's natural hci ordering and USB/UART sysfs detection."""
    adapters = []
    paths = [
        path for path in (sys_root / "class/bluetooth").glob("hci*")
        if re.fullmatch(r"hci\d+", path.name)
    ]
    model = _read(sys_root / "firmware/devicetree/base/model")
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
        details = [{"usb": "USB", "serial": "Onboard / UART", "other": subsystem}.get(kind)]
        if usb:
            chip = f"{_read(usb / 'idVendor')}:{_read(usb / 'idProduct')}".lower()
            details.extend([
                _read(usb / "manufacturer"), _read(usb / "product"),
                KNOWN_USB_ADAPTERS.get(chip, ""), chip.upper(),
            ])
        elif kind == "serial" and model:
            details.append(model)
        address = _read(path / "address")
        details.append(address)
        label = " / ".join(dict.fromkeys(detail for detail in details if detail))
        adapters.append(Adapter(path.name, kind, f"{path.name} - {label}", address))
    return adapters


def select_adapter(selection, adapters):
    if selection == "default":
        selection = "hci0"
    if selection not in {"usb", "serial"} and not re.fullmatch(r"hci\d+", selection):
        raise ValueError(f"Invalid Bluetooth adapter selection: {selection}")
    for adapter in adapters:
        if adapter.id == selection or adapter.kind == selection:
            return adapter.id
    raise ValueError(
        f"Bluetooth adapter '{selection}' not detected. "
        "Check the connection, BlueZ and the adapter setting; no fallback was used."
    )


def inventory():
    return [asdict(adapter) for adapter in detect_adapters()]
