# SteelSeries GG Device Descriptors

`decode-steelseries-device-descriptors.sh` records the OpenPGP passphrase and the
repeatable extraction process used to decode SteelSeries GG `.edevice` descriptors.
It is intentionally kept in this local research directory, outside the application
source checkout.

Download a Windows GG installer, then run:

```bash
~/.local/share/linux-arctis-manager/research/decode-steelseries-device-descriptors.sh \
  --installer /path/to/SteelSeriesGGSetup.exe \
  --version 114.0.0
```

To decode a descriptor directory that has already been extracted from an installer:

```bash
~/.local/share/linux-arctis-manager/research/decode-steelseries-device-descriptors.sh \
  --source-dir /path/to/apps/engine/deviceSpecifications \
  --version 114.0.0 \
  --replace
```

The script requires `7z` only for `--installer`; it otherwise requires `gpg`,
`python3`, and standard GNU command-line utilities. It removes GG's custom armor
label and checksum before asking GnuPG to decrypt the OpenPGP packet. GnuPG verifies
the authenticated encryption, and the script then checks that every result is UTF-8
text with no null bytes and that all descriptor includes resolve.

For each version, output lives in `steelseries-gg-VERSION/`:

- `specs/apps/engine/deviceSpecifications/`: original encrypted `.edevice` files
- `decoded-device-specifications/`: decrypted descriptor sources as `.device` files
- `decode-results.tsv`: source/output mapping and byte counts
- `encrypted-device-specifications.sha256` and `decoded-device-specifications.sha256`: manifests

Existing output is protected by default. Pass `--replace` only when deliberately
refreshing the same GG version.

## Recovering the Passphrase

The passphrase to decrypt the edvice descriptor files is embedded in the `SteelSeriesEngine.exe` 
file and needs to be extracted and set in `decode-steelseries-device-descriptors.sh` before running
the script.

```bash
engine=/path/to/apps/engine/SteelSeriesEngine.exe
key="$(python3 ~/.local/share/linux-arctis-manager/research/recover-steelseries-descriptor-key.py "$engine")"
printf '%s\n' "$key"
```

The helper reads the executable's Go metadata, finds
`fileencryption.DecryptDeviceFile.func1`, and recovers the static strings it passes
to `runtime.concatbyte2`. The result is a candidate key. Validate it against one
descriptor before updating the persisted value:

```bash
descriptor=/path/to/apps/engine/deviceSpecifications/arctis_nova_pro_omni_tx.edevice
awk '/^-----/{next} /^=/{next} NF {print}' "$descriptor" \
  | base64 -d \
  | gpg --batch --yes --pinentry-mode loopback --passphrase "$key" --decrypt \
  > /tmp/descriptor.device
```

GnuPG must report successful decryption. Then replace only
`DESCRIPTOR_PASSPHRASE` in `decode-steelseries-device-descriptors.sh`, rerun the
extractor, and confirm its validation and checksum manifests succeed.

If the helper reports that GG's layout changed, open `SteelSeriesEngine.exe` in
Ghidra or rizin. Locate the Go function named
`github.com/steelseries/engine/pkg/utils/fileencryption.DecryptDeviceFile.func1`,
follow its call to `runtime.concatbyte2`, and concatenate the ASCII strings passed
as its two byte-slice arguments. Validate the resulting candidate with GnuPG before
persisting it.

# Arctis Nova Pro Omni firmware updater

> [!CAUTION]
> THIS SCRIPT IS EXPERIMENTAL AND INTENDED FOR REFERENCE USE ONLY! USE AT YOUR OWN RISK!

`arctis-omni-firmware-update.py` is a standalone Linux firmware updater for
the SteelSeries Arctis Nova Pro Omni. It reproduces the
`SSFWFIZZFEATURE` update flow used by SteelSeries GG 114.0.0, while keeping
the updater separate from Linux Arctis Manager.

It is intentionally locked to the five Omni firmware images packaged in the
local SteelSeries GG 114.0.0 installer. It does not download firmware and
will refuse images whose size or SHA-256 hash does not match the values
compiled into the script.

> [!WARNING]
>
> Firmware flashing erases the selected component before writing its
> replacement. Keep the relevant USB device connected, do not suspend or
> reboot the computer, and do not interrupt the command once flashing has
> begun. Stop Linux Arctis Manager, SteelSeries GG, and other programs that
> access the headset first.

## Supported hardware and firmware

This tool supports only the Arctis Nova Pro Omni USB devices and the GG
114.0.0 images below. It is not a generic SteelSeries or generic Arctis
firmware flasher.

| Component key | Hardware component | Target version | USB requirement |
| --- | --- | --- | --- |
| `mcu1` | Base-station MCU 5528 | 1.32.0 | Base station connected directly by USB |
| `mcu2` | Base-station MCU 5516 | 1.32.0 | Base station connected directly by USB |
| `dsp` | Base-station DSP | 0.36.0 | Base station connected directly by USB |
| `rx-mcu` | Headset receiver MCU 1585 | 0.36.0 | Headset connected directly to the computer by USB |
| `rx-bt` | Headset receiver Bluetooth 1565 | 0.36.0 | Headset connected directly to the computer by USB |

The base station is USB ID `1038:2290`; its distinct MCU-1 bootloader is
`1038:2291`. The directly connected headset receiver is `1038:2296`; its MCU
bootloader is `1038:2297`.

The wireless connection between the base station and headset is not a
firmware-update transport for `rx-mcu` or `rx-bt`. The headset itself must be
connected by a USB data cable before selecting either component.

## Requirements

