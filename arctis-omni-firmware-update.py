#!/usr/bin/env python3
"""Safely update Arctis Nova Pro Omni firmware from SteelSeries GG 114.0.0.

This is a standalone research tool.  It neither depends on nor modifies Linux
Arctis Manager.  It verifies firmware extracted from the local GG installer
before touching USB and implements the Fizz HID update flow used by GG's
``SSFWFIZZFEATURE`` implementation:

* enter boot mode: ``[logical-report-id, 0x01, 0x01, file-system-id]``
* erase: ``[logical-report-id, 0x02, file-system-id, file-id]``
* write: ``[logical-report-id, 0x03, fs, file, size-le16, offset-le32, data]``
* CRC: ``[logical-report-id, 0x84, fs, file]``
* reset: ``[logical-report-id, 0x01, 0x00, fs]``

Every erase/write/CRC uses GG's ``HIDFEATURE_OUT_INPUT_IN`` transport: a
full-length SET_FEATURE followed by one interrupt-IN acknowledgement.

The base station is USB product 1038:2290 (MCU-1 bootloader: 1038:2291).
The headset receiver is a *separate direct USB device*, 1038:2296
(MCU bootloader: 1038:2297).  The receiver cannot be flashed wirelessly via
the base station, so ``update`` preflights every selected target before it
erases anything.  Connect the headset by USB before selecting ``rx-mcu`` or
``rx-bt``.

The script is intentionally locked to the five Omni firmware images shipped
inside GG 114.0.0.  It never downloads a firmware image.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import usb.core
    import usb.util
except ImportError as exc:  # pragma: no cover - depends on the host install
    raise SystemExit("PyUSB is required (install the Python package 'pyusb').") from exc


VENDOR_ID = 0x1038
BASE_APP_PRODUCT_ID = 0x2290
BASE_BOOTLOADER_PRODUCT_ID = 0x2291
HEADSET_APP_PRODUCT_ID = 0x2296
HEADSET_BOOTLOADER_PRODUCT_ID = 0x2297

# The normal Omni base station exposes the control HID on interface 3.  The
# headset's descriptor can differ, so the transport falls back to other HID
# interfaces after trying this safe, known interface first.
CONTROL_INTERFACE_HINT = 3
GG_OUTPUT_REPORT_ID = 0x07
BLOCK_SIZE = 1012
WRITE_PACKET_SIZE = 10 + BLOCK_SIZE
# `create_fw_update_payload` sets its extended-timeout byte.  GG consequently
# configures its HID wrapper with `{10, 3, 60000}` before erase, and that
# configuration remains active for the erase, every write, and the CRC read.
GG_FIZZ_TRANSACTION_TIMEOUT_MS = 60_000
# GG's FlashFirmwareFizzFeature deliberately waits 0x1388 ms after the
# prepare/boot-entry command before it issues erase.  The normal HID function
# can otherwise acknowledge Fizz-sized feature requests while the selected
# secondary MCU is still changing state, producing deceptive all-zero status
# and CRC fields.
BOOT_PREPARE_SETTLE_SECONDS = 5.0
# Before every Fizz SET_FEATURE transaction, GG's HID wrapper repeatedly
# issues a zero-wait ReadFile until no packet is immediately ready.  Use a
# one-millisecond USB timeout (libusb treats zero as an infinite timeout) and
# bound the loop so a stream of telemetry cannot starve a firmware command.
HID_INPUT_DRAIN_TIMEOUT_MS = 1
HID_INPUT_DRAIN_MAX_REPORTS = 64

DEFAULT_INSTALLER = (
    Path("/home/cisien/.local/share/linux-arctis-manager/research")
    / "steelseries-gg-114.0.0"
    / "SteelSeriesGG114.0.0Setup.exe"
)

# USB HID class request constants.
_SET_REPORT = 0x09
_REPORT_TYPE_OUTPUT = 0x02
_REPORT_TYPE_FEATURE = 0x03
_HID_CLASS = 0x03
_DESCRIPTOR_REPORT = 0x22


class UpdateError(RuntimeError):
    """A recoverable error that should be shown without a traceback."""


class HidDeviceDisconnected(UpdateError):
    """A HID transfer lost its USB device while it was re-enumerating."""


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    app_product_id: int
    logical_report_id: int
    application_boot_report_id: int
    version_labels: tuple[str, ...]
    interface_hint: int = CONTROL_INTERFACE_HINT


BASE = Target(
    key="base",
    label="Omni base station",
    app_product_id=BASE_APP_PRODUCT_ID,
    logical_report_id=0x01,
    # The GG SSFWFIZZFEATURE payload hard-codes report ID 1.  Its native
    # PrepareDeviceFizzFeature routine places this in byte zero of the
    # full-length output report used to enter each component bootloader.
    application_boot_report_id=0x01,
    version_labels=("TX MCU 5528", "TX MCU 5516", "TX DSP", "headset MCU", "headset Bluetooth"),
)
HEADSET = Target(
    key="headset",
    label="Omni headset receiver (direct USB)",
    app_product_id=HEADSET_APP_PRODUCT_ID,
    logical_report_id=0x00,
    application_boot_report_id=0x00,
    version_labels=("headset MCU", "headset Bluetooth"),
)
TARGETS = (BASE, HEADSET)


def to_wire_payload(target: Target, payload: bytes) -> bytes:
    """Translate GG's protocol struct to the HID payload used by this target.

    GG's receiver structs retain a leading report-ID field even though its
    value is zero.  The direct USB receiver has no physical HID report IDs,
    so that zero is not present on its wire payload or replies.  The base
    station's report-1 protocol prefix *is* present and must be retained.
    """

    if target.logical_report_id == 0:
        if not payload or payload[0] != 0:
            raise ValueError("Receiver protocol payload must begin with report ID 0")
        return payload[1:]
    return payload


def from_wire_payload(target: Target, payload: bytes) -> bytes:
    """Restore GG's report-ID field for uniform response parsing."""

    return b"\0" + payload if target.logical_report_id == 0 else payload


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    target: Target
    file_system_id: int
    file_id: int
    archive_path: str
    filename: str
    sha256: str
    expected_size: int
    target_version: str
    version_field: int
    bootloader_product_id: int
    # MCU-1's distinct boot PID exposes HID interface 0.  The other
    # components can retain both their PID and their HID interface, so their
    # only reliable transition signal is USB re-enumeration.
    boot_interface_hint: int | None = None


