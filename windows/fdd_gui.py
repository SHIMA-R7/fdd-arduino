"""Windows desktop interface for the ATmega328P floppy controller."""

from dataclasses import dataclass
from pathlib import Path
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from serial.tools import list_ports

from fdd_core import Cancelled, Drive, Fat12, IMAGE_SIZE, SECTORS, blank_metadata

BG = "#edf2f8"
WHITE = "#ffffff"
INK = "#17263c"
MUTED = "#536780"
NAVY = "#152844"
BLUE = "#2857c5"
PALE = "#e0eafd"


@dataclass
class View:
    disk: str
    label: str
    free: int
    capacity: int
    fats_match: bool
    directory: object
    trail: tuple


def read_view(volume, trail=()):
    directory = volume.directory(trail[-1][1] if trail else 0)
    return View(volume.disk_id, volume.label, volume.free, volume.capacity,
                volume.fats_match, directory, trail)


def stat_size(n):
    return f"{n / 1024:,.1f} KiB"


class App:
    def __init__(self, root):
        self.root = root
        self.port = tk.StringVar()
        self.fast = tk.BooleanVar(value=True)
        self.link_label = tk.StringVar(value="")
        self.path = tk.StringVar(value="\\")
        self.disk = tk.StringVar(value="ディスク情報なし")
        self.space = tk.StringVar(value="一覧を更新すると空き容量を表示")
        self.status = tk.StringVar(value="COMポートを選び、［一覧を更新］を押してください。")
        self.summary = tk.StringVar(value="—")
        self.current = None
        self.rows = {}
        self.busy = False
        self.close_after = False
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.buttons = {}
        self.setup()
        self.rescan()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self.receive)

    def setup(self):
        r = self.root
        r.title("フロッピー・ツール 高速版 | ATmega FDD")
        r.geometry("1060x700")
        r.minsize(890, 640)
        r.configure(bg=BG)
        r.option_add("*Font", ("Meiryo UI", 10))
        menu = tk.Menu(r)
        self.communication = tk.Menu(menu, tearoff=False)
        self.communication.add_checkbutton(label="高速通信を使う", variable=self.fast,
                                           command=lambda: self.link_label.set(""))
        self.communication.add_command(label="読み取り速度を測る", command=self.benchmark)
        menu.add_cascade(label="通信", menu=self.communication)
        r.configure(menu=menu)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", font=("Meiryo UI", 10))
        style.configure("TButton", padding=(11, 8), background=PALE, foreground=INK, borderwidth=0)
        style.map("TButton", background=[("disabled", "#e8edf4"), ("active", "#c9dafb")],
                  foreground=[("disabled", "#8393a9")])
        style.configure("Primary.TButton", background=BLUE, foreground=WHITE)
        style.configure("Compact.TButton", padding=(9, 4))
        style.map("Primary.TButton", background=[("disabled", "#a8b6d4"), ("active", "#2046a7")],
                  foreground=[("disabled", WHITE)])
        style.configure("Treeview", background=WHITE, fieldbackground=WHITE, foreground=INK,
                        rowheight=32, borderwidth=0)
        style.configure("Treeview.Heading", padding=(8, 9), background="#e6edf7", foreground=INK)
        style.map("Treeview", background=[("selected", "#d7e6ff")], foreground=[("selected", INK)])
        style.configure("Horizontal.TProgressbar", thickness=7, troughcolor="#dbe5f2", background=BLUE, borderwidth=0)

        top = tk.Frame(r, bg=NAVY, padx=24, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="フロッピー・ツール", font=("Meiryo UI", 20, "bold"),
                 fg=WHITE, bg=NAVY).pack(anchor="w")
        tk.Label(top, text="ATmega328P  •  3.5インチ 2HD / 1.44MB", font=("Meiryo UI", 10),
                 fg="#b7c9e5", bg=NAVY).pack(anchor="w", pady=(4, 0))
        tk.Label(top, textvariable=self.link_label, fg="#b7c9e5", bg=NAVY).place(relx=1, anchor="ne")

        footer = tk.Frame(r, bg=BG, padx=21, pady=8)
        footer.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(footer, mode="determinate")
        self.progress.pack(fill="x")
        line = tk.Frame(footer, bg=BG)
        line.pack(fill="x", pady=(7, 0))
        tk.Label(line, textvariable=self.status, bg=BG, fg=MUTED, anchor="w").pack(side="left", fill="x", expand=True)
        self.stop = ttk.Button(line, text="中止", command=self.stop_work, state="disabled")
        self.stop.pack(side="right", padx=(8, 0))

        body = tk.Frame(r, bg=BG, padx=20, pady=10)
        body.pack(fill="both", expand=True)
        side = tk.Frame(body, bg=WHITE, padx=13, pady=13, width=218)
        side.pack(side="left", fill="y", padx=(0, 14))
        side.pack_propagate(False)
        self.label(side, "接続", bold=True).pack(anchor="w", pady=(0, 7))
        portline = tk.Frame(side, bg=WHITE)
        portline.pack(fill="x", pady=(0, 7))
        self.portbox = ttk.Combobox(portline, textvariable=self.port, width=9, state="readonly")
        self.portbox.pack(side="left", fill="x", expand=True)
        self.portbox.bind("<<ComboboxSelected>>", lambda _: self.invalidate())
        self.portbox.bind("<<ComboboxSelected>>", lambda _: self.link_label.set(""), add="+")
        self.buttons["rescan"] = ttk.Button(portline, text="再検出", command=self.rescan, width=6, style="Compact.TButton")
        self.buttons["rescan"].pack(side="left", padx=(6, 0))
        self.button(side, "probe", "基板チェック", self.probe, compact=True).pack(fill="x", pady=(0, 8))

        self.label(side, "ディスク", bold=True).pack(anchor="w", pady=(9, 5))
        self.label(side, variable=self.disk, wrap=182).pack(anchor="w")
        self.usage = ttk.Progressbar(side, maximum=100)
        self.usage.pack(fill="x", pady=(7, 5))
        self.label(side, variable=self.space, small=True, wrap=182).pack(anchor="w")
        self.label(side, "ディスク操作", bold=True).pack(anchor="w", pady=(15, 5))
        for key, title, fn in (
            ("backup", "イメージを保存…", self.backup),
            ("restore", "イメージを復元…", self.restore),
            ("scan", "全セクターを検査", self.scan),
            ("sector", "セクターを表示…", self.inspect_sector),
            ("format", "フォーマット…", self.format),
        ):
            self.button(side, key, title, fn, compact=True).pack(fill="x", pady=2)

        main = tk.Frame(body, bg=WHITE, padx=13, pady=13)
        main.pack(side="left", fill="both", expand=True)
        row = tk.Frame(main, bg=WHITE)
        row.pack(fill="x")
        for key, title, fn in (
            ("refresh", "一覧を更新", self.refresh),
            ("add", "ファイルを追加…", self.add),
            ("extract", "取り出す…", self.extract),
            ("delete", "削除…", self.delete),
        ):
            self.button(row, key, title, fn, primary=key == "refresh").pack(side="left", padx=(0, 6))
        location = tk.Frame(main, bg=WHITE)
        location.pack(fill="x", pady=(12, 7))
        self.button(location, "parent", "上へ", self.parent).pack(side="left")
        self.label(location, variable=self.path, wrap=520).pack(side="left", padx=10)
        tree_wrap = tk.Frame(main, bg=WHITE)
        tree_wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_wrap, columns=("kind", "size", "date"), selectmode="browse")
        self.tree.heading("#0", text="名前", anchor="w")
        for column, title, width in (("kind", "種類", 85), ("size", "サイズ (B)", 100), ("date", "更新日時", 141)):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, minwidth=66, stretch=False, anchor="e" if column == "size" else "center")
        self.tree.column("#0", width=270, minwidth=130)
        scrollbar = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("folder", foreground=BLUE)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.enable())
        self.tree.bind("<Double-1>", self.double_click)
        self.tree.bind("<Return>", self.double_click)
        self.label(main, variable=self.summary, small=True).pack(anchor="w", pady=(6, 0))

    def label(self, parent, text="", variable=None, bold=False, small=False, wrap=0):
        return tk.Label(parent, text=text, textvariable=variable, bg=WHITE, fg=MUTED if small else INK,
                        font=("Meiryo UI", 9 if small else 11 if bold else 10, "bold" if bold else "normal"),
                        anchor="w", justify="left", wraplength=wrap)

    def button(self, parent, key, title, fn, primary=False, compact=False):
        theme = "Primary.TButton" if primary else "Compact.TButton" if compact else "TButton"
        widget = ttk.Button(parent, text=title, command=fn, style=theme)
        self.buttons[key] = widget
        return widget

    def rescan(self):
        ports = sorted(list_ports.comports(), key=lambda p: (not (p.vid == 0x0403 and p.pid == 0x6015), p.device))
        names = [p.device for p in ports if p.vid is not None or not p.hwid.startswith("BTHENUM")]
        self.portbox.configure(values=names)
        old = self.port.get()
        if old not in names:
            self.port.set(names[0] if names else "")
            self.link_label.set("")
            self.invalidate()
        self.status.set(f"{len(names)}個のCOMポートを検出。ポートを選択してください。" if names else "USBシリアルを接続し、再検出してください。")
        self.enable()

    def invalidate(self):
        self.current = None
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.disk.set("ディスク情報なし")
        self.space.set("一覧を更新すると空き容量を表示")
        self.usage["value"] = 0
        self.summary.set("—")
        self.path.set("\\")
        self.enable()

    def show_view(self, view):
        self.current = view
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.path.set("\\" + "\\".join(name for name, _ in view.trail))
        self.disk.set(view.label or "1.44MB FAT12")
        self.space.set(f"空き {stat_size(view.free)} / {stat_size(view.capacity)}")
        self.usage["value"] = 100 * (1 - view.free / view.capacity)
        items = sorted(view.directory.entries, key=lambda e: (not e.folder, e.name.casefold()))
        for e in items:
            iid = self.tree.insert("", "end", text=e.name,
                                   values=("フォルダー" if e.folder else "ファイル", "" if e.folder else f"{e.size:,}", e.timestamp),
                                   tags=("folder",) if e.folder else ())
            self.rows[iid] = e
        self.summary.set(f"{len(items)}項目" + ("  •  FAT不一致：読み取り専用" if not view.fats_match else ""))
        self.enable()

    def selected(self):
        rows = self.tree.selection()
        return self.rows.get(rows[0]) if rows else None

    def enable(self):
        available = not self.busy and bool(self.port.get())
        e = self.selected()
        view = self.current
        requirements = {
            "rescan": not self.busy,
            "parent": available and view and view.trail,
            "add": available and view and view.fats_match,
            "extract": available and e and not e.folder,
            "delete": available and view and view.fats_match and e and not e.folder and not (e.attr & 1),
        }
        for key, button in self.buttons.items():
            button.configure(state="normal" if requirements.get(key, available) else "disabled")
        self.portbox.configure(state="disabled" if self.busy else "readonly")
        self.communication.entryconfigure(0, state="disabled" if self.busy else "normal")
        self.communication.entryconfigure(1, state="normal" if available else "disabled")
        self.stop.configure(state="normal" if self.busy else "disabled")

    def start(self, label, operation, *, invalidate_on_failure=False):
        if self.busy or not self.port.get():
            return
        port = self.port.get()
        fast = self.fast.get()
        self.busy = True
        self.stop_event.clear()
        self.status.set(f"{port}  •  {label} — FDを抜かずにお待ちください")
        self.progress["value"] = 0
        self.enable()

        def worker():
            last = 0

            def progress(phase, count, total):
                nonlocal last
                now = time.monotonic()
                if now - last > .15 or count == total:
                    self.events.put(("progress", phase, count, total))
                    last = now

            try:
                with Drive(port, self.stop_event.is_set, progress, fast=fast) as drive:
                    self.events.put(("link", f"{drive.baudrate:,} bps  •  {drive.transport}"))
                    result = operation(drive)
                self.events.put(("done", result))
            except Cancelled as err:
                self.events.put(("cancelled", str(err), invalidate_on_failure))
            except Exception as err:
                self.events.put(("failed", str(err), invalidate_on_failure))
        threading.Thread(target=worker, daemon=True).start()

    def receive(self):
        try:
            while True:
                item = self.events.get_nowait()
                if item[0] == "link":
                    self.link_label.set(item[1])
                    continue
                if item[0] == "progress":
                    _, phase, count, total = item
                    self.progress["value"] = 100 * count / max(1, total)
                    self.status.set(f"{phase}  {count:,}/{total:,}")
                    continue
                self.busy = False
                if self.close_after:
                    self.root.destroy()
                    return
                if item[0] == "done":
                    message, view, clear, detail = item[1]
                    if clear:
                        self.invalidate()
                    if view is not None:
                        self.show_view(view)
                    self.status.set(message)
                    self.progress["value"] = 100
                    if detail is not None:
                        self.show_text(*detail)
                else:
                    _, message, clear = item
                    if clear:
                        self.invalidate()
                    self.progress["value"] = 0
                    self.status.set("中止しました。" if item[0] == "cancelled" else "処理に失敗しました。")
                    if item[0] == "failed":
                        messagebox.showerror("フロッピー・ツール", message, parent=self.root)
                self.enable()
        except queue.Empty:
            pass
        self.root.after(50, self.receive)

    def stop_work(self):
        self.stop_event.set()
        self.status.set("現在の処理が区切りに達し次第、中止します。")

    def close(self):
        if not self.busy:
            self.root.destroy()
        elif messagebox.askyesno("終了", "処理を中止し、モーターを停止してから終了しますか？", parent=self.root):
            self.close_after = True
            self.stop_work()

    def show_text(self, title, contents):
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.geometry("760x480")
        dlg.transient(self.root)
        box = tk.Text(dlg, wrap="none", font=("Consolas", 10), bg=WHITE, fg=INK)
        box.pack(fill="both", expand=True, padx=12, pady=(12, 4))
        box.insert("1.0", contents)
        box.configure(state="disabled")
        ttk.Button(dlg, text="閉じる", command=dlg.destroy).pack(pady=6)

    @staticmethod
    def result(message, view=None, clear=False, detail=None):
        return message, view, clear, detail

    def refresh(self):
        self.start("ファイル一覧を取得", lambda d: self.result("一覧を更新しました。", read_view(Fat12(d))),
                   invalidate_on_failure=True)

    def same_directory(self, drive, view):
        volume = Fat12(drive)
        if volume.disk_id != view.disk:
            raise ValueError("FDが入れ替わっています。先に一覧を更新してください。")
        directory = volume.directory(view.trail[-1][1] if view.trail else 0)
        if directory.token != view.directory.token:
            raise ValueError("ファイル一覧が変わっています。先に一覧を更新してください。")
        return volume, directory

    def double_click(self, _):
        if self.busy or not self.current:
            return
        e = self.selected()
        if not e:
            return
        if not e.folder:
            self.extract()
            return
        view = self.current

        def run(d):
            volume, _ = self.same_directory(d, view)
            return self.result("フォルダーを開きました。", read_view(volume, view.trail + ((e.name, e.cluster),)))
        self.start("フォルダーを開く", run, invalidate_on_failure=True)

    def parent(self):
        view = self.current
        if not view or not view.trail:
            return

        def run(d):
            volume, _ = self.same_directory(d, view)
            return self.result("上のフォルダーを開きました。", read_view(volume, view.trail[:-1]))
        self.start("上のフォルダーを開く", run, invalidate_on_failure=True)

    def add(self):
        view = self.current
        if not view:
            return
        paths = filedialog.askopenfilenames(title="FDに追加するファイル", parent=self.root)
        if not paths:
            return

        def run(d):
            volume, directory = self.same_directory(d, view)
            total = sum(((Path(p).stat().st_size + volume.cluster_size - 1) // volume.cluster_size)
                        * volume.cluster_size for p in paths)
            if total > volume.free:
                raise ValueError(f"空き容量が足りません（空き {volume.free:,} B / 必要 {total:,} B）。")
            for path in map(Path, paths):
                volume.add(directory, path.name, path.read_bytes())
                directory = volume.directory(view.trail[-1][1] if view.trail else 0)
            return self.result(f"{len(paths)}件を追加し、元ファイルとの一致を確認しました。", read_view(volume, view.trail))
        self.start("ファイル追加", run, invalidate_on_failure=True)

    def extract(self):
        e = self.selected()
        view = self.current
        if not e or e.folder:
            return
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", e.name)
        path = filedialog.asksaveasfilename(title="取り出したファイルを保存", initialfile=filename, parent=self.root)
        if not path:
            return

        def run(d):
            volume, _ = self.same_directory(d, view)
            volume.extract(e, path)
            return self.result(f"取り出しました: {Path(path).name}（{e.size:,} B）")
        self.start("ファイルを取り出す", run)

    def delete(self):
        e = self.selected()
        view = self.current
        if not e or e.folder:
            return
        if not messagebox.askyesno("FDから削除", f"FDの「{e.name}」を削除しますか？\nごみ箱には移動しません。", default="no", parent=self.root):
            return

        def run(d):
            volume, directory = self.same_directory(d, view)
            volume.delete(directory, e)
            return self.result(f"削除しました: {e.name}", read_view(Fat12(d), view.trail))
        self.start("ファイル削除", run, invalidate_on_failure=True)

    def probe(self):
        def run(d):
            readings = d.request("P")
            text = "\n".join(f"{name:>8}  WPT={readings[i * 2]}  RDATA={readings[i * 2 + 1]}"
                             for i, name in enumerate(("選択直後", "100µs後", "約2ms後")))
            info = f"{d.port} / 115200 bps\n転送スケッチから応答を受信しました。\n\n{text}\n\nWPT=0: 書き込み禁止信号が有効。\n基板チェックはディスクの読み取り検査とは別です。"
            return self.result("基板の応答を確認しました。", detail=("基板チェック", info))
        self.start("基板チェック", run)

    def backup(self):
        path = filedialog.asksaveasfilename(title="FD全体のイメージを保存", defaultextension=".img",
                                            filetypes=[("フロッピーイメージ", "*.img"), ("すべて", "*.*")],
                                            initialfile="floppy.img", parent=self.root)
        if not path:
            return

        def run(d):
            d.save_image(path)
            return self.result(f"1,474,560 Bのイメージを保存しました: {Path(path).name}")
        self.start("イメージ保存", run)

    def restore(self):
        path = filedialog.askopenfilename(title="復元する1.44MBイメージ", filetypes=[("フロッピーイメージ", "*.img *.ima"), ("すべて", "*.*")], parent=self.root)
        if not path:
            return
        try:
            if Path(path).stat().st_size != IMAGE_SIZE:
                raise ValueError("イメージは1,474,560バイトである必要があります。")
        except (OSError, ValueError) as err:
            messagebox.showerror("イメージ復元", str(err), parent=self.root)
            return
        choice = messagebox.askyesnocancel(
            "復元方式",
            "復元前にトラックを初期化しますか？\n\n"
            "はい：初期化してから復元\nいいえ：初期化済みFDに復元\nキャンセル：実行しない", parent=self.root)
        if choice is None:
            return
        if not messagebox.askyesno(
                "FD全体を上書き",
                f"挿入中のFDを「{Path(path).name}」で上書きします。\n"
                f"方式：{'初期化して復元' if choice else '初期化せず復元'}\n"
                "既存データはすべて消えます。実行しますか？", default="no", parent=self.root):
            return

        def run(d):
            d.restore_image(Path(path).read_bytes(), reformat=choice)
            return self.result("イメージを復元し、全セクターの照合に成功しました。", clear=True)
        self.start("イメージ復元", run, invalidate_on_failure=True)

    def scan(self):
        def run(d):
            issues, count = d.scan()
            if issues:
                summary = f"{count:,}セクターを検査し、{len(issues)}件のエラーを検出しました。"
                if count < SECTORS:
                    summary += "\nエラーが32件に達したため検査を終了しました。"
                return self.result("検査で読み取りエラーを検出しました。", detail=("セクター検査結果", summary + "\n\n" + "\n".join(issues)))
            return self.result("全2,880セクターを正常に読み取れました。")
        self.start("読み取り検査", run)

    def benchmark(self):
        def run(d):
            addresses = range(36, 180)
            d.read(addresses.start)
            start = time.monotonic()
            data = d.read_many(addresses, "読み取り速度を測定中")
            elapsed = time.monotonic() - start
            kib = sum(map(len, data.values())) / 1024
            return self.result(f"読み取り {kib / elapsed:.1f} KiB/秒（{kib:g} KiB / {elapsed:.1f}秒）")
        self.start("読み取り速度測定", run)

    def inspect_sector(self):
        lba = simpledialog.askinteger("セクター表示", "読み取るLBA (0〜2879)", minvalue=0, maxvalue=2879, parent=self.root)
        if lba is None:
            return

        def run(d):
            data = d.read(lba)
            lines = []
            for i in range(0, len(data), 16):
                row = data[i:i + 16]
                lines.append(f"{i:04X}  {row.hex(' ').upper()}  {''.join(chr(b) if 32 <= b <= 126 else '.' for b in row)}")
            geometry = f"LBA {lba} / トラック {lba // 36} / 面 {(lba // 18) % 2} / セクター {lba % 18 + 1}"
            return self.result(f"LBA {lba}を読み取りました。", detail=("セクター表示", geometry + "\n\n" + "\n".join(lines)))
        self.start("セクターを読み取る", run)

    def format(self):
        label = simpledialog.askstring("フォーマット", "新しいディスク名（半角英数字・空白・_・-、11文字以内）",
                                       initialvalue="FLOPPY", parent=self.root)
        if label is None:
            return
        try:
            blank_metadata(label)
        except ValueError as err:
            messagebox.showerror("フォーマット", str(err), parent=self.root)
            return
        quick = messagebox.askyesnocancel(
            "フォーマット方式",
            "はい：クイック（既存の1.44MB FAT12の管理情報を初期化）\n"
            "いいえ：通常（80トラックと管理情報を初期化）\n"
            "キャンセル：実行しない", parent=self.root)
        if quick is None:
            return
        mode = "クイック" if quick else "通常"
        if not messagebox.askyesno("フォーマットの確認", f"挿入中の2HD FDを{mode}フォーマットします。\nFDの既存ファイルはすべて消えます。実行しますか？", default="no", parent=self.root):
            return

        def run(d):
            start = time.monotonic()
            d.format_fat12(label, quick=quick)
            elapsed = time.monotonic() - start
            return self.result(f"1.44MB FAT12で{mode}フォーマットしました（{elapsed:.1f}秒）。", read_view(Fat12(d)))
        self.start("フォーマット", run, invalidate_on_failure=True)


def main():
    root = tk.Tk()
    app = App(root)
    if "--smoke-test" in sys.argv:
        path = Path(sys.argv[sys.argv.index("--smoke-test") + 1])

        def report():
            try:
                assert len(app.buttons) == 12 and app.tree.winfo_viewable()
                root.geometry("890x640")
                root.update_idletasks()
                assert all(button.winfo_ismapped() and button.winfo_height() >= 22
                           for button in app.buttons.values())
                path.write_text("GUI launch and minimum-size layout OK\n", encoding="utf-8")
            finally:
                root.destroy()
        root.after(350, report)
    root.mainloop()


if __name__ == "__main__":
    main()
