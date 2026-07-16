#!/usr/bin/env python3
"""Apply a GG parametric-EQ model to an Arctis Nova Pro Omni or Nova Elite.

This is a standalone research utility.  It consumes the JSON exported by
``steelseries-gg-eq-preset-extract.py`` and implements only the shared
Nova Elite radio/2.4 GHz parametric-EQ write.  It does not install or launch
SteelSeries GG, modify Linux Arctis Manager, or update firmware.

GG writes a model to this device family through HID feature report 1:
``[1, 0x1b, slot, alias[6], name[61], ten packed filters]``.  Each packed
filter is ``frequency-le16, type-u8, gain-s8-in-tenths-dB, q-le16-in-1/1000``.
The physical report is padded to the report descriptor's feature length.
"""

from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Keep catalogue inspection usable even when PyUSB is not installed.
    import usb.core
    import usb.util
except ImportError:  # pragma: no cover - depends on the host install
    usb = None  # type: ignore[assignment]


VENDOR_ID = 0x1038
CONTROL_INTERFACE_HINT = 3
FEATURE_REPORT_ID = 1
FEATURE_REPORT_TYPE = 3
SET_REPORT = 0x09
HID_CLASS = 0x03
DESCRIPTOR_REPORT = 0x22
MIN_FEATURE_PAYLOAD = 130

MODELS = {
    "omni": (0x2290, "Arctis Nova Pro Omni"),
    "elite": (0x2244, "Arctis Nova Elite"),
    "elite-sng": (0x2270, "Arctis Nova Elite (SNG)"),
}

FILTER_TYPES = {
    "peakingEQ": 1,
    "lowPass": 2,
    "highPass": 3,
    "lowShelving": 4,
    "highShelving": 5,
}
SLOT_NAMES = {
    "flat": 0,
    "bass-boost": 1,
    "focus": 2,
    "smiley": 3,
    "custom": 4,
    "game": 5,
}
BUILTIN_SLOT_NAMES = {
    "flat": 0,
    "bass boost": 1,
    "focus": 2,
    "smiley": 3,
}


class EqError(RuntimeError):
    """A recoverable user-facing error."""


@dataclass(frozen=True)
class ReportLayout:
    input: dict[int, int]
    output: dict[int, int]
    feature: dict[int, int]

    @staticmethod
    def wire_size(report_id: int, descriptor_size: int) -> int:
        """HID report descriptor lengths omit a non-zero physical report ID."""

        return descriptor_size + int(report_id != 0)


@dataclass(frozen=True)
class PackedModel:
    source_id: str
    display_name: str
    alias_name: str
    slot: int
    filters: tuple[tuple[int, int, int, int], ...]
    payload: bytes


def parse_report_descriptor(descriptor: bytes) -> ReportLayout:
    """Parse enough of a HID report descriptor to validate feature report 1."""

    report_size = 0
    report_count = 0
    report_id = 0
    input_reports: dict[int, int] = {}
    output_reports: dict[int, int] = {}
    feature_reports: dict[int, int] = {}
    index = 0

    while index < len(descriptor):
        prefix = descriptor[index]
        index += 1
        if prefix == 0xFE:
            if index + 2 > len(descriptor):
                break
            size = descriptor[index]
            index += 2 + size
            continue

        size_code = prefix & 0x03
        size = 4 if size_code == 3 else size_code
        item_type = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        if index + size > len(descriptor):
            break
        value = int.from_bytes(descriptor[index : index + size], "little")
        index += size

        if item_type == 1 and tag == 7:  # Report Size
            report_size = value
        elif item_type == 1 and tag == 8:  # Report ID
            report_id = value
        elif item_type == 1 and tag == 9:  # Report Count
            report_count = value
        elif item_type == 0 and tag in (8, 9, 11):  # Input, Output, Feature
            if report_size % 8:
                continue
            length = report_size * report_count // 8
            reports = {8: input_reports, 9: output_reports, 11: feature_reports}[tag]
            reports[report_id] = max(reports.get(report_id, 0), length)

    return ReportLayout(input_reports, output_reports, feature_reports)


def require_pyusb() -> None:
    if usb is None:
        raise EqError("PyUSB is required for device access. Install the Python 'pyusb' package first.")


def render_reports(reports: dict[int, int]) -> str:
    return ", ".join(f"id {report_id}: {size} B" for report_id, size in sorted(reports.items())) or "none"


