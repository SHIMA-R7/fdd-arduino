"""Serve a 1.44 MB floppy to the local ImDisk DevIO client, read-only."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout, suppress
import errno
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from threading import Event, Lock
import time

from fdd_core import Drive, Fat12, IMAGE_SIZE, SIZE, SectorError

MOUNT_POINT = "A:"
POLL_INTERVAL = 1
RETRY_INTERVAL = 4
HIDE_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class MediaChanged(OSError):
    pass


class MountedMedia:
    def __init__(self, drive, generation):
        self.drive = drive
        self.generation = generation
        self.lock = Lock()
        self.changed = Event()

    def check(self):
        if self.changed.is_set():
            raise MediaChanged()
        try:
            present, generation = self.drive.request("E", timeout=2)
            same_disk = present == 1 and generation == self.generation
        except (SectorError, OSError, ValueError):
            same_disk = False
        if not same_disk:
            self.changed.set()
            raise MediaChanged()

    def read_many(self, sectors, label):
        with self.lock:
            self.check()
            try:
                return self.drive.read_many(sectors, label)
            finally:
                self.check()


def serve_client(stream, drive):
    # DevIO uses little-endian 64-bit fields. A plain host:port needs no
    # CONNECT request. The INFO response advertises 512-byte aligned reads.
    while header := stream.read(8):
        request, = struct.unpack("<Q", header)
        if request == 1:  # INFO
            stream.write(struct.pack("<QQQ", IMAGE_SIZE, SIZE, 1))
        elif request == 2:  # READ
            offset, length = struct.unpack("<QQ", stream.read(16))
            if offset % SIZE or length % SIZE or offset + length > IMAGE_SIZE:
                stream.write(struct.pack("<QQ", errno.EINVAL, 0))
            else:
                try:
                    sectors = range(offset // SIZE, (offset + length) // SIZE)
                    blocks = drive.read_many(sectors, "")
                    data = b"".join(blocks[address] for address in sectors)
                except MediaChanged:
                    stream.write(struct.pack("<QQ", errno.EIO, 0))
                except SectorError as error:
                    print(error, file=sys.stderr, flush=True)
                    stream.write(struct.pack("<QQ", errno.EIO, 0))
                else:
                    stream.write(struct.pack("<QQ", 0, len(data)) + data)
        elif request == 3:  # WRITE
            # The client sees a write-protected volume in INFO. Close the
            # connection on an unexpected write instead of accessing the FD.
            return
        elif request == 5:  # CLOSE
            return
        else:
            raise ValueError(f"未対応のDevIOリクエスト ({request})")
        stream.flush()


def imdisk_args(listener):
    endpoint = f"127.0.0.1:{listener.getsockname()[1]}"
    return ["-a", "-t", "proxy", "-o", "ip,fd,ro", "-f", endpoint, "-m", MOUNT_POINT]


def mount_until_changed(listener, media, volume, imdisk):
    args = imdisk_args(listener)
    endpoint = f"127.0.0.1:{listener.getsockname()[1]}".encode("ascii")
    with subprocess.Popen([imdisk, *args], creationflags=HIDE_CONSOLE) as attach:
        try:
            client, _ = listener.accept()
            with client, ThreadPoolExecutor(max_workers=1) as pool:
                def serve():
                    with client.makefile("rwb") as stream:
                        serve_client(stream, media)

                serving = pool.submit(serve)
                try:
                    if attach.wait(timeout=25):
                        raise OSError(f"{MOUNT_POINT} を割り当てられませんでした。")
                    print(f"{MOUNT_POINT} に {volume.label} を割り当てました。", flush=True)
                    while not media.changed.wait(POLL_INTERVAL) and not serving.done():
                        with media.lock:
                            media.check()
                    if not media.changed.is_set():
                        serving.result()
                except MediaChanged:
                    pass
                finally:
                    try:
                        # Check ownership before removing a drive letter.
                        status = subprocess.run(
                            [imdisk, "-l", "-m", MOUNT_POINT], capture_output=True,
                            timeout=10, creationflags=HIDE_CONSOLE
                        )
                        if status.returncode == 0 and endpoint in status.stdout:
                            flag = "-D" if media.changed.is_set() else "-d"
                            subprocess.run(
                                [imdisk, flag, "-m", MOUNT_POINT], check=True,
                                timeout=15, creationflags=HIDE_CONSOLE
                            )
                    finally:
                        with suppress(OSError):
                            client.shutdown(socket.SHUT_RDWR)
        finally:
            if attach.poll() is None:
                attach.kill()
    return media.changed.is_set()


def watch_media(listener, port):
    imdisk = Path(os.environ["WINDIR"]) / "System32" / "imdisk.exe"
    if not imdisk.is_file():
        raise ValueError("ImDiskが見つかりません。")
    listener.settimeout(12)
    last_error = None
    while True:
        try:
            with Drive(port, fast=False) as drive:
                while True:
                    present, generation = drive.request("E", timeout=2)
                    if present == 1:
                        break
                    time.sleep(POLL_INTERVAL)
                media = MountedMedia(drive, generation)
                volume = Fat12(drive)
                media.check()
                last_error = None
                if not mount_until_changed(listener, media, volume, imdisk):
                    return 0
        except (OSError, ValueError, SectorError, subprocess.SubprocessError) as error:
            message = str(error)
            if message != last_error:
                print(f"{port} のディスクを待機中 ({message})", flush=True)
                last_error = message
        time.sleep(RETRY_INTERVAL)


def run(args):
    try:
        with socket.create_server(("127.0.0.1", args.listen_port)) as listener:
            if args.auto:
                return watch_media(listener, args.port)
            # Windows may wait between reads longer than the firmware's
            # timeout for reverting a negotiated baud rate.
            with Drive(args.port, fast=False) as drive:
                volume = Fat12(drive)
                print(f"{args.port} の {volume.label} を読み取り専用で待機中。", flush=True)
                print("imdisk " + " ".join(imdisk_args(listener)), flush=True)
                client, _ = listener.accept()
                with client, client.makefile("rwb") as stream:
                    serve_client(stream, drive)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, SectorError, struct.error) as error:
        print(f"マウント接続を終了しました: {error}", file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="COM8", help="FDD基板のCOMポート")
    parser.add_argument("--listen-port", type=int, default=19876)
    parser.add_argument("--auto", action="store_true", help="ディスクの挿入を監視しA:を自動で更新")
    parser.add_argument("--log", type=Path, help="ログの保存先")
    args = parser.parse_args()
    if args.log is None:
        return run(args)
    with args.log.open("a", encoding="utf-8", buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
        return run(args)


if __name__ == "__main__":
    sys.exit(main())
