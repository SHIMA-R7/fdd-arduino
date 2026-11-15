"""IBM/OS/2 3.5-inch HD XDF image layout and raw sector transfer."""
import binascii
from dataclasses import dataclass
import struct
import time

CYLINDERS = 80
CYLINDER_BYTES = 23 * 1024
IBM_XDF_IMAGE_SIZE = CYLINDER_BYTES * CYLINDERS
BAUD = 666667

# Logical image order from fdutils xdfcopy. Physical IDs are 0x80 + N.
MIXED_LAYOUT = ((0, 3), (0, 4), (1, 6), (0, 2), (1, 2), (0, 6), (1, 4), (1, 3))


@dataclass(frozen=True)
class Sector:
    cylinder: int
    side: int
    identifier: int
    offset: int
    size: int

    @property
    def address(self):
        return self.cylinder | (self.side << 7) | (self.identifier << 8)

    def data(self, image):
        return bytes(image[self.offset:self.offset + self.size])


def image_layout(image):
    if len(image) != IBM_XDF_IMAGE_SIZE:
        raise ValueError("IBM／OS/2 XDFイメージは1,884,160バイトです。")
    size, cluster, reserved, fats, entries, total, _, fat_size, spt, heads = struct.unpack_from(
        "<HBHBHHBHHH", image, 11
    )
    if (size, reserved, fats, total, spt, heads) != (512, 1, 2, 3680, 23, 2):
        raise ValueError("IBM／OS/2 XDFのBPBと一致しません。")
    if cluster not in (1, 2, 4, 8, 16, 32, 64, 128) or entries == 0 or entries % 16:
        raise ValueError("IBM／OS/2 XDFのクラスタまたはルートディレクトリの構成が不正です。")
    root_size = entries // 16
    data_count = 40 - 2 * fat_size - root_size
    if fat_size < 8 or data_count < 0 or 1 + fat_size + root_size + data_count > 30:
        raise ValueError("IBM／OS/2 XDFのFATとルートディレクトリがシリンダ0に収まりません。")

    def main_sector(index, offset):
        side = int(index >= 11)
        identifier = 129 + index - 11 * side
        return Sector(0, side, identifier, offset * 512, 512)

    boot_and_fat = 1 + fat_size
    root_offset = boot_and_fat + fat_size
    data_offset = root_offset + root_size + 5
    first = [main_sector(i, i) for i in range(boot_and_fat)]
    first.extend(Sector(0, 0, i + 1, (boot_and_fat + i) * 512, 512) for i in range(8))
    first.extend(main_sector(boot_and_fat + i, root_offset + i) for i in range(root_size))
    first.extend(
        main_sector(boot_and_fat + root_size + i, data_offset + i) for i in range(data_count)
    )
    layout = [tuple(sorted(first, key=lambda sector: (sector.side, sector.identifier)))]
    for cylinder in range(1, CYLINDERS):
        offset = cylinder * CYLINDER_BYTES
        sectors = []
        for side, size_code in MIXED_LAYOUT:
            size = 128 << size_code
            sectors.append(Sector(cylinder, side, 128 + size_code, offset, size))
            offset += size
        layout.append(tuple(sorted(sectors, key=lambda sector: sector.side)))
    return tuple(layout)


def read_field(drive, op, address, size):
    for attempt in range(3):
        drive.request(op, address, timeout=2)
        packet = bytearray()
        deadline = time.monotonic() + 5
        while len(packet) < size + 5 and time.monotonic() < deadline:
            packet.extend(drive.link.read(size + 5 - len(packet)))
            if len(packet) == 2 and packet[0] == 0x5A and packet[1] != 0:
                break
        if len(packet) != 2 or packet[0] != 0x5A:
            break
        if packet[1] not in (2, 3, 4) or attempt == 2:
            raise ValueError(f"XDFの読み取りに失敗しました (セクター {address:04X}、状態 {packet[1]})")
        time.sleep(0.12)
    if len(packet) != size + 5 or packet[-2:] != b"\x5a\x00":
        raise ValueError(f"XDFの読み取りに失敗しました (セクター {address:04X}、{len(packet)} バイト受信)")
    field = packet[:-4]
    disk_crc = int.from_bytes(packet[-4:-2], "big")
    calculated = binascii.crc_hqx(b"\xa1" * 3 + field, 0xFFFF)
    if field[0] != 0xFB or calculated != disk_crc:
        raise ValueError(f"セクター {address:04X} のデータマークまたはCRCが不正です")
    return bytes(field[1:]), disk_crc


def write_field(drive, op, address, data):
    crc = binascii.crc_hqx(b"\xa1\xa1\xa1\xfb" + data, 0xFFFF)
    prefix = data[:512]
    tail = data[512:] + crc.to_bytes(2, "big") + b"\x4e"
    for attempt in range(3):
        drive.request(op, address, timeout=2)
        drive.link.write(prefix)
        deadline = time.monotonic() + 6
        marker = drive.exact(1, deadline)
        if marker != b"\xa6":
            status = drive.exact(1, deadline).hex() if marker == b"\x5a" else marker.hex()
            raise ValueError(f"XDFの書き込みを開始できません (セクター {address:04X}、応答 {status})")
        drive.link.write(tail)
        response = drive.exact(2, time.monotonic() + 3)
        if response == b"\x5a\x00":
            return
        # The firmware preserves the ID field and drains pending UART bytes
        # before reporting a streaming interruption.
        if response != b"\x5a\x0a" or attempt == 2:
            raise ValueError(f"XDFの書き込みが中断されました (セクター {address:04X}、応答 {response.hex()})")