class EliteEqTransport:
    """Claim and validate the shared Omni/Nova Elite vendor-HID interface."""

    def __init__(self, device: Any):
        self.device = device
        self.interface: int | None = None
        self.layout: ReportLayout | None = None
        self.detached_kernel_driver = False

    def _hid_interfaces(self) -> list[int]:
        try:
            configuration = self.device.get_active_configuration()
        except usb.core.USBError as error:
            raise EqError(f"Could not read the active USB configuration: {error}") from error
        return sorted(
            int(interface.bInterfaceNumber)
            for interface in configuration
            if interface.bAlternateSetting == 0 and interface.bInterfaceClass == HID_CLASS
        )

    def _close_interface(self) -> None:
        if self.interface is None:
            return
        try:
            usb.util.release_interface(self.device, self.interface)
        except usb.core.USBError:
            pass
        if self.detached_kernel_driver:
            try:
                self.device.attach_kernel_driver(self.interface)
            except usb.core.USBError:
                pass
        self.interface = None
        self.layout = None
        self.detached_kernel_driver = False

    def __enter__(self) -> EliteEqTransport:
        candidates = self._hid_interfaces()
        if not candidates:
            raise EqError("The selected device has no HID control interface.")

        candidates.sort(key=lambda number: (number != CONTROL_INTERFACE_HINT, number))
        failures: list[str] = []
        for number in candidates:
            self.interface = number
            try:
                try:
                    if self.device.is_kernel_driver_active(number):
                        self.device.detach_kernel_driver(number)
                        self.detached_kernel_driver = True
                except (NotImplementedError, usb.core.USBError):
                    pass
                usb.util.claim_interface(self.device, number)
                descriptor = bytes(
                    self.device.ctrl_transfer(
                        0x81,
                        0x06,
                        DESCRIPTOR_REPORT << 8,
                        number,
                        4096,
                        timeout=2_000,
                    )
                )
                layout = parse_report_descriptor(descriptor)
                if layout.feature.get(FEATURE_REPORT_ID, 0) < MIN_FEATURE_PAYLOAD:
                    raise EqError(
                        f"feature report {FEATURE_REPORT_ID} is too small "
                        f"({layout.feature.get(FEATURE_REPORT_ID, 0)} B; need {MIN_FEATURE_PAYLOAD} B)"
                    )
                self.layout = layout
                return self
            except (EqError, usb.core.USBError) as error:
                failures.append(f"interface {number}: {error}")
                self._close_interface()
        raise EqError(
            "Could not open the Omni/Nova Elite EQ HID interface. Stop Linux Arctis Manager, "
            f"SteelSeries GG, and other headset-control programs, then retry. ({'; '.join(failures)})"
        )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._close_interface()

    def describe(self) -> str:
        if self.interface is None or self.layout is None:
            raise AssertionError("HID transport has not been opened")
        return (
            f"interface {self.interface}, input [{render_reports(self.layout.input)}], "
            f"output [{render_reports(self.layout.output)}], feature [{render_reports(self.layout.feature)}]"
        )

    def write_radio_eq(self, payload: bytes) -> None:
        if self.interface is None or self.layout is None:
            raise AssertionError("HID transport has not been opened")
        feature_size = self.layout.feature[FEATURE_REPORT_ID]
        wire_size = ReportLayout.wire_size(FEATURE_REPORT_ID, feature_size)
        if len(payload) > wire_size:
            raise EqError(f"EQ payload is {len(payload)} B but HID report {FEATURE_REPORT_ID} permits {wire_size} B.")
        if not payload or payload[0] != FEATURE_REPORT_ID:
            raise EqError("EQ payload does not start with physical HID report ID 1.")
        try:
            transferred = self.device.ctrl_transfer(
                0x21,
                SET_REPORT,
                (FEATURE_REPORT_TYPE << 8) | FEATURE_REPORT_ID,
                self.interface,
                payload.ljust(wire_size, b"\0"),
                timeout=10_000,
            )
        except usb.core.USBError as error:
            raise EqError(f"HID SET_FEATURE failed while applying the radio EQ: {error}") from error
        if isinstance(transferred, int) and transferred not in (0, wire_size):
            raise EqError(f"HID SET_FEATURE wrote only {transferred} of {wire_size} bytes.")