COMPONENTS = {
    "mcu1": Component(
        key="mcu1",
        label="base-station MCU 5528",
        target=BASE,
        file_system_id=0x01,
        file_id=0x01,
        archive_path="apps/engine/firmware/272114320/firmware_arctis_nova_pro_omni_tx_mcu_5528_v1.32.0.bin",
        filename="firmware_arctis_nova_pro_omni_tx_mcu_5528_v1.32.0.bin",
        sha256="75b41488edecbddfc672734f86f0b163b9486cd87de2aa28a656e61dcd80ca16",
        expected_size=285_612,
        target_version="1.32.0",
        version_field=0,
        bootloader_product_id=BASE_BOOTLOADER_PRODUCT_ID,
        boot_interface_hint=0,
    ),
    "mcu2": Component(
        key="mcu2",
        label="base-station MCU 5516",
        target=BASE,
        file_system_id=0x02,
        file_id=0x01,
        archive_path="apps/engine/firmware/272114320/firmware_arctis_nova_pro_omni_tx_mcu_5516_v1.32.0.bin",
        filename="firmware_arctis_nova_pro_omni_tx_mcu_5516_v1.32.0.bin",
        sha256="ae92cc3ca3cd59d3c4925fcb80c896f9369dbc3059dcbb34d78dae6f8392b368",
        expected_size=78_488,
        target_version="1.32.0",
        version_field=1,
        # GG resets this processor but it reuses 2290 and its normal HID
        # interface before exposing the Fizz feature transport.
        bootloader_product_id=BASE_APP_PRODUCT_ID,
    ),
    "dsp": Component(
        key="dsp",
        label="base-station DSP",
        target=BASE,
        file_system_id=0x03,
        file_id=0x01,
        archive_path="apps/engine/firmware/272114320/firmware_arctis_nova_pro_omni_tx_dsp_v0.36.0.bin",
        filename="firmware_arctis_nova_pro_omni_tx_dsp_v0.36.0.bin",
        sha256="320dace4ddd866e30787c2ac4220f0b5b4e6d51c48dc2568ce3f6566c5b2c5f2",
        expected_size=2_490_372,
        target_version="0.36.0",
        version_field=2,
        bootloader_product_id=BASE_APP_PRODUCT_ID,
    ),
    "rx-mcu": Component(
        key="rx-mcu",
        label="headset receiver MCU 1585",
        target=HEADSET,
        file_system_id=0x04,
        file_id=0x01,
        archive_path="apps/engine/firmware/272114326/firmware_arctis_nova_pro_omni_rx_mcu_1585_v0.36.0.bin",
        filename="firmware_arctis_nova_pro_omni_rx_mcu_1585_v0.36.0.bin",
        sha256="73f1c440558e2cb3c55a20b43217e506edac16373cdc6aeaf2697d5dc4a8bfd6",
        expected_size=3_244_036,
        target_version="0.36.0",
        version_field=0,
        bootloader_product_id=HEADSET_BOOTLOADER_PRODUCT_ID,
    ),
    "rx-bt": Component(
        key="rx-bt",
        label="headset receiver Bluetooth 1565",
        target=HEADSET,
        file_system_id=0x05,
        file_id=0x01,
        archive_path="apps/engine/firmware/272114326/firmware_arctis_nova_pro_omni_rx_bt_1565_v0.36.0.bin",
        filename="firmware_arctis_nova_pro_omni_rx_bt_1565_v0.36.0.bin",
        sha256="0ec7c7ec87650ed61d931c40cec944826e5049c8f09032c3077a3251b813ac7e",
        expected_size=2_322_436,
        target_version="0.36.0",
        version_field=1,
        # GG explicitly uses the normal PID for the Bluetooth boot mode.
        bootloader_product_id=HEADSET_APP_PRODUCT_ID,
    ),
}
DEFAULT_COMPONENT_KEYS = tuple(COMPONENTS)


@dataclass
class ReportLayout:
    input: dict[int, int]
    output: dict[int, int]
    feature: dict[int, int]

    def select(
        self, kind: str, minimum_size: int, preferred_report_id: int | None = None
    ) -> tuple[int, int]:
        reports_by_kind = {"input": self.input, "output": self.output, "feature": self.feature}
        try:
            reports = reports_by_kind[kind]
        except KeyError as exc:
            raise ValueError(f"Unknown HID report kind: {kind}") from exc
        candidates = [(report_id, size) for report_id, size in reports.items() if size >= minimum_size]
        if not candidates:
            found = ", ".join(f"id {report_id}: {size} bytes" for report_id, size in sorted(reports.items()))
            raise UpdateError(
                f"The device exposes no {kind} report large enough for {minimum_size} bytes"
                + (f" (found {found})." if found else ".")
            )

        # Omni's normal commands have a logical protocol prefix that is also
        # the physical HID report ID on the base (1) and direct headset (0).
        # Prefer that exact report whenever it exists.  This matters because
        # the base additionally exposes report 7, which accepts some regular
        # settings but does not answer the 0x10 firmware-version request.
        if preferred_report_id is not None:
            for report_id, size in candidates:
                if report_id == preferred_report_id:
                    return report_id, size

        # For bootloader feature reports, where no preferred physical report
        # is supplied, prefer the largest usable report.  This also covers a
        # descriptor that changes its report ID while its USB PID stays put.
        return max(
            candidates,
            key=lambda entry: (entry[1], entry[0] == GG_OUTPUT_REPORT_ID, entry[0]),
        )

    @staticmethod
    def wire_size(report_id: int, descriptor_size: int) -> int:
        """Return the HIDP/ReadFile size including a physical report ID.

        HID report descriptors count only report payload bytes.  GG obtains
        HIDP_CAPS' Input/Output/FeatureReportByteLength, which adds one byte
        when the descriptor has a physical non-zero report ID.  Its native
        Fizz buffers include that byte at offset zero, so control transfers
        must do the same.
        """

        return descriptor_size + int(report_id != 0)


