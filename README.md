# LoxBerry Plugin: Xiaomi MiFlora Flower Sensor

Reads Xiaomi MiFlora / Flower Care and Flower Mate sensors using **Bleak and
the standard BlueZ D-Bus API**. Onboard Bluetooth (including Raspberry Pi UART
controllers) and USB Bluetooth adapters are supported. No bluepy, btlewrap,
raw HCI sockets, root polling or `setcap` are required.

**MQTT is the primary transport.** UDP remains available alongside MQTT or on
its own. Existing MQTT topics, UDP payloads and hourly polling intervals are
preserved.

## Requirements and installation

- Linux with **Python 3.9 or later** and **BlueZ 5.55 or later**, for example a
  current LoxBerry installation based on Debian 11 or newer. Older LoxBerry
  images must be upgraded first; the installer reports unsupported runtimes.
- A powered, unblocked Bluetooth LE adapter, with the `bluetooth` service running.
- Internet access during installation to install the Python dependencies.

The installer installs BlueZ, D-Bus and Python venv support, starts BlueZ and
creates a plugin-local `bin/venv` with Bleak and paho-mqtt. It does not modify
system Python packages or remove old shared bluepy installations. Polling
runs as the normal LoxBerry user through BlueZ's standard system-bus policy.
Systems with custom D-Bus restrictions must permit that user to communicate
with `org.bluez`.

## Settings

The settings page follows the TeslaCmd layout, with separate sections and
inline help for general scheduling, Bluetooth, MQTT, optional UDP and diagnostics.
Save settings before using **Poll now**.

### Bluetooth adapter