def read_json(path: Path) -> Any:
    if not path.is_file():
        raise EqError(f"Preset JSON file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EqError(f"Could not parse preset JSON {path}: {error}") from error


def records_from_document(document: Any) -> list[dict[str, Any]]:
    if isinstance(document, list):
        records = document
    elif isinstance(document, dict) and isinstance(document.get("presets"), list):
        records = document["presets"]
    elif isinstance(document, dict) and "eq_preset_data" in document:
        records = [document]
    elif isinstance(document, dict) and "filter1" in document:
        records = [{"display_name": "Unnamed EQ", "alias_name": "", "preset_type": 1, "eq_preset_data": document}]
    else:
        raise EqError("Expected a GG preset list, one GG preset object, or an eq_preset_data filter object.")
    if not all(isinstance(record, dict) for record in records):
        raise EqError("The preset document contains a non-object record.")
    return records


def preset_label(record: dict[str, Any]) -> str:
    return str(record.get("display_name", "")) or "<unnamed>"


def choose_preset(records: Iterable[dict[str, Any]], selector: str) -> dict[str, Any]:
    selector_folded = selector.casefold()
    candidates = list(records)
    exact = [
        record
        for record in candidates
        if selector_folded in {
            str(record.get("id", "")).casefold(),
            str(record.get("display_name", "")).casefold(),
            str(record.get("alias_name", "")).casefold(),
        }
    ]
    matches = exact or [
        record
        for record in candidates
        if selector_folded in preset_label(record).casefold()
        or selector_folded in str(record.get("alias_name", "")).casefold()
    ]
    if not matches:
        raise EqError(f"No preset matches {selector!r}.")
    if len(matches) > 1:
        rendered = ", ".join(f"{preset_label(record)!r} ({record.get('id', '?')})" for record in matches[:12])
        suffix = ", ..." if len(matches) > 12 else ""
        raise EqError(f"Preset selector {selector!r} is ambiguous: {rendered}{suffix}")
    return matches[0]


def require_text(value: object, field: str, maximum_bytes: int) -> str:
    if not isinstance(value, str) or not value:
        raise EqError(f"Preset {field} must be a non-empty string.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise EqError(f"Preset {field} cannot be encoded as UTF-8.") from error
    if len(encoded) > maximum_bytes:
        raise EqError(f"Preset {field} is {len(encoded)} UTF-8 bytes; device limit is {maximum_bytes}.")
    return value


def pad_text(value: str, size: int) -> bytes:
    return value.encode("utf-8").ljust(size, b"\0")


def number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise EqError(f"{field} must be numeric.")
    result = float(value)
    if not math.isfinite(result):
        raise EqError(f"{field} must be finite.")
    return result


def quantize(value: float, multiplier: int) -> int:
    """Match GG's float-to-integer cast used by int16-to-bytes/uint16-to-bytes."""

    return int(value * multiplier)


def pack_filter(data: object, index: int) -> tuple[int, int, int, int]:
    if not isinstance(data, dict):
        raise EqError(f"filter{index} must be an object.")
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise EqError(f"filter{index}.enabled must be true or false.")
    filter_name = data.get("type")
    if filter_name not in FILTER_TYPES:
        raise EqError(
            f"filter{index}.type must be one of {', '.join(FILTER_TYPES)} (got {filter_name!r})."
        )
    frequency = number(data.get("frequency"), f"filter{index}.frequency")
    gain = number(data.get("gain"), f"filter{index}.gain")
    q_factor = number(data.get("qFactor"), f"filter{index}.qFactor")
    if not 20 <= frequency <= 20_001:
        raise EqError(f"filter{index}.frequency={frequency} is outside the device range 20..20001 Hz.")
    if not -12 <= gain <= 12:
        raise EqError(f"filter{index}.gain={gain} is outside the device range -12..12 dB.")
    if not 0.2 <= q_factor <= 10:
        raise EqError(f"filter{index}.qFactor={q_factor} is outside the device range 0.2..10.")
    gain_tenths = quantize(gain, 10)
    q_thousandths = quantize(q_factor, 1000)
    # The Elite/Omni report has no enabled bit. GG marks a disabled filter by
    # putting it beyond the audible range; 20001 is its standard sentinel.
    frequency_u16 = 20_001 if not enabled else int(frequency)
    return frequency_u16, FILTER_TYPES[filter_name], gain_tenths, q_thousandths


def select_slot(record: dict[str, Any], override: str) -> int:
    if override != "auto":
        return SLOT_NAMES[override]
    preset_type = record.get("preset_type", 0)
    if preset_type == 1:
        return SLOT_NAMES["custom"]
    return BUILTIN_SLOT_NAMES.get(preset_label(record).casefold(), SLOT_NAMES["game"])


def build_model(record: dict[str, Any], slot_override: str, name_override: str | None, alias_override: str | None) -> PackedModel:
    eq_data = record.get("eq_preset_data")
    if not isinstance(eq_data, dict):
        raise EqError(f"Preset {preset_label(record)!r} has no eq_preset_data object.")
    display_name = require_text(name_override or record.get("display_name"), "display_name", 61)
    alias_name = alias_override if alias_override is not None else str(record.get("alias_name", ""))
    if len(alias_name.encode("utf-8")) > 6:
        raise EqError(f"Preset alias_name is {len(alias_name.encode('utf-8'))} UTF-8 bytes; device limit is 6.")
    filters = tuple(pack_filter(eq_data.get(f"filter{index}"), index) for index in range(1, 11))
    slot = select_slot(record, slot_override)
    packed_filters = b"".join(struct.pack("<HBbH", *filter_data) for filter_data in filters)
    payload = bytes((FEATURE_REPORT_ID, 0x1B, slot)) + pad_text(alias_name, 6) + pad_text(display_name, 61) + packed_filters
    if len(payload) != MIN_FEATURE_PAYLOAD:
        raise AssertionError(f"Unexpected radio EQ payload size: {len(payload)}")
    return PackedModel(
        source_id=str(record.get("id", "")),
        display_name=display_name,
        alias_name=alias_name,
        slot=slot,
        filters=filters,
        payload=payload,
    )


def find_device(model: str) -> tuple[Any, str, int]:
    require_pyusb()
    choices = MODELS.items() if model == "auto" else ((model, MODELS[model]),)
    found: list[tuple[Any, str, int]] = []
    for key, (product_id, label) in choices:
        for device in usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=product_id):
            found.append((device, label, product_id))
    if not found:
        choices_text = ", ".join(f"1038:{entry[0]:04x}" for _, entry in choices)
        raise EqError(f"No supported Omni/Nova Elite base station is connected ({choices_text}).")
    if len(found) > 1:
        names = ", ".join(f"{label} (1038:{product_id:04x})" for _, label, product_id in found)
        raise EqError(f"More than one supported base station is connected: {names}. Unplug duplicates and retry.")
    return found[0]


def print_plan(model: PackedModel) -> None:
    slot_name = next(name for name, value in SLOT_NAMES.items() if value == model.slot)
    print(f"Preset: {model.display_name} ({model.source_id or 'no id'})")
    print(f"Alias: {model.alias_name or '<empty>'}")
    print(f"Radio slot: {model.slot} ({slot_name})")
    print("Packed radio model: 10 filters, 130 meaningful bytes in HID feature report 1.")
    for index, (frequency, filter_type, gain, q_factor) in enumerate(model.filters, start=1):
        suffix = " (disabled)" if frequency == 20_001 else ""
        print(
            f"  {index:2d}: {frequency:5d} Hz, type {filter_type}, {gain / 10:+.1f} dB, "
            f"Q {q_factor / 1000:.3f}{suffix}"
        )


def command_list(arguments: argparse.Namespace) -> int:
    records = records_from_document(read_json(arguments.preset_file))
    query = arguments.filter.casefold() if arguments.filter else None
    selected = [
        record
        for record in records
        if query is None
        or query in preset_label(record).casefold()
        or query in str(record.get("alias_name", "")).casefold()
    ]
    for record in sorted(selected, key=lambda item: preset_label(item).casefold()):
        print(f"{preset_label(record)}\t{record.get('alias_name', '')}\t{record.get('id', '')}")
    print(f"Listed {len(selected)} preset(s).", file=sys.stderr)
    return 0


def command_show(arguments: argparse.Namespace) -> int:
    record = choose_preset(records_from_document(read_json(arguments.preset_file)), arguments.preset)
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def command_probe(arguments: argparse.Namespace) -> int:
    device, label, product_id = find_device(arguments.device)
    with EliteEqTransport(device) as transport:
        print(f"{label} (1038:{product_id:04x}): {transport.describe()}")
    return 0


def command_apply(arguments: argparse.Namespace) -> int:
    record = choose_preset(records_from_document(read_json(arguments.preset_file)), arguments.preset)
    supported_mode = record.get("supported_mode")
    if supported_mode == 4 and not arguments.allow_mic_model:
        raise EqError(
            "This is marked by GG as a microphone-only model. It is not a radio preset; "
            "use --allow-mic-model only if you intentionally want its filter curve on the radio output."
        )
    model = build_model(record, arguments.slot, arguments.name, arguments.alias)
    print_plan(model)
    if not arguments.yes_apply:
        print(
            "Dry run only; no USB report was sent. Re-run with --yes-apply after stopping other headset-control programs.",
            file=sys.stderr,
        )
        return 0

    device, label, product_id = find_device(arguments.device)
    print(f"Applying to {label} (1038:{product_id:04x}). Do not disconnect it during the HID write.", file=sys.stderr)
    with EliteEqTransport(device) as transport:
        print(f"  {transport.describe()}", file=sys.stderr)
        transport.write_radio_eq(model.payload)
    print("Radio EQ applied successfully.", file=sys.stderr)
    return 0


def command_self_test(_arguments: argparse.Namespace) -> int:
    record = {
        "id": "test",
        "display_name": "Test curve",
        "alias_name": "TEST",
        "preset_type": 0,
        "eq_preset_data": {
            **{
                f"filter{index}": {
                    "enabled": index != 10,
                    "frequency": float(index * 100),
                    "gain": 0.1 * index,
                    "qFactor": 0.707,
                    "type": "peakingEQ",
                }
                for index in range(1, 11)
            },
        },
    }
    model = build_model(record, "auto", None, None)
    assert model.slot == SLOT_NAMES["game"]
    assert len(model.payload) == 130
    assert model.filters[-1][0] == 20_001
    assert model.payload[:9] == b"\x01\x1b\x05TEST\0\0"
    print("Self-test passed: GG model validation and Omni/Nova Elite radio payload encoding.")
    return 0


def add_preset_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--preset-file",
        type=Path,
        default=Path("gg-eq-presets.json"),
        help="JSON produced by steelseries-gg-eq-preset-extract.py (default: gg-eq-presets.json)",
    )