def parse_report_descriptor(descriptor: bytes) -> ReportLayout:
    """Return HID input/output/feature payload lengths keyed by report ID."""

    report_size = 0
    report_count = 0
    report_id = 0
    input: dict[int, int] = {}
    output: dict[int, int] = {}
    feature: dict[int, int] = {}
    index = 0

    while index < len(descriptor):
        prefix = descriptor[index]
        index += 1
        if prefix == 0xFE:  # Long item: size, tag, data.
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

        # Global items: Report Size, Report ID, and Report Count.
        if item_type == 1 and tag == 7:
            report_size = value
        elif item_type == 1 and tag == 8:
            report_id = value
        elif item_type == 1 and tag == 9:
            report_count = value
        # Main items: Input, Output, and Feature.  The count is in bits and
        # excludes the physical report-ID byte.
        elif item_type == 0 and tag in (8, 9, 11):
            if report_size % 8:
                continue
            length = (report_size * report_count) // 8
            if tag == 8:
                input[report_id] = max(input.get(report_id, 0), length)
            elif tag == 9:
                output[report_id] = max(output.get(report_id, 0), length)
            else:
                feature[report_id] = max(feature.get(report_id, 0), length)

    return ReportLayout(input=input, output=output, feature=feature)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as firmware:
        for block in iter(lambda: firmware.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_firmware(installer: Path, destination: Path, components: Iterable[Component]) -> dict[str, Path]:
    """Extract exactly the requested GG files and verify their identity."""

    if not installer.is_file():
        raise UpdateError(f"GG installer was not found: {installer}")
    extractor = shutil.which("7z")
    if extractor is None:
        raise UpdateError("The '7z' executable is required to extract the GG firmware payloads.")

    requested = list(components)
    destination.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [extractor, "e", "-y", f"-o{destination}", str(installer), *(component.archive_path for component in requested)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise UpdateError(f"7z could not extract GG firmware:\n{detail}")

    firmware: dict[str, Path] = {}
    for component in requested:
        path = destination / component.filename
        actual_size = path.stat().st_size if path.is_file() else None
        actual_digest = sha256(path) if path.is_file() else "missing"
        if actual_size != component.expected_size or actual_digest != component.sha256:
            raise UpdateError(
                f"Refusing to flash {component.label}: GG artifact validation failed for {path.name}\n"
                f"expected size {component.expected_size}, SHA-256 {component.sha256}\n"
                f"actual   size {actual_size if actual_size is not None else 'missing'}, SHA-256 {actual_digest}"
            )
        firmware[component.key] = path
    return firmware


def find_device(product_id: int) -> usb.core.Device | None:
    devices = list(usb.core.find(find_all=True, idVendor=VENDOR_ID, idProduct=product_id))
    if len(devices) > 1:
        raise UpdateError(f"More than one 1038:{product_id:04x} device is connected; unplug duplicates and retry.")
    return devices[0] if devices else None


def wait_for_device(product_id: int, timeout: float, description: str) -> usb.core.Device:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        device = find_device(product_id)
        if device is not None:
            return device
        time.sleep(0.1)
    raise UpdateError(f"Timed out waiting {timeout:.0f}s for {description} (1038:{product_id:04x}).")


class UsbHidTransport:
    """A claimed vendor-HID transport matching GG's Fizz I/O paths.

    A target normally uses HID interface 3, but probing only HID interfaces is
    important for the direct headset USB function.  The report descriptor is
    validated before an interface is accepted, so no audio interface is ever
    claimed or used for firmware data.
    """

    def __init__(
        self,
        device: usb.core.Device,
        interface_hint: int,
        minimum_output: int | None = None,
        minimum_feature: int | None = None,
        minimum_input: int | None = None,
        required_interface: int | None = None,
    ):
        self.device = device
        self.interface_hint = interface_hint
        self.minimum_output = minimum_output
        self.minimum_feature = minimum_feature
        self.minimum_input = minimum_input
        self.required_interface = required_interface
        self.interface: int | None = None
        self.in_endpoint: int | None = None
        self.in_packet_size = 64
        self.layout: ReportLayout | None = None
        self._detached_kernel_driver = False

    def _hid_interfaces(self) -> list[tuple[int, int | None, int]]:
        try:
            configuration = self.device.get_active_configuration()
        except usb.core.USBError as exc:
            raise UpdateError(f"Could not read the active USB configuration: {exc}") from exc

        candidates: list[tuple[int, int | None, int]] = []
        for interface in configuration:
            if interface.bAlternateSetting != 0 or interface.bInterfaceClass != _HID_CLASS:
                continue
            if self.required_interface is not None and interface.bInterfaceNumber != self.required_interface:
                continue
            endpoint_in: int | None = None
            packet_size = 64
            for endpoint in interface:
                address = int(endpoint.bEndpointAddress)
                if usb.util.endpoint_direction(address) == usb.util.ENDPOINT_IN:
                    endpoint_in = address
                    packet_size = max(packet_size, int(endpoint.wMaxPacketSize))
                    break
            candidates.append((int(interface.bInterfaceNumber), endpoint_in, packet_size))

        return sorted(candidates, key=lambda item: (item[0] != self.interface_hint, item[0]))

    def __enter__(self) -> UsbHidTransport:
        candidates = self._hid_interfaces()
        if not candidates:
            raise UpdateError("The selected device has no HID control interface.")

        failures: list[str] = []
        for number, endpoint_in, packet_size in candidates:
            self.interface = number
            self.in_endpoint = endpoint_in
            self.in_packet_size = packet_size
            try:
                try:
                    if self.device.is_kernel_driver_active(number):
                        self.device.detach_kernel_driver(number)
                        self._detached_kernel_driver = True
                except (NotImplementedError, usb.core.USBError):
                    # Claiming below is the authoritative access check.
                    pass

                usb.util.claim_interface(self.device, number)
                self.layout = parse_report_descriptor(self._get_report_descriptor())
                if self.minimum_output is not None:
                    self.layout.select("output", self.minimum_output)
                if self.minimum_feature is not None:
                    self.layout.select("feature", self.minimum_feature)
                if self.minimum_input is not None:
                    self.layout.select("input", self.minimum_input)
                return self
            except (UpdateError, usb.core.USBError) as exc:
                failures.append(f"interface {number}: {exc}")
                self._close_current_interface()

        detail = "; ".join(failures)
        raise UpdateError(
            "Could not open a suitable HID control interface. Stop Linux Arctis Manager, SteelSeries GG, "
            f"and other headset-control programs, then retry. ({detail})"
        )

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._close_current_interface()

    def _close_current_interface(self) -> None:
        number = self.interface
        if number is None:
            return
        try:
            usb.util.release_interface(self.device, number)
        except usb.core.USBError:
            pass
        if self._detached_kernel_driver:
            try:
                self.device.attach_kernel_driver(number)
            except usb.core.USBError:
                pass
        self._detached_kernel_driver = False
        self.interface = None
        self.in_endpoint = None
        self.layout = None

    def _interface(self) -> int:
        if self.interface is None:
            raise AssertionError("USB transport has not been opened")
        return self.interface

    def _get_report_descriptor(self) -> bytes:
        try:
            # HID report descriptors are returned shorter than wLength.
            return bytes(
                self.device.ctrl_transfer(0x81, 0x06, _DESCRIPTOR_REPORT << 8, self._interface(), 4096, timeout=2_000)
            )
        except usb.core.USBError as exc:
            raise UpdateError(f"Could not read the HID report descriptor: {exc}") from exc

    def _layout(self) -> ReportLayout:
        if self.layout is None:
            raise AssertionError("USB transport has not been opened")
        return self.layout

    def _set_report(self, report_type: int, report_id: int, data: bytes, size: int, timeout: int) -> None:
        wire_size = self._layout().wire_size(report_id, size)
        if len(data) > wire_size:
            raise UpdateError(
                f"Refusing to send {len(data)} bytes in report {report_id}; descriptor allows only {wire_size}."
            )
        if report_id and (not data or data[0] != report_id):
            raise UpdateError(
                f"Refusing to send physical report {report_id} with leading byte "
                f"{data[0] if data else 'missing'} instead."
            )
        try:
            self.device.ctrl_transfer(
                0x21,
                _SET_REPORT,
                (report_type << 8) | report_id,
                self._interface(),
                data.ljust(wire_size, b"\0"),
                timeout=timeout,
            )
        except usb.core.USBError as exc:
            if getattr(exc, "errno", None) == 19:
                raise HidDeviceDisconnected(
                    f"HID SET_REPORT lost its device while sending type {report_type}, id {report_id}: {exc}"
                ) from exc
            raise UpdateError(f"HID SET_REPORT failed (type {report_type}, id {report_id}): {exc}") from exc

    def send_output(
        self, data: bytes, timeout: int = 2_000, preferred_report_id: int | None = None
    ) -> None:
        # The first byte is GG's logical protocol report ID.  On these two
        # Omni targets it is also the correct physical output report ID.  A
        # direct receiver payload omits its logical zero, so its caller passes
        # the physical report ID explicitly.
        report_id, size = self._layout().select(
            "output",
            len(data),
            preferred_report_id=preferred_report_id if preferred_report_id is not None else (data[0] if data else None),
        )
        self._set_report(_REPORT_TYPE_OUTPUT, report_id, data, size, timeout)

    def set_feature(
        self, data: bytes, timeout: int = 10_000, preferred_report_id: int | None = None
    ) -> tuple[int, int]:
        report_id, size = self._layout().select("feature", len(data), preferred_report_id)
        self._set_report(_REPORT_TYPE_FEATURE, report_id, data, size, timeout)
        return report_id, size

    def _input_read_size(self, report_id: int, descriptor_size: int) -> int:
        if self.in_endpoint is None:
            raise UpdateError("The HID control interface has no interrupt-IN endpoint for a Fizz response.")
        # HIDP's input report length includes the physical report ID.  The
        # endpoint is 64 bytes on the Omni targets; retaining its maximum
        # packet size also avoids libusb's EOVERFLOW behaviour for full
        # reports on descriptors with a shorter logical payload.
        return max(self.in_packet_size, self._layout().wire_size(report_id, descriptor_size))

    @staticmethod
    def _is_timeout(exc: usb.core.USBError) -> bool:
        # PyUSB/libusb represents a timed-out interrupt read as errno 110;
        # some backends leave errno unset.
        return getattr(exc, "errno", None) in (None, 110)

    def drain_pending_input(self) -> int:
        """Discard interrupt-IN reports already queued before a Fizz command.

        This mirrors ``HIDLib::fcn18004ff00`` in GG, which starts an
        overlapped ReadFile with a zero wait and repeats while it completes
        immediately.  Omni report 7 carries asynchronous telemetry (such as
        headset battery state), which must not be mistaken for the response
        to the next feature report.
        """

        report_id, descriptor_size = self._layout().select("input", 1)
        read_size = self._input_read_size(report_id, descriptor_size)
        drained = 0
        for _ in range(HID_INPUT_DRAIN_MAX_REPORTS):
            try:
                self.device.read(self.in_endpoint, read_size, timeout=HID_INPUT_DRAIN_TIMEOUT_MS)
            except usb.core.USBError as exc:
                if self._is_timeout(exc):
                    return drained
                raise UpdateError(f"Could not drain pending HID interrupt-IN reports: {exc}") from exc
            drained += 1
        return drained

    def read_input(self, report_id: int, descriptor_size: int, timeout: int) -> bytes:
        """Read GG's ReadFile acknowledgement for a Fizz command.

        `FlashFirmwareFizzFeature` uses the native ``HIDFEATURE_OUT_INPUT_IN``
        path: DeviceIoControl/HID_SET_FEATURE followed by ReadFile on the HID
        interrupt-IN endpoint.  It does *not* call HID_GET_FEATURE.

        GG drains already-queued input before setting the feature report.  A
        new asynchronous report can still arrive immediately afterwards, so
        only accept the physical report ID selected for this transaction.
        """

        read_size = self._input_read_size(report_id, descriptor_size)
        deadline = time.monotonic() + timeout / 1_000
        ignored_report_ids: list[int] = []
        while True:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1_000))
            try:
                packet = bytes(self.device.read(self.in_endpoint, read_size, timeout=remaining_ms))
            except usb.core.USBError as exc:
                if self._is_timeout(exc):
                    ignored = ""
                    if ignored_report_ids:
                        rendered = ", ".join(f"{value}" for value in ignored_report_ids[:8])
                        suffix = "..." if len(ignored_report_ids) > 8 else ""
                        ignored = f"; ignored asynchronous report ID(s) {rendered}{suffix}"
                    raise UpdateError(
                        f"Timed out waiting for Fizz acknowledgement on HID input report {report_id}{ignored}."
                    ) from exc
                raise UpdateError(
                    f"HID interrupt-IN read failed after Fizz feature report {report_id}: {exc}"
                ) from exc

            # A report-ID-zero device has no physical report prefix.  For the
            # base station, a raw USB interrupt packet begins with the
            # physical report ID, so ignore unrelated telemetry reports.
            if report_id == 0 or (packet and packet[0] == report_id):
                return packet
            ignored_report_ids.append(packet[0] if packet else -1)

    def fizz_feature_transaction(
        self, data: bytes, timeout: int, preferred_report_id: int | None = None
    ) -> bytes:
        self.drain_pending_input()
        report_id, _feature_size = self.set_feature(data, timeout, preferred_report_id)
        input_report_id, input_size = self._layout().select("input", 3, preferred_report_id=report_id)
        return self.read_input(input_report_id, input_size, timeout)

    def describe_reports(self) -> str:
        layout = self._layout()

        def fmt(reports: dict[int, int]) -> str:
            return ", ".join(f"id {report_id}: {size} B" for report_id, size in sorted(reports.items())) or "none"

        return (
            f"interface {self._interface()}, input [{fmt(layout.input)}], "
            f"output [{fmt(layout.output)}], feature [{fmt(layout.feature)}]"
        )

    def read_firmware_versions(self, target: Target) -> tuple[str, ...]:
        """Use GG's normal HIDIO firmware-version request on the HID IN endpoint."""

        if self.in_endpoint is None:
            raise UpdateError("The HID control interface has no interrupt-IN endpoint for firmware versions.")
        request = bytes((target.logical_report_id, 0x10))
        expected_length = 2 + 12 * len(target.version_labels)
        last_error: usb.core.USBError | None = None

        # A normal control report can be preceded by an asynchronous headset
        # notification.  Also, detaching the kernel HID driver briefly while
        # claiming USBfs can make the first request disappear.  Reissue this
        # harmless read-only command a few times before declaring the device
        # unresponsive; this is still entirely before any boot/erase command.
        for attempt in range(3):
            self.send_output(
                to_wire_payload(target, request), preferred_report_id=target.logical_report_id
            )
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                try:
                    # Both live Omni firmware-version endpoints are 64-byte
                    # interrupt endpoints. Requesting 65 (the engine's logical
                    # HIDIO chunk size) makes libusb report EOVERFLOW whenever
                    # a full 64-byte response arrives.
                    packet = bytes(
                        self.device.read(self.in_endpoint, max(self.in_packet_size, expected_length), timeout=250)
                    )
                except usb.core.USBError as exc:
                    if getattr(exc, "errno", None) in (None, 110):
                        continue
                    last_error = exc
                    break
                packet = from_wire_payload(target, packet)
                if len(packet) >= expected_length and packet[:2] == request:
                    values = []
                    for offset in range(2, expected_length, 12):
                        raw = packet[offset : offset + 12].split(b"\0", 1)[0]
                        values.append(raw.decode("ascii", "replace"))
                    return tuple(values)
            if attempt < 2:
                time.sleep(0.15)

        if last_error is not None:
            raise UpdateError(f"Could not read firmware-version response: {last_error}") from last_error
        raise UpdateError("Timed out waiting for the firmware-version response after three requests.")


