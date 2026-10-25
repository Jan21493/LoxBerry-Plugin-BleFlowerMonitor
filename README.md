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
- MQTT Basics configured in LoxBerry when MQTT delivery is enabled.
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
inventory. As in TeslaCmd, read-only `hciconfig -a` diagnostics supply the HCI
version and revision. This command is not used for selection, scanning or GATT
communication; missing diagnostics are logged without blocking BLE operation.

| Selection | Behaviour |
| --- | --- |
| Default - hci0 | Always use `hci0`; no fallback if it is missing. |
| Onboard | First onboard/UART controller in natural hci-number order. |
| USB | First USB controller in natural hci-number order. |

The dropdown contains **Default - hci0, USB and Onboard**. Default explicitly
uses `hci0`. USB and Onboard store the adapter type, not an hci number.
Each poll resolves the first matching adapter of the selected type again, so the
selection survives hci renumbering. A missing selected type fails explicitly;
there is **no fallback** to another type. An internally mounted USB controller
is classified as USB, not UART.

Example dropdown labels (the actual USB chipset is determined from VID:PID,
and the board model is read from the system, not hardcoded):

```text
USB - currently mapped to hci6 - Realtek Bluetooth 5.4 USB-Adapter (RTL8761BU or newer)
Onboard - Raspberry Pi 5 Model B Rev 1.1 Bluetooth
```

Dropdown labels omit separate manufacturer/address/version/VID:PID fields.
The installed-adapter list remains detailed, with hci numbers, product/chipset
and address information, HCI version with revision, and USB VID:PID at the end.
Onboard labels use hyphens instead of slashes.

Both discovery and GATT reads use the selected controller. Sensors are read
sequentially, with up to three bounded attempts per sensor. Errors on one sensor
do not prevent other sensors from being read. A process lock prevents cron and
manual polls from overlapping. The hourly cron checks the configured interval;
it is not a continuously running daemon.

### MQTT (default for new installations)

The plugin **always uses the broker configured in LoxBerry's MQTT Basics**.
Host, port, username, password and TLS settings are read from the `Mqtt` section
of `$LBSCONFIG/general.json` at each delivery, following LoxBerry's
`mqtt_connectiondetails` schema (`Brokerhost`, `Brokerport`, `Brokeruser`,
`Brokerpass` and the TLS flags). Missing or invalid central configuration is
reported explicitly, without falling back to former plugin-local credentials.
Configure only the topic prefix (default `miflora`) and enable/disable MQTT in
the plugin. No Miniserver configuration is needed for MQTT-only operation.

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
TLS and certificate verification follow MQTT Basics, including the local
broker's TLS port and the external broker's certificate-validation setting.
If certificate validation is enabled, the configured LoxBerry CA file must
be readable. Without TLS, use MQTT only on a trusted network.

### UDP (optional)

Enable UDP and select a Miniserver configured in LoxBerry plus its virtual UDP
input port (default `5006`). Each value is a separate datagram in the unchanged
format `<MAC without colons>.<SensorName>=<Value>`:

```text
C47C8D66275B.Temperature=27.1
C47C8D66275B.Moisture=4
```

Both `general.json` and legacy `general.cfg` Miniserver configuration files are
supported for UDP. MQTT reads `general.json` independently for MQTT Basics;
it never requires a configured Miniserver. UDP has no delivery acknowledgements.

### Upgrading existing installations

Transport enablement, topics and UDP settings are preserved, including UDP-only
or MQTT-disabled configurations. MQTT is **not silently enabled** during an
upgrade, but when enabled it now uses MQTT Basics exclusively. Old plugin-local
broker credentials are removed during migration or the next settings save.

The `default` selection is preserved as Default - hci0. Legacy explicit `hciX`
selections are migrated to the currently matching USB/Onboard type during upgrade.
If that controller is absent, migration reports
a warning and polling requires explicitly choosing USB or Onboard and saving;
the plugin never guesses another type. Missing legacy selections are treated
as the former `hci0` default during migration. Fresh installs default to Onboard,
enable MQTT and disable UDP.

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

- 3.0.1: Default/USB/Onboard adapter selection with TeslaCmd-style labels and
  detailed HCI diagnostics, central MQTT Basics integration, and restored
  Save / Poll now / Refresh buttons.
- 3.0.0: Bleak/BlueZ D-Bus, shared adapter inventory/selection, MQTT-first
  defaults, acknowledged MQTT delivery and structured bilingual settings.
- 2022-02-13: 2.0.4, initial experimental MQTT support.
- 2021-08-01: 2.0.3, WebUI switch fix.
- 2020-01-05: 2.0.2, btlewrap initialization fix.
- 2019-12-31: 2.0.1, LoxBerry 2 support.
- 2018-06-18: 1.0.1, installation fix.
- 2018-06-04: 1.0.0, initial release.