def add_model_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--device", choices=("auto", *MODELS), default="auto", help="target base station (default: auto)")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply GG parametric-EQ models to Arctis Nova Pro Omni and Nova Elite radio EQ."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list presets in an exported GG JSON file")
    add_preset_file_argument(list_parser)
    list_parser.add_argument("--filter", help="only list matching display names or aliases")
    list_parser.set_defaults(handler=command_list)

    show_parser = subparsers.add_parser("show", help="show one GG preset JSON object")
    add_preset_file_argument(show_parser)
    show_parser.add_argument("--preset", required=True, help="exact or unique partial display name, alias, or UUID")
    show_parser.set_defaults(handler=command_show)

    probe_parser = subparsers.add_parser("probe", help="read-only HID compatibility check")
    add_model_argument(probe_parser)
    probe_parser.set_defaults(handler=command_probe)

    apply_parser = subparsers.add_parser("apply", help="dry-run or apply one model to the radio EQ")
    add_preset_file_argument(apply_parser)
    add_model_argument(apply_parser)
    apply_parser.add_argument("--preset", required=True, help="exact or unique partial display name, alias, or UUID")
    apply_parser.add_argument(
        "--slot",
        choices=("auto", *SLOT_NAMES),
        default="auto",
        help="on-device radio EQ slot; auto matches GG's slot selection (default: auto)",
    )
    apply_parser.add_argument("--name", help="replace the on-device long name (UTF-8, at most 61 bytes)")
    apply_parser.add_argument("--alias", help="replace the on-device short name (UTF-8, at most 6 bytes)")
    apply_parser.add_argument(
        "--allow-mic-model",
        action="store_true",
        help="allow a GG model marked microphone-only to be written to the radio EQ",
    )
    apply_parser.add_argument(
        "--yes-apply",
        action="store_true",
        help="actually send the HID feature report; without this, apply is a dry run",
    )
    apply_parser.set_defaults(handler=command_apply)

    self_test_parser = subparsers.add_parser("self-test", help="verify model validation and payload encoding without USB")
    self_test_parser.set_defaults(handler=command_self_test)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = make_parser().parse_args(argv)
    try:
        return arguments.handler(arguments)
    except EqError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