def enter_boot_packet(component: Component) -> bytes:
    # GG's PrepareDeviceFizzFeature constructs a five-byte (zero-padded)
    # boot-entry buffer whose meaningful prefix is
    # ``[report-id, 0x01, 0x01, fs-id]``.  The middle 0x01 was previously
    # omitted here, so the normal base station accepted the HID write but did
    # not transition to its MCU bootloader.
    return bytes((component.target.logical_report_id, 0x01, 0x01, component.file_system_id))


def reset_packet(component: Component) -> bytes:
    """GG's FizzFeature post-flash reset packet (mode byte zero)."""

    return bytes((component.target.logical_report_id, 0x01, 0x00, component.file_system_id))


def erase_packet(component: Component) -> bytes:
    return bytes((component.target.logical_report_id, 0x02, component.file_system_id, component.file_id))


def crc_packet(component: Component) -> bytes:
    return bytes((component.target.logical_report_id, 0x84, component.file_system_id, component.file_id))


def write_packet(component: Component, offset: int, data: bytes) -> bytes:
    if not 0 < len(data) <= BLOCK_SIZE:
        raise ValueError(f"Firmware block must contain 1..{BLOCK_SIZE} bytes, got {len(data)}")
    header = bytes((component.target.logical_report_id, 0x03, component.file_system_id, component.file_id))
    packet = header + len(data).to_bytes(2, "little") + offset.to_bytes(4, "little") + data
    return packet.ljust(WRITE_PACKET_SIZE, b"\0")


