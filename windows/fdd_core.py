"""COM protocol for the ATmega FDD board and FAT12 file operations."""

import binascii
import contextlib
import dataclasses
import datetime
import hashlib
import os
from pathlib import Path
import struct
import tempfile
import time

import serial
from ftdi_link import FtdiLink
from ibm_xdf import BAUD as IBM_XDF_BAUD, IBM_XDF_IMAGE_SIZE, image_layout, read_field, write_field

SIZE = 512
SECTORS = 2880
IMAGE_SIZE = SIZE * SECTORS
DMF_SECTORS_PER_SIDE = 21
DMF_IMAGE_SIZE = SIZE * 80 * 2 * DMF_SECTORS_PER_SIDE
X68_IMAGE_SIZE = 77 * 2 * 8 * 1024
# The JU-256A046PC passed repeated scales through this frequency.
MUSIC_GUI_MAX_HZ = 262
SECTOR_ERRORS = {
    2: "ドライブが準備できていません（電源・ディスク・信号を確認）",
    3: "同期マークが見つかりません",
    4: "セクターヘッダーが見つかりません",
    5: "データマークが不正です",
    6: "データのCRCが一致しません",
    7: "トラック0を検出できません",
    8: "書き込み後の照合に失敗しました",
    9: "書き込み禁止です（可動ツメ側の穴を閉じてください）",
    98: "送信データのCRCが一致しません",
    99: "未対応コマンドです。フォーマット対応版のスケッチを確認してください",
}


class Cancelled(Exception):
    pass


class SectorError(Exception):
    def __init__(self, op, address, code):
        self.code = code
        self.address = address
        label = "トラック" if op in ("F", "G") else "演奏 Hz" if op == "M" else "LBA"
        if op == "G":
            address = f"{address & 255}〜{address >> 8}"
        super().__init__(f"{label} {address}: {SECTOR_ERRORS.get(code, f'エラー {code}')}")


def noop(*args):
    pass


