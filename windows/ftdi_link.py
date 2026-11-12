"""Optional low-latency FTDI transport. Use the installed Windows driver."""

import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path

from serial.tools import list_ports


class FtdiUnavailable(OSError):
    pass


class FtdiLink:
    latency = 2

    @classmethod
    def open_port(cls, port, baudrate=115200):
        if os.name != "nt":
            raise FtdiUnavailable("Windows only")
        match = next((p for p in list_ports.comports() if p.device.casefold() == port.casefold()), None)
        if match is None or match.vid != 0x0403 or not match.serial_number:
            raise FtdiUnavailable("No FTDI serial number for this COM port")
        return cls(port, match.serial_number, baudrate)

    def __init__(self, port, serial_number, baudrate):
        self.handle = c.c_void_p()
        self.original_latency = None
        self.dll = c.WinDLL(str(Path(os.environ["SystemRoot"]) / "System32" / "ftd2xx.dll"))
        h, p, d, b = c.c_void_p, c.c_void_p, w.DWORD, c.c_ubyte
        self.bind("OpenEx", p, d, c.POINTER(h))
        self.bind("Close", h)
        self.bind("GetComPortNumber", h, c.POINTER(w.LONG))
        self.bind("SetBaudRate", h, d)
        self.bind("SetDataCharacteristics", h, b, b, b)
        self.bind("SetFlowControl", h, c.c_ushort, b, b)
        self.bind("SetTimeouts", h, d, d)
        self.bind("GetLatencyTimer", h, c.POINTER(b))
        self.bind("SetLatencyTimer", h, b)
        self.bind("Purge", h, d)
        self.bind("Read", h, p, d, c.POINTER(d))
        self.bind("Write", h, p, d, c.POINTER(d))
        try:
            # The VCP enumerator can append interface "A" to the native serial.
            candidates = [serial_number]
            if serial_number.endswith("A"):
                candidates.append(serial_number[:-1])
            for candidate in candidates:
                number = c.create_string_buffer(candidate.encode("ascii"))
                handle = c.c_void_p()
                status = self.dll.FT_OpenEx(number, 1, c.byref(handle))
                if status == 0:
                    self.handle = handle
                    break
            else:
                raise FtdiUnavailable(f"FTDI OpenEx: {status}")
            actual = w.LONG()
            self.call("GetComPortNumber", self.handle, c.byref(actual))
            if f"COM{actual.value}".casefold() != port.casefold():
                raise FtdiUnavailable("FTDI handle does not match the selected COM port")
            current = b()
            self.call("GetLatencyTimer", self.handle, c.byref(current))
            self.original_latency = current.value
            self.call("SetLatencyTimer", self.handle, min(self.latency, current.value))
            self.call("SetTimeouts", self.handle, 150, 3000)
            self.call("SetDataCharacteristics", self.handle, 8, 0, 0)
            self.call("SetFlowControl", self.handle, 0, 0, 0)
            self.baudrate = baudrate
        except Exception:
            self.close()
            raise

    def bind(self, name, *arguments):
        function = getattr(self.dll, "FT_" + name)
        function.argtypes = arguments
        function.restype = w.DWORD

    def call(self, name, *args):
        status = getattr(self.dll, "FT_" + name)(*args)
        if status:
            raise FtdiUnavailable(f"FTDI {name}: {status}")

    @property
    def baudrate(self):
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value):
        self.call("SetBaudRate", self.handle, value)
        self._baudrate = value

    def reset_input_buffer(self):
        self.call("Purge", self.handle, 1)

    def read(self, size):
        buffer = c.create_string_buffer(size)
        received = w.DWORD()
        self.call("Read", self.handle, buffer, size, c.byref(received))
        return buffer.raw[:received.value]

    def write(self, data):
        buffer = c.create_string_buffer(data)
        sent = w.DWORD()
        self.call("Write", self.handle, buffer, len(data), c.byref(sent))
        if sent.value != len(data):
            raise TimeoutError("FTDIの送信がタイムアウトしました。")
        return sent.value

    def close(self):
        if self.handle.value:
            try:
                if self.original_latency is not None:
                    self.call("SetLatencyTimer", self.handle, self.original_latency)
            finally:
                try:
                    self.call("Close", self.handle)
                finally:
                    self.handle = c.c_void_p()