def expect_success(response: bytes, operation: str) -> None:
    if len(response) < 3:
        raise UpdateError(f"{operation} returned only {len(response)} bytes; expected a Fizz status response.")
    if response[2] != 0:
        raise UpdateError(f"{operation} failed with bootloader status 0x{response[2]:02x}.")


def has_fizz_transport(device: usb.core.Device, target: Target) -> bool:
    """Check whether a device exposes GG's complete Fizz transport."""

    try:
        with UsbHidTransport(
            device,
            target.interface_hint,
            minimum_feature=WRITE_PACKET_SIZE,
            minimum_input=3,
        ):
            return True
    except UpdateError:
        return False


def existing_boot_device(component: Component) -> usb.core.Device | None:
    device = find_device(component.bootloader_product_id)
    if device is None:
        return None
    # A distinct boot PID is enough to identify a recoverable MCU bootloader.
    # The base application's normal descriptor already contains a large Fizz
    # feature report, so that report is *not* evidence of boot mode when the
    # bootloader reuses the application PID (MCU-2, DSP, and receiver BT).
    if component.bootloader_product_id != component.target.app_product_id:
        return device
    return None


def open_boot_transport(component: Component, device: usb.core.Device) -> UsbHidTransport:
    """Create a Fizz transport only for the component's known boot layout."""

    return UsbHidTransport(
        device,
        component.target.interface_hint,
        minimum_feature=WRITE_PACKET_SIZE,
        minimum_input=3,
        required_interface=component.boot_interface_hint,
    )


def wait_for_boot_transport(component: Component, timeout: float = 60.0) -> UsbHidTransport:
    """Claim GG's Fizz transport after the component-specific boot entry.

    MCU-1 changes PID and must therefore disappear before its boot transport
    is available.  GG's `PrepareDeviceFizzFeature` takes a different path
    when a component's application and bootloader PIDs are equal: it sends
    the prepare packet, then reopens a suitable Fizz feature report without
    demanding a new USB address.  MCU-2 and DSP can either re-enumerate or
    simply reset their existing 2290 function, so treating an address change
    as mandatory incorrectly leaves them in the update-ready screen. The
    returned transport remains claimed through the five-second settle, as
    GG keeps the handle it found in PrepareDeviceFizzFeature.
    """

    deadline = time.monotonic() + timeout
    last_reason = "device has not appeared"
    while time.monotonic() < deadline:
        device = find_device(component.bootloader_product_id)
        if device is None:
            time.sleep(0.1)
            continue
        transport = open_boot_transport(component, device)
        try:
            transport.__enter__()
            return transport
        except UpdateError as exc:
            last_reason = str(exc)
            transport.__exit__(None, None, None)
            time.sleep(0.2)
    raise UpdateError(f"Timed out waiting for the {component.label} bootloader transport: {last_reason}")