def order_sectors(addresses, spacing=None, sectors_per_side=18):
    """Group by track-side and leave enough sectors between transfers."""
    addresses = set(addresses)
    if spacing is not None:
        return sorted(addresses, key=lambda n: (
            n // sectors_per_side, (n % sectors_per_side) % spacing, (n % sectors_per_side) // spacing))
    return sorted(addresses, key=lambda n: (n // sectors_per_side, (n % sectors_per_side) * 13 % sectors_per_side))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_save(path, data):
    path = Path(path)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        staging = Path(stream.name)
        try:
            stream.write(data)
            stream.close()
            os.replace(staging, path)
        finally:
            if staging.exists():
                staging.unlink()


class Drive:
    def __init__(self, port, cancelled=noop, progress=noop, *, fast=True, low_latency=True, max_baudrate=500000):
        self.port = port
        self.cancelled = cancelled
        self.progress = progress
        self.link = None
        self.fast = fast
        self.low_latency = low_latency
        self.max_baudrate = max_baudrate
        self.baudrate = 115200
        self.transport = "COM"
        self.paired_reads = False
        self.sectors_per_side = 18
        self.sector_size = SIZE
        self.cylinders = 80

    def __enter__(self):
        try:
            if self.low_latency:
                try:
                    self.link = FtdiLink.open_port(self.port)
                    self.transport = "FTDI・低遅延"
                except (OSError, AttributeError, ValueError, KeyError):
                    pass
            if self.link is None:
                self.link = serial.Serial(self.port, 115200, timeout=.15, write_timeout=3)
            time.sleep(1)
            self.link.reset_input_buffer()
            try:
                self.request("O", check_cancel=False, timeout=2)
            except TimeoutError:
                # Recover from a previous application's interrupted fast session.
                time.sleep(4.5)
                self.link.reset_input_buffer()
                self.request("O", check_cancel=False, timeout=2)
            if self.fast:
                self.negotiate()
        except Exception:
            if self.link is not None:
                with contextlib.suppress(Exception):
                    self.link.close()
            raise
        return self

    def __exit__(self, kind, error, trace):
        try:
            self.request("O", check_cancel=False, timeout=2)
            if self.baudrate != 115200:
                self.request("H", 0, check_cancel=False, timeout=2)
        except Exception:
            if error is None:
                raise
        finally:
            self.link.close()

    def negotiate(self):
        probe = bytes(range(256)) + bytes(reversed(range(256)))
        for code, baudrate in ((3, 1000000), (2, 500000), (1, 250000)):
            if baudrate > self.max_baudrate:
                continue
            try:
                self.request("H", code, timeout=2)
            except SectorError as error:
                if error.code == 99:  # The existing firmware does not support speed switching.
                    return
                raise
            try:
                time.sleep(.065)
                self.link.baudrate = baudrate
                self.link.reset_input_buffer()
                for _ in range(3):
                    if self.request("T", payload=probe, timeout=1.5) != probe:
                        raise ValueError("速度テストのデータが一致しません。")
                self.baudrate = baudrate
                self.paired_reads = baudrate >= 500000
                return
            except (OSError, ValueError, SectorError):
                # The firmware returns to 115200 after five seconds without a command.
                # Wait even if a partial data packet occupied the input reader.
                self.link.baudrate = 115200
                time.sleep(7.5)
                self.link.reset_input_buffer()
                self.request("O", check_cancel=False, timeout=2)

    def order(self, addresses):
        low = self.transport == "FTDI・低遅延"
        if self.baudrate >= 1000000:
            spacing = 2 if low else 4
        elif self.baudrate >= 500000:
            spacing = 3 if low else 4
        elif self.baudrate >= 250000:
            spacing = 4 if low else 5
        else:
            spacing = 6 if low else None
        return order_sectors(addresses, spacing, self.sectors_per_side)

    def check(self):
        if self.cancelled():
            raise Cancelled("中止しました。")

    def exact(self, size, deadline):
        result = bytearray()
        while len(result) < size:
            result.extend(self.link.read(size - len(result)))
            if time.monotonic() > deadline:
                raise TimeoutError(f"{self.port}: 基板からの応答がありません。接続・スケッチを確認してください。")
        return bytes(result)

    def request(self, op, address=0, payload=None, check_cancel=True, timeout=12):
        if check_cancel:
            self.check()
        header = bytes((0xA5, ord(op))) + struct.pack("<H", address)
        if payload is not None:
            expected_size = SIZE if op == "T" else self.sector_size
            if len(payload) != expected_size:
                raise ValueError(f"セクターサイズは{expected_size}バイトです。")
            header += payload + struct.pack("<H", binascii.crc_hqx(payload, 0xFFFF))
        self.link.write(header)
        deadline = time.monotonic() + timeout
        mark, status = self.exact(2, deadline)
        if mark != 0x5A:
            raise ValueError("基板の応答形式が違います。対応する転送用スケッチを確認してください。")
        if status:
            raise SectorError(op, address, status)
        if op == "P":
            return self.exact(6, deadline)
        if op == "E":
            return self.exact(2, deadline)
        if op == "D":
            packets = self.exact(2 * (SIZE + 2), deadline)
            return (self.read_packet(address, packets[:SIZE + 2]),
                    self.read_packet(address + 1, packets[SIZE + 2:]))
        if op in ("R", "T"):
            size = SIZE if op == "T" else self.sector_size
            return self.read_packet(address, self.exact(size + 2, deadline))

    @staticmethod
    def read_packet(address, packet):
        data = packet[:-2]
        expected = struct.unpack_from("<H", packet, len(data))[0]
        if expected != binascii.crc_hqx(data, 0xFFFF):
            raise ValueError(f"LBA {address}: シリアル転送のCRCが一致しません。")
        return data

    def sector(self, op, address, payload=None, check_cancel=True):
        for attempt in range(3):
            try:
                return self.request(op, address, payload, check_cancel=check_cancel)
            except SectorError as error:
                if attempt == 2 or error.code not in (2, 3, 4, 6, 8):
                    raise
                time.sleep(.12)

    def read(self, address):
        return self.sector("R", address)

    def play_notes(self, notes, *, fit_drive_range=False, max_hz=MUSIC_GUI_MAX_HZ):
        if not notes or any(
            type(frequency) is not int or type(duration) is not int
            or (frequency != 0 and not 20 <= frequency <= 2000) or duration <= 0
            for frequency, duration in notes
        ):
            raise ValueError("音符は [周波数Hz, 長さms] で指定してください。休符は0 Hz、音は20〜2000 Hzです。")
        total = sum(duration for _, duration in notes)
        elapsed_ms = 0
        deadline = None
        try:
            for frequency, duration in notes:
                if fit_drive_range:
                    while frequency > max_hz:
                        frequency = (frequency + 1) // 2
                self.request("M", frequency, timeout=2)
                now = time.monotonic()
                deadline = (deadline if deadline is not None else now) + duration / 1000
                refresh = now + .3
                while now < deadline:
                    self.check()
                    if now >= refresh:
                        self.request("M", frequency, timeout=2)
                        refresh = now + .3
                    time.sleep(max(0, min(.025, deadline - time.monotonic())))
                    now = time.monotonic()
                elapsed_ms += duration
                self.progress("演奏中", elapsed_ms, total)
        except SectorError as error:
            if error.code == 99:
                raise ValueError("演奏対応の転送用ファームウェアを基板に書き込んでください。") from error
            raise
        finally:
            with contextlib.suppress(OSError, ValueError, SectorError):
                self.request("O", check_cancel=False, timeout=2)

    def read_many(self, addresses, label):
        pending = set(addresses)
        groups = {}
        for address in sorted(pending):
            if address in pending:
                pair = (self.paired_reads and address % self.sectors_per_side != self.sectors_per_side - 1
                        and address + 1 in pending)
                groups[address] = (address, address + 1) if pair else (address,)
                pending.difference_update(groups[address])
        spacing = 4 if self.baudrate == 1000000 and self.transport == "FTDI・低遅延" else 6
        ordered = order_sectors(groups, spacing, self.sectors_per_side) if self.paired_reads else self.order(groups)
        total = sum(map(len, groups.values()))
        result = {}
        for address in ordered:
            group = groups[address]
            if len(group) == 2 and self.paired_reads:
                try:
                    result.update(zip(group, self.request("D", address)))
                except SectorError as error:
                    if error.code == 99:
                        self.paired_reads = False
                    result.update((item, self.read(item)) for item in group)
            else:
                result.update((item, self.read(item)) for item in group)
            self.progress(label, len(result), total)
        return result

    def save_image(self, path):
        data = self.read_many(range(SECTORS), "FDイメージを読み取り中")
        self.check()
        atomic_save(path, b"".join(data[i] for i in range(SECTORS)))

    def scan(self):
        issues = []
        for i, address in enumerate(self.order(range(SECTORS)), 1):
            try:
                self.read(address)
            except SectorError as error:
                issues.append(str(error))
                if len(issues) >= 32:
                    return issues, i
            self.progress("全セクターの読み取り検査", i, SECTORS)
        return issues, SECTORS

    def format_tracks(self):
        # A range avoids homing the head for every cylinder. Keep the ranges
        # short enough to provide progress and let the user cancel between them.
        try:
            self.request("G", 9 << 8, timeout=40)
        except SectorError as error:
            if error.code != 99:
                raise
        else:
            self.progress("トラックを初期化中", 10, self.cylinders)
            for first in range(10, self.cylinders, 10):
                last = min(first + 9, self.cylinders - 1)
                self.request("G", first | last << 8, timeout=40)
                self.progress("トラックを初期化中", last + 1, self.cylinders)
            return
        for track in range(self.cylinders):
            self.request("F", track)
            self.progress("トラックを初期化中", track + 1, self.cylinders)

    def restore_image(self, image, reformat):
        if len(image) == IBM_XDF_IMAGE_SIZE:
            self.restore_ibm_xdf(image, reformat)
            return
        if len(image) == IMAGE_SIZE:
            self.restore_tracks(image, reformat)
            return
        if len(image) == DMF_IMAGE_SIZE:
            mode, name, sectors, size, cylinders = 1, "DMF", DMF_SECTORS_PER_SIDE, SIZE, 80
        elif len(image) == X68_IMAGE_SIZE:
            mode, name, sectors, size, cylinders = 2, "X68000／PC-98 XDF", 8, 1024, 77
        else:
            raise ValueError("1.44 MB、DMF、X68000／PC-98標準2HD、IBM／OS/2 XDFの生イメージに対応します。")
        try:
            self.request("J", mode)
        except SectorError as error:
            if error.code == 99:
                raise ValueError(f"{name}対応ファームウェアを基板に書き込んでください。") from error
            raise
        self.sectors_per_side, self.sector_size, self.cylinders = sectors, size, cylinders
        paired = self.paired_reads
        self.paired_reads = paired and size == SIZE
        try:
            self.restore_tracks(image, reformat)
            sectors_per_cylinder = self.sectors_per_side * 2
            # Restart the motor before the long seek back to cylinder zero.
            self.request("O")
            self.request("J", mode)
            for track in range(cylinders):
                addresses = range(track * sectors_per_cylinder, (track + 1) * sectors_per_cylinder)
                blocks = self.read_many(addresses, f"{name}のシリンダを再照合中")
                for address in addresses:
                    if blocks[address] != image[address * size:(address + 1) * size]:
                        raise SectorError("R", address, 8)
                self.progress(f"{name}の全セクターを再照合中", track + 1, cylinders)
        except BaseException:
            with contextlib.suppress(OSError, ValueError, SectorError):
                self.request("O", check_cancel=False, timeout=2)
            raise
        else:
            self.request("O", check_cancel=False, timeout=2)
        finally:
            self.sectors_per_side = 18
            self.sector_size = SIZE
            self.cylinders = 80
            self.paired_reads = paired

    def restore_ibm_xdf(self, image, reformat):
        layout = image_layout(image)
        if self.transport != "FTDI・低遅延":
            raise ValueError("IBM／OS/2 XDFの復元にはFTDIの低遅延接続が必要です。")
        try:
            self.request("q", timeout=2)
        except SectorError as error:
            if error.code == 99:
                raise ValueError("IBM／OS/2 XDF対応ファームウェアを基板に書き込んでください。") from error
            raise
        self.request("H", 4, timeout=2)
        time.sleep(0.1)
        self.link.baudrate = IBM_XDF_BAUD
        self.baudrate = IBM_XDF_BAUD
        self.link.reset_input_buffer()
        if reformat:
            for cylinder in range(len(layout)):
                self.request("I", cylinder, timeout=8)
                self.request("I", cylinder | 0x80, timeout=8)
                self.progress("IBM XDFを初期化中", cylinder + 1, len(layout))
        for cylinder, sectors in enumerate(layout, 1):
            for sector in sectors:
                data = sector.data(image)
                try:
                    write_field(self, "U", sector.address, data)
                except SectorError as error:
                    if error.code != 99:
                        raise
                    # q already established support. Let an outstanding
                    # prefix receive time out before resynchronizing.
                    time.sleep(2.2)
                    self.link.reset_input_buffer()
                    self.request("q", timeout=2)
                    write_field(self, "U", sector.address, data)
            self.progress("IBM XDFを書き込み中", cylinder, len(layout))
        for cylinder, sectors in enumerate(layout, 1):
            for sector in sectors:
                data, _ = read_field(self, "u", sector.address, sector.size)
                if data != sector.data(image):
                    raise ValueError(
                        f"シリンダ{sector.cylinder}、面{sector.side}、"
                        f"セクター{sector.identifier:02X}のデータが一致しません。"
                    )
            self.progress("IBM XDFの全セクターを照合中", cylinder, len(layout))

    def restore_tracks(self, image, reformat):
        if reformat:
            self.format_tracks()
        sectors = self.sectors_per_side * 2
        size = self.sector_size
        for track in range(self.cylinders):
            addresses = self.order(range(track * sectors, (track + 1) * sectors))
            for attempt in range(2):
                try:
                    for address in addresses:
                        offset = address * size
                        self.sector("w", address, image[offset:offset + size])
                    readback = self.read_many(addresses, "イメージのトラックを照合中")
                    for address in addresses:
                        offset = address * size
                        if readback[address] != image[offset:offset + size]:
                            raise SectorError("R", address, 8)
                    break
                except SectorError as error:
                    if not reformat or attempt or error.code not in (2, 3, 4, 6, 8):
                        raise
                    self.request("F", track)
            self.progress("イメージを書き込み・照合中", track + 1, self.cylinders)

    def format_fat12(self, label, *, quick=False):
        metadata = blank_metadata(label)
        addresses = self.order(range(1, 33))
        if quick:
            old = Fat12(self)
            if (old.spc, old.first_fat, old.fats, old.root_count, old.fat_length) != (1, 1, 2, 224, 9):
                raise ValueError("このFDには通常フォーマットを選んでください。")
            if not old.fats_match or old.fat[:3] != b"\xf0\xff\xff":
                raise ValueError("既存のFATを確認できません。通常フォーマットを選んでください。")
            fat = bytearray(b"".join(metadata[i] for i in range(1, 10)))
            for cluster in range(2, old.limit):
                if old.get_fat(cluster) == 0xFF7:
                    Fat12.set_fat(fat, cluster, 0xFF7)
            for i in range(9):
                metadata[i + 1] = metadata[i + 10] = bytes(fat[i * SIZE:(i + 1) * SIZE])
            existing = old.original | self.read_many(range(19, 33), "既存のファイル一覧を読み取り中")
            addresses = [address for address in addresses if existing[address] != metadata[address]]
        else:
            self.format_tracks()
        for attempt in range(2):
            try:
                for i, address in enumerate(addresses, 1):
                    self.sector("w", address, metadata[address])
                    self.progress("FAT12の管理情報を書き込み中", i, len(addresses))
                readback = self.read_many(addresses, "FAT12の管理情報を照合中")
                for address in addresses:
                    if readback[address] != metadata[address]:
                        raise SectorError("R", address, 8)
                self.sector("W", 0, metadata[0])
                self.progress("FAT12の管理情報を作成中", 1, 1)
                break
            except SectorError as error:
                if quick or attempt or error.code not in (2, 3, 4, 6, 8):
                    raise
                self.request("F", 0)


def blank_metadata(label="FLOPPY"):
    label = label.strip().upper()
    if not label or len(label) > 11 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _-" for c in label):
        raise ValueError("ディスク名は半角英数字・空白・「_」「-」の11文字以内です。")
    disk_name = label.encode("ascii").ljust(11, b" ")
    boot = bytearray(SIZE)
    boot[:11] = bytes.fromhex("eb3c90") + b"MSDOS5.0"
    struct.pack_into("<HBHBHHBHHHII", boot, 11, 512, 1, 1, 2, 224, 2880, 0xF0, 9, 18, 2, 0, 0)
    boot[38] = 0x29
    boot[39:43] = os.urandom(4)
    boot[43:54] = disk_name
    boot[54:62] = b"FAT12   "
    boot[62:66] = bytes.fromhex("faf4ebfd")
    boot[510:512] = bytes.fromhex("55aa")
    records = {i: bytes(SIZE) for i in range(33)}
    records[0] = bytes(boot)
    records[1] = records[10] = bytes.fromhex("f0ffff") + bytes(509)
    records[19] = disk_name + b"\x08" + bytes(500)
    return records


def u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def checksum_alias(name):
    result = 0
    for b in name:
        result = (((result & 1) << 7) + (result >> 1) + b) & 255
    return result


LFN_OFFSETS = (1, 3, 5, 7, 9, 14, 16, 18, 20, 22, 24, 28, 30)


@dataclasses.dataclass(frozen=True)
class Entry:
    name: str
    short: bytes
    attr: int
    cluster: int
    size: int
    slots: tuple
    timestamp: str

    @property
    def folder(self):
        return bool(self.attr & 16)


@dataclasses.dataclass
class Directory:
    sectors: list
    data: bytearray
    entries: list

    @property
    def token(self):
        return digest(self.data)


def decode_short(raw, flags):
    raw = (b"\xe5" + raw[1:]) if raw[0] == 5 else raw
    stem = raw[:8].rstrip().decode("cp932", errors="replace")
    ext = raw[8:].rstrip().decode("cp932", errors="replace")
    if flags & 8:
        stem = stem.lower()
    if flags & 16:
        ext = ext.lower()
    return f"{stem}.{ext}" if ext else stem


def decode_lfn(parts, short):
    if not parts or len(parts) > 20:
        return None
    seq = [i[1][0] for i in parts]
    if seq != [len(parts) | 0x40] + list(range(len(parts) - 1, 0, -1)):
        return None
    crc = checksum_alias(short)
    if any(e[13] != crc or e[12] or u16(e, 26) for _, e in parts):
        return None
    code = [u16(e, j) for _, e in reversed(parts) for j in LFN_OFFSETS]
    if 0 in code:
        code = code[:code.index(0)]
    try:
        return struct.pack("<" + "H" * len(code), *code).decode("utf-16le") or None
    except UnicodeDecodeError:
        return None


def entries_of(data):
    parts = []
    for offset in range(0, len(data), 32):
        e = bytes(data[offset:offset + 32])
        if e[0] == 0:
            break
        if e[0] == 0xE5:
            parts.clear()
            continue
        if e[11] == 15:
            parts.append((offset, e))
            continue
        name = decode_lfn(parts, e[:11])
        slots = tuple(i for i, _ in parts) if name else ()
        parts.clear()
        if e[11] & 8 or e[0] == 46:
            continue
        date, clock = u16(e, 24), u16(e, 22)
        timestamp = f"{1980 + (date >> 9):04d}/{(date >> 5) & 15:02d}/{date & 31:02d} {clock >> 11:02d}:{(clock >> 5) & 63:02d}" if date else ""
        yield Entry(name or decode_short(e[:11], e[12]), e[:11], e[11],
                    u16(e, 26), struct.unpack_from("<I", e, 28)[0], slots + (offset,), timestamp)


def encode_entries(name, existing, cluster, size):
    invalid = set('<>:"/\\|?*')
    units = name.encode("utf-16le")
    if not name or name[-1] in ". " or len(units) > 510 or any(ord(c) < 32 or c in invalid for c in name):
        raise ValueError("FAT12に保存できないファイル名です。")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789$%'-_@~`!(){}^#&"
    clean = lambda s: "".join(c if c in allowed else "_" for c in s.upper())
    upper = name.upper()
    st, ex = clean(stem), clean(ext)
    fits = st == stem.upper() and ex == ext.upper() and 1 <= len(st) <= 8 and len(ex) <= 3
    short = (st.ljust(8) + ex.ljust(3)).encode("ascii") if fits else None
    if short is None or short in existing:
        for n in range(1, 10000):
            tail = f"~{n}"
            short = (((st or "FILE")[:8 - len(tail)] + tail).ljust(8) + ex[:3].ljust(3)).encode("ascii")
            if short not in existing:
                break
        else:
            raise ValueError("短いファイル名の別名を作成できません。")
    result = []
    if name != upper or decode_short(short, 0) != name:
        code = list(struct.unpack("<" + "H" * (len(units) // 2), units)) + [0]
        code += [0xFFFF] * (-len(code) % 13)
        count = len(code) // 13
        for seq in range(count, 0, -1):
            entry = bytearray(32)
            entry[0] = seq | (0x40 if seq == count else 0)
            entry[11] = 15
            entry[13] = checksum_alias(short)
            for pos, unit in zip(LFN_OFFSETS, code[(seq - 1) * 13:seq * 13]):
                struct.pack_into("<H", entry, pos, unit)
            result.append(entry)
    record = bytearray(32)
    record[:11] = short
    record[11] = 32
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    date = ((now.year - 1980) << 9) | (now.month << 5) | now.day
    clock = (now.hour << 11) | (now.minute << 5) | now.second // 2
    struct.pack_into("<HHHI", record, 22, clock, date, cluster, size)
    result.append(record)
    return b"".join(result)


class Fat12:
    def __init__(self, drive):
        self.drive = drive
        boot = drive.read(0)
        self.disk_id = digest(boot)
        self.label = boot[43:54].decode("cp932", errors="replace").strip()
        self.spc = boot[13]
        self.first_fat = u16(boot, 14)
        self.fats = boot[16]
        self.root_count = u16(boot, 17)
        self.fat_length = u16(boot, 22)
        self.root_start = self.first_fat + self.fats * self.fat_length
        self.root_sectors = (32 * self.root_count + 511) // 512
        self.first_data = self.root_start + self.root_sectors
        if (u16(boot, 11), u16(boot, 19), u16(boot, 24), u16(boot, 26)) != (512, 2880, 18, 2) or boot[-2:] != b"\x55\xaa":
            raise ValueError("1.44MB FAT12の管理情報を認識できません。診断・イメージ保存・フォーマットは別操作です。")
        if not (self.spc in (1, 2, 4, 8, 16, 32, 64) and 1 <= self.first_fat <= 32
                and self.fats in (1, 2) and 0 < self.fat_length <= 32
                and 0 < self.root_sectors <= 64 and self.first_data < SECTORS):
            raise ValueError("FAT12の構成が不正です。")
        self.limit = 2 + (SECTORS - self.first_data) // self.spc
        self.cluster_size = SIZE * self.spc
        if self.limit >= 0xFF0 or ((self.limit * 3 + 1) // 2) > self.fat_length * SIZE:
            raise ValueError("FAT12のクラスタ数が不正です。")
        blocks = drive.read_many(range(self.first_fat, self.root_start), "FATを読み取り中")
        self.original = blocks.copy()
        self.fat = bytearray(b"".join(blocks[i] for i in range(self.first_fat, self.first_fat + self.fat_length)))
        self.fats_match = all(
            blocks[self.first_fat + n * self.fat_length + i] == self.fat[i * SIZE:(i + 1) * SIZE]
            for n in range(1, self.fats) for i in range(self.fat_length)
        )

    def get_fat(self, cluster):
        pos = cluster + cluster // 2
        pair = u16(self.fat, pos)
        return (pair >> 4 if cluster & 1 else pair) & 4095

    @staticmethod
    def set_fat(fat, cluster, value):
        pos = cluster + cluster // 2
        old = u16(fat, pos)
        pair = (old & 15) | (value << 4) if cluster & 1 else (old & 0xF000) | value
        struct.pack_into("<H", fat, pos, pair)

    @property
    def free(self):
        return sum(self.get_fat(c) == 0 for c in range(2, self.limit)) * self.cluster_size

    @property
    def capacity(self):
        return (self.limit - 2) * self.cluster_size

    def chain(self, cluster):
        result, seen = [], set()
        if not cluster:
            return result
        while cluster < 0xFF8:
            if not 2 <= cluster < self.limit or cluster in seen:
                raise ValueError("FATのクラスタ参照が壊れています。")
            seen.add(cluster)
            result.append(cluster)
            cluster = self.get_fat(cluster)
        return result

    def addresses(self, chain):
        return [self.first_data + (c - 2) * self.spc + i for c in chain for i in range(self.spc)]

    def directory(self, cluster=0):
        sectors = self.addresses(self.chain(cluster)) if cluster else list(range(self.root_start, self.first_data))
        if not sectors:
            raise ValueError("フォルダーが見つかりません。")
        blocks = self.drive.read_many(sectors, "ファイル一覧を読み取り中")
        self.original.update(blocks)
        data = bytearray(b"".join(blocks[i] for i in sectors))
        return Directory(sectors, data, list(entries_of(data)))

    def extract(self, entry, path):
        if entry.folder:
            raise ValueError("ファイルを選択してください。")
        sectors = self.addresses(self.chain(entry.cluster))
        count = (entry.size + 511) // 512
        if len(sectors) < count:
            raise ValueError("ファイルのクラスタ参照が不足しています。")
        sectors = sectors[:count]
        blocks = self.drive.read_many(sectors, "ファイルを取り出し中")
        self.drive.check()
        atomic_save(path, b"".join(blocks[i] for i in sectors)[:entry.size])

    def changes(self, directory, new_data, fat, delete=False):
        d = [(address, bytes(new_data[i * SIZE:(i + 1) * SIZE]))
             for i, address in enumerate(directory.sectors)]
        f = [(self.first_fat + n * self.fat_length + i, bytes(fat[i * SIZE:(i + 1) * SIZE]))
             for n in reversed(range(self.fats)) for i in range(self.fat_length)]
        return [(i, data) for i, data in (d + f if delete else f + d) if data != self.original[i]]

    def commit(self, changes):
        self.drive.check()
        changed = []
        try:
            for i, (address, data) in enumerate(changes, 1):
                changed.append(address)
                self.drive.sector("W", address, data, check_cancel=False)
                self.drive.progress("管理情報を確定中", i, len(changes))
        except Exception as error:
            for address in reversed(changed):
                try:
                    self.drive.sector("W", address, self.original[address], check_cancel=False)
                except Exception as rollback:
                    raise RuntimeError(f"管理情報の更新と復元に失敗しました。以後の書き込みを止めてください。\n{error}\n{rollback}") from error
            raise RuntimeError(f"管理情報の更新に失敗し、元に戻しました。\n{error}") from error
        self.original.update(changes)

    def add(self, directory, name, data):
        if not self.fats_match:
            raise ValueError("2つのFATが一致しないため、ファイルの変更を中止しました。")
        if any(e.name.casefold() == name.casefold() for e in directory.entries):
            raise ValueError(f"同名ファイルがあります: {name}")
        need = (len(data) + self.cluster_size - 1) // self.cluster_size
        chain = [c for c in range(2, self.limit) if self.get_fat(c) == 0][:need]
        if len(chain) != need:
            raise ValueError(f"空き容量が足りません（空き {self.free:,} B / ファイル {len(data):,} B）。")
        entries = encode_entries(name, {e.short for e in directory.entries}, chain[0] if chain else 0, len(data))
        root = bytearray(directory.data)
        run = 0
        ended = False
        start = None
        for offset in range(0, len(root), 32):
            ended = ended or root[offset] == 0
            run = run + 32 if ended or root[offset] == 0xE5 else 0
            if run == len(entries):
                start = offset + 32 - run
                break
        if start is None:
            raise ValueError("ファイル名を登録するための連続した空き項目がありません。")
        old_end = next((i for i in range(0, len(root), 32) if root[i] == 0), len(root))
        root[start:start + len(entries)] = entries
        if start <= old_end < start + len(entries) < len(root):
            root[start + len(entries):start + len(entries) + 32] = bytes(32)
        fat = bytearray(self.fat)
        for i, cluster in enumerate(chain):
            self.set_fat(fat, cluster, chain[i + 1] if i + 1 < len(chain) else 0xFFF)
        sectors = self.addresses(chain)
        desired = {address: data[i * SIZE:(i + 1) * SIZE].ljust(SIZE, b"\0") for i, address in enumerate(sectors)}
        ordered = self.drive.order(sectors)
        for i, address in enumerate(ordered, 1):
            self.drive.sector("w", address, desired[address])
            self.drive.progress(f"{name} を書き込み中", i, len(ordered))
        readback = self.drive.read_many(sectors, "ファイル全体を読み戻し・照合中")
        if readback != desired:
            raise ValueError("書き込み後の照合に失敗したため、ファイルは登録しませんでした。")
        self.commit(self.changes(directory, root, fat))
        self.fat = fat

    def delete(self, directory, entry):
        if not self.fats_match or entry.folder or entry.attr & 1:
            raise ValueError("フォルダー、読み取り専用ファイル、またはFATが不一致のディスクは削除できません。")
        chain = self.chain(entry.cluster)
        for other in directory.entries:
            if other != entry and set(chain).intersection(self.chain(other.cluster)):
                raise ValueError("別の項目とクラスタを共有しているため削除を中止しました。")
        new_data = bytearray(directory.data)
        for offset in entry.slots:
            new_data[offset] = 0xE5
        fat = bytearray(self.fat)
        for cluster in chain:
            self.set_fat(fat, cluster, 0)
        self.commit(self.changes(directory, new_data, fat, delete=True))
