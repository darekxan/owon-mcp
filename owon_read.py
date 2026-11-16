#!/usr/bin/env python3
"""Read OWON identity and SCREEN data via installed libusb on macOS.

Only documented queries are sent. No USB reset, configuration change,
kernel-driver detach, halt clearing, or acquisition controls are used.
SCREEN bytes are preserved without assigning voltage or time units.
"""

import argparse
import base64
import ctypes as C
import ctypes.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path

U8 = C.c_uint8
U16 = C.c_uint16
PTR = C.c_void_p


class DeviceDescriptor(C.Structure):
    _fields_ = [
        ("bLength", U8),
        ("bDescriptorType", U8),
        ("bcdUSB", U16),
        ("bDeviceClass", U8),
        ("bDeviceSubClass", U8),
        ("bDeviceProtocol", U8),
        ("bMaxPacketSize0", U8),
        ("idVendor", U16),
        ("idProduct", U16),
        ("bcdDevice", U16),
        ("iManufacturer", U8),
        ("iProduct", U8),
        ("iSerialNumber", U8),
        ("bNumConfigurations", U8),
    ]


class Endpoint(C.Structure):
    _fields_ = [
        ("bLength", U8),
        ("bDescriptorType", U8),
        ("bEndpointAddress", U8),
        ("bmAttributes", U8),
        ("wMaxPacketSize", U16),
        ("bInterval", U8),
        ("bRefresh", U8),
        ("bSynchAddress", U8),
        ("extra", C.POINTER(U8)),
        ("extra_length", C.c_int),
    ]


class Alternate(C.Structure):
    _fields_ = [
        ("bLength", U8),
        ("bDescriptorType", U8),
        ("bInterfaceNumber", U8),
        ("bAlternateSetting", U8),
        ("bNumEndpoints", U8),
        ("bInterfaceClass", U8),
        ("bInterfaceSubClass", U8),
        ("bInterfaceProtocol", U8),
        ("iInterface", U8),
        ("endpoint", C.POINTER(Endpoint)),
        ("extra", C.POINTER(U8)),
        ("extra_length", C.c_int),
    ]


class Interface(C.Structure):
    _fields_ = [("altsetting", C.POINTER(Alternate)), ("num_altsetting", C.c_int)]


class Configuration(C.Structure):
    _fields_ = [
        ("bLength", U8),
        ("bDescriptorType", U8),
        ("wTotalLength", U16),
        ("bNumInterfaces", U8),
        ("bConfigurationValue", U8),
        ("iConfiguration", U8),
        ("bmAttributes", U8),
        ("MaxPower", U8),
        ("interface", C.POINTER(Interface)),
        ("extra", C.POINTER(U8)),
        ("extra_length", C.c_int),
    ]