def enter_component_bootloader(component: Component) -> UsbHidTransport:
    existing = existing_boot_device(component)
    if existing is not None:
        print("  existing bootloader detected; resuming without another reset")
        return wait_for_boot_transport(component)

    app_device = wait_for_device(component.target.app_product_id, 15, component.target.label)
    boot_entry_reenumerated = False
    with UsbHidTransport(app_device, component.target.interface_hint, minimum_output=3) as transport:
        print(f"  application reports: {transport.describe_reports()}")
        try:
            transport.send_output(
                to_wire_payload(component.target, enter_boot_packet(component)),
                preferred_report_id=component.target.application_boot_report_id,
            )
        except HidDeviceDisconnected:
            # The direct headset receiver processes the output report and can
            # reset to PID 2297 before libusb observes the control-transfer
            # completion.  Kernel evidence shows this as ENODEV even though
            # the prepare command succeeded.  Continue only by finding the
            # component's known bootloader transport below.
            boot_entry_reenumerated = True

    if boot_entry_reenumerated:
        print("  boot-entry command triggered USB re-enumeration; locating the bootloader")

    # PrepareDeviceFizzFeature first waits until a matching HID handle with
    # non-zero input and feature reports is available.  Only then does
    # FlashFirmwareFizzFeature sleep 0x1388 ms before erase/write.  For a
    # same-PID component the handle can already be present at this point;
    # that is GG's intended readiness criterion, not a version reply.
    transport = wait_for_boot_transport(component)
    print(f"  waiting {BOOT_PREPARE_SETTLE_SECONDS:g} seconds for bootloader readiness")
    time.sleep(BOOT_PREPARE_SETTLE_SECONDS)
    return transport


def query_target_versions(target: Target, timeout: float = 10.0) -> tuple[str, ...]:
    app_device = wait_for_device(target.app_product_id, timeout, target.label)
    with UsbHidTransport(app_device, target.interface_hint, minimum_output=2) as transport:
        return transport.read_firmware_versions(target)


def wait_for_application(target: Target, timeout: float = 60.0) -> tuple[str, ...]:
    """Wait for a post-reset application that answers its version command."""

    deadline = time.monotonic() + timeout
    last_reason = "device has not appeared"
    while time.monotonic() < deadline:
        device = find_device(target.app_product_id)
        if device is None:
            time.sleep(0.2)
            continue
        try:
            with UsbHidTransport(device, target.interface_hint, minimum_output=2) as transport:
                return transport.read_firmware_versions(target)
        except UpdateError as exc:
            last_reason = str(exc)
            time.sleep(0.3)
    raise UpdateError(f"Timed out waiting for {target.label} to resume its application firmware: {last_reason}")


def wait_for_component_version(component: Component, timeout: float = 60.0) -> tuple[str, ...]:
    """Wait past transient boot replies until the flashed component is live."""

    deadline = time.monotonic() + timeout
    last_version = "no version reply"
    last_reason = "device has not appeared"
    while time.monotonic() < deadline:
        try:
            versions = query_target_versions(component.target, timeout=1)
            value = versions[component.version_field]
            last_version = value or "(empty)"
            if value == component.target_version:
                return versions
            last_reason = f"reports transient/current version {last_version}"
        except UpdateError as exc:
            last_reason = str(exc)
        time.sleep(0.4)
    raise UpdateError(
        f"{component.label} did not report the expected {component.target_version} after reset "
        f"(last observed {last_version}; {last_reason})."
    )


def flash_component(component: Component, firmware_path: Path) -> tuple[str, ...]:
    print(f"Updating {component.label} from {firmware_path.name} ({firmware_path.stat().st_size:,} bytes)")
    transport = enter_component_bootloader(component)

    erase_completed = False
    try:
        report_id, report_size = transport._layout().select("feature", WRITE_PACKET_SIZE)
        print(f"  bootloader reports: {transport.describe_reports()} (using feature id {report_id}, {report_size} B)")

        # The SSFWFIZZFEATURE payload's extended-timeout bit makes GG
        # configure all Fizz feature/input transactions for 60 seconds,
        # not just the erase.
        erase_response = from_wire_payload(
            component.target,
            transport.fizz_feature_transaction(
                to_wire_payload(component.target, erase_packet(component)),
                timeout=GG_FIZZ_TRANSACTION_TIMEOUT_MS,
                preferred_report_id=component.target.logical_report_id,
            ),
        )
        expect_success(erase_response, "erase")
        erase_completed = True

        total_size = firmware_path.stat().st_size
        block_count = (total_size + BLOCK_SIZE - 1) // BLOCK_SIZE
        with firmware_path.open("rb") as firmware:
            for block_number in range(block_count):
                data = firmware.read(BLOCK_SIZE)
                response = from_wire_payload(
                    component.target,
                    transport.fizz_feature_transaction(
                        to_wire_payload(
                            component.target, write_packet(component, block_number * BLOCK_SIZE, data)
                        ),
                        timeout=GG_FIZZ_TRANSACTION_TIMEOUT_MS,
                        preferred_report_id=component.target.logical_report_id,
                    ),
                )
                expect_success(response, f"write block {block_number + 1}/{block_count}")
                if block_number == 0 or block_number + 1 == block_count or (block_number + 1) % 100 == 0:
                    print(f"  wrote block {block_number + 1}/{block_count}")

        crc_response = from_wire_payload(
            component.target,
            transport.fizz_feature_transaction(
                to_wire_payload(component.target, crc_packet(component)),
                timeout=GG_FIZZ_TRANSACTION_TIMEOUT_MS,
                preferred_report_id=component.target.logical_report_id,
            ),
        )
        expect_success(crc_response, "CRC check")
        if len(crc_response) < 11:
            raise UpdateError("CRC response was too short to contain both stored and calculated CRC values.")
        stored_crc = crc_response[3:7]
        calculated_crc = crc_response[7:11]
        if stored_crc != calculated_crc:
            raise UpdateError(
                "Bootloader CRC mismatch after flashing "
                f"(stored {stored_crc.hex()}, calculated {calculated_crc.hex()})."
            )
        print(f"  CRC verified: {stored_crc.hex()}")

        # GG's final reset is an ordinary HID output report, not a Fizz
        # feature transaction.
        try:
            transport.send_output(
                to_wire_payload(component.target, reset_packet(component)),
                timeout=10_000,
                preferred_report_id=component.target.logical_report_id,
            )
        except HidDeviceDisconnected:
            # As with boot entry, the direct receiver can process reset and
            # begin its 2297 -> 2296 re-enumeration before libusb completes
            # the output control transfer.  The version poll below is the
            # authoritative confirmation that the new application booted.
            print("  reset command triggered USB re-enumeration; waiting for application firmware")
    except UpdateError as exc:
        if erase_completed:
            raise UpdateError(
                f"{component.label} failed after its flash area was erased. The device was deliberately "
                "left in bootloader mode; do not unplug it. Re-run update for this same component after "
                f"resolving the error, or recover it with GG. Original error: {exc}"
            ) from exc
        raise
    finally:
        transport.__exit__(None, None, None)

    versions = wait_for_component_version(component)
    value = versions[component.version_field]
    print(f"  {component.label} reset complete and reports {value}")
    return versions


