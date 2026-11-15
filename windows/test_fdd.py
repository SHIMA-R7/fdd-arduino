import binascii
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from fdd_core import Cancelled, DMF_IMAGE_SIZE, Drive, Fat12, IMAGE_SIZE, SIZE, X68_IMAGE_SIZE, SectorError, blank_metadata, order_sectors


class ImageDrive:
    def __init__(self):
        self.blocks = {i: bytes(SIZE) for i in range(2880)}
        self.blocks.update(blank_metadata("TESTDISK"))
        self.fail_address = None
        self.fail_once = False
        self.write_log = []
        self.cancel_at = None

    def check(self):
        if self.cancel_at is not None and len(self.write_log) >= self.cancel_at:
            raise Cancelled()

    def progress(self, *args):
        pass

    def read(self, address):
        return self.blocks[address]

    def read_many(self, addresses, label):
        return {i: self.blocks[i] for i in addresses}

    def order(self, addresses):
        return order_sectors(addresses)

    def sector(self, op, address, data=None, check_cancel=True):
        if check_cancel:
            self.check()
        if address == self.fail_address and self.fail_once:
            self.fail_once = False
            raise SectorError(op, address, 8)
        self.write_log.append(address)
        self.blocks[address] = data


class FatTests(unittest.TestCase):
    def test_long_name_round_trip_and_delete(self):
        d = ImageDrive()
        v = Fat12(d)
        name = "日本語の長い名前📼_FLOPPY.webm"
        data = bytes(range(256)) * 5 + b"end"
        v.add(v.directory(), name, data)
        r = Fat12(d)
        root = r.directory()
        self.assertEqual(len(root.entries), 1)
        self.assertEqual(root.entries[0].name, name)
        self.assertEqual(root.entries[0].size, len(data))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "extracted.webm"
            r.extract(root.entries[0], path)
            self.assertEqual(path.read_bytes(), data)
        r.delete(root, root.entries[0])
        v = Fat12(d)
        self.assertEqual(v.directory().entries, [])
        self.assertEqual(v.free, v.capacity)

    def test_preserve_existing_file_and_fragmented_cluster_chain(self):
        d = ImageDrive()
        v = Fat12(d)
        v.add(v.directory(), "FIRST.TXT", b"old")
        v.add(v.directory(), "KEEP.TXT", b"keep")
        r = Fat12(d)
        root = r.directory()
        r.delete(root, root.entries[0])
        r = Fat12(d)
        data = b"A" * 512 + b"B" * 512 + b"C" * 33
        r.add(r.directory(), "new file.bin", data)
        r = Fat12(d)
        entries = {e.name: e for e in r.directory().entries}
        self.assertEqual(r.chain(entries["new file.bin"].cluster), [2, 4, 5])
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "file"
            r.extract(entries["new file.bin"], p)
            self.assertEqual(p.read_bytes(), data)
            r.extract(entries["KEEP.TXT"], p)
            self.assertEqual(p.read_bytes(), b"keep")

    def test_capacity_and_duplicate_refused_before_writing(self):
        d = ImageDrive()
        v = Fat12(d)
        v.add(v.directory(), "A.TXT", b"old")
        before = len(d.write_log)
        with self.assertRaisesRegex(ValueError, "同名"):
            v.add(v.directory(), "a.txt", b"new")
        with self.assertRaisesRegex(ValueError, "空き容量"):
            v.add(v.directory(), "TOOBIG.BIN", bytes(IMAGE_SIZE))
        self.assertEqual(len(d.write_log), before)

    def test_cancel_before_commit_leaves_metadata_unchanged(self):
        d = ImageDrive()
        v = Fat12(d)
        d.cancel_at = 1
        with self.assertRaises(Cancelled):
            v.add(v.directory(), "CANCEL.BIN", b"x" * 1024)
        self.assertEqual(Fat12(d).directory().entries, [])
        self.assertEqual(Fat12(d).free, v.capacity)

    def test_metadata_rollback_after_failure(self):
        d = ImageDrive()
        v = Fat12(d)
        before = {i: d.blocks[i] for i in range(33)}
        d.fail_address, d.fail_once = 19, True
        with self.assertRaisesRegex(RuntimeError, "元に戻しました"):
            v.add(v.directory(), "ROLLBACK.BIN", b"y" * 1024)
        self.assertEqual({i: d.blocks[i] for i in range(33)}, before)

    def test_mismatched_fat_refuses_mutation(self):
        d = ImageDrive()
        d.blocks[10] = d.blocks[10][:-1] + b"\xff"
        v = Fat12(d)
        with self.assertRaisesRegex(ValueError, "一致しない"):
            v.add(v.directory(), "SAFE.TXT", b"x")
        self.assertEqual(d.write_log, [])

    def test_empty_file(self):
        d = ImageDrive()
        v = Fat12(d)
        v.add(v.directory(), "EMPTY.TXT", b"")
        e = v.directory().entries[0]
        self.assertEqual((e.cluster, e.size), (0, 0))

    def test_transfer_order_and_geometry(self):
        ordered = order_sectors(range(2880))
        self.assertEqual(ordered[:5], [0, 7, 14, 3, 10])
        self.assertEqual(sorted(ordered), list(range(2880)))


