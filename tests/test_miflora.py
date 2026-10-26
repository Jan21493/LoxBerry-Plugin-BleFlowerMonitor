import argparse
import asyncio
import configparser
import importlib.util
from html.parser import HTMLParser
import json
import logging
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from miflora import adapters, miflora_poller as poller, miflora_scanner as scanner
from miflora import session
from bleak.exc import BleakError

spec = importlib.util.spec_from_file_location("plugin", ROOT / "bin/miflora.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def settings(**changes):
    config = configparser.ConfigParser(interpolation=None)
    config.read(ROOT / "config/miflora.cfg")
    config["FLOWERMON"].update(changes)
    return config["FLOWERMON"]


def sensor_data(**changes):
    values = dict(temperature=-123, light=100000, moisture=45, conductivity=54321)
    values.update(changes)
    return struct.pack("<hxIBH", *values.values()) + b"\x01\0\0\0\0\0"


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.diagnostics = patch.object(adapters, "hci_details", return_value={
            "hci10": "HCI 5.4 (0xd) Revision: 0x1234",
        })
        self.diagnostics.start()
        self.addCleanup(self.diagnostics.stop)

    def test_inventory_type_selection_metadata_and_natural_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controllers = root / "class/bluetooth"
            controllers.mkdir(parents=True)
            model = root / "firmware/devicetree/base/model"
            model.parent.mkdir(parents=True)
            model.write_text("Raspberry Pi 5 Model B Rev 1.1\0")
            uart = root / "devices/platform/serial/tty/ttyAMA0/bluetooth/hci2"
            usb_parent = root / "devices/pci/usb1/1-1"
            usb = usb_parent / "1-1:1.0/bluetooth/hci10"
            for target in (usb, uart):
                target.mkdir(parents=True)
                (controllers / target.name).symlink_to(target)
                (target / "device").symlink_to(target.parent)
                (target / "address").write_text("AA:BB:CC:DD:EE:FF")
            for key, value in {
                "idVendor": "0bda", "idProduct": "a728",
                "manufacturer": "Realtek", "product": "Bluetooth Radio",
            }.items():
                (usb_parent / key).write_text(value)
            detected = adapters.detect_adapters(root)
            self.assertEqual([item.id for item in detected], ["hci2", "hci10"])
            self.assertEqual([item.kind for item in detected], ["serial", "usb"])
            self.assertTrue(detected[1].label.startswith("hci10 - "))
            self.assertTrue(detected[1].label.endswith("0BDA:A728"))
            self.assertIn("HCI 5.4 (0xd) Revision: 0x1234", detected[1].label)
            self.assertEqual(detected[1].selection_label,
                             "USB - currently mapped to hci10 - Realtek Bluetooth 5.4 USB-Adapter (RTL8761BU or newer)")
            self.assertEqual(detected[0].selection_label,
                             "Onboard - Raspberry Pi 5 Model B Rev 1.1 Bluetooth")
            self.assertNotIn("/", detected[0].selection_label)
            self.assertEqual(adapters.select_adapter("usb", detected), "hci10")
            self.assertEqual(adapters.select_adapter("serial", detected), "hci2")
            self.assertEqual(adapters.adapter_kind("hci10", detected), "usb")
            with self.assertRaisesRegex(ValueError, "no fallback"):
                adapters.select_adapter("default", detected)

    def test_hci_version_includes_revision_for_all_adapters(self):
        self.assertEqual(adapters.parse_hci_details("""
hci6: Type: Primary Bus: USB
    HCI Version: 5.4 (0xd)  Revision: 0x1234
hci0: Type: Primary Bus: UART
    HCI Version: 5.0 (0x9)  Revision: 0x15c
"""), {"hci6": "HCI 5.4 (0xd)  Revision: 0x1234", "hci0": "HCI 5.0 (0x9)  Revision: 0x15c"})

    def test_chip_description_is_based_on_actual_vid_pid(self):
        self.assertIn("(RTL8761B)", adapters.KNOWN_USB_ADAPTERS["0bda:a725"])
        self.assertNotIn("5.4", adapters.KNOWN_USB_ADAPTERS["0bda:a725"])

    def test_missing_and_invalid_selections_do_not_fallback(self):
        for selection in ("usb", "serial", "default", "hci3", "other", "../hci0"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                adapters.select_adapter(selection, [])

    def test_first_usb_sorted_by_number_not_name(self):
        detected = [adapters.Adapter("hci2", "usb", "", ""), adapters.Adapter("hci10", "usb", "", "")]
        self.assertEqual(adapters.select_adapter("usb", detected), "hci2")
        self.assertEqual(adapters.select_adapter("usb", list(reversed(detected))), "hci2")
        renumbered = [adapters.Adapter("hci6", "usb", "", ""), adapters.Adapter("hci0", "serial", "", "")]
        self.assertEqual(adapters.select_adapter("usb", renumbered), "hci6")
        self.assertEqual(adapters.adapter_kind("default", renumbered), "default")
        self.assertEqual(adapters.select_adapter("default", renumbered), "hci0")
        self.assertEqual(adapters.select_adapter("default", [
            adapters.Adapter("hci0", "usb", "", ""),
        ]), "hci0")
        self.assertEqual(adapters.adapter_kind("hci6", renumbered), "usb")


class ProtocolTests(unittest.TestCase):
    def test_sample_preserves_names_and_decodes_full_unsigned_values(self):
        sample = poller.decode_sample("Flower care", b"\x56\0" + b"3.10.0", sensor_data())
        self.assertEqual(sample, {
            "Name": "Flower care", "Firmware": "3.10.0", "Temperature": -12.3,
            "Moisture": 45, "Light": 100000, "Conductivity": 54321, "Battery": 86,
        })

    def test_invalid_sensor_frames(self):
        for data in (b"", b"\0" * 16, sensor_data(moisture=101), sensor_data()[:10] + b"\0" * 6):
            with self.subTest(data=data), self.assertRaises(ValueError):
                poller.decode_sample("Flower care", b"\x50\0" + b"3.1.0", data)

    def test_invalid_firmware_frames(self):
        for data in (b"", b"\x50", b"\xff\0" + b"3.1.0", b"\x50\0garbage"):
            with self.subTest(data=data), self.assertRaises(ValueError):
                poller.decode_sample("Flower care", data, sensor_data())

    def test_old_firmware_does_not_need_new_trailer(self):
        sample = poller.decode_sample("Flower mate", b"\x50\0" + b"2.6.5", sensor_data()[:10] + b"\0" * 6)
        self.assertEqual(sample["Moisture"], 45)


class BLETests(unittest.IsolatedAsyncioTestCase):
    async def test_scanner_passes_adapter_and_deduplicates_devices(self):
        device = SimpleNamespace(address="c4:7c:8d:00:00:01", name=None)
        named = SimpleNamespace(address="AA:BB:CC:00:00:02", name="Flower mate")
        other = SimpleNamespace(address="AA:BB:CC:00:00:03", name="not a sensor")
        factory = MagicMock()

        async def start():
            callback = factory.call_args.kwargs["detection_callback"]
            for entry in (device, device, named, other):
                callback(entry, SimpleNamespace(local_name=None, rssi=-61))

        factory.return_value.__aenter__ = AsyncMock(side_effect=start)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(scanner, "BleakScanner", factory), patch.object(scanner.asyncio, "sleep", AsyncMock()):
            with self.assertLogs(level="INFO") as captured:
                found = await scanner.scan("hci2")
        self.assertEqual(found, [named, device])
        self.assertEqual(factory.call_args.kwargs["adapter"], "hci2")
        self.assertEqual(factory.call_args.kwargs["bluez"]["adapter"], "hci2")
        self.assertEqual(len(captured.records), 2)
        self.assertTrue(all(record.levelname == "INFO" for record in captured.records))
        self.assertTrue(any("Discovered" in record.message for record in captured.records))
        self.assertTrue(any("approx. 1 m line of sight without walls" in record.message for record in captured.records))

    def test_rssi_distance_is_documented_free_space_estimate(self):
        self.assertEqual(scanner.estimate_distance_m(-59), 1)
        self.assertEqual(scanner.estimate_distance_m(-79), 10)
        self.assertEqual(scanner.estimate_distance_m(-94), 56)
        self.assertIsNone(scanner.estimate_distance_m(None))

    async def test_gatt_reads_use_discovered_device_and_uuid(self):
        for firmware, writes in (("2.6.5", 0), ("2.6.6", 1), ("2.10.0", 1)):
            with self.subTest(firmware=firmware):
                device = SimpleNamespace(address="C4:7C:8D:00:00:01")
                client = SimpleNamespace(
                    read_gatt_char=AsyncMock(side_effect=[
                        b"Flower care", b"\x50\0" + firmware.encode(), sensor_data(),
                    ]),
                    write_gatt_char=AsyncMock(),
                )
                factory = MagicMock()
                factory.return_value.__aenter__ = AsyncMock(return_value=client)
                factory.return_value.__aexit__ = AsyncMock(return_value=False)
                with patch.object(poller, "BleakClient", factory), patch.object(poller.asyncio, "sleep", AsyncMock()):
                    with self.assertLogs(level="DEBUG") as captured:
                        result = await poller.read_sample(device)
                self.assertIs(factory.call_args.args[0], device)
                self.assertEqual(result["Firmware"], firmware)
                self.assertEqual(client.write_gatt_char.await_count, writes)
                self.assertTrue(all(record.levelname == "DEBUG" for record in captured.records))
                self.assertEqual(
                    [call.args[0] for call in client.read_gatt_char.await_args_list],
                    [
                        poller.NAME_UUID,
                        "00001a02-0000-1000-8000-00805f9b34fb",
                        "00001a01-0000-1000-8000-00805f9b34fb",
                    ],
                )
                if writes:
                    client.write_gatt_char.assert_awaited_once_with(
                        "00001a00-0000-1000-8000-00805f9b34fb", b"\xa0\x1f", response=True
                    )

    async def test_retry_is_bounded_and_failure_not_hidden(self):
        with patch.object(poller, "_read_sample", AsyncMock(side_effect=BleakError("offline"))) as read:
            with patch.object(poller.asyncio, "sleep", AsyncMock()):
                with self.assertRaisesRegex(BleakError, "offline"):
                    await poller.read_sample(SimpleNamespace(address="AA"))
        self.assertEqual(read.await_count, 3)


class ConfigTests(unittest.TestCase):
    def test_plugin_identity_uses_new_installation_namespace(self):
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read(ROOT / "plugin.cfg")
        self.assertEqual(config["PLUGIN"]["NAME"], "ble-flower-monitor")
        self.assertEqual(config["PLUGIN"]["FOLDER"], "ble-flower-monitor")
        self.assertEqual(config["PLUGIN"]["TITLE"], "BLE Flower Monitor")
        self.assertEqual(config["AUTHOR"]["NAME"], "Jan W.")
        for release in ("release.cfg", "prerelease.cfg"):
            text = (ROOT / release).read_text()
            self.assertIn("VERSION=3.0.1", text)
            self.assertIn("jan21493/LoxBerry-Plugin-BleFlowerMonitor", text)

    def test_migration_persists_type_and_removes_old_plugin_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "miflora.cfg"
            path.write_text((ROOT / "config/miflora.cfg").read_text().replace(
                "[FLOWERMON]", "[MIFLORA]").replace(
                "BTADAPTER=serial", "BTADAPTER=hci0") +
                "\nMQTTBROKER=old-host\nMQTTPASSWORD=secret\n")
            with patch.object(plugin, "detect_adapters", return_value=[
                adapters.Adapter("hci0", "usb", "", ""),
            ]):
                plugin.migrate_config(path)
            config = plugin.read_config(path)
            self.assertEqual(config["BTADAPTER"], "usb")
            self.assertNotIn("MQTTBROKER", path.read_text())
            self.assertNotIn("MQTTPASSWORD", path.read_text())
            self.assertIn("[FLOWERMON]", path.read_text())
            self.assertNotIn("[MIFLORA]", path.read_text())
            # Migration keeps upper-case INI keys for Config::Simple.
            self.assertIn("BTADAPTER = usb", path.read_text())

    def test_missing_legacy_adapter_requires_explicit_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "miflora.cfg"
            before = (ROOT / "config/miflora.cfg").read_text().replace(
                "BTADAPTER=serial", "BTADAPTER=hci9")
            path.write_text(before)
            with patch.object(plugin, "detect_adapters", return_value=[]):
                with self.assertRaisesRegex(ValueError, "Select Default, USB or Onboard"):
                    plugin.migrate_config(path)
            self.assertEqual(path.read_text(), before)

    def test_new_defaults_and_password_percent(self):
        config = configparser.ConfigParser(interpolation=None)
        config.read(ROOT / "config/miflora.cfg")
        self.assertIn("FLOWERMON", config)
        self.assertNotIn("MIFLORA", config)
        self.assertEqual(settings()["MQTTENABLED"], "1")
        self.assertEqual(settings()["UDPENABLED"], "0")
        self.assertEqual(settings()["MQTTTOPIC"], "flowermon")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "miflora.cfg"
            path.write_text((ROOT / "config/miflora.cfg").read_text().replace("MQTTTOPIC=flowermon", "MQTTTOPIC=miflora%test"))
            self.assertEqual(plugin.read_config(path)["MQTTTOPIC"], "miflora%test")

    def test_invalid_config_rejected(self):
        for changes in (
            {"UDPENABLED": "1", "UDPPORT": "65536"}, {"MQTTTOPIC": "bad/+"},
            {"POLLFREQUENCY": "0"}, {"MQTTENABLED": "2"},
        ):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                config = configparser.ConfigParser()
                config["FLOWERMON"] = settings(**changes)
                path = Path(directory) / "miflora.cfg"
                with path.open("w") as handle:
                    config.write(handle)
                with self.assertRaises(ValueError):
                    plugin.read_config(path)

    def test_legacy_and_json_miniserver(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "general.cfg").write_text("[MINISERVER1]\nIPADDRESS=192.0.2.1\n")
            self.assertEqual(plugin.miniserver_address(settings(), root), "192.0.2.1")
            (root / "general.json").write_text(json.dumps({"Miniserver": {"1": {"Ipaddress": "192.0.2.2"}}}))
            self.assertEqual(plugin.miniserver_address(settings(), root), "192.0.2.2")

    def test_scheduling_and_disabled_plugin_skip_poll(self):
        for changes in ({"ENABLED": "0"}, {"POLLFREQUENCY": "12"}):
            with self.subTest(changes=changes), patch.object(plugin, "read_config", return_value=settings(**changes)):
                with patch.object(plugin.time, "strftime", return_value="03"), patch.object(plugin, "poll") as poll:
                    self.assertEqual(plugin.run(argparse.Namespace(configfile="unused", daemon=True)), 0)
                    poll.assert_not_called()

    def test_timestamp_compatibility(self):
        with patch.object(plugin.time, "time", return_value=1230768000), patch.object(plugin.time, "localtime", return_value=SimpleNamespace(tm_gmtoff=3600)):
            with patch.object(plugin.time, "strftime", return_value="01.01.2009 01:00:00"):
                self.assertEqual(plugin.timestamp_values(True)["PollTime"], 3600)
                self.assertEqual(plugin.timestamp_values(False)["PollTime"], 0)


class LanguageTests(unittest.TestCase):
    def test_web_language_keys_and_formatting_match(self):
        import re
        catalogs = []
        for language in ("de", "en"):
            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read(ROOT / f"templates/lang/language_{language}.ini", encoding="utf-8")
            catalogs.append(dict(config["FLOWERMON"]))
        self.assertEqual(catalogs[0].keys(), catalogs[1].keys())
        for key in catalogs[0]:
            self.assertEqual(re.findall(r"</?(?:b|i|code)>", catalogs[0][key]),
                             re.findall(r"</?(?:b|i|code)>", catalogs[1][key]), key)


class RunTests(unittest.TestCase):
    def setUp(self):
        native = patch.object(session, "NativeLog", FakeNativeLog)
        native.start()
        self.addCleanup(native.stop)
    def test_lock_prevents_overlapping_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            datafile = Path(directory) / "lastdata.dat"
            args = argparse.Namespace(configfile=ROOT / "config/miflora.cfg",
                                      datafile=str(datafile), daemon=False)
            with (Path(directory) / ".poll.lock").open("a") as lock:
                plugin.fcntl.flock(lock, plugin.fcntl.LOCK_EX | plugin.fcntl.LOCK_NB)
                with patch.object(plugin, "poll") as poll:
                    self.assertEqual(plugin.run(args), 0)
                    poll.assert_not_called()

    def test_bluez_error_has_failing_exit_and_visible_status(self):
        with tempfile.TemporaryDirectory() as directory:
            datafile = Path(directory) / "lastdata.dat"
            args = argparse.Namespace(configfile=ROOT / "config/miflora.cfg",
                                      datafile=str(datafile), daemon=False)
            with patch.object(plugin, "poll", AsyncMock(side_effect=BleakError("Adapter powered off"))):
                self.assertEqual(plugin.run(args), 1)
            self.assertIn("ERROR: Adapter powered off", datafile.read_text())

    def test_installed_folder_name_is_used_for_default_data_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configfile = root / "config/custom-plugin/miflora.cfg"
            configfile.parent.mkdir(parents=True)
            configfile.write_text((ROOT / "config/miflora.cfg").read_text())
            args = argparse.Namespace(configfile=configfile, datafile=None, daemon=False)
            with patch.dict(os.environ, {"LBPDATA": str(root / "data")}):
                with patch.object(plugin, "poll", AsyncMock(return_value=0)) as poll:
                    self.assertEqual(plugin.run(args), 0)
                    self.assertEqual(poll.call_args.args[1], root / "data/custom-plugin/lastdata.dat")

    def test_manual_disabled_plugin_gets_its_own_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = root / "miflora.cfg"
            cfg.write_text("[MIFLORA]\nENABLED=0\n")
            args = argparse.Namespace(configfile=cfg, datafile=str(root / "lastdata.dat"), daemon=False)
            with patch.object(plugin, "poll") as poll:
                self.assertEqual(plugin.run(args), 0)
                poll.assert_not_called()
            text = next(root.glob("*_xiaomi-miflora.log")).read_text()
            self.assertIn("START", text)
            self.assertIn("LOGEND", text)
            self.assertIn("DISABLED", text)

    def test_invalid_config_has_session_start_stop_and_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = root / "miflora.cfg"
            cfg.write_text("[MIFLORA]\nPOLLFREQUENCY=3\n")
            args = argparse.Namespace(configfile=cfg, datafile=str(root / "lastdata.dat"), daemon=False)
            self.assertEqual(plugin.run(args), 1)
            text = next(root.glob("*_xiaomi-miflora.log")).read_text()
            self.assertIn("START", text)
            self.assertIn("LOGEND", text)
            self.assertIn("Invalid polling frequency", text)

    def test_inherited_web_lock_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (root / ".poll.lock").open("a") as lock:
                plugin.fcntl.flock(lock, plugin.fcntl.LOCK_EX | plugin.fcntl.LOCK_NB)
                args = argparse.Namespace(configfile=ROOT / "config/miflora.cfg",
                                          datafile=str(root / "lastdata.dat"), daemon=False,
                                          lock_fd=os.dup(lock.fileno()))
                with patch.object(plugin, "poll", AsyncMock(return_value=0)) as poll:
                    self.assertEqual(plugin.run(args), 0)
                    poll.assert_awaited_once()


class FakeNativeLog(session.NativeLog):
    def __init__(self, filename, level):
        logging.Handler.__init__(self)
        self.threshold = logging.DEBUG
        self.truncated = False
        self.size = 0
        self.filename = filename
        self.setFormatter(logging.Formatter("%(message)s"))

    def send(self, method, message):
        with self.filename.open("a") as handle:
            handle.write(f"<{method}> {message}\n")

    def start(self, message):
        self.send("LOGSTART", message)

    def end(self, message):
        self.send("LOGEND", message)

    def close(self):
        logging.Handler.close(self)


class SessionTests(unittest.TestCase):
    def setUp(self):
        native = patch.object(session, "NativeLog", FakeNativeLog)
        native.start()
        self.addCleanup(native.stop)

    def test_native_log_receives_messages_and_progress_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with session.poll_session(root / "data.dat", root / "plugin.log") as outcome:
                logging.info("Sensor found")
                logging.error("Test failure")
                outcome["status"] = "ERROR"
            text = next(root.glob("*_xiaomi-miflora.log")).read_text()
            self.assertIn("START", text)
            self.assertIn("LOGEND", text)
            self.assertIn("Test failure", text)
            self.assertIn("Sensor found", text)
            self.assertIn("Sensor found", (root / "poll-progress.log").read_text())

    def test_exception_keeps_end_marker_and_leaves_retention_to_loxberry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root
            for i in range(35):
                (logs / f"20200101_000000_{i:06}_xiaomi-miflora.log").write_text("old")
            with self.assertRaisesRegex(RuntimeError, "offline"):
                with session.poll_session(root / "data.dat", root / "plugin.log", logging.DEBUG):
                    raise RuntimeError("offline")
            self.assertEqual(len(list(logs.glob("*_xiaomi-miflora.log"))), 36)
            text = sorted(logs.glob("*_xiaomi-miflora.log"))[-1].read_text()
            self.assertFalse((root / "sessions").exists())
            self.assertIn("LOGEND", text)
            self.assertIn("ERROR", text)

    def test_size_limit_preserves_stop(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(session, "MAX_LOG_BYTES", 200):
            root = Path(directory)
            with session.poll_session(root / "data.dat", root / "plugin.log", logging.DEBUG):
                for _ in range(10):
                    logging.debug("details " * 20)
            text = next(root.glob("*_xiaomi-miflora.log")).read_text()
            self.assertIn("size limit", text)
            self.assertIn("LOGEND", text)
            self.assertLess(len(text), 700)

    def test_messages_use_native_severity_methods(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with session.poll_session(root / "data.dat", root / "plugin.log", logging.DEBUG) as outcome:
                logging.debug("details")
                logging.info("progress")
                logging.info("delivered", extra={"success": True})
                logging.warning("retry")
                logging.error("failed")
                outcome["status"] = "ERROR"
            text = next(root.glob("*_xiaomi-miflora.log")).read_text()
            for expected in ("<LOGSTART>", "<DEB> details", "<INF> progress",
                             "<OK> delivered", "<WARN> retry", "<ERR> failed", "<LOGEND>"):
                self.assertIn(expected, text)


class NativeBridgeTests(unittest.TestCase):
    def test_bridge_writes_umlauts_as_utf8(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LoxBerry").mkdir()
            (root / "LoxBerry/Log.pm").write_text(r'''
package LoxBerry::Log;
sub new { my ($class, %args) = @_; return bless \%args, $class; }
sub INF { my ($self, $message) = @_; open(my $f, '>>:raw', $self->{filename}) or die $!; print $f $message, "\n"; close($f); }
sub LOGSTART { shift->INF(@_); }
sub LOGEND { shift->INF(@_); }
1;
''')
            filename = root / "utf8.log"
            with patch.dict(os.environ, {"PERL5LIB": str(root)}):
                log = session.NativeLog(filename)
                try:
                    log.start("query")
                    log.send("INF", "UTF-8 test: Wände, Größe")
                    log.send("INF", "D-Bus: <dbus_fast.signature.Variant ('q', 13)>")
                    log.send("INF", "Sensor name: </div><script>alert(1)</script>")
                    log.end("OK")
                finally:
                    log.close()
            content = filename.read_bytes()
            self.assertIn("UTF-8 test: Wände, Größe", content.decode("utf-8"))
            self.assertIn(b"W\xc3\xa4nde", content)
            self.assertIn("D-Bus: (dbus_fast.signature.Variant ('q', 13))", content.decode("utf-8"))
            self.assertIn("Sensor name: (/div)(script)alert(1)(/script)", content.decode("utf-8"))
            self.assertNotIn(b"<", content)
            self.assertNotIn(b">", content)

    def test_bridge_uses_loxberry_helper_and_finishes_session(self):
        process = MagicMock()
        process.stdout.readline.return_value = "READY\n"
        process.wait.return_value = 0
        process.poll.return_value = 0
        with patch.object(session.subprocess, "Popen", return_value=process) as start:
            log = session.NativeLog(Path("/logs/xiaomi-miflora/20261026_002247_xiaomi-miflora.log"), logging.DEBUG)
            try:
                log.start("query")
                log.emit(logging.makeLogRecord({"msg": "done", "levelno": logging.INFO, "success": True}))
                log.end("OK")
            finally:
                log.close()
        self.assertEqual(start.call_args.args[0][-2:], ["xiaomi-miflora", "7"])
        records = [json.loads(call.args[0]) for call in process.stdin.write.call_args_list]
        self.assertEqual([record["method"] for record in records], ["LOGSTART", "OK", "LOGEND"])
        process.wait.assert_called_once_with(timeout=10)

    def test_native_default_level_is_not_overridden(self):
        process = MagicMock()
        process.poll.return_value = 0
        with patch.object(session.subprocess, "Popen", return_value=process) as start:
            log = session.NativeLog(Path("/logs/custom-folder/20261026_002247_xiaomi-miflora.log"))
            log.close()
        self.assertEqual(start.call_args.args[0][-1], "custom-folder")
        self.assertEqual(len(start.call_args.args[0]), 4)


class TransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.system_config = Path(directory.name)
        environment = patch.dict(os.environ, {"LBSCONFIG": directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.write_basics()

    def write_basics(self, **changes):
        mqtt = dict(Brokerhost="central-broker", Brokerport=1884,
                    Brokeruser="central-user", Brokerpass=r'50%comma,quote"back\slash',
                    Uselocalbroker="false")
        mqtt.update(changes)
        (self.system_config / "general.json").write_text(json.dumps({"Mqtt": mqtt}))

    def test_mqtt_basics_loads_and_refreshes_central_credentials(self):
        connection = plugin.mqtt_connectiondetails()
        self.assertEqual(connection.host, "central-broker")
        self.assertEqual(connection.port, 1884)
        self.assertEqual(connection.password, r'50%comma,quote"back\slash')
        self.write_basics(Brokerhost="new-broker", Brokerport="1885", Brokeruser="", Brokerpass="")
        connection = plugin.mqtt_connectiondetails()
        self.assertEqual(connection.host, "new-broker")
        self.assertEqual(connection.port, 1885)
        self.assertEqual(connection.username, "")

    def test_missing_or_invalid_mqtt_basics_never_falls_back(self):
        for content in (
            "{}", "invalid json", '{"Mqtt": null}',
            '{"Mqtt": {"Brokerhost": "", "Brokerport": 1883}}',
            '{"Mqtt": {"Brokerhost": "broker", "Brokerport": 65536}}',
        ):
            (self.system_config / "general.json").write_text(content)
            with self.assertRaisesRegex(ValueError, "MQTT Basics"):
                plugin.mqtt_connectiondetails()
        (self.system_config / "general.json").unlink()
        with self.assertRaisesRegex(ValueError, "MQTT Basics"):
            plugin.mqtt_connectiondetails()

    def test_mqtt_basics_tls_settings_follow_loxberry_schema(self):
        self.write_basics(Uselocalbroker="true", Tlsenabled="true", Tlsport="8884")
        connection = plugin.mqtt_connectiondetails()
        self.assertTrue(connection.tls)
        self.assertFalse(connection.verify)
        self.assertEqual(connection.port, 8884)
        self.assertEqual(connection.cafile, "/etc/mosquitto/tls/ca.crt")
        self.write_basics(TlsExternalEnabled="true", TlsExternalValidatecert="true")
        connection = plugin.mqtt_connectiondetails()
        self.assertTrue(connection.tls)
        self.assertTrue(connection.verify)
        self.assertEqual(connection.cafile, str(self.system_config / "mqtt_external_ca.crt"))

    def mqtt_client(self, rejected=False):
        from paho.mqtt import client as mqtt
        client = MagicMock()
        client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
        client.publish.return_value.is_published.return_value = True

        def connect():
            client.on_connect(client, None, None, SimpleNamespace(is_failure=rejected), None)
        client.loop_start.side_effect = connect
        return client

    def test_mqtt_acknowledged_lowercase_topics_and_cleanup(self):
        client = self.mqtt_client()
        with patch("paho.mqtt.client.Client", return_value=client):
            plugin.mqtt_publish(settings(MQTTTOPIC="MiFlora", MQTTUSERNAME="name", MQTTPASSWORD="password"),
                                [("C47C8D000001", {"Temperature": -12.3})])
        client.publish.assert_called_once_with("miflora/c47c8d000001/temperature", "-12.3", qos=1)
        client.publish.return_value.wait_for_publish.assert_called_once_with(timeout=10)
        client.username_pw_set.assert_called_once_with("central-user", r'50%comma,quote"back\slash')
        client.connect.assert_called_once_with("central-broker", 1884)
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()

    def test_mqtt_basics_missing_reports_error_without_using_old_plugin_broker(self):
        (self.system_config / "general.json").unlink()
        with patch("paho.mqtt.client.Client") as client:
            with self.assertRaisesRegex(ValueError, "MQTT Basics"):
                plugin.mqtt_publish(settings(MQTTBROKER="stale-host", MQTTPORT="1883"), [])
            client.assert_not_called()

    def test_tls_client_uses_central_certificate_policy(self):
        self.write_basics(TlsExternalEnabled="true", TlsExternalValidatecert="true")
        client = self.mqtt_client()
        with patch("paho.mqtt.client.Client", return_value=client), patch.object(plugin.ssl, "create_default_context") as context:
            plugin.mqtt_publish(settings(), [])
        context.assert_called_once_with(cafile=str(self.system_config / "mqtt_external_ca.crt"))
        client.tls_set_context.assert_called_once_with(context.return_value)

    def test_mqtt_failure_is_explicit_and_connection_closed(self):
        for rejected in (True, False):
            client = self.mqtt_client(rejected)
            client.publish.return_value.is_published.return_value = False
            with patch("paho.mqtt.client.Client", return_value=client):
                with self.assertRaises((ConnectionError, TimeoutError)):
                    plugin.mqtt_publish(settings(), [("AA", {"Name": "Flower care"})])
            client.disconnect.assert_called_once()
            client.loop_stop.assert_called_once()

    def test_udp_wire_format_over_real_socket(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(2)
            plugin.udp_publish(settings(UDPPORT=str(receiver.getsockname()[1])),
                               [("C47C8D000001", {"Temperature": 27.1})], "127.0.0.1")
            self.assertEqual(receiver.recv(1024), b"C47C8D000001.Temperature=27.1")

    def test_mqtt_qos1_over_real_tcp_connection(self):
        packets = []
        broker_errors = []

        def read_packet(connection):
            first = connection.recv(1)
            if not first:
                raise EOFError("MQTT connection closed")
            length = 0
            multiplier = 1
            while True:
                value = connection.recv(1)[0]
                length += (value & 127) * multiplier
                if value < 128:
                    break
                multiplier *= 128
            data = b""
            while len(data) < length:
                chunk = connection.recv(length - len(data))
                if not chunk:
                    raise EOFError("Incomplete MQTT packet")
                data += chunk
            return first[0], data

        def broker(listener):
            try:
                with listener.accept()[0] as connection:
                    connection.settimeout(5)
                    packet_type, _ = read_packet(connection)
                    self.assertEqual(packet_type, 0x10)
                    connection.sendall(b"\x20\x02\x00\x00")
                    packet_type, data = read_packet(connection)
                    self.assertEqual(packet_type, 0x32)  # PUBLISH, QoS 1, no retain
                    topic_length = int.from_bytes(data[:2], "big")
                    message_id = data[2 + topic_length:4 + topic_length]
                    packets.append((data[2:2 + topic_length], data[4 + topic_length:]))
                    connection.sendall(b"\x40\x02" + message_id)
                    packet_type, _ = read_packet(connection)
                    self.assertEqual(packet_type, 0xE0)
            except (OSError, EOFError, AssertionError) as error:
                broker_errors.append(error)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(5)
            worker = threading.Thread(target=broker, args=(listener,))
            worker.start()
            try:
                self.write_basics(Brokerhost="127.0.0.1", Brokerport=listener.getsockname()[1])
                plugin.mqtt_publish(settings(MQTTBROKER="127.0.0.1", MQTTPORT=str(listener.getsockname()[1])),
                                    [("C47C8D000001", {"Temperature": 27.1})])
            finally:
                worker.join(timeout=7)
        self.assertFalse(worker.is_alive())
        self.assertEqual(broker_errors, [])
        self.assertEqual(packets, [(b"flowermon/c47c8d000001/temperature", b"27.1")])


class PollTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_adapter_type_and_description_are_logged(self):
        for selection, kind, label in (("usb", "usb", "USB"), ("serial", "serial", "Onboard"),
                                       ("default", "serial", "Default - hci0")):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as directory:
                adapter = adapters.Adapter("hci0", kind, "hci0 - test Bluetooth chipset", "")
                with patch.object(plugin, "detect_adapters", return_value=[adapter]):
                    with patch.object(scanner, "scan", AsyncMock(return_value=[])):
                        with self.assertLogs(level="INFO") as logs:
                            await plugin.poll(settings(BTADAPTER=selection), Path(directory) / "lastdata.dat")
                self.assertTrue(any(f"Adapter selection: {label} - hci0 - test Bluetooth chipset" in line
                                    for line in logs.output))

    async def execute_poll(self, directory, devices, read, config=None):
        with patch.object(plugin, "detect_adapters", return_value=[adapters.Adapter("hci0", "serial", "", "")]):
            with patch.object(scanner, "scan", AsyncMock(return_value=devices)):
                with patch.object(poller, "read_sample", read):
                    return await plugin.poll(config or settings(), Path(directory) / "lastdata.dat")

    async def test_mqtt_only_does_not_read_miniserver_and_stores_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(plugin, "mqtt_publish") as mqtt:
            with patch.object(plugin, "miniserver_address") as server:
                status = await self.execute_poll(directory, [SimpleNamespace(address="C4:7C:8D:00:00:01")],
                                                 AsyncMock(return_value={"Temperature": 27.1}))
                self.assertEqual(status, 0)
                mqtt.assert_called_once()
                server.assert_not_called()
                self.assertIn("C47C8D000001.Temperature=27.1", (Path(directory) / "lastdata.dat").read_text())

    async def test_sensor_failure_does_not_stop_next_sensor_and_udp_survives_mqtt_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(plugin, "mqtt_publish", side_effect=ConnectionError("broker unavailable")):
                with patch.object(plugin, "miniserver_address", return_value="192.0.2.1"):
                    with patch.object(plugin, "udp_publish") as udp:
                        read = AsyncMock(side_effect=[BleakError("sensor offline"), {"Moisture": 45}])
                        status = await self.execute_poll(directory, [
                            SimpleNamespace(address="AA:01"), SimpleNamespace(address="AA:02"),
                        ], read, settings(UDPENABLED="1"))
                        self.assertEqual(status, 1)
                        udp.assert_called_once()
                        self.assertEqual(udp.call_args.args[1][0][0], "AA02")
                        data = (Path(directory) / "lastdata.dat").read_text()
                        self.assertIn("ERROR AA:01", data)
                        self.assertIn("ERROR MQTT", data)

    async def test_scan_failure_redetects_reset_usb_adapter_once(self):
        detected = [
            [adapters.Adapter("hci2", "usb", "", "")],
            [adapters.Adapter("hci3", "usb", "", "")],
        ]
        scan = AsyncMock(side_effect=[BleakError("Operation already in progress"), []])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(plugin, "detect_adapters", side_effect=detected):
                with patch.object(scanner, "scan", scan), patch.object(plugin.asyncio, "sleep", AsyncMock()):
                    status = await plugin.poll(settings(BTADAPTER="usb"), Path(directory) / "lastdata.dat")
        self.assertEqual(status, 0)
        self.assertEqual([call.args[0] for call in scan.await_args_list], ["hci2", "hci3"])

    async def test_no_sensors_clears_stale_data(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(plugin, "mqtt_publish") as mqtt:
            datafile = Path(directory) / "lastdata.dat"
            datafile.write_text("old values")
            self.assertEqual(await self.execute_poll(directory, [], AsyncMock()), 0)
            self.assertIn("No MiFlora sensors found", datafile.read_text())
            self.assertNotIn("old values", datafile.read_text())
            mqtt.assert_not_called()


class FrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(["perl", "-MConfig::Simple", "-MHTML::Template", "-MCGI", "-e", "1"],
                                capture_output=True)
        if result.returncode:
            raise unittest.SkipTest("Frontend tests require Config::Simple, HTML::Template and CGI")

    def test_frontend_renders_and_saves_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "LoxBerry").mkdir()
            (root / "LoxBerry/System.pm").write_text(r'''
package LoxBerry::System;
use Exporter 'import';
use Config::Simple;
our @EXPORT = qw($lbpconfigdir $lbptemplatedir $lbpbindir $lbplogdir $lbpdatadir);
our ($lbpconfigdir, $lbplogdir, $lbpdatadir) = ($ENV{TEST_DATA}) x 3;
our $lbptemplatedir = "$ENV{TEST_ROOT}/templates";
our $lbpbindir = "$ENV{TEST_DATA}/bin";
our $lbsconfigdir = "$ENV{TEST_DATA}";
sub readlanguage {
    my ($template) = @_;
    open(my $handle, '<', "$lbptemplatedir/lang/language_en.ini") or die $!;
    my %language;
    my $section = '';
    while (my $line = <$handle>) {
        if ($line =~ /^\[(\w+)\]/) { $section = $1; next; }
        if ($line =~ /^(\w+)="(.*)"\s*$/) { $language{"$section.$1"} = $2; }
    }
    return %language;
}
sub get_miniservers { return (); }
1;
''')
            (root / "LoxBerry/Web.pm").write_text(
                "package LoxBerry::Web; sub lbheader {} sub lbfooter {} "
                "sub loglist_html { my %p = @_; return '<div>Native log list: '.$p{NAME}."
                "q{<a href='/admin/system/tools/logfile.cgi?logfile=plugins%2Ftest%2F20261027_000000_123_xiaomi-miflora.log&amp;format=template' target='_blank'>Logfile</a></div>}; } 1;")
            (root / "bin").mkdir()
            (root / "bin/miflora.py").write_text(
                "print(" + repr(json.dumps([
                    dict(id="hci6", kind="usb",
                         label="hci6 - Realtek Bluetooth 5.4 USB-Adapter (RTL8761BU or newer) / HCI 5.4 Revision: 0x1234 / 0BDA:A728",
                         selection_label="USB - currently mapped to hci6 - Realtek Bluetooth 5.4 USB-Adapter (RTL8761BU or newer)"),
                    dict(id="hci2", kind="serial", label="hci2 - Raspberry Pi 5 Model B Rev 1.1 Bluetooth - HCI 5.0 Revision: 0x15c",
                         selection_label="Onboard - Raspberry Pi 5 Model B Rev 1.1 Bluetooth"),
                ])) + ")"
            )
            (root / "bin/run.sh").write_text('#!/bin/sh\nprintf "poll executed" > "' + str(root / "poll-executed") + '"\n')
            configfile = root / "miflora.cfg"
            configfile.write_text((ROOT / "config/miflora.cfg").read_text())
            generalfile = root / "general.json"
            generalfile.write_text(json.dumps({"Base": {"Version": "4.0.1.1"},
                                              "Mqtt": {"Gatewayversion": 2, "Brokerhost": "localhost"}}))
            env = dict(os.environ, PERL5LIB=str(root) + os.pathsep + os.environ.get("PERL5LIB", ""),
                       TEST_DATA=str(root), TEST_ROOT=str(ROOT), REQUEST_METHOD="GET")
            result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                    env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            html = result.stdout.decode()
            self.assertIn("MQTT", html)
            self.assertIn("BlueZ D-Bus", html)
            self.assertIn("<h1>BLE Flower Monitor</h1>", html)
            self.assertIn("<b>HHCC Flower Care</b>", html)
            self.assertIn("<b>&#39;MQTT Basics&#39;</b>", html)
            self.assertIn("/&lt;MQTT prefix&gt;/&lt;MAC address&gt;/&lt;parameter&gt;", html)
            self.assertIn("successor to Michael Miklis", html)
            self.assertIn("selBTAdapter", html)
            self.assertIn("Installed Bluetooth adapters", html)
            self.assertIn('<div class="miflora-save"><input', html)
            self.assertNotIn("ARRAY(", html)
            self.assertNotIn("<TMPL_", html)
            self.assertIn("<h2>Send to Miniserver</h2>", html)
            self.assertIn("<h3>MQTT (recommended)</h3>", html)
            self.assertIn("<h3>UDP (alternative)</h3>", html)
            for field in ("chkPluginEnabled", "selPollFrequency", "chkLocaltime", "selBTAdapter",
                          "chkMQTTEnabled", "txtMQTTTopic", "chkUDPEnabled", "selMiniServer", "txtMsUDPPort"):
                self.assertRegex(html, rf'<div class="miflora-field">\s*<label for="{field}">[^<]+</label>'
                                       rf'\s*<div><(?:select|input)\b[^>]*\bid="{field}"')
            class Controls(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.inputs = {}
                    self.adapter_options = []
                    self.adapter_select = False

                def handle_starttag(self, tag, attrs):
                    attrs = dict(attrs)
                    if tag == "input":
                        self.inputs[attrs.get("name")] = attrs
                    if tag == "select":
                        self.adapter_select = attrs.get("name") == "selBTAdapter"
                    if tag == "option" and self.adapter_select:
                        self.adapter_options.append(attrs["value"])

                def handle_endtag(self, tag):
                    if tag == "select":
                        self.adapter_select = False

            controls = Controls()
            controls.feed(html)
            for button in ("btnSave",):
                self.assertEqual(controls.inputs[button]["type"], "submit")
                self.assertNotIn("disabled", controls.inputs[button])
                self.assertEqual(controls.inputs[button].get("data-inline"), "true")
            self.assertEqual(controls.adapter_options, ["default", "usb", "serial"])
            self.assertIn("Default - hci0", html)
            self.assertIn("USB - currently mapped to hci6 - Realtek Bluetooth 5.4 USB-Adapter (RTL8761BU or newer)", html)
            self.assertIn("Onboard - Raspberry Pi 5 Model B Rev 1.1 Bluetooth", html)
            for removed in ("txtMQTTBroker", "txtMQTTPort", "txtMQTTUsername", "txtMQTTPassword"):
                self.assertNotIn(removed, html)
            status_page = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        env=dict(env, QUERY_STRING="tab=status"), capture_output=True)
            self.assertEqual(status_page.returncode, 0, status_page.stderr.decode())
            status_html = status_page.stdout.decode()
            self.assertNotIn("Installed Bluetooth adapters", status_html)
            self.assertIn("Virtual inputs in Loxone Config", status_html)
            self.assertIn("MQTT Gateway version 2 is selected, great!", status_html)
            for field in ("inputTransport",):
                self.assertRegex(status_html, rf'<div class="miflora-field">\s*<label for="{field}">[^<]+</label>'
                                              rf'\s*<div><(?:select|input)\b[^>]*\bid="{field}"')
            self.assertIn('<label for="sensorSelect">Sensor MAC address</label>', status_html)
            self.assertIn('id="clearOutput"', status_html)
            self.assertIn('id="editMac"', status_html)
            self.assertIn('5C:85:7E:B0:F3:AD (Example)', status_html)
            self.assertRegex(status_html, r'<option value="mqtt" selected>MQTT \(recommended\)</option>')
            self.assertRegex(status_html, r'<option value="udp">UDP</option>')
            self.assertIn("Resend all", status_html)
            self.assertIn("single dot (<code>.</code>)", status_html)
            self.assertIn("<code>\\.</code>", status_html)
            self.assertIn("<b><i>'Subscriptions'</i></b>", status_html)
            self.assertNotIn("&lt;b&gt;", status_html)
            self.assertIn("°C", status_html)
            self.assertIn("µS/cm", status_html)
            self.assertIn("virtual text input", status_html)
            self.assertNotIn('id="mqttSubscription"', status_html)
            self.assertNotIn('name="btnSave"', status_html)
            self.assertIn('id="dataAutoScroll"', status_html)
            self.assertNotIn('name="btnRefresh"', status_html)
            status_controls = Controls()
            status_controls.feed(status_html)
            controls.inputs.update(status_controls.inputs)
            for button in ("btnPoll",):
                self.assertEqual(controls.inputs[button]["type"], "submit")
                self.assertEqual(controls.inputs[button]["data-inline"], "true")
            form = dict(btnSave="Save", chkPluginEnabled="1", selPollFrequency="4", chkLocaltime="1",
                        selBTAdapter="usb", chkMQTTEnabled="1", txtMQTTTopic="miflora",
                        chkUDPEnabled="0", selMiniServer="", txtMsUDPPort="5006")
            # Old broker values are removed rather than reused after saving.
            with configfile.open("a") as handle:
                handle.write("\nMQTTBROKER=stale-host\nMQTTPORT=1234\nMQTTUSERNAME=old\nMQTTPASSWORD=secret\n")
            for topic in ("miflora", "miflora%test", "comma,backslash\\test"):
                form["txtMQTTTopic"] = topic
                payload = urlencode(form)
                env.update(REQUEST_METHOD="POST", CONTENT_TYPE="application/x-www-form-urlencoded",
                           CONTENT_LENGTH=str(len(payload)))
                result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        input=payload.encode(), env=env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertIn("Settings saved.", result.stdout.decode())
                saved = plugin.read_config(configfile)
                self.assertEqual(saved["BTADAPTER"], "usb")
                self.assertEqual(saved["POLLFREQUENCY"], "4")
                self.assertEqual(saved["MQTTTOPIC"], topic)
                for removed in ("MQTTBROKER", "MQTTPORT", "MQTTUSERNAME", "MQTTPASSWORD"):
                    self.assertNotIn(removed, saved)
                # A subsequent page load must keep punctuation and empty values.
                result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        env=dict(env, REQUEST_METHOD="GET", CONTENT_LENGTH="0"), capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotIn("ARRAY(", result.stdout.decode())
            for field, invalid in (("selBTAdapter", "hci6"), ("txtMQTTTopic", "bad/#"),
                                   ("chkMQTTEnabled", "0"), ("selBTAdapter", "invalid")):
                bad_form = dict(form, **{field: invalid})
                payload = urlencode(bad_form)
                before = configfile.read_text()
                result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        input=payload.encode(), env=dict(env, QUERY_STRING="tab=status", CONTENT_LENGTH=str(len(payload))),
                                        capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertIn('role="alert"', result.stdout.decode())
                self.assertEqual(configfile.read_text(), before)
            for button in ("btnPoll",):
                payload = urlencode({button: controls.inputs[button]["value"]})
                result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        input=payload.encode(), env=dict(env, QUERY_STRING="tab=status", CONTENT_LENGTH=str(len(payload))),
                                        capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(configfile.read_text(), before)
                self.assertIn("Starting scan", result.stdout.decode())
                if button == "btnPoll":
                    import time
                    for _ in range(100):
                        if (root / "poll-executed").exists():
                            break
                        time.sleep(0.01)
                    self.assertTrue((root / "poll-executed").exists())
            get_env = dict(env, REQUEST_METHOD="GET", CONTENT_LENGTH="0")
            def endpoint(query, payload=None):
                call_env = dict(get_env, QUERY_STRING=query)
                if payload is not None:
                    call_env.update(REQUEST_METHOD="POST", CONTENT_LENGTH=str(len(payload)))
                result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        input=payload.encode() if payload else None, env=call_env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                return result.stdout.decode()
            for version, lbversion, v2, recommendation in (
                (2, "4.0.1.1", True, False),
                ("2", "4.0.1.1", True, False),
                (1, "4.0.1.1", False, True),
                (None, "4.0.1.1", False, True),
                (None, "3.0.0", False, False),
                (1, "3.0.0", False, False),
                (2, "3.0.0", False, False),
            ):
                mqtt = {"Brokerhost": "localhost"}
                if version is not None:
                    mqtt["Gatewayversion"] = version
                generalfile.write_text(json.dumps({"Base": {"Version": lbversion}, "Mqtt": mqtt}))
                page = endpoint("tab=status")
                self.assertEqual('id="mqttSubscription"' in page, not v2)
                self.assertEqual("Resend all" in page, v2)
                self.assertEqual("recommended for efficiency" in page, recommendation)
                summary = page.split('<p class="miflora-gateway-status">', 1)[1].split("</p>", 1)[0]
                self.assertIn("MQTT configuration found", summary)
                self.assertEqual("MQTT Gateway version 2" in summary, lbversion.startswith("4."))
                if v2 or recommendation:
                    self.assertLess(summary.index("MQTT configuration found"), summary.index("MQTT Gateway version 2"))
                self.assertLess(page.index("MQTT configuration found"),
                                page.index("<ol>") if v2 else page.index('id="mqttSubscription"'))
                self.assertIn('id="udpHelp" hidden', page)
            generalfile.write_text("invalid")
            self.assertIn("Could not read the MQTT Gateway version", endpoint("tab=status"))
            generalfile.write_text(json.dumps({"Mqtt": {}}))
            self.assertIn("No MQTT broker configuration found", endpoint("tab=status"))
            generalfile.write_text(json.dumps({"Base": {"Version": "4.0.1.1"},
                                              "Mqtt": {"Gatewayversion": 2}}))
            page = endpoint("tab=status")
            summary = page.split('<p class="miflora-gateway-status">', 1)[1].split("</p>", 1)[0]
            self.assertIn("No MQTT broker configuration found", summary)
            self.assertIn('style="color:red"', summary)
            self.assertIn("MQTT Gateway version 2 is selected", summary)
            configfile.write_text(configfile.read_text().replace("MQTTENABLED=1", "MQTTENABLED=0")
                                  .replace("UDPENABLED=0", "UDPENABLED=1"))
            udp_status = endpoint("tab=status")
            self.assertRegex(udp_status, r'<option value="mqtt">MQTT \(recommended\)</option>')
            self.assertRegex(udp_status, r'<option value="udp" selected>UDP</option>')
            configfile.write_text(configfile.read_text().replace("MQTTENABLED=0", "MQTTENABLED=1")
                                  .replace("UDPENABLED=1", "UDPENABLED=0"))
            logpage = endpoint("tab=logs")
            self.assertIn("Native log list: FlowerCare", logpage)
            self.assertNotIn('id="sessionSelect"', logpage)
            self.assertNotIn('id="selLogLevel"', logpage)
            self.assertNotIn('name="btnLogSave"', logpage)
            filename = "20261027_000000_123_xiaomi-miflora.log"
            self.assertIn(
                f"href='/admin/system/tools/logfile.cgi?logfile=plugins%2Ftest%2F{filename}"
                "&amp;format=template' target='_blank'", logpage)
            self.assertNotIn("index.cgi?tab=logs&amp;logfile=", logpage)
            self.assertNotIn('id="sessionLog"', logpage)
            status = endpoint("action=status")
            self.assertIn('"running":false', status)
            self.assertIn("400 Bad Request", endpoint("action=log&session=../../miflora.cfg"))
            self.assertIn("400 Bad Request", endpoint("action=poll"))
            old_settings = plugin.read_config(configfile)
            endpoint("tab=logs", "btnLogSave=1&selLogLevel=DEBUG")
            self.assertNotIn("LOGLEVEL", plugin.read_config(configfile))
            self.assertEqual(plugin.read_config(configfile)["MQTTTOPIC"], old_settings["MQTTTOPIC"])
            before = configfile.read_text()
            endpoint("tab=logs", "btnLogSave=1&selLogLevel=INVALID")
            self.assertEqual(configfile.read_text(), before)
            (root / "poll-progress.log").write_text("Temperature=21.8\nMQTT delivery acknowledged\n")
            (root / "lastdata.dat").write_text("Temperature=21.8\n")
            status = endpoint("action=status")
            self.assertEqual(status.count("Temperature=21.8"), 1)
            progress = (
                "12:00:00 Adapter address 11:22:33:44:55:66\n"
                "12:00:01 Discovered aa:bb:cc:dd:ee:ff (Flower care), RSSI -60 dBm\n"
                "12:00:02 Discovered 5C:85:7E:B0:F3:AD (Flower care), RSSI -70 dBm\n"
                "12:00:03 Discovered AA:BB:CC:DD:EE:FF (Flower care), RSSI -60 dBm\n"
                "12:00:04 Cannot poll AA:BB:CC:DD:EE:FF: timeout\n"
            )
            (root / "poll-progress.log").write_text(progress)
            response = json.loads(endpoint("action=status").split("\r\n\r\n", 1)[-1])
            self.assertEqual(response["sensors"], ["AA:BB:CC:DD:EE:FF", "5C:85:7E:B0:F3:AD"])
            page = endpoint("tab=status")
            self.assertIn('value="AA:BB:CC:DD:EE:FF"', page)
            self.assertIn('<option value="5C:85:7E:B0:F3:AD">5C:85:7E:B0:F3:AD</option>', page)
            self.assertNotIn("(Example)", page)
            self.assertIn("400 Bad Request", endpoint("action=clear-output"))
            self.assertEqual((root / "poll-progress.log").read_text(), progress)
            import fcntl
            with (root / ".poll.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertIn("cannot be cleared", endpoint("action=clear-output", "clear=1"))
                self.assertEqual((root / "poll-progress.log").read_text(), progress)
            (root / "retained_xiaomi-miflora.log").write_text("query log")
            endpoint("action=clear-output", "clear=1")
            self.assertEqual((root / "poll-progress.log").read_text(), "")
            self.assertEqual((root / "lastdata.dat").read_text(), "")
            self.assertEqual((root / "retained_xiaomi-miflora.log").read_text(), "query log")
            self.assertIn('5C:85:7E:B0:F3:AD (Example)', endpoint("tab=status"))

    def test_js_examples_match_actual_transports(self):
        import shutil
        if not shutil.which("node"):
            self.skipTest("Node.js needed for JavaScript regression tests")
        result = subprocess.run(["node", str(ROOT / "tests/test_frontend.js")], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