def print_versions(target: Target, versions: tuple[str, ...]) -> None:
    print(f"{target.label} (1038:{target.app_product_id:04x}):")
    for label, version in zip(target.version_labels, versions, strict=True):
        print(f"  {label:20} {version or '(not paired / unavailable)'}")


def status() -> None:
    found = False
    for target in TARGETS:
        if find_device(target.app_product_id) is not None:
            print_versions(target, query_target_versions(target, timeout=1))
            found = True
            continue

        boot_components = [
            component for component in COMPONENTS.values()
            if component.target is target and component.bootloader_product_id != target.app_product_id
            and find_device(component.bootloader_product_id) is not None
        ]
        if boot_components:
            names = ", ".join(component.key for component in boot_components)
            print(
                f"{target.label}: bootloader detected; resume with "
                f"update --component {names} --yes-flash",
                file=sys.stderr,
            )
            found = True
        else:
            print(f"{target.label}: not connected")
    if not found:
        raise UpdateError("No supported Omni base-station or direct-headset USB device is connected.")


def parse_components(values: list[str] | None) -> list[Component]:
    if not values:
        return [COMPONENTS[key] for key in DEFAULT_COMPONENT_KEYS]

    selected: list[Component] = []
    for value in values:
        for key in value.split(","):
            key = key.strip()
            if key == "all":
                candidates = [COMPONENTS[name] for name in DEFAULT_COMPONENT_KEYS]
            else:
                try:
                    candidates = [COMPONENTS[key]]
                except KeyError as exc:
                    choices = ", ".join((*COMPONENTS, "all"))
                    raise UpdateError(f"Unknown component '{key}'. Choose one of: {choices}.") from exc
            for component in candidates:
                if component not in selected:
                    selected.append(component)
    return selected


def component_can_resume(component: Component) -> bool:
    return (device := existing_boot_device(component)) is not None and has_fizz_transport(device, component.target)


def preflight_update(components: list[Component], force: bool) -> list[Component]:
    """Validate every USB target and return only components that need flashing.

    This runs before the first erase.  Consequently, ``--component all`` will
    fail cleanly when the direct headset USB device is missing rather than
    updating the base station and failing part-way through the requested set.
    """

    planned: list[Component] = []
    by_target: dict[Target, list[Component]] = {}
    for component in components:
        by_target.setdefault(component.target, []).append(component)

    for target, group in by_target.items():
        app_device = find_device(target.app_product_id)
        versions: tuple[str, ...] | None = None
        query_error: UpdateError | None = None
        if app_device is not None:
            try:
                versions = query_target_versions(target, timeout=1)
            except UpdateError as exc:
                query_error = exc

        for component in group:
            if versions is not None:
                current = versions[component.version_field]
                if current == component.target_version and not force:
                    print(f"Skipping {component.label}: already at {current} (use --force to reflash).")
                else:
                    if current:
                        print(f"Planning {component.label}: {current} -> {component.target_version}")
                    else:
                        print(f"Planning {component.label}: version unavailable -> {component.target_version}")
                    planned.append(component)
                continue

            # A previously erased component may have only its bootloader PID,
            # or a shared app/boot PID with a Fizz-sized feature report.
            if component_can_resume(component):
                print(f"Planning recovery of {component.label} from its existing bootloader.")
                planned.append(component)
                continue

            if app_device is None:
                raise UpdateError(
                    f"{component.label} requires {target.label} at 1038:{target.app_product_id:04x}. "
                    "Connect that exact USB device before updating; a wireless/base-station link is not enough."
                )
            raise UpdateError(
                f"Could not verify {target.label} before flashing: {query_error}. "
                "Do not flash while its control interface is busy or unresponsive."
            )
    return planned


