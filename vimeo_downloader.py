#!/usr/bin/env python3
"""
Vimeo Library Downloader
========================
Lädt alle Videos aus dem eigenen Vimeo-Account herunter (über die offizielle Vimeo-API).

Voraussetzungen
  * Python 3.9+  und  pip install requests
  * Vimeo-Plan Standard oder höher (sonst liefert die API keine Download-Links)
  * Personal Access Token mit den Scopes:  public, private, video_files
    -> https://developer.vimeo.com/apps  -> App anlegen -> "Generate Access Token"

Start
  * Ohne Argumente:            GUI
  * Kommandozeile, Beispiel:   python vimeo_downloader.py --token XYZ --out "D:/Vimeo Backup" --quality best
  * Token geht auch per Umgebungsvariable VIMEO_TOKEN.

Verhalten
  * Bereits geladene Videos werden übersprungen (erkannt an der Video-ID im Dateinamen).
  * Abgebrochene Downloads (.part) werden fortgesetzt, sofern der Server das erlaubt.
  * Optional wird die Vimeo-Ordnerstruktur nachgebaut.
  * Am Ende liegen im Zielordner: vimeo_manifest.json sowie eine nummerierte Videoliste
    (Vimeo_Videoliste.txt und .csv) – auch nach einem Testlauf (--dry-run).
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import queue
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, unquote

try:
    import requests
except ImportError:
    print("Das Modul 'requests' fehlt. Bitte installieren:  pip install requests")
    sys.exit(1)

API = "https://api.vimeo.com"
FIELDS = "uri,name,created_time,duration,parent_folder.name,download"
CONFIG = Path.home() / ".vimeo_library_downloader.json"
DEFAULT_OUT = str(Path.home() / "Videos" / "Vimeo Backup")
QUALITIES = ["best", "2160", "1440", "1080", "720", "540", "360"]
TYPE_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
            "video/x-msvideo": ".avi", "video/x-matroska": ".mkv"}


def safe_name(s, maxlen=120):
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s or "").strip().rstrip(". ")
    return s[:maxlen].rstrip() or "Unbenannt"


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def pick_file(downloads, quality="best"):
    """Wählt aus der 'download'-Liste der API die passende Datei."""
    files = [d for d in (downloads or []) if d.get("link")]
    if not files:
        return None
    if quality == "best":
        src = [d for d in files if d.get("quality") == "source"]
        if src:
            return src[0]
        return max(files, key=lambda d: (d.get("height") or 0, d.get("size") or 0))
    limit = int(quality)
    fitting = [d for d in files if d.get("quality") != "source" and (d.get("height") or 0) <= limit]
    if fitting:
        return max(fitting, key=lambda d: (d.get("height") or 0, d.get("size") or 0))
    return min(files, key=lambda d: (d.get("height") or 0, d.get("size") or 0))


def guess_ext(resp, entry):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    for candidate in (m.group(1) if m else "", urlparse(resp.url).path):
        ext = os.path.splitext(unquote(candidate))[1].lower()
        if 2 <= len(ext) <= 6 and ext[1:].isalnum():
            return ext
    return TYPE_EXT.get(entry.get("type", ""), ".mp4")


class Stopped(Exception):
    pass


class Downloader:
    def __init__(self, token, out_dir, quality="best", by_folder=True, dry_run=False,
                 log=print, progress=None, stop_event=None):
        self.out = Path(out_dir)
        self.quality = quality
        self.by_folder = by_folder
        self.dry_run = dry_run
        self.log = log
        self.progress = progress or (lambda *a: None)
        self.stop_event = stop_event or threading.Event()
        self.api = requests.Session()
        self.api.headers.update({
            "Authorization": f"bearer {token.strip()}",
            "Accept": "application/vnd.vimeo.*+json;version=3.4",
            "User-Agent": "VimeoLibraryDownloader/1.0",
        })
        self.dl = requests.Session()  # ohne Token – geht an das CDN
        self.dl.headers["User-Agent"] = "VimeoLibraryDownloader/1.0"
        self.manifest = []

    # ---------- Hilfen ----------
    def check_stop(self):
        if self.stop_event.is_set():
            raise Stopped()

    def sleep(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.check_stop()
            time.sleep(0.5)

    @staticmethod
    def _reset_wait(resp):
        reset = resp.headers.get("X-RateLimit-Reset")
        try:
            dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            return int(min(max((dt - datetime.now(timezone.utc)).total_seconds() + 2, 5), 900))
        except Exception:
            return 60

    def api_get(self, path, params=None):
        url = path if path.startswith("http") else API + path
        for attempt in range(6):
            self.check_stop()
            try:
                r = self.api.get(url, params=params, timeout=60)
            except requests.RequestException as e:
                self.log(f"  Netzwerkfehler ({e.__class__.__name__}) – neuer Versuch …")
                self.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                wait = self._reset_wait(r)
                self.log(f"  API-Limit erreicht – warte {wait} s …")
                self.sleep(wait)
                continue
            if r.status_code >= 500:
                self.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 401:
                raise RuntimeError("Token ungültig oder abgelaufen (401).")
            if r.status_code == 403:
                raise RuntimeError("Zugriff verweigert (403) – hat der Token die Scopes 'private' und 'video_files'?")
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"API nicht erreichbar: {url}")

    # ---------- Ablauf ----------
    def check_account(self):
        verify = self.api_get("/oauth/verify")
        scopes = (verify.get("scope") or "").split()
        user = verify.get("user") or {}
        self.log(f"Account: {user.get('name', '?')}  ·  Plan: {user.get('account', '?')}")
        self.log(f"Token-Scopes: {' '.join(scopes) or '?'}")
        if "video_files" not in scopes:
            self.log("WARNUNG: Scope 'video_files' fehlt – ohne ihn gibt es keine Download-Links.")
        if "private" not in scopes:
            self.log("WARNUNG: Scope 'private' fehlt – private Videos werden nicht gefunden.")

    def videos(self):
        params = {"per_page": 50, "fields": FIELDS, "sort": "date", "direction": "asc"}
        page = self.api_get("/me/videos", params)
        total = page.get("total", 0)
        index = 0
        while True:
            for v in page.get("data", []):
                index += 1
                yield index, total, v
            nxt = (page.get("paging") or {}).get("next")
            if not nxt:
                break
            page = self.api_get(nxt)

    def run(self):
        self.out.mkdir(parents=True, exist_ok=True)
        self.check_account()
        stats = {"geladen": 0, "übersprungen": 0, "fehler": 0, "ohne_link": 0, "testlauf": 0}
        try:
            for i, total, v in self.videos():
                self.progress("overall", i - 1, total)
                status = self.handle(i, total, v)
                stats[status] = stats.get(status, 0) + 1
                self.progress("overall", i, total)
        except Stopped:
            self.log("\nAbgebrochen.")
        finally:
            self.write_manifest()
        if self.dry_run:
            self.log("\nTestlauf fertig: {testlauf} Videos würden geladen · {übersprungen} schon vorhanden · "
                     "{ohne_link} ohne Download-Link".format(**stats))
        else:
            self.log("\nFertig: {geladen} geladen · {übersprungen} übersprungen · "
                     "{ohne_link} ohne Download-Link · {fehler} Fehler".format(**stats))
        self.log(f"Zielordner: {self.out}")
        return stats

    def handle(self, i, total, v):
        vid = v["uri"].rsplit("/", 1)[-1]
        name = v.get("name") or vid
        date = (v.get("created_time") or "")[:10]
        folder_name = ((v.get("parent_folder") or {}).get("name") or "")
        folder = self.out / safe_name(folder_name) if (self.by_folder and folder_name) else self.out
        base = f"{date} {safe_name(name)} [{vid}]".strip()
        entry = pick_file(v.get("download"), self.quality)
        rec = {"nr": i, "id": vid, "name": name, "created": v.get("created_time"),
               "duration": v.get("duration"), "folder": folder_name,
               "quality": (entry.get("rendition") or entry.get("quality")) if entry else "",
               "size": entry.get("size") if entry else None,
               "link": f"https://vimeo.com/{vid}", "file": None, "status": None}
        self.manifest.append(rec)
        label = f"[{i}/{total}] {name}"

        if not entry:
            self.log(f"{label}  –  kein Download-Link (Plan/Scope prüfen oder Video noch in Bearbeitung)")
            rec["status"] = "ohne_link"
            return "ohne_link"

        existing = self.find_existing(folder, vid)
        if existing and (not entry.get("size") or existing.stat().st_size == entry["size"]):
            self.log(f"{label}  –  schon vorhanden, übersprungen")
            rec.update(file=str(existing), status="übersprungen")
            return "übersprungen"

        info = f"{entry.get('rendition') or entry.get('quality')} · {human(entry.get('size'))}"
        if self.dry_run:
            self.log(f"{label}  –  würde laden: {info}  →  {folder.name or self.out}")
            rec["status"] = "testlauf"
            return "testlauf"

        self.log(f"{label}  –  lade {info}")
        folder.mkdir(parents=True, exist_ok=True)
        for attempt in range(3):
            try:
                path = self.fetch(entry, folder, base)
                rec.update(file=str(path), status="geladen")
                return "geladen"
            except Stopped:
                raise
            except LinkExpired:
                self.log("  Link abgelaufen – hole neuen …")
                fresh = self.api_get(v["uri"], {"fields": "download"})
                entry = pick_file(fresh.get("download"), self.quality) or entry
            except Exception as e:
                self.log(f"  Fehler: {e} – Versuch {attempt + 2}/3" if attempt < 2 else f"  Fehler: {e}")
                self.sleep(5)
        rec["status"] = "fehler"
        return "fehler"

    @staticmethod
    def find_existing(folder, vid):
        if not folder.exists():
            return None
        tag = f"[{vid}]."
        for p in folder.iterdir():
            if p.is_file() and tag in p.name and not p.name.endswith(".part"):
                return p
        return None

    def fetch(self, entry, folder, base):
        part = folder / f"{base}.part"
        done = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={done}-"} if done else {}
        with self.dl.get(entry["link"], stream=True, headers=headers, timeout=60) as r:
            if r.status_code in (403, 410):
                raise LinkExpired()
            if r.status_code == 416:  # .part passt nicht – neu beginnen
                part.unlink(missing_ok=True)
                return self.fetch(entry, folder, base)
            r.raise_for_status()
            if r.status_code == 206:
                mode = "ab"
                self.log(f"  setze bei {human(done)} fort")
            else:
                mode, done = "wb", 0
            total = done + int(r.headers.get("Content-Length") or 0) or entry.get("size") or 0
            ext = guess_ext(r, entry)
            last = 0.0
            with open(part, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    self.check_stop()
                    if chunk:
                        f.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 0.25:
                            self.progress("file", done, total)
                            last = time.time()
        self.progress("file", done, total)
        if entry.get("size") and done != entry["size"]:
            raise RuntimeError(f"Größe stimmt nicht ({human(done)} statt {human(entry['size'])})")
        final = folder / f"{base}{ext}"
        part.replace(final)
        return final

    def write_manifest(self):
        if not self.manifest:
            return
        path = self.out / "vimeo_manifest.json"
        try:
            path.write_text(json.dumps(self.manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            self.log(f"Manifest konnte nicht geschrieben werden: {e}")
        self.write_list()

    def write_list(self):
        """Nummerierte Videoliste als Text (zum Lesen) und CSV (für Excel)."""
        def dur(s):
            s = int(s or 0)
            return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

        rows = self.manifest
        width = len(str(len(rows)))
        stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        total_size = sum(r.get("size") or 0 for r in rows)
        lines = [f"Vimeo-Videoliste – {len(rows)} Videos – Stand {stamp}",
                 f"Gesamtgröße (gewählte Qualität): {human(total_size)}", ""]
        for r in rows:
            extra = " · ".join(x for x in (
                (r.get("created") or "")[:10], dur(r.get("duration")),
                r.get("folder") and f"Ordner: {r['folder']}") if x)
            lines.append(f"{str(r['nr']).rjust(width)}. {r['name']}   ({extra})")
        txt = self.out / "Vimeo_Videoliste.txt"
        csv_path = self.out / "Vimeo_Videoliste.csv"
        try:
            txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
            import csv
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["Nr", "Name", "Datum", "Dauer", "Ordner", "Qualität",
                            "Größe (MB)", "Video-ID", "Link", "Status"])
                for r in rows:
                    w.writerow([r["nr"], r["name"], (r.get("created") or "")[:10], dur(r.get("duration")),
                                r.get("folder") or "", r.get("quality") or "",
                                f"{(r.get('size') or 0) / 1048576:.1f}".replace(".", ","),
                                r["id"], r["link"], r.get("status") or ""])
            self.log(f"Videoliste: {txt.name} / {csv_path.name}")
        except OSError as e:
            self.log(f"Videoliste konnte nicht geschrieben werden (Datei in Excel geöffnet?): {e}")


class LinkExpired(Exception):
    pass


# ---------------- Einstellungen ----------------
def load_config():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data):
    try:
        CONFIG.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------- GUI ----------------
HELP = """So funktioniert's