class FirmwarePort:
    """Simulated firmware with a backing image; deliberately splits replies."""
    def __init__(self, *, high_speed=False, corrupt_at=None, music=False, dmf=False, x68=False):
        self.disk = bytearray(X68_IMAGE_SIZE if x68 else DMF_IMAGE_SIZE if dmf else IMAGE_SIZE)
        self.rx = bytearray()
        self.commands = []
        self.closed = False
        self.corrupt_reply = False
        self.high_speed = high_speed
        self.corrupt_at = corrupt_at
        self.music = music
        self.dmf = dmf
        self.x68 = x68
        self.sectors_per_side = 18
        self.sector_size = SIZE
        self.baudrate = 115200

    def reset_input_buffer(self):
        self.rx.clear()

    def close(self):
        self.closed = True

    def read(self, size):
        size = min(size, 71)
        result = self.rx[:size]
        del self.rx[:size]
        return result

    def write(self, packet):
        assert packet[0] == 0xA5
        op = chr(packet[1])
        lba = struct.unpack_from("<H", packet, 2)[0]
        self.commands.append((op, lba))
        if ((op in ("H", "G", "D") and not self.high_speed) or
                (op == "M" and not self.music) or
                (op == "J" and not (self.x68 if lba == 2 else self.dmf))):
            self.rx.extend(bytes((0x5A, 99)))
            return
        self.rx.extend(bytes((0x5A, 0)))
        if op in ("F", "G"):
            first, last = (lba & 255, lba >> 8) if op == "G" else (lba, lba)
            cylinder_bytes = self.sectors_per_side * 2 * self.sector_size
            start, end = first * cylinder_bytes, (last + 1) * cylinder_bytes
            self.disk[start:end] = b"\xf6" * (end - start)
        elif op in ("w", "W", "T"):
            data = packet[4:-2]
            assert len(data) == (SIZE if op == "T" else self.sector_size)
            assert binascii.crc_hqx(data, 0xFFFF) == struct.unpack("<H", packet[-2:])[0]
            if op == "T":
                crc = binascii.crc_hqx(data, 0xFFFF) ^ (self.baudrate == self.corrupt_at)
                self.rx.extend(data + struct.pack("<H", crc))
            else:
                self.disk[lba * self.sector_size:(lba + 1) * self.sector_size] = data
        elif op in ("R", "D"):
            assert op != "D" or lba % self.sectors_per_side != self.sectors_per_side - 1
            for address in range(lba, lba + (2 if op == "D" else 1)):
                data = self.disk[address * self.sector_size:(address + 1) * self.sector_size]
                crc = binascii.crc_hqx(data, 0xFFFF) ^ self.corrupt_reply
                self.rx.extend(data + struct.pack("<H", crc))
        elif op == "J":
            self.sectors_per_side = {0: 18, 1: 21, 2: 8}[lba]
            self.sector_size = 1024 if lba == 2 else SIZE
        elif op == "O":
            self.sectors_per_side = 18
            self.sector_size = SIZE
        elif op not in ("H", "M"):
            raise AssertionError(op)


