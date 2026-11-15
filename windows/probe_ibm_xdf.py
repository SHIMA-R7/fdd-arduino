"""Write and verify the previously identified IBM XDF scratch track."""
import argparse
import hashlib
from pathlib import Path
import time

from serial.tools import list_ports

from fdd_core import Drive, X68_IMAGE_SIZE
from ibm_xdf import BAUD, read_field, write_field

SCRATCH_KEY = 0x864C
DATA_SIZE = 8192
MIXED_SIZES = (2048, 512, 1024, 8192)


def verify_original_tracks(drive, reference, addresses):
    for address in addresses:
        offset = address * 1024
        if drive.read(address) != reference[offset:offset + 1024]:
            raise ValueError(f"テスト用FDと一致しません (LBA {address})")


def sector_key(size):
    return 0x804C | ((size.bit_length() - 8) << 8)


def read_sector(drive, size=DATA_SIZE):
    return read_field(drive, "b", sector_key(size), size)


def write_sector(drive, data):
    write_field(drive, "V", sector_key(len(data)), data)

def verify_mixed_track(drive):
    drive.request("K", SCRATCH_KEY, timeout=8)
    print("シリンダ76の面1を4セクターで初期化", flush=True)
    for size in MIXED_SIZES:
        actual, crc = read_sector(drive, size)
        if actual != bytes([0xF6]) * size:
            raise ValueError(f"初期化した {size} バイトセクターが一致しません")
        print(f"初期化後の {size} バイトとCRC {crc:04X} が一致", flush=True)
    patterns = {
        size: hashlib.shake_256(b"IBM XDF mixed track" + size.to_bytes(2, "big")).digest(size)
        for size in MIXED_SIZES
    }
    for data in patterns.values():
        write_sector(drive, data)
    for size, data in patterns.items():
        actual, crc = read_sector(drive, size)
        if actual != data:
            raise ValueError(f"上書きした {size} バイトセクターが一致しません")
        print(f"全4セクター上書き後の {size} バイトとCRC {crc:04X} が一致", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port")
    parser.add_argument("reference", type=Path, help="original X68000 test disk image")
    parser.add_argument("--mixed", action="store_true", help="format and verify all four XDF sector sizes")
    args = parser.parse_args()
    reference = args.reference.read_bytes()
    if len(reference) != X68_IMAGE_SIZE:
        parser.error("X68000テスト用イメージのサイズが一致しません")
    device = next((p for p in list_ports.comports() if p.device.casefold() == args.port.casefold()), None)
    if device is None or device.vid != 0x0403:
        parser.error("FTDIのCOMポートを指定してください")

    with Drive(args.port, fast=False) as drive:
        drive.request("J", 2)
        drive.sector_size = 1024
        verify_original_tracks(drive, reference, (0, 1, 20, 1200, *range(1216, 1224)))
        drive.request("O")
        drive.request("H", 4, timeout=2)
        time.sleep(0.1)
        drive.link.baudrate = BAUD
        drive.link.reset_input_buffer()
        drive.baudrate = BAUD
        _, crc = read_sector(drive)
        print(f"識別用12セクターと試験用8 KiBセクターを確認 (CRC {crc:04X})", flush=True)

        if args.mixed:
            verify_mixed_track(drive)
        else:
            patterns = (hashlib.shake_256(b"IBM XDF scratch sector").digest(DATA_SIZE), b"\x55\xaa" * 4096)
            for index, data in enumerate(patterns, 1):
                write_sector(drive, data)
                actual, crc = read_sector(drive)
                if actual != data:
                    raise ValueError(f"試験 {index} の読み戻しデータが一致しません")
                print(f"試験 {index}: 8,192バイトとCRC {crc:04X} が一致", flush=True)

        drive.request("H", 0, timeout=2)
        time.sleep(0.1)
        drive.link.baudrate = 115200
        drive.link.reset_input_buffer()
        drive.baudrate = 115200
        drive.request("J", 2)
        verify_original_tracks(drive, reference, range(1216, 1224))
        print("同じシリンダの反対面8セクターも一致", flush=True)


if __name__ == "__main__":
    main()