- Linux with Python 3.
- The Python `pyusb` package and a working libusb backend.
- The `7z` executable, used to extract the selected firmware files from the
  installer.
- The GG 114.0.0 installer, normally named `SteelSeriesGG114.0.0Setup.exe`.
- Root access for a live update, so the script can claim the HID interface.

The default installer location is:

```text
/home/cisien/.local/share/linux-arctis-manager/research/steelseries-gg-114.0.0/SteelSeriesGG114.0.0Setup.exe
```

Supply a different copy with the global `--installer` option before the
subcommand:

```sh
python3 arctis-omni-firmware-update.py \
  --installer /path/to/SteelSeriesGG114.0.0Setup.exe verify
```

An installer from another GG version is expected to fail validation rather
than being flashed.

## Quick start

Run these commands from this directory.

```sh
# No USB access or installer extraction.
python3 arctis-omni-firmware-update.py self-test

# Inspect the versions and connected targets.
sudo -E python3 arctis-omni-firmware-update.py status

# Extract and validate the five pinned firmware images, without USB writes.
python3 arctis-omni-firmware-update.py verify

# Validate the live update plan without entering a bootloader or flashing.
sudo -E python3 arctis-omni-firmware-update.py update --dry-run
```

When the dry run succeeds and every required target is connected, perform the
update:

```sh
sudo -E python3 arctis-omni-firmware-update.py update --yes-flash
```

With no `--component` option, the tool selects all five components. Its
preflight verifies every selected USB target before the first erase, so an
unconnected headset prevents a mixed base-only update.

## Updating individual components

Use `--component` to limit an update. The option accepts a comma-separated
list and can be repeated.

```sh
# Base-station components
sudo -E python3 arctis-omni-firmware-update.py update --component mcu1 --yes-flash
sudo -E python3 arctis-omni-firmware-update.py update --component mcu2 --yes-flash
sudo -E python3 arctis-omni-firmware-update.py update --component dsp --yes-flash

# Headset: connect the headset directly by USB first.
sudo -E python3 arctis-omni-firmware-update.py update --component rx-mcu --yes-flash
sudo -E python3 arctis-omni-firmware-update.py update --component rx-bt --yes-flash
```

The updater skips components already reporting their target version. Use
`--force` only when deliberately reflashing a current component:

```sh
sudo -E python3 arctis-omni-firmware-update.py \
  update --component mcu2 --force --yes-flash
```

## What the updater does

For each selected component, the script:

1. Extracts only that component's firmware from the GG installer and checks
   its expected size and SHA-256 digest.
2. Reads the device's HID report descriptor and selects a suitable control
   interface and reports.
3. Reads current firmware versions and skips already-current components.
4. Sends the GG boot-entry request, waits for the component's bootloader
   transport, and allows GG's five-second bootloader settle period.
5. Erases the selected flash area, writes 1012-byte blocks, and verifies the
   bootloader's CRC response.
6. Sends the ordinary HID reset request, waits for the application firmware,
   and checks the flashed component's reported version.

The base station uses HID report ID 1 for this protocol; the direct headset
receiver uses no physical report ID. The script drains queued asynchronous HID
notifications and filters acknowledgements by report ID, avoiding confusion
with headset telemetry such as battery events.

Entering and leaving bootloader mode can make a USB control transfer return
`No such device` while the device is already re-enumerating. The script
recognizes that expected transition and confirms success from the returned
application firmware version.

## Recovery

Do not manually enter a special flashing mode: the script sends the required
boot-entry command itself.

If a component has been erased or `status` reports a bootloader, leave the
device connected and rerun the update for that same component. For example,
an interrupted headset MCU update can be resumed with:

```sh
sudo -E python3 arctis-omni-firmware-update.py update --component rx-mcu --yes-flash
```

For base MCU-1 recovery, use `mcu1`; for an MCU-2, DSP, or receiver Bluetooth
failure, retry the exact component that failed. Do not begin another component
until the affected component has completed and `status` reports its expected
version.

If the script cannot open a bootloader transport, do not unplug a device that
has already been erased. Preserve its power and USB connection, collect the
full command output and `sudo dmesg --ctime` tail, then retry the same
component or recover it with SteelSeries GG.

## Troubleshooting

| Symptom | Action |
| --- | --- |
| `PyUSB is required` | Install the Python `pyusb` package for the Python interpreter being used. |
| `The '7z' executable is required` | Install a package providing the `7z` command. |
| `Access denied` or a HID interface cannot be claimed | Stop other headset-control software and use `sudo -E` for a live update. |
| The headset components cannot be found | Connect the headset directly to the computer with a USB data cable; a base-station wireless connection is insufficient. |
| A base display says firmware update is available | Keep the base connected and retry the component that was being flashed. The script handles normal boot-entry transitions. |
| A transfer reports `No such device` during boot entry or reset | Check the subsequent output or run `status`; this can be the expected USB re-enumeration into or out of bootloader mode. |

`Ctrl-C` is deliberately ignored once on a live run to reduce accidental
interruptions. A second `Ctrl-C` follows normal Python interrupt behavior, so
avoid using either while a component is flashing.

## Protocol scope

The implementation is based on GG's `SSFWFIZZFEATURE` handler used by the
Omni device definitions. It is deliberately not applied to other Arctis
families: GG also contains distinct `SSFWFIZZ`, `SSFWARCTISNOVA7GEN2`, Nova
Pro Wireless, `SSFWWITHMODE`, and legacy update handlers with different
metadata layouts, block sizes, acknowledgements, or bootloader behavior.
