"""Bildretter desktop app: drop broken image files, get repaired images in Downloads."""
import ctypes
import math
import os
import shutil
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog

from tkinterdnd2 import DND_FILES, TkinterDnD

from .engine import process_files

BG = "#1e2230"
CARD = "#2a3042"
ACCENT = "#4f9dff"
TEXT = "#e8ecf4"
MUTED = "#9aa3b8"
GOOD = "#5cd18b"
BAD = "#ff7a7a"
FONT = "Segoe UI"


def downloads_folder():
    """The user's real Downloads folder (it can be moved in Windows), else ~/Downloads."""
    try:
        from ctypes import wintypes
        guid = ctypes.c_byte * 16
        fid = guid.from_buffer_copy(bytes.fromhex("90e24d373f126545916439c4925e467b"))  # FOLDERID_Downloads
        path = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(path)) == 0:
            result = Path(path.value)
            ctypes.windll.ole32.CoTaskMemFree(path)
            return result
    except Exception:
        pass
    return Path.home() / "Downloads"


class Spinner(tk.Canvas):
    """Rotating ring of dots around a small camera symbol."""

    def __init__(self, master, size=120):
        super().__init__(master, width=size, height=size, bg=BG, highlightthickness=0)
        self.size, self.angle, self.job = size, 0.0, None

    def start(self):
        if not self.job:
            self._tick()

    def stop(self):
        if self.job:
            self.after_cancel(self.job)
            self.job = None

    def _tick(self):
        self.delete("all")
        c, r = self.size / 2, self.size / 2 - 10
        for i in range(12):
            a = self.angle + i * math.pi / 6
            x, y = c + r * math.cos(a), c + r * math.sin(a)
            shade = int(60 + 195 * (i / 11))
            color = f"#{shade // 3:02x}{shade * 2 // 3:02x}{shade:02x}"
            d = 3 + 4 * i / 11
            self.create_oval(x - d, y - d, x + d, y + d, fill=color, outline="")
        # camera symbol in the middle
        self.create_rectangle(c - 18, c - 10, c + 18, c + 14, outline=TEXT, width=2)
        self.create_rectangle(c - 7, c - 15, c + 7, c - 10, outline=TEXT, width=2)
        self.create_oval(c - 7, c - 5, c + 7, c + 9, outline=ACCENT, width=2)
        self.angle += 0.12
        self.job = self.after(40, self._tick)


