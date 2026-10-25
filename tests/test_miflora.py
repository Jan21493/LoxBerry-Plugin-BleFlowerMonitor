import argparse
import asyncio
import configparser
import importlib.util
import json
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
from bleak.exc import BleakError

spec = importlib.util.spec_from_file_location("plugin", ROOT / "bin/miflora.py")
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def settings(**changes):
    config = configparser.ConfigParser(interpolation=None)
    config.read(ROOT / "config/miflora.cfg")
    config["MIFLORA"].update(changes)
    return config["MIFLORA"]


def sensor_data(**changes):
    values = dict(temperature=-123, light=100000, moisture=45, conductivity=54321)
    values.update(changes)
    return struct.pack("<hxIBH", *values.values()) + b"\x01\0\0\0\0\0"


class AdapterTests(unittest.TestCase):
    def test_inventory_type_selection_metadata_and_natural_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controllers = root / "class/bluetooth"
            controllers.mkdir(parents=True)
            uart = root / "devices/platform/serial/tty/ttyAMA0/bluetooth/hci2"
            usb_parent = root / "devices/pci/usb1/1-1"
            usb = usb_parent / "1-1:1.0/bluetooth/hci10"
            for target in (usb, uart):
                target.mkdir(parents=True)
                (controllers / target.name).symlink_to(target)
                (target / "device").symlink_to(target.parent)
                (target / "address").write_text("AA:BB:CC:DD:EE:FF")
            for key, value in {
                "idVendor": "0bda", "idProduct": "a725",
                "manufacturer": "Realtek", "product": "Bluetooth Radio",
            }.items():
                (usb_parent / key).write_text(value)
            detected = adapters.detect_adapters(root)
            self.assertEqual([item.id for item in detected], ["hci2", "hci10"])
            self.assertEqual([item.kind for item in detected], ["serial", "usb"])
            self.assertIn("RTL8761B", detected[1].label)
            self.assertIn("0BDA:A725", detected[1].label)
            self.assertEqual(adapters.select_adapter("usb", detected), "hci10")
            self.assertEqual(adapters.select_adapter("serial", detected), "hci2")
            self.assertEqual(adapters.select_adapter("hci10", detected), "hci10")
            with self.assertRaisesRegex(ValueError, "no fallback"):
                adapters.select_adapter("default", detected)

    def test_missing_and_invalid_selections_do_not_fallback(self):
        for selection in ("usb", "serial", "default", "hci3", "other", "../hci0"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                adapters.select_adapter(selection, [])

    def test_first_usb_sorted_by_number_not_name(self):
        detected = [adapters.Adapter("hci2", "usb", "", ""), adapters.Adapter("hci10", "usb", "", "")]
        self.assertEqual(adapters.select_adapter("usb", detected), "hci2")


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
                callback(entry, SimpleNamespace(local_name=None))

        factory.return_value.__aenter__ = AsyncMock(side_effect=start)
        factory.return_value.__aexit__ = AsyncMock(return_value=False)
        with patch.object(scanner, "BleakScanner", factory), patch.object(scanner.asyncio, "sleep", AsyncMock()):
            found = await scanner.scan("hci2")
        self.assertEqual(found, [named, device])
        self.assertEqual(factory.call_args.kwargs["adapter"], "hci2")
        self.assertEqual(factory.call_args.kwargs["bluez"]["adapter"], "hci2")

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
                    result = await poller.read_sample(device)
                self.assertIs(factory.call_args.args[0], device)
                self.assertEqual(result["Firmware"], firmware)
                self.assertEqual(client.write_gatt_char.await_count, writes)
                if writes:
                    client.write_gatt_char.assert_awaited_once_with(
                        poller.MODE_UUID, b"\xa0\x1f", response=True
                    )

    async def test_retry_is_bounded_and_failure_not_hidden(self):
        with patch.object(poller, "_read_sample", AsyncMock(side_effect=BleakError("offline"))) as read:
            with patch.object(poller.asyncio, "sleep", AsyncMock()):
                with self.assertRaisesRegex(BleakError, "offline"):
                    await poller.read_sample(SimpleNamespace(address="AA"))
        self.assertEqual(read.await_count, 3)


class ConfigTests(unittest.TestCase):
    def test_new_defaults_and_password_percent(self):
        self.assertEqual(settings()["MQTTENABLED"], "1")
        self.assertEqual(settings()["UDPENABLED"], "0")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "miflora.cfg"
            path.write_text((ROOT / "config/miflora.cfg").read_text().replace("MQTTPASSWORD=", "MQTTPASSWORD=50%secret"))
            self.assertEqual(plugin.read_config(path)["MQTTPASSWORD"], "50%secret")

    def test_invalid_config_rejected(self):
        for changes in (
            {"MQTTPORT": "0"}, {"MQTTPORT": "65536"}, {"MQTTTOPIC": "bad/+"},
            {"MQTTBROKER": ""}, {"POLLFREQUENCY": "0"}, {"MQTTENABLED": "2"},
        ):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                config = configparser.ConfigParser()
                config["MIFLORA"] = settings(**changes)
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


class RunTests(unittest.TestCase):
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


class TransportTests(unittest.TestCase):
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
        client.username_pw_set.assert_called_once_with("name", "password")
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()

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
                plugin.mqtt_publish(settings(MQTTBROKER="127.0.0.1", MQTTPORT=str(listener.getsockname()[1])),
                                    [("C47C8D000001", {"Temperature": 27.1})])
            finally:
                worker.join(timeout=7)
        self.assertFalse(worker.is_alive())
        self.assertEqual(broker_errors, [])
        self.assertEqual(packets, [(b"miflora/c47c8d000001/temperature", b"27.1")])


class PollTests(unittest.IsolatedAsyncioTestCase):
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
our $lbpbindir = "$ENV{TEST_ROOT}/bin";
sub readlanguage {
    my ($template) = @_;
    open(my $handle, '<', "$lbptemplatedir/lang/language_en.ini") or die $!;
    my %language;
    while (my $line = <$handle>) {
        if ($line =~ /^(\w+)="(.*)"\s*$/) { $language{"MIFLORA.$1"} = $2; }
    }
    return %language;
}
sub get_miniservers { return (); }
1;
''')
            (root / "LoxBerry/Web.pm").write_text("package LoxBerry::Web; sub lbheader {} sub lbfooter {} 1;")
            configfile = root / "miflora.cfg"
            configfile.write_text((ROOT / "config/miflora.cfg").read_text())
            env = dict(os.environ, PERL5LIB=str(root) + os.pathsep + os.environ.get("PERL5LIB", ""),
                       TEST_DATA=str(root), TEST_ROOT=str(ROOT), REQUEST_METHOD="GET")
            result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                    env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            html = result.stdout.decode()
            self.assertIn("MQTT", html)
            self.assertIn("BlueZ D-Bus", html)
            self.assertIn("selBTAdapter", html)
            self.assertIn("Installed Bluetooth adapters", html)
            self.assertNotIn("ARRAY(", html)
            self.assertNotIn("<TMPL_", html)
            form = dict(btnSave="Save", chkPluginEnabled="1", selPollFrequency="4", chkLocaltime="1",
                        selBTAdapter="usb", chkMQTTEnabled="1", txtMQTTBroker="localhost",
                        txtMQTTPort="1883", txtMQTTTopic="miflora", txtMQTTUsername="test",
                        chkUDPEnabled="0", selMiniServer="", txtMsUDPPort="5006")
            for password in ("", "50%secret", r'comma,backslash\quote"semicolon;hash#'):
                form["txtMQTTPassword"] = password
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
                self.assertEqual(saved["MQTTPASSWORD"], password)
                # A subsequent page load must keep punctuation and empty values.
                result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        env=dict(env, REQUEST_METHOD="GET", CONTENT_LENGTH="0"), capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertNotIn("ARRAY(", result.stdout.decode())
            for field, invalid in (("txtMQTTPort", "65536"), ("txtMQTTTopic", "bad/#"),
                                   ("chkMQTTEnabled", "0")):
                bad_form = dict(form, **{field: invalid})
                payload = urlencode(bad_form)
                before = configfile.read_text()
                result = subprocess.run(["perl", str(ROOT / "webfrontend/htmlauth/index.cgi")],
                                        input=payload.encode(), env=dict(env, CONTENT_LENGTH=str(len(payload))),
                                        capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertIn('role="alert"', result.stdout.decode())
                self.assertEqual(configfile.read_text(), before)


if __name__ == "__main__":
    unittest.main()