class Scope:
    def __init__(self):
        library = ctypes.util.find_library("usb-1.0")
        if not library:
            library = next(
                (
                    p
                    for p in (
                        "/opt/homebrew/lib/libusb-1.0.dylib",
                        "/usr/local/lib/libusb-1.0.dylib",
                    )
                    if Path(p).exists()
                ),
                None,
            )
        if not library:
            raise RuntimeError("Installed libusb was not found")
        self.lib = C.CDLL(library)
        self.context = PTR()
        self.handle = PTR()
        self.claimed = False
        signatures = {
            "init": ([C.POINTER(PTR)], C.c_int),
            "exit": ([PTR], None),
            "get_device_list": ([PTR, C.POINTER(C.POINTER(PTR))], C.c_ssize_t),
            "free_device_list": ([C.POINTER(PTR), C.c_int], None),
            "get_device_descriptor": ([PTR, C.POINTER(DeviceDescriptor)], C.c_int),
            "get_active_config_descriptor": ([PTR, C.POINTER(C.POINTER(Configuration))], C.c_int),
            "free_config_descriptor": ([C.POINTER(Configuration)], None),
            "open": ([PTR, C.POINTER(PTR)], C.c_int),
            "close": ([PTR], None),
            "kernel_driver_active": ([PTR, C.c_int], C.c_int),
            "claim_interface": ([PTR, C.c_int], C.c_int),
            "release_interface": ([PTR, C.c_int], C.c_int),
            "bulk_transfer": (
                [PTR, U8, C.POINTER(U8), C.c_int, C.POINTER(C.c_int), C.c_uint],
                C.c_int,
            ),
            "error_name": ([C.c_int], C.c_char_p),
        }
        for name, (args, result) in signatures.items():
            func = getattr(self.lib, "libusb_" + name)
            func.argtypes, func.restype = args, result

    def checked(self, code, operation):
        if code < 0:
            error = self.lib.libusb_error_name(code).decode("ascii")
            raise RuntimeError(f"{operation}: {error} ({code})")
        return code

    def __enter__(self):
        self.checked(self.lib.libusb_init(C.byref(self.context)), "libusb init")
        devices = C.POINTER(PTR)()
        config = C.POINTER(Configuration)()
        try:
            count = self.checked(
                self.lib.libusb_get_device_list(self.context, C.byref(devices)), "enumerate USB"
            )
            matches = []
            for index in range(count):
                descriptor = DeviceDescriptor()
                self.checked(
                    self.lib.libusb_get_device_descriptor(devices[index], C.byref(descriptor)),
                    "read device descriptor",
                )
                if (descriptor.idVendor, descriptor.idProduct) == (0x5345, 0x1234):
                    matches.append(devices[index])
            if len(matches) != 1:
                raise RuntimeError(f"Expected one OWON 5345:1234, found {len(matches)}")
            device = matches[0]
            self.checked(
                self.lib.libusb_get_active_config_descriptor(device, C.byref(config)),
                "read active configuration",
            )
            candidates = []
            for index in range(config.contents.bNumInterfaces):
                interface = config.contents.interface[index]
                for position in range(interface.num_altsetting):
                    alt = interface.altsetting[position]
                    if alt.bAlternateSetting != 0:
                        continue
                    endpoints = [
                        alt.endpoint[i]
                        for i in range(alt.bNumEndpoints)
                        if alt.endpoint[i].bmAttributes & 3 == 2
                    ]
                    inputs = [ep.bEndpointAddress for ep in endpoints if ep.bEndpointAddress & 0x80]
                    outputs = [
                        ep.bEndpointAddress for ep in endpoints if not ep.bEndpointAddress & 0x80
                    ]
                    if len(inputs) == len(outputs) == 1:
                        candidates.append((alt.bInterfaceNumber, inputs[0], outputs[0]))
            if len(candidates) != 1:
                raise RuntimeError(f"Ambiguous bulk interfaces: {candidates}")
            self.interface, self.input, self.output = candidates[0]
            self.usb = {
                "vid": "5345",
                "pid": "1234",
                "configuration": config.contents.bConfigurationValue,
                "interface": self.interface,
                "endpoint_in": f"0x{self.input:02x}",
                "endpoint_out": f"0x{self.output:02x}",
            }
            self.checked(self.lib.libusb_open(device, C.byref(self.handle)), "open OWON")
            active = self.lib.libusb_kernel_driver_active(self.handle, self.interface)
            if active == 1:
                raise RuntimeError("Interface has a kernel driver; it will not be detached")
            if active not in (0, -12):  # NOT_SUPPORTED is allowed on some hosts.
                self.checked(active, "check kernel driver")
            self.checked(
                self.lib.libusb_claim_interface(self.handle, self.interface), "claim interface"
            )
            self.claimed = True
            return self
        finally:
            if config:
                self.lib.libusb_free_config_descriptor(config)
            if devices:
                self.lib.libusb_free_device_list(devices, 1)
            if not self.claimed:
                self.close()

    def close(self):
        if self.claimed:
            self.lib.libusb_release_interface(self.handle, self.interface)
            self.claimed = False
        if self.handle:
            self.lib.libusb_close(self.handle)
            self.handle = PTR()
        if self.context:
            self.lib.libusb_exit(self.context)
            self.context = PTR()

    def __exit__(self, *_):
        self.close()

    def query(self, command, framed=False):
        allowed = {
            "*IDN?",
            ":DATA:WAVE:SCREEN:HEAD?",
            ":DATA:WAVE:SCREEN:CH1?",
            ":DATA:WAVE:SCREEN:CH2?",
            ":CH1:SCALE?",
            ":CH1:PROBE?",
            ":TRIGGER:STATUS?",
            ":MEASUREMENT:CH1:MAX?",
            ":MEASUREMENT:CH1:MIN?",
            ":MEASUREMENT:CH1:PKPK?",
        }
        if command not in allowed:
            raise ValueError("Only documented read queries are allowed")
        request = (command + "\n").encode("ascii")
        buffer = (U8 * len(request)).from_buffer_copy(request)
        sent = C.c_int()
        self.checked(
            self.lib.libusb_bulk_transfer(
                self.handle, self.output, buffer, len(buffer), C.byref(sent), 1000
            ),
            f"write {command}",
        )
        if sent.value != len(request):
            raise RuntimeError(f"Short command write: {sent.value}/{len(request)}")
        response = bytearray()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            buffer = (U8 * 16384)()
            received = C.c_int()
            status = self.lib.libusb_bulk_transfer(
                self.handle, self.input, buffer, len(buffer), C.byref(received), 1000
            )
            response.extend(bytes(buffer[: received.value]))
            if status not in (0, -7):
                self.checked(status, f"read {command}")
            if framed and len(response) >= 4:
                size = int.from_bytes(response[:4], "little")
                if not 0 < size <= 1024 * 1024:
                    raise RuntimeError(
                        f"Invalid DATA payload length: {size}; prefix={response[:32].hex()}"
                    )
                if len(response) > size + 4:
                    raise RuntimeError(
                        f"DATA response contains unexpected trailing bytes: "
                        f"{len(response)} > {size + 4}"
                    )
                if len(response) == size + 4:
                    return bytes(response), bytes(response[4:])
            elif not framed and (b"\n" in response or (status == -7 and response)):
                return bytes(response), bytes(response)
            if status == -7:
                break
        raise RuntimeError(
            f"Incomplete response to {command}: {len(response)} bytes; prefix={response[:64].hex()}"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, help="Save one CH1 SCREEN frame as new JSON")
    parser.add_argument("--note", help="User-supplied context for the saved capture")
    parser.add_argument(
        "--measure",
        action="store_true",
        help="Also query CH1 scale, probe, trigger status and numeric extrema",
    )
    args = parser.parse_args()
    with Scope() as scope:
        identity_raw, identity = scope.query("*IDN?")
        identity = identity.decode("ascii").rstrip("\r\n")
        print("Identity:", identity, flush=True)
        if len(identity.split(",")) != 4 or not identity.upper().startswith("OWON,HDS2102S,"):
            raise RuntimeError("Unexpected identity; no waveform queries will be sent")
        raw_header, payload = scope.query(":DATA:WAVE:SCREEN:HEAD?", framed=True)
        header = json.loads(payload.decode("utf-8"))
        print("USB:", json.dumps(scope.usb), flush=True)
        print("Header:", json.dumps(header), flush=True)
        measurements = {}
        if args.measure:
            for command in (
                ":CH1:SCALE?",
                ":CH1:PROBE?",
                ":TRIGGER:STATUS?",
                ":MEASUREMENT:CH1:MAX?",
                ":MEASUREMENT:CH1:MIN?",
                ":MEASUREMENT:CH1:PKPK?",
            ):
                raw, text = scope.query(command)
                measurements[command] = {
                    "text": text.decode("ascii").rstrip("\r\n"),
                    "raw_base64": base64.b64encode(raw).decode("ascii"),
                }
                print(command, repr(measurements[command]["text"]), flush=True)
        if args.capture:
            started = datetime.now().astimezone().isoformat()
            raw_channel, samples = scope.query(":DATA:WAVE:SCREEN:CH1?", framed=True)
            raw_after, payload_after = scope.query(":DATA:WAVE:SCREEN:HEAD?", framed=True)
            header_after = json.loads(payload_after.decode("utf-8"))
            ended = datetime.now().astimezone().isoformat()
            result = {
                "identity": identity,
                "usb": scope.usb,
                "notes": [
                    "Live sequential reads; acquisition is not stopped and data is not atomic.",
                    "SCREEN bytes are unscaled; time and voltage conversion are not established.",
                ],
                "frames": [
                    {
                        "captured_at": started,
                        "read_completed_at": ended,
                        "header": header,
                        "header_after": header_after,
                        "samples_u8": {"CH1": list(samples)},
                        "raw_responses_base64": {
                            "identity": base64.b64encode(identity_raw).decode("ascii"),
                            "header": base64.b64encode(raw_header).decode("ascii"),
                            "CH1": base64.b64encode(raw_channel).decode("ascii"),
                            "header_after": base64.b64encode(raw_after).decode("ascii"),
                        },
                    }
                ],
            }
            if args.note:
                result["user_context"] = args.note
            if measurements:
                result["measurements"] = measurements
            with args.capture.open("x") as destination:
                json.dump(result, destination, indent=2)
                destination.write("\n")
            print(f"Saved {len(samples)} CH1 bytes to {args.capture}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