class TransferTests(unittest.TestCase):
    def test_xdf_restores_1024_byte_sectors_and_77_cylinders(self):
        port = FirmwarePort(high_speed=True, x68=True)
        image = b"".join(struct.pack("<H", address) * 512 for address in range(1232))
        self.assertEqual(len(image), X68_IMAGE_SIZE)
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False) as drive:
                drive.restore_image(image, reformat=True)
                self.assertEqual((drive.sector_size, drive.cylinders), (512, 80))
        self.assertEqual(port.disk, image)
        self.assertIn(("G", 70 | 76 << 8), port.commands)
        self.assertEqual(port.commands.count(("R", 1231)), 2)
        self.assertFalse(any(op == "D" for op, _ in port.commands))

    def test_dmf_restore_checks_last_sector_and_restores_standard_mode(self):
        port = FirmwarePort(high_speed=True, dmf=True)
        image = b"".join(struct.pack("<H", address) * (SIZE // 2)
                         for address in range(DMF_IMAGE_SIZE // SIZE))
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False) as drive:
                drive.restore_image(image, reformat=True)
                self.assertEqual(drive.sectors_per_side, 18)
        self.assertEqual(port.disk, image)
        self.assertIn(("w", 3359), port.commands)
        self.assertEqual(port.commands.count(("R", 3359)), 2)
        self.assertIn(("D", 3339), port.commands)
        self.assertEqual(port.sectors_per_side, 18)

    def test_old_firmware_rejects_dmf_before_mutating_disk(self):
        port = FirmwarePort(high_speed=True)
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False) as drive:
                with self.assertRaisesRegex(ValueError, "DMF対応"):
                    drive.restore_image(bytes(DMF_IMAGE_SIZE), reformat=True)
        self.assertFalse(any(op in ("F", "G", "w", "W") for op, _ in port.commands))

    def test_gui_music_fits_high_notes_into_drive_range(self):
        port = FirmwarePort(music=True)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep", sleep), \
                patch("fdd_core.time.monotonic", side_effect=lambda: clock[0]):
            with Drive("COM99", low_latency=False, fast=False) as drive:
                drive.play_notes([(523, 20), (784, 20), (0, 20)], fit_drive_range=True)
        self.assertEqual([frequency for op, frequency in port.commands if op == "M"], [262, 196, 0])

    def test_music_refreshes_long_notes_then_releases_drive_for_disk_io(self):
        port = FirmwarePort(high_speed=True, music=True)
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep", sleep), \
                patch("fdd_core.time.monotonic", side_effect=lambda: clock[0]):
            with Drive("COM99", low_latency=False) as drive:
                drive.play_notes([(440, 800), (0, 50)])
                self.assertEqual(drive.read(0), bytes(SIZE))
        first_note = port.commands.index(("M", 440))
        read = port.commands.index(("R", 0))
        self.assertEqual(port.commands[first_note:read], [("M", 440)] * 3 + [("M", 0), ("O", 0)])

    def test_old_firmware_refuses_music_and_still_allows_disk_io(self):
        port = FirmwarePort(high_speed=True)
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False) as drive:
                with self.assertRaisesRegex(ValueError, "演奏対応"):
                    drive.play_notes([(440, 100)])
                self.assertEqual(drive.read(0), bytes(SIZE))

    def test_format_and_file_copy_via_serial_protocol(self):
        port = FirmwarePort()
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False) as d:
                self.assertEqual(d.baudrate, 115200)
                d.format_fat12("NEW DISK")
                volume = Fat12(d)
                self.assertEqual(volume.label, "NEW DISK")
                self.assertEqual(volume.directory().entries, [])
                volume.add(volume.directory(), "Video on floppy.webm", b"data" * 300)
                self.assertEqual(volume.directory().entries[0].name, "Video on floppy.webm")
        self.assertEqual([address for op, address in port.commands if op == "F"], list(range(80)))
        self.assertEqual(port.commands[-1], ("O", 0))
        self.assertTrue(port.closed)

    def test_full_image_restore_and_backup(self):
        port = FirmwarePort()
        image = bytes(range(256)) * (IMAGE_SIZE // 256)
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False) as d, tempfile.TemporaryDirectory() as directory:
                d.restore_image(image, reformat=True)
                path = Path(directory) / "backup.img"
                d.save_image(path)
                self.assertEqual(path.read_bytes(), image)
        self.assertEqual(port.commands[-1], ("O", 0))

    def test_serial_crc_failure_still_stops_motor(self):
        port = FirmwarePort()
        port.corrupt_reply = True
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with self.assertRaisesRegex(ValueError, "CRC"):
                with Drive("COM99", low_latency=False) as d:
                    d.read(0)
        self.assertEqual(port.commands[-1], ("O", 0))
        self.assertTrue(port.closed)

    def test_fast_connection_checks_both_directions_and_resets_speed(self):
        port = FirmwarePort(high_speed=True)
        with patch("fdd_core.FtdiLink.open_port", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", max_baudrate=1000000) as d:
                self.assertEqual(d.baudrate, 1000000)
                self.assertEqual(d.order(range(18))[:5], [0, 2, 4, 6, 8])
                port.disk[16 * SIZE:19 * SIZE] = b"a" * SIZE + b"b" * SIZE + b"c" * SIZE
                self.assertEqual(d.read_many([16, 17, 18], ""), {16: b"a" * SIZE, 17: b"b" * SIZE, 18: b"c" * SIZE})
        self.assertEqual(port.commands[:5], [("O", 0), ("H", 3), ("T", 0), ("T", 0), ("T", 0)])
        self.assertEqual(port.commands[-2:], [("O", 0), ("H", 0)])
        self.assertIn(("D", 16), port.commands)
        self.assertIn(("R", 18), port.commands)

    def test_bad_fast_crc_retries_at_lower_speed(self):
        port = FirmwarePort(high_speed=True, corrupt_at=1000000)
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False, max_baudrate=1000000) as d:
                self.assertEqual(d.baudrate, 500000)
                self.assertEqual(d.read(0), bytes(SIZE))
        self.assertEqual([address for op, address in port.commands if op == "H"], [3, 2, 0])

    def test_full_format_uses_ranges_and_quick_format_preserves_bad_clusters(self):
        port = FirmwarePort(high_speed=True)
        with patch("fdd_core.serial.Serial", return_value=port), patch("fdd_core.time.sleep"):
            with Drive("COM99", low_latency=False) as d:
                d.format_fat12("FIRST")
                volume = Fat12(d)
                volume.add(volume.directory(), "OLD.TXT", b"old")
                fat = Fat12(d).fat
                Fat12.set_fat(fat, 57, 0xFF7)
                for address in (1, 10):
                    d.sector("W", address, fat[:SIZE])
                old_data = bytes(port.disk[33 * SIZE:])
                before = len(port.commands)
                d.format_fat12("QUICK", quick=True)
                volume = Fat12(d)
                self.assertEqual(volume.label, "QUICK")
                self.assertEqual(volume.directory().entries, [])
                self.assertEqual(volume.get_fat(57), 0xFF7)
                self.assertTrue(volume.fats_match)
                self.assertEqual(port.disk[33 * SIZE:], old_data)
        self.assertEqual([address for op, address in port.commands if op == "G"],
                         [first | (first + 9) << 8 for first in range(0, 80, 10)])
        self.assertFalse(any(op in ("F", "G") for op, _ in port.commands[before:]))


if __name__ == "__main__":
    unittest.main()