class App:
    def __init__(self):
        self.root = TkinterDnD.Tk()
        self.root.title("Bildretter")
        self.root.geometry("560x520")
        self.root.minsize(480, 460)
        self.root.configure(bg=BG)
        self.work_dir = None
        self.results = []
        self.frame = None
        self.show_drop()

    # --- screens ---------------------------------------------------------------------------

    def _new_frame(self):
        if self.frame:
            self.frame.destroy()
        self.frame = tk.Frame(self.root, bg=BG)
        self.frame.pack(fill="both", expand=True, padx=24, pady=24)
        return self.frame

    def show_drop(self):
        f = self._new_frame()
        tk.Label(f, text="Bildretter", font=(FONT, 22, "bold"), fg=TEXT, bg=BG).pack(pady=(0, 4))
        tk.Label(f, text="Kaputte Bilddateien retten", font=(FONT, 11), fg=MUTED, bg=BG).pack()
        zone = tk.Frame(f, bg=CARD, highlightbackground=ACCENT, highlightthickness=2)
        zone.pack(fill="both", expand=True, pady=20)
        inner = tk.Frame(zone, bg=CARD)
        inner.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(inner, text="⬇", font=(FONT, 40), fg=ACCENT, bg=CARD).pack()
        tk.Label(inner, text="Dateien hier hineinziehen", font=(FONT, 14, "bold"), fg=TEXT, bg=CARD).pack()
        tk.Label(inner, text="oder", font=(FONT, 10), fg=MUTED, bg=CARD).pack(pady=4)
        tk.Button(inner, text="Dateien auswählen", font=(FONT, 11), bg=ACCENT, fg="white",
                  activebackground="#3a85e6", relief="flat", padx=16, pady=6, cursor="hand2",
                  command=self.pick_files).pack()
        for w in (zone, inner) + tuple(inner.winfo_children()):
            w.drop_target_register(DND_FILES)
            w.dnd_bind("<<Drop>>", self.on_drop)

    def show_working(self, count):
        f = self._new_frame()
        box = tk.Frame(f, bg=BG)
        box.place(relx=0.5, rely=0.45, anchor="center")
        self.spinner = Spinner(box)
        self.spinner.pack(pady=(0, 18))
        self.spinner.start()
        tk.Label(box, text=f"{count} Datei(en) werden repariert …", font=(FONT, 14, "bold"),
                 fg=TEXT, bg=BG).pack()
        tk.Label(box, text="Bilder reparieren kann einige Minuten dauern.", font=(FONT, 11),
                 fg=MUTED, bg=BG).pack(pady=(6, 14))
        self.status = tk.Label(box, text="", font=(FONT, 9), fg=MUTED, bg=BG)
        self.status.pack()

    def show_done(self):
        f = self._new_frame()
        files = [r for r in self.results if not r.extra.get("combined")]
        ok = [r for r in files if r.ok]
        combined = sum(1 for r in self.results if r.ok and r.extra.get("combined"))
        summary = f"{len(ok)} von {len(files)} Datei(en) gerettet"
        if combined:
            summary += f" · {combined} zusammengesetztes Bild"
        tk.Label(f, text="Fertig!", font=(FONT, 22, "bold"), fg=TEXT, bg=BG).pack()
        tk.Label(f, text=summary, font=(FONT, 12), fg=GOOD if ok else BAD, bg=BG).pack(pady=(2, 12))
        lst = tk.Frame(f, bg=CARD)
        lst.pack(fill="both", expand=True)
        canvas = tk.Canvas(lst, bg=CARD, highlightthickness=0)
        rows = tk.Frame(canvas, bg=CARD)
        bar = tk.Scrollbar(lst, command=canvas.yview)
        canvas.configure(yscrollcommand=bar.set)
        canvas.create_window((0, 0), window=rows, anchor="nw")
        rows.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        bar.pack(side="right", fill="y")
        for r in sorted(self.results, key=lambda r: (not r.ok, not r.extra.get("combined"), r.name)):
            line = tk.Frame(rows, bg=CARD)
            line.pack(fill="x", pady=2)
            tk.Label(line, text="✔" if r.ok else "✖", fg=GOOD if r.ok else BAD, bg=CARD,
                     font=(FONT, 11)).pack(side="left", padx=(0, 6))
            tk.Label(line, text=f"{r.name} – {r.message}", fg=TEXT, bg=CARD, font=(FONT, 10),
                     anchor="w").pack(side="left")
        btns = tk.Frame(f, bg=BG)
        btns.pack(fill="x", pady=(16, 0))
        if ok:
            tk.Button(btns, text="Fertige Bilder herunterladen", font=(FONT, 13, "bold"), bg=GOOD,
                      fg="#10301e", activebackground="#48b877", relief="flat", padx=18, pady=10,
                      cursor="hand2", command=self.download).pack(fill="x")
        tk.Button(btns, text="Neue Dateien", font=(FONT, 10), bg=CARD, fg=TEXT, relief="flat",
                  padx=12, pady=6, cursor="hand2", command=self.reset).pack(pady=(10, 0))

    # --- actions ---------------------------------------------------------------------------

    def pick_files(self):
        paths = filedialog.askopenfilenames(title="Kaputte Bilddateien auswählen")
        if paths:
            self.start(list(paths))

    def on_drop(self, event):
        paths = [p for p in self.root.tk.splitlist(event.data) if os.path.isfile(p)]
        if paths:
            self.start(paths)

    def start(self, paths):
        self.work_dir = Path(tempfile.mkdtemp(prefix="bildretter_"))
        self.show_working(len(paths))
        threading.Thread(target=self._run, args=(paths,), daemon=True).start()

    def _run(self, paths):
        report = lambda text: self.root.after(0, lambda: self.status.config(text=text))
        results = process_files(paths, self.work_dir, report)
        self.root.after(0, lambda: self._finished(results))

    def _finished(self, results):
        self.spinner.stop()
        self.results = results
        self.show_done()

    def download(self):
        stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
        target = downloads_folder() / f"Gerettete Bilder {stamp}"
        target.mkdir(parents=True, exist_ok=True)
        for r in self.results:
            if r.ok and r.output:
                shutil.copy2(r.output, target / r.output.name)
        os.startfile(target)

    def reset(self):
        if self.work_dir:
            shutil.rmtree(self.work_dir, ignore_errors=True)
        self.results = []
        self.show_drop()

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.mainloop()

    def _close(self):
        if self.work_dir:
            shutil.rmtree(self.work_dir, ignore_errors=True)
        self.root.destroy()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
    except Exception:
        pass
    App().run()


if __name__ == "__main__":
    main()