1. Token holen: developer.vimeo.com/apps → App anlegen (oder öffnen) → „Generate Access Token“.
   Scopes anhaken: Public, Private, Video Files.
   (Download-Links gibt es nur ab Vimeo-Plan Standard.)

2. Token einfügen, Zielordner wählen, Start.

Qualität
   best = Originaldatei (Source), falls verfügbar, sonst höchste Auflösung.
   Zahl = höchste Auflösung bis zu dieser Höhe (z. B. 1080).

Vimeo-Ordner nachbauen: legt Unterordner wie in deiner Vimeo-Library an.
Nur auflisten: Testlauf – geht die ganze Library durch und zeigt,
was geladen würde, ohne etwas zu laden.

Nach jedem Lauf liegt im Zielordner eine nummerierte Videoliste:
Vimeo_Videoliste.txt (zum Lesen) und Vimeo_Videoliste.csv (für Excel).

Erneut starten ist gefahrlos: Vorhandenes wird übersprungen,
abgebrochene Downloads werden fortgesetzt.

„Token merken“ speichert den Token im Klartext in:
""" + str(CONFIG)


def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    BG, PANEL, FG, MUTED, ACCENT = "#141414", "#1f1f1f", "#f2f2f2", "#9a9a9a", "#ff6a00"
    cfg = load_config()

    root = tk.Tk()
    root.title("Vimeo Library Downloader")
    root.configure(bg=BG)
    root.minsize(640, 560)

    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=FG, fieldbackground=PANEL, bordercolor="#333",
                 lightcolor=PANEL, darkcolor=PANEL, font=("Segoe UI", 10))
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Muted.TLabel", foreground=MUTED, font=("Segoe UI", 9))
    st.configure("Title.TLabel", font=("Segoe UI Semibold", 15))
    st.configure("TCheckbutton", background=BG, foreground=FG)
    st.map("TCheckbutton", background=[("active", BG)], indicatorcolor=[("selected", ACCENT), ("!selected", PANEL)])
    st.configure("TEntry", foreground=FG, insertcolor=FG, padding=5)
    st.configure("TCombobox", foreground=FG, arrowcolor=FG, padding=4)
    st.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", FG)])
    st.configure("TButton", background=PANEL, foreground=FG, padding=(12, 6), borderwidth=0)
    st.map("TButton", background=[("active", "#2c2c2c"), ("disabled", "#191919")],
           foreground=[("disabled", "#555")])
    st.configure("Accent.TButton", background=ACCENT, foreground="#000", font=("Segoe UI Semibold", 10))
    st.map("Accent.TButton", background=[("active", "#ff8533"), ("disabled", "#5a3010")])
    st.configure("Info.TButton", background=BG, foreground=ACCENT, padding=(6, 0),
                 font=("Segoe UI Semibold", 12))
    st.configure("O.Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL,
                 bordercolor=PANEL, lightcolor=ACCENT, darkcolor=ACCENT, thickness=8)

    frm = ttk.Frame(root, padding=18)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    head = ttk.Frame(frm)
    head.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 12))
    ttk.Label(head, text="Vimeo Library Downloader", style="Title.TLabel").pack(side="left")
    ttk.Button(head, text="i", style="Info.TButton", width=2,
               command=lambda: messagebox.showinfo("Kurzhilfe", HELP)).pack(side="right")

    token = tk.StringVar(value=cfg.get("token") or os.environ.get("VIMEO_TOKEN", ""))
    out = tk.StringVar(value=cfg.get("out", DEFAULT_OUT))
    quality = tk.StringVar(value=cfg.get("quality", "best"))
    remember = tk.BooleanVar(value=bool(cfg.get("token")))
    by_folder = tk.BooleanVar(value=cfg.get("by_folder", True))
    dry = tk.BooleanVar(value=False)

    ttk.Label(frm, text="Access Token").grid(row=1, column=0, sticky="w", pady=4)
    ent = ttk.Entry(frm, textvariable=token, show="•")
    ent.grid(row=1, column=1, sticky="ew", padx=8)
    show = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm, text="zeigen", variable=show,
                    command=lambda: ent.configure(show="" if show.get() else "•")).grid(row=1, column=2, sticky="w")

    ttk.Label(frm, text="Zielordner").grid(row=2, column=0, sticky="w", pady=4)
    ttk.Entry(frm, textvariable=out).grid(row=2, column=1, sticky="ew", padx=8)
    ttk.Button(frm, text="…", width=3,
               command=lambda: out.set(filedialog.askdirectory(initialdir=out.get()) or out.get())
               ).grid(row=2, column=2, sticky="w")

    ttk.Label(frm, text="Qualität").grid(row=3, column=0, sticky="w", pady=4)
    ttk.Combobox(frm, textvariable=quality, values=QUALITIES, state="readonly", width=10
                 ).grid(row=3, column=1, sticky="w", padx=8)

    opts = ttk.Frame(frm)
    opts.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 10))
    ttk.Checkbutton(opts, text="Vimeo-Ordner nachbauen", variable=by_folder).pack(side="left", padx=(0, 16))
    ttk.Checkbutton(opts, text="Nur auflisten (Testlauf)", variable=dry).pack(side="left", padx=(0, 16))
    ttk.Checkbutton(opts, text="Token merken", variable=remember).pack(side="left")

    btns = ttk.Frame(frm)
    btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(0, 12))
    start_btn = ttk.Button(btns, text="Start", style="Accent.TButton")
    stop_btn = ttk.Button(btns, text="Stopp", state="disabled")
    start_btn.pack(side="left", padx=(0, 8))
    stop_btn.pack(side="left")

    overall_lbl = ttk.Label(frm, text="Gesamt", style="Muted.TLabel")
    overall_lbl.grid(row=6, column=0, columnspan=3, sticky="w")
    overall = ttk.Progressbar(frm, style="O.Horizontal.TProgressbar", maximum=1)
    overall.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(2, 8))
    file_lbl = ttk.Label(frm, text="Aktuelle Datei", style="Muted.TLabel")
    file_lbl.grid(row=8, column=0, columnspan=3, sticky="w")
    filebar = ttk.Progressbar(frm, style="O.Horizontal.TProgressbar", maximum=1)
    filebar.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(2, 10))

    logbox = tk.Text(frm, height=14, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                     font=("Consolas", 9), wrap="word", padx=8, pady=6)
    logbox.grid(row=10, column=0, columnspan=3, sticky="nsew")
    frm.rowconfigure(10, weight=1)

    q = queue.Queue()
    stop_event = threading.Event()

    def pump():
        try:
            while True:
                kind, *data = q.get_nowait()
                if kind == "log":
                    logbox.insert("end", data[0] + "\n")
                    logbox.see("end")
                elif kind == "overall":
                    done, total = data
                    overall.configure(maximum=max(total, 1), value=done)
                    overall_lbl.configure(text=f"Gesamt  {done}/{total}")
                elif kind == "file":
                    done, total = data
                    filebar.configure(maximum=max(total, 1), value=done)
                    file_lbl.configure(text=f"Aktuelle Datei  {human(done)} / {human(total)}")
                elif kind == "end":
                    start_btn.configure(state="normal")
                    stop_btn.configure(state="disabled")
        except queue.Empty:
            pass
        root.after(100, pump)

    def worker(args):
        try:
            Downloader(**args, log=lambda m: q.put(("log", m)),
                       progress=lambda k, a, b: q.put((k, a, b)), stop_event=stop_event).run()
        except Stopped:
            q.put(("log", "Abgebrochen."))
        except Exception as e:
            q.put(("log", f"FEHLER: {e}"))
        finally:
            q.put(("end",))

    def start():
        if not token.get().strip():
            messagebox.showwarning("Token fehlt", "Bitte einen Vimeo Access Token eintragen (Hilfe: „i“).")
            return
        save_config({"token": token.get().strip() if remember.get() else "", "out": out.get(),
                     "quality": quality.get(), "by_folder": by_folder.get()})
        stop_event.clear()
        logbox.delete("1.0", "end")
        start_btn.configure(state="disabled")
        stop_btn.configure(state="normal")
        threading.Thread(target=worker, daemon=True, args=({
            "token": token.get(), "out_dir": out.get(), "quality": quality.get(),
            "by_folder": by_folder.get(), "dry_run": dry.get()},)).start()

    def stop():
        stop_event.set()
        q.put(("log", "Stoppe nach dem aktuellen Block …"))

    start_btn.configure(command=start)
    stop_btn.configure(command=stop)
    pump()
    root.mainloop()


# ---------------- CLI ----------------
def run_cli(a):
    cfg = load_config()
    token = a.token or os.environ.get("VIMEO_TOKEN") or cfg.get("token")
    if not token:
        print("Kein Token. --token angeben oder VIMEO_TOKEN setzen.")
        sys.exit(2)

    def progress(kind, done, total):
        if kind == "file" and total:
            pct = done * 100 // total
            print(f"\r  {pct:3d}%  {human(done)} / {human(total)}   ", end="", flush=True)
            if done >= total:
                print()

    stop_event = threading.Event()
    d = Downloader(token, a.out, a.quality, not a.no_folders, a.dry_run,
                   progress=progress, stop_event=stop_event)
    try:
        stats = d.run()
    except KeyboardInterrupt:
        stop_event.set()
        d.write_manifest()
        print("\nAbgebrochen.")
        sys.exit(130)
    sys.exit(1 if stats.get("fehler") else 0)


def main():
    p = argparse.ArgumentParser(description="Alle Videos aus dem eigenen Vimeo-Account herunterladen.")
    p.add_argument("--token", help="Vimeo Personal Access Token (Scopes: public private video_files)")
    p.add_argument("--out", default=DEFAULT_OUT, help="Zielordner")
    p.add_argument("--quality", default="best", choices=QUALITIES, help="best = Original, sonst max. Höhe")
    p.add_argument("--no-folders", action="store_true", help="Vimeo-Ordnerstruktur nicht nachbauen")
    p.add_argument("--dry-run", action="store_true", help="Nur auflisten, nichts laden")
    p.add_argument("--gui", action="store_true", help="GUI erzwingen")
    a = p.parse_args()
    if a.gui or len(sys.argv) == 1:
        try:
            run_gui()
            return
        except Exception as e:  # z. B. kein Display
            if a.gui:
                raise
            print(f"GUI nicht verfügbar ({e}) – nutze die Kommandozeile, siehe --help.")
            sys.exit(2)
    run_cli(a)


if __name__ == "__main__":
    main()