def self_test() -> None:
    base_descriptor = bytes.fromhex(
        "06c0ff0a0100a1018501953f7508150026ff0009f0810209f191028501960b047508150026ff0009f2b102c0"
        "0600ff0a0100a1018507953f7508150026ff0009f0810209f191028507963f007508150026ff0009f2b102c0"
    )
    layout = parse_report_descriptor(base_descriptor)
    assert layout.input == {1: 63, 7: 63}
    assert layout.output == {1: 63, 7: 63}
    assert layout.feature == {1: 1035, 7: 63}
    assert layout.select("output", 2, preferred_report_id=1) == (1, 63)
    assert layout.select("input", 3, preferred_report_id=1) == (1, 63)
    assert layout.wire_size(1, 63) == 64
    assert layout.wire_size(1, 1035) == 1036

    class FakeInputDevice:
        def __init__(self, packets: list[bytes]):
            self.packets = packets

        def read(self, endpoint: int, size: int, timeout: int) -> bytes:
            assert endpoint == 0x83
            assert size == 64
            if self.packets:
                return self.packets.pop(0)
            raise usb.core.USBError("timed out", errno=110)

    # GG drains queued notifications before each Fizz transaction.  Test both
    # that behaviour and the defensive report-ID filter for a notification
    # arriving immediately after the feature report is sent.
    fake_transport = object.__new__(UsbHidTransport)
    fake_transport.in_endpoint = 0x83
    fake_transport.in_packet_size = 64
    fake_transport.layout = layout
    fake_transport.device = FakeInputDevice([bytes((7, 0xB7, 0x64, 0x64))])
    assert fake_transport.drain_pending_input() == 1
    fake_transport.device = FakeInputDevice([bytes((7, 0xB7, 0x64, 0x64)), bytes((1, 0, 0, 0))])
    assert fake_transport.read_input(1, 63, 100) == bytes((1, 0, 0, 0))

    no_physical_id_descriptor = bytes.fromhex(
        "06c0ff0a0100a101953f7508150026ff0009f0810209f19102963f007508150026ff0009f2b102c0"
    )
    layout = parse_report_descriptor(no_physical_id_descriptor)
    assert layout.input == {0: 63}
    assert layout.output == {0: 63}
    assert layout.feature == {0: 63}
    assert layout.wire_size(0, 63) == 63

    base_packet = write_packet(COMPONENTS["mcu1"], 0x12345678, b"abc")
    assert len(base_packet) == WRITE_PACKET_SIZE
    assert base_packet[:10] == bytes((1, 3, 1, 1, 3, 0, 0x78, 0x56, 0x34, 0x12))
    assert enter_boot_packet(COMPONENTS["mcu1"]) == bytes((1, 1, 1, 1))
    assert reset_packet(COMPONENTS["mcu1"]) == bytes((1, 1, 0, 1))
    assert BASE.application_boot_report_id == 1
    headset_packet = write_packet(COMPONENTS["rx-mcu"], 0, b"x")
    assert headset_packet[:11] == bytes((0, 3, 4, 1, 1, 0, 0, 0, 0, 0, ord("x")))
    assert erase_packet(COMPONENTS["rx-bt"]) == bytes((0, 2, 5, 1))
    assert to_wire_payload(HEADSET, bytes((0, 0x10))) == bytes((0x10,))
    assert from_wire_payload(HEADSET, bytes((0x10,))) == bytes((0, 0x10))
    assert to_wire_payload(BASE, bytes((1, 0x10))) == bytes((1, 0x10))
    assert parse_components(["all"]) == [COMPONENTS[key] for key in DEFAULT_COMPONENT_KEYS]
    print("Self-test passed.")


def add_component_option(parser: argparse.ArgumentParser, help_text: str) -> None:
    parser.add_argument(
        "--component",
        action="append",
        metavar="{mcu1,mcu2,dsp,rx-mcu,rx-bt,all}",
        help=help_text,
    )


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone GG 114.0.0 firmware updater for the Arctis Nova Pro Omni base station and direct USB headset."
    )
    parser.add_argument(
        "--installer",
        type=Path,
        default=DEFAULT_INSTALLER,
        help=f"GG 114.0.0 installer (default: {DEFAULT_INSTALLER})",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Show firmware fields from each connected supported USB target (default).")
    verify = subparsers.add_parser("verify", help="Extract and validate GG firmware without touching USB.")
    add_component_option(verify, "Components to validate; repeat or use comma-separated names. Default: all five.")
    update = subparsers.add_parser("update", help="Flash validated GG firmware to every selected direct USB target.")
    add_component_option(update, "Components to flash; repeat or use comma-separated names. Default: all five.")
    update.add_argument("--force", action="store_true", help="Reflash components already reporting the GG 114.0.0 target version.")
    update.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate firmware and the live USB update plan without entering a bootloader or flashing.",
    )
    update.add_argument(
        "--yes-flash",
        action="store_true",
        help="Required acknowledgement; firmware updates must not be interrupted or unplugged.",
    )
    subparsers.add_parser("self-test", help="Run packet and HID descriptor parser checks without USB or extraction.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    command = args.command or "status"

    try:
        if command == "self-test":
            self_test()
            return 0
        if command == "status":
            status()
            return 0

        components = parse_components(getattr(args, "component", None))
        if command == "verify":
            with tempfile.TemporaryDirectory(prefix="arctis-omni-fw-") as directory:
                images = require_firmware(args.installer, Path(directory), components)
                for component in components:
                    image = images[component.key]
                    print(f"verified {component.label}: {image.name} ({image.stat().st_size:,} bytes)")
            return 0

        if command != "update":
            raise AssertionError(f"Unhandled command: {command}")
        if not args.yes_flash and not args.dry_run:
            raise UpdateError("Refusing to flash without --yes-flash.")
        if not args.dry_run and os.geteuid() != 0:
            raise UpdateError(
                "Run the update as root (for example: sudo -E python3 arctis-omni-firmware-update.py update --yes-flash)."
            )

        if args.dry_run:
            print("Dry run: validating firmware and USB preflight only; no bootloader command will be sent.")
        else:
            print("Do not unplug any selected USB device or interrupt this process while flashing.")
            print("Stop Linux Arctis Manager, SteelSeries GG, and other headset-control programs first.")
        with tempfile.TemporaryDirectory(prefix="arctis-omni-fw-") as directory:
            images = require_firmware(args.installer, Path(directory), components)
            planned = preflight_update(components, force=args.force)
            if args.dry_run:
                if planned:
                    print("Dry-run preflight succeeded. These components would be flashed:")
                    for component in planned:
                        print(f"  {component.key}: {component.label}")
                else:
                    print("Dry-run preflight succeeded; all selected components are already current.")
                return 0
            if not planned:
                print("All selected components already report the GG 114.0.0 target versions.")
                return 0
            for component in planned:
                flash_component(component, images[component.key])

        print("Firmware update protocol completed successfully.")
        print("Final device status:")
        try:
            status()
        except UpdateError as exc:
            print(f"Warning: unable to query final firmware status: {exc}", file=sys.stderr)
        return 0
    except UpdateError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    # Ctrl-C during an erase/write could leave the target in bootloader mode.
    # Ignore the first signal so an accidental terminal focus does not abort a
    # live flash; a second Ctrl-C retains normal Python behaviour.
    def _handle_sigint(signum: int, frame: object) -> None:
        if getattr(_handle_sigint, "interrupted", False):
            signal.default_int_handler(signum, frame)  # type: ignore[arg-type]
        _handle_sigint.interrupted = True  # type: ignore[attr-defined]
        print("\nIgnoring first Ctrl-C during a potential firmware update; press Ctrl-C again to abort.", file=sys.stderr)

    signal.signal(signal.SIGINT, _handle_sigint)
    raise SystemExit(main())
