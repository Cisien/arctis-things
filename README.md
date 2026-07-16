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


# Arctis Nova Pro Omni / Nova Elite EQ tool

`arctis-nova-elite-eq.py` applies one GG parametric-EQ model to the
2.4 GHz/radio EQ stored by a supported SteelSeries base station. It is a
standalone research tool: it does not install or start SteelSeries GG, modify
Linux Arctis Manager, or update firmware.

It consumes JSON exported by `steelseries-gg-eq-preset-extract.py` from a
local SteelSeries GG installer. The two scripts can remain outside a Linux
Arctis Manager checkout.

> [!Warning]
>
> A live `apply --yes-apply` command changes the selected on-device radio EQ
> slot immediately. Stop Linux Arctis Manager, SteelSeries GG, and other
> headset-control programs first. Keep the base station connected by USB and
> do not disconnect it while the report is being sent.

## Scope

Supported base stations:

| Device | USB ID | Tool selector |
| --- | --- | --- |
| Arctis Nova Pro Omni | `1038:2290` | `omni` |
| Arctis Nova Elite | `1038:2244` | `elite` |
| Arctis Nova Elite SNG | `1038:2270` | `elite-sng` |

The default `--device auto` detects exactly one of these base stations. The
tool applies only the shared 2.4 GHz/radio **parametric** EQ. It does not
write Bluetooth or microphone EQ: those destinations are fixed-band,
gain-only formats and cannot reproduce a GG parametric game preset exactly.

The directly connected headset receiver is not an EQ target for this tool.

## Requirements

- Python 3.10 or newer.
- Python `pyusb` and a working libusb backend for `probe` or a live apply.
- Permission to claim the base station's vendor-HID interface. On a normal
  Linux system, use `sudo -E` for `probe` and a live apply.
- A GG preset JSON file. Generate one with the adjacent extractor:

  ```sh
  python3 steelseries-gg-eq-preset-extract.py --quiet --output gg-eq-presets.json
  ```

No USB access, root access, or PyUSB installation is needed to list, show, or
dry-run a preset.

## Quick start

Run from the directory containing the scripts and the generated preset file.

```sh
# Check the pure JSON-to-HID encoder without a device.
python3 arctis-nova-elite-eq.py self-test

# Find candidate presets.
python3 arctis-nova-elite-eq.py list --filter forza

# Print the original exported JSON for one preset.
python3 arctis-nova-elite-eq.py show --preset 'Forza Horizon 5'

# Validate and display the exact radio-EQ plan. This is a dry run.
python3 arctis-nova-elite-eq.py apply --preset 'Forza Horizon 5'

# Confirm that the connected base exposes the expected vendor-HID report.
sudo -E python3 arctis-nova-elite-eq.py probe

# Perform the write only after reviewing the dry run.
sudo -E python3 arctis-nova-elite-eq.py \
  apply --preset 'Forza Horizon 5' --yes-apply
```

`apply` is always a dry run unless `--yes-apply` is present. A dry run never
opens or claims a USB interface.

## Selecting a preset file and device

The default input is `gg-eq-presets.json` in the current directory. Use
`--preset-file` with an alternative exported catalogue or a single preset
object:

```sh
python3 arctis-nova-elite-eq.py \
  apply --preset-file /path/to/gg-eq-presets.json --preset 'Apex Legends'

# Use a specific hardware family instead of automatic detection.
sudo -E python3 arctis-nova-elite-eq.py \
  apply --device omni --preset 'Apex Legends' --yes-apply
```

`--preset` accepts an exact display name, alias, UUID, or a unique partial
name. An ambiguous partial match fails and prints the candidate names; it
never guesses which preset to write.

## On-device slots

GG does not store arbitrary game curves in the built-in Flat/Bass/Focus/Smiley
slots. The default `--slot auto` follows GG's own mapping:

| GG model | Device slot |
| --- | --- |
| `Flat` | `flat` (0) |
| `Bass Boost` | `bass-boost` (1) |
| `Focus` | `focus` (2) |
| `Smiley` | `smiley` (3) |
| GG custom model (`preset_type: 1`) | `custom` (4) |
| Other game/device models | `game` (5) |

Use an explicit slot to save a game curve in the Custom slot instead:

```sh
sudo -E python3 arctis-nova-elite-eq.py \
  apply --preset 'Forza Horizon 5' --slot custom --yes-apply
```

`--name` changes the stored long name (maximum 61 UTF-8 bytes), and `--alias`
changes its short name (maximum 6 UTF-8 bytes).

## Model format and conversion

The tool accepts an exported GG row such as:

```json
{
  "display_name": "Example curve",
  "alias_name": "EXAMP",
  "preset_type": 0,
  "eq_preset_data": {
    "filter1": {
      "enabled": true,
      "frequency": 100.0,
      "gain": 6.0,
      "qFactor": 0.707,
      "type": "peakingEQ"
    }
  }
}
```

All ten filters (`filter1` through `filter10`) are required. The supported
filter types are `peakingEQ`, `lowPass`, `highPass`, `lowShelving`, and
`highShelving`.

The radio hardware stores each filter as six bytes:

| GG property | Device encoding |
| --- | --- |
| `frequency` | unsigned 16-bit little-endian Hz, 20–20,001 |
| `type` | 1=peak, 2=low-pass, 3=high-pass, 4=low-shelf, 5=high-shelf |
| `gain` | signed byte, decibels × 10, range −12.0 to +12.0 dB |
| `qFactor` | unsigned 16-bit little-endian, Q × 1000, range 0.2–10 |
| `enabled: false` | frequency `20,001`, GG's disabled-band sentinel |

Non-integer frequency, gain, and Q values are truncated to the hardware's
integer representation using the same scaling in GG's decoded payload recipe.
The dry-run output shows the final encoded values.

The complete radio write is a 130-byte meaningful payload in HID feature
report 1: report ID, command `0x1b`, selected slot, six-byte alias,
61-byte name, and ten packed filters. The kernel pads it to the base's
feature-report length.

## Catalogue compatibility

The tool validates every filter before a USB write. In the GG 114.0.0
catalogue, 381 of 382 exported models fit this protocol. The exception is
`DOOM: The Dark Ages`, which uses `notchFilter`; that type has no Omni/Nova
Elite radio hardware equivalent, so the tool refuses it rather than silently
substituting a different filter.

GG marks microphone-only models with `supported_mode: 4`. They are rejected by
default because a microphone curve is not normally suitable for radio output.
If that is intentional, add `--allow-mic-model`; this still writes the model
to the radio EQ, never to the microphone EQ.

## Verification and troubleshooting

`probe` is a read-only compatibility check. It claims the preferred HID
control interface, reads its report descriptor, and reports the available
input/output/feature report sizes. A compatible base needs feature report ID
1 large enough for the 130-byte model; current Omni/Elite bases expose the
much larger 1036-byte physical report.

The write protocol has no acknowledgement or read-back in GG's device
definition. Therefore, `Radio EQ applied successfully` means that the
operating system accepted the complete HID feature report; it is not a
separate persistence verification. Confirm the selected name/curve on the
base station or in GG if independent confirmation is needed.

| Symptom | Action |
| --- | --- |
| `PyUSB is required` | Install the `pyusb` package for the Python interpreter being used. |
| No supported base station is connected | Connect the base directly by USB and check `lsusb` for one of the supported IDs. |
| Cannot open the HID interface | Stop headset-control software and retry `probe` or apply with `sudo -E`. |
| Preset selector is ambiguous | Use the complete display name or the UUID shown by `list`. |
| Model is microphone-only | Select a radio/game model, or explicitly add `--allow-mic-model`. |
| Unsupported filter type | The target hardware cannot reproduce that curve exactly; choose another model. |

Do not run this tool while a firmware updater is operating on the same base
station.