The selection and inventory are ported from the sysfs-based adapter logic in
[Jan21493/LoxBerry-Plugin-TeslaCmd](https://github.com/Jan21493/LoxBerry-Plugin-TeslaCmd),
`webfrontend/htmlauth/tesla_inc.php`, commit
`49cc2c3598a3b3e5d7c6f7b8c24b214170671d51` (Apache-2.0, see
[LICENSE.md](LICENSE.md)). The Python port shares one implementation between the
web UI and poller, uses sysfs product/manufacturer and USB chip IDs for the
inventory, and does not use TeslaCmd's legacy `hciconfig` diagnostics.

| Selection | Behaviour |
| --- | --- |
| Default | Use `hci0`, matching the previous plugin behaviour. |
| Onboard / UART | First onboard/UART controller in natural hci-number order. |
| USB | First USB controller in natural hci-number order. |
| Specific `hciX` | Use exactly that controller, useful with multiple USB adapters. |

Installed adapters are listed with their hci ID, bus type, available product,
chip and address information. Unplugged selections remain visible. A missing
selected adapter fails explicitly; there is **no fallback** to another controller.
Type-based selection handles changes in hci numbering after reboots; explicit
hci IDs follow the OS numbering, not a persistent physical-device identity.
An internally mounted USB controller is classified as USB, not UART.

Both discovery and GATT reads use the selected controller. Sensors are read
sequentially, with up to three bounded attempts per sensor. Errors on one sensor
do not prevent other sensors from being read. A process lock prevents cron and
manual polls from overlapping. The hourly cron checks the configured interval;
it is not a continuously running daemon.

### MQTT (default for new installations)

Configure the broker host, port (default `1883`), topic prefix (default `miflora`)
and optional credentials. A broker such as the LoxBerry MQTT Gateway can be
used; no Miniserver configuration is needed for MQTT-only operation.

Each value is published separately with **QoS 1**, waiting for connection and
publish acknowledgements before disconnecting. QoS 1 can deliver duplicates;
messages are not retained. Connection/authentication/publish failures are
reported in the plugin log and latest-data display, and return a failing exit
code. If UDP is also enabled, its delivery is attempted independently.

```text
miflora/c47c8d66275b/temperature  27.1
miflora/c47c8d66275b/moisture    4
```

The topic is `<prefix>/<MAC without colons>/<sensor name>`, entirely lowercase
as in previous releases. Wildcards (`+`, `#`) are not valid in the prefix.
Username and password may be empty for a broker without authentication.
MQTT uses plain TCP, **not TLS**; use it only on a trusted network.

### UDP (optional)

Enable UDP and select a Miniserver configured in LoxBerry plus its virtual UDP
input port (default `5006`). Each value is a separate datagram in the unchanged
format `<MAC without colons>.<SensorName>=<Value>`:

```text
C47C8D66275B.Temperature=27.1
C47C8D66275B.Moisture=4
```

Both `general.json` and legacy `general.cfg` Miniserver configuration files are
supported. Neither is read when UDP is disabled. UDP has no delivery
acknowledgements.

### Upgrading existing installations

Existing settings and credentials are preserved, including UDP-only or
MQTT-disabled configurations. MQTT is **not silently enabled** during an
upgrade. `BTADAPTER=default` is added for compatibility with the previous
`hci0` behaviour. Switch to MQTT and select the desired adapter in the settings
page when ready. Fresh installs enable MQTT and disable UDP.

## Sensor values

| Sensor name | Description | Example |
| --- | --- | --- |
| Name | Sensor name | Flower care |
| PollTime | Seconds since 2009-01-01, with optional local-time offset | 297370058 |
| PollTimeString | Local date/time string | 03.06.2018 18:47:38 |
| Firmware | Firmware version | 2.7.0 |
| Temperature | Degrees Celsius | 27.1 |
| Moisture | Percent | 4 |
| Light | Lux | 136 |
| Conductivity | µS/cm | 0 |
| Battery | Percent | 86 |

Firmware 2.6.6 and newer is switched to live-data mode before reading. GATT
characteristic UUIDs replace the old fixed ATT handles. Invalid or incomplete
sensor responses are reported instead of being published as zero values.

## Troubleshooting

Check **Polling and diagnostics** in the settings page and the plugin log
`xiaomi-miflora.log`. A scan takes ten seconds; retries and multiple sensors can
make the full poll take several minutes. Refresh the page to see the results.

On the LoxBerry host (adjust paths if the plugin has a different folder):

```sh
systemctl status bluetooth
bluetoothctl list
bluetoothctl show
/usr/bin/python3 /opt/loxberry/bin/plugins/xiaomi-miflora/miflora.py --list-adapters
/bin/sh /opt/loxberry/bin/plugins/xiaomi-miflora/run.sh \
  --configfile /opt/loxberry/config/plugins/xiaomi-miflora/miflora.cfg \
  --logfile /opt/loxberry/log/plugins/xiaomi-miflora/xiaomi-miflora.log \
  --datafile /opt/loxberry/data/plugins/xiaomi-miflora/lastdata.dat --debug
```

If no adapter appears, check the USB connection, OS driver, firmware and
Bluetooth blocking state (`rfkill list`). If a powered-off controller is shown,
power on the intended controller using the OS Bluetooth tools. The plugin does
not reset adapters or restart drivers automatically. Check batteries and range
if discovery finds no sensors. D-Bus permission errors require checking the
host's BlueZ policy; do not run the poller as root or restore raw-socket caps.

## Development

On Linux, install `bin/requirements.txt` into a virtual environment and run:

```sh
python3 -m unittest discover -s tests -v
```

The tests mock BlueZ/BLE and MQTT; actual controller discovery, GATT reads and
LoxBerry web rendering still need verification on a LoxBerry host with sensors.

## Feedback and history

[LoxForum discussion](https://www.loxforum.com/forum/projektforen/loxberry/plugins/156917-plugin-xiaomi-miflora-flower-monitor)

- 3.0.0: Bleak/BlueZ D-Bus, shared adapter inventory/selection, MQTT-first
  defaults, acknowledged MQTT delivery and structured bilingual settings.
- 2022-02-13: 2.0.4, initial experimental MQTT support.
- 2021-08-01: 2.0.3, WebUI switch fix.
- 2020-01-05: 2.0.2, btlewrap initialization fix.
- 2019-12-31: 2.0.1, LoxBerry 2 support.
- 2018-06-18: 1.0.1, installation fix.
- 2018-06-04: 1.0.0, initial release.
