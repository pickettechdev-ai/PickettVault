#!/usr/bin/env python3
"""
PickettVault v1.4 — USB project sync for PICKETTECH
====================================================

v1.4: Remove project; warns when project folders overlap.
v1.3: Help button in the header.
v1.2: PICKETTECH branding, dark theme, Help menu (F1).
v1.1: adds "Mirror" — a full backup copy of the whole vault on a second
      drive (e.g. your 3 TB desktop drive), run by hand or after every sync.

Keeps project folders on your PC in sync with a dedicated USB stick, keeps
every overwritten/deleted file as a timestamped snapshot, and writes a
CHANGELOG.md with a version number for each project on every sync.

SETUP
  1. Rename your USB stick's volume label to  PICKETVAULT
     (File Explorer > right-click the drive > Rename).
  2. Run:  python pickettvault_v1.0.py   (or pythonw to hide the console)
  3. Click "Add project", give it a name and pick its folder on this PC.

HOW SYNC DECIDES WHAT TO DO
  PickettVault remembers the state of every file after each sync (per PC).
  Next time it compares both sides against that memory:
    - changed only on PC      -> copied PC → USB
    - changed only on USB     -> copied USB → PC
    - deleted on one side     -> deleted on the other (snapshot kept)
    - changed on BOTH sides   -> CONFLICT: you choose (default = skip)
  Double-click any row in the preview to change what happens to it.

MIRROR (backup of the whole USB)
  Click "Mirror settings…" and pick a folder on your backup drive. PickettVault
  creates  <folder>\\PICKETVAULT_MIRROR\\  and copies only new/changed files.
  Files removed from the USB are NOT deleted from the mirror — they're moved to
  PICKETVAULT_MIRROR\\_removed\\<timestamp>\\ so the mirror is always safe.

WHERE THINGS LIVE ON THE USB
  PICKETVAULT:\\Projects\\<name>\\                   your synced files
  PICKETVAULT:\\Projects\\<name>\\CHANGELOG.md       version history
  PICKETVAULT:\\.pickettvault\\vault.json            project list/versions
  PICKETVAULT:\\.pickettvault\\versions\\<name>\\<timestamp>\\(pc|usb)\\...
                                                     snapshots of old files

Requires Python 3.8+ (Tkinter is included with the standard Windows installer).
"""

import datetime
import fnmatch
import hashlib
import json
import os
import queue
import re
import shutil
import socket
import string
import sys
import threading

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
except ImportError:  # allows the sync engine to be imported/tested headless
    tk = None

APP_NAME = "PickettVault"
APP_VERSION = "1.4"
MIRROR_DIR = "PICKETVAULT_MIRROR"
MIRROR_SKIP_TOP = {"$RECYCLE.BIN", "System Volume Information", "_removed"}
VAULT_LABEL = "PICKETVAULT"
META_DIR = ".pickettvault"
PROJECTS_DIR = "Projects"
CHANGELOG = "CHANGELOG.md"
IGNORE_PATTERNS = [
    META_DIR, ".git", "__pycache__", "node_modules", "Thumbs.db", "desktop.ini",
    ".DS_Store", "~$*", "*.tmp", "$RECYCLE.BIN", "System Volume Information",
]
MTIME_TOLERANCE = 2.0  # seconds — FAT32 sticks only store even-second times
COMPUTER = socket.gethostname()


# ─────────────────────────────── helpers ────────────────────────────────

def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)  # atomic — no half-written files if the USB is pulled


def safe_name(s):
    return re.sub(r"[^\w\-. ]", "_", s)


def find_vault_drive():
    """Return e.g. 'E:\\' for the drive labelled PICKETVAULT, or None."""
    if os.name != "nt":
        return None
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(1)  # suppress "insert a disk" popups for empty drives
    mask = k32.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        label = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(ctypes.c_wchar_p(root), label, 261,
                                       None, None, None, None, 0)
        if ok and label.value.strip().upper() == VAULT_LABEL:
            return root
    return None


def bump_version(ver, part):
    try:
        ma, mi, pa = (int(x) for x in ver.split("."))
    except ValueError:
        ma, mi, pa = 1, 0, 0
    if part == "major":
        return f"{ma + 1}.0.0"
    if part == "minor":
        return f"{ma}.{mi + 1}.0"
    if part == "patch":
        return f"{ma}.{mi}.{pa + 1}"
    return f"{ma}.{mi}.{pa}"


# ───────────────────────────── sync engine ──────────────────────────────

def is_ignored(name):
    return any(fnmatch.fnmatch(name, pat) for pat in IGNORE_PATTERNS)


def native(root, rel):
    return os.path.join(root, *rel.split("/"))


def stat_sig(path):
    try:
        st = os.stat(path)
        return [st.st_size, round(st.st_mtime, 3)]
    except OSError:
        return None


def scan(root):
    """{ 'sub/file.ext': [size, mtime] } for every file under root."""
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_ignored(d)]
        reldir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        reldir = "" if reldir == "." else reldir + "/"
        for f in filenames:
            if is_ignored(f):
                continue
            sig = stat_sig(os.path.join(dirpath, f))
            if sig:
                out[reldir + f] = sig
    return out


def same_sig(a, b):
    return (a is not None and b is not None and a[0] == b[0]
            and abs(a[1] - b[1]) <= MTIME_TOLERANCE)


def changed(cur, prev):
    if cur is None and prev is None:
        return False
    return not same_sig(cur, prev)


def sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_plan(pc_root, usb_root, state):
    """Compare both sides with the last-sync state. Returns (actions, forget)."""
    pc, usb = scan(pc_root), scan(usb_root)
    actions, forget = [], []
    for rel in sorted(set(pc) | set(usb) | set(state)):
        p, u = pc.get(rel), usb.get(rel)
        s = state.get(rel) or {}
        pc_ch, usb_ch = changed(p, s.get("pc")), changed(u, s.get("usb"))
        if not pc_ch and not usb_ch:
            continue
        if p is None and u is None:
            forget.append(rel)
            continue
        detail = ""
        if pc_ch and not usb_ch:
            kind = "to_usb" if p else "del_usb"
            detail = "new file" if (p and u is None) else ("updated" if p else "")
        elif usb_ch and not pc_ch:
            kind = "to_pc" if u else "del_pc"
            detail = "new file" if (u and p is None) else ("updated" if u else "")
        elif p and u and p[0] == u[0] and \
                sha1(native(pc_root, rel)) == sha1(native(usb_root, rel)):
            kind = "same"  # identical content — just record it
        else:
            kind = "conflict"
            if p is None:
                detail = "deleted on PC, changed on USB"
            elif u is None:
                detail = "deleted on USB, changed on PC"
            else:
                detail = "changed on both"
        actions.append({"rel": rel, "kind": kind, "detail": detail,
                        "choice": "skip" if kind == "conflict" else "default"})
    return actions, forget


def resolve(a):
    """What will actually happen to this row, or None to leave it alone."""
    if a["choice"] == "skip":
        return None
    if a["kind"] == "conflict":
        return {"keep_pc": "to_usb", "keep_usb": "to_pc"}.get(a["choice"])
    return a["kind"]


def apply_plan(pc_root, usb_root, snap_root, state, actions, forget, progress=None):
    new_state = dict(state)
    for rel in forget:
        new_state.pop(rel, None)
    done = {"to_usb": 0, "to_pc": 0, "deleted": 0}
    files, errors = [], []
    todo = [a for a in actions if resolve(a)]
    for i, a in enumerate(todo, 1):
        rel, op = a["rel"], resolve(a)
        if progress:
            progress(i, len(todo), rel)
        pc_f, usb_f = native(pc_root, rel), native(usb_root, rel)
        try:
            if op in ("to_usb", "del_usb"):
                src, dst, side = pc_f, usb_f, "usb"
            elif op in ("to_pc", "del_pc"):
                src, dst, side = usb_f, pc_f, "pc"
            else:
                src = dst = side = None  # "same"
            if src is not None:
                if os.path.exists(dst):  # keep the old copy before touching it
                    snap = native(os.path.join(snap_root, side), rel)
                    os.makedirs(os.path.dirname(snap), exist_ok=True)
                    shutil.move(dst, snap)
                if os.path.exists(src):
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    done["to_" + side] += 1
                    files.append(("→ USB  " if side == "usb" else "→ PC   ") + rel)
                else:
                    done["deleted"] += 1
                    files.append(f"deleted on {side.upper()}  {rel}")
        except OSError as e:
            errors.append(f"{rel}: {e}")
            continue
        ps, us = stat_sig(pc_f), stat_sig(usb_f)
        if ps and us:
            new_state[rel] = {"pc": ps, "usb": us}
        else:
            new_state.pop(rel, None)
    return new_state, done, files, errors


def walk_all(root):
    """Every file under root (including vault metadata), minus system folders."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        if os.path.normpath(dirpath) == os.path.normpath(root):
            dirnames[:] = [d for d in dirnames if d not in MIRROR_SKIP_TOP]
        reldir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        reldir = "" if reldir == "." else reldir + "/"
        for f in filenames:
            if f.endswith(".tmp"):
                continue
            sig = stat_sig(os.path.join(dirpath, f))
            if sig:
                out[reldir + f] = sig
    return out


def mirror_vault(src, dst, progress=None):
    """One-way incremental copy src -> dst. Never deletes: removed files are
    moved into dst/_removed/<timestamp>/."""
    os.makedirs(dst, exist_ok=True)
    src_files, dst_files = walk_all(src), walk_all(dst)
    todo = [r for r, sig in src_files.items() if not same_sig(sig, dst_files.get(r))]
    gone = [r for r in dst_files if r not in src_files]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    total, n = len(todo) + len(gone), 0
    res = {"copied": 0, "removed": 0, "errors": []}
    for rel in todo:
        n += 1
        if progress:
            progress(n, total, "mirror: " + rel)
        try:
            d = native(dst, rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(native(src, rel), d)
            res["copied"] += 1
        except OSError as e:
            res["errors"].append(f"{rel}: {e}")
    for rel in gone:
        n += 1
        if progress:
            progress(n, total, "mirror: " + rel)
        try:
            d = native(os.path.join(dst, "_removed", stamp), rel)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.move(native(dst, rel), d)
            res["removed"] += 1
        except OSError as e:
            res["errors"].append(f"{rel}: {e}")
    return res


def _norm(p):
    return os.path.normcase(os.path.normpath(os.path.abspath(p)))


def is_inside(child, parent):
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def folder_problem(vault, name, folder):
    """Why this folder is a bad PC folder for project `name`, or None if it's fine."""
    if os.path.splitdrive(_norm(folder))[1] in ("", os.sep):
        return "That's the root of a whole drive. Pick the project's own folder instead."
    if is_inside(folder, vault.root) or is_inside(vault.root, folder):
        return "That folder is on (or contains) the vault itself. Pick a folder on this PC."
    for other in vault.projects:
        if other == name:
            continue
        o = vault.pc_dir(other)
        if not o:
            continue
        if _norm(o) == _norm(folder):
            return f"'{other}' already uses this exact folder."
        if is_inside(o, folder):
            return (f"This folder contains '{other}'s folder:\n{o}\n\n"
                    f"Scanning it would mix {other}'s files into {name}. "
                    "Pick the project's own folder instead.")
        if is_inside(folder, o):
            return (f"This folder is inside '{other}'s folder:\n{o}\n\n"
                    "Its files would be synced twice.")
    return None


def write_changelog(pc_root, usb_root, name, version, note, files, done):
    header = f"# {name} — Changelog\n\n"
    old = ""
    for root in (usb_root, pc_root):
        p = os.path.join(root, CHANGELOG)
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                old = fh.read()
            break
    body = old[len(header):] if old.startswith(header) else old
    lines = [f"## v{version} — {now_str()} ({COMPUTER})", "",
             note.strip() or "(no note)", "",
             f"_{done['to_usb']} → USB · {done['to_pc']} → PC · "
             f"{done['deleted']} deleted_", ""]
    lines += [f"- `{f}`" for f in files[:25]]
    if len(files) > 25:
        lines.append(f"- …and {len(files) - 25} more")
    text = header + "\n".join(lines) + "\n\n" + body
    for root in (usb_root, pc_root):
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, CHANGELOG), "w", encoding="utf-8") as fh:
            fh.write(text)


class Vault:
    def __init__(self, root):
        self.root = root
        self.meta = os.path.join(root, META_DIR)
        os.makedirs(os.path.join(self.meta, "state"), exist_ok=True)
        self.cfg_path = os.path.join(self.meta, "vault.json")
        self.cfg = read_json(self.cfg_path, {"created_by": f"{APP_NAME} v{APP_VERSION}",
                                             "projects": {}})
        if os.name == "nt":  # hide the metadata folder in Explorer
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(self.meta, 0x02)
            except Exception:
                pass

    @property
    def projects(self):
        return self.cfg["projects"]

    def save(self):
        write_json(self.cfg_path, self.cfg)

    def usb_dir(self, name):
        return os.path.join(self.root, PROJECTS_DIR, name)

    def pc_dir(self, name):
        return self.projects[name].get("local_paths", {}).get(COMPUTER)

    def set_pc_dir(self, name, path):
        self.projects[name].setdefault("local_paths", {})[COMPUTER] = path
        self.save()

    def add_project(self, name, path):
        self.projects[name] = {"version": "1.0.0", "created": now_str(),
                               "last_sync": None, "local_paths": {COMPUTER: path}}
        os.makedirs(self.usb_dir(name), exist_ok=True)
        self.save()

    def mirror(self):
        return self.cfg.setdefault("mirrors", {}).get(COMPUTER)

    def set_mirror(self, path, auto):
        m = self.cfg.setdefault("mirrors", {}).setdefault(COMPUTER, {})
        m.update({"path": path, "auto": auto})
        self.save()

    def remove_project(self, name, keep_usb_files=True):
        """Forget a project. The PC folder is never touched. USB files are either
        left where they are or moved to .pickettvault/removed_projects/."""
        usb = self.usb_dir(name)
        if os.path.isdir(usb):
            if not os.listdir(usb):
                os.rmdir(usb)
            elif not keep_usb_files:
                stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
                dest = os.path.join(self.meta, "removed_projects", f"{safe_name(name)}_{stamp}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(usb, dest)
        prefix = safe_name(name) + "__"
        sdir = os.path.join(self.meta, "state")
        for f in os.listdir(sdir):
            if f.startswith(prefix):
                os.remove(os.path.join(sdir, f))
        del self.projects[name]
        self.save()

    def state_file(self, name):
        return os.path.join(self.meta, "state",
                            f"{safe_name(name)}__{safe_name(COMPUTER)}.json")

    def load_state(self, name):
        return read_json(self.state_file(name), {})

    def save_state(self, name, state):
        write_json(self.state_file(name), state)

    def snap_root(self, name):
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        return os.path.join(self.meta, "versions", safe_name(name), stamp)


# ──────────────────────────────── GUI ───────────────────────────────────

# PICKETTECH palette (blue/white taken from the logo)
BLUE = "#0691E6"
BLUE_HOVER = "#27A6F5"
BLUE_DEEP = "#0B4F7F"
WHITE = "#FAFAFA"
BG = "#14171C"
PANEL = "#1C2027"
FIELD = "#232830"
BORDER = "#323843"
MUTED = "#8E98A4"
RED_BG = "#48212A"
RED_FG = "#FF8C8C"
AMBER = "#E8A55A"
GREEN = "#4CD08A"
FONT = "Segoe UI" if os.name == "nt" else "Helvetica"
MONO = "Consolas" if os.name == "nt" else "Courier"

ACTION_LABELS = {"to_usb": "PC → USB", "to_pc": "USB → PC",
                 "del_usb": "Delete on USB", "del_pc": "Delete on PC"}
CONFLICT_LABELS = {"skip": "⚠ CONFLICT – skip",
                   "keep_pc": "⚠ CONFLICT – keep PC version",
                   "keep_usb": "⚠ CONFLICT – keep USB version"}


def action_text(a):
    if a["kind"] == "conflict":
        return CONFLICT_LABELS[a["choice"]]
    return "Skip" if a["choice"] == "skip" else ACTION_LABELS[a["kind"]]


def b64(s):
    return "".join(s.split())


def dark_titlebar(win):
    """Ask Windows 10/11 to draw a dark title bar for this window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:
        pass


def apply_theme(root):
    root.configure(bg=BG)
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=WHITE, fieldbackground=FIELD,
                 bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                 troughcolor=PANEL, focuscolor=BLUE, selectbackground=BLUE_DEEP,
                 selectforeground=WHITE, insertcolor=WHITE, font=(FONT, 10))
    st.configure("TFrame", background=BG)
    st.configure("Panel.TFrame", background=PANEL)
    st.configure("TLabel", background=BG, foreground=WHITE)
    st.configure("Panel.TLabel", background=PANEL, foreground=WHITE)
    st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(FONT, 9))
    st.configure("PanelMuted.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 9))
    st.configure("Section.TLabel", background=BG, foreground=MUTED, font=(FONT, 9, "bold"))
    st.configure("Title.TLabel", background=BG, foreground=WHITE, font=(FONT, 12, "bold"))
    st.configure("TButton", background=FIELD, foreground=WHITE, bordercolor=BORDER,
                 lightcolor=FIELD, darkcolor=FIELD, padding=(12, 5), focusthickness=0)
    st.map("TButton",
           background=[("disabled", PANEL), ("pressed", BORDER), ("active", BORDER)],
           foreground=[("disabled", "#555C66")])
    st.configure("Accent.TButton", background=BLUE, foreground="white", bordercolor=BLUE,
                 lightcolor=BLUE, darkcolor=BLUE, font=(FONT, 10, "bold"))
    st.map("Accent.TButton",
           background=[("disabled", BLUE_DEEP), ("pressed", BLUE_DEEP), ("active", BLUE_HOVER)],
           foreground=[("disabled", "#9CB8CC")])
    st.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=WHITE,
                 rowheight=26, borderwidth=0, relief="flat")
    st.map("Treeview", background=[("selected", BLUE_DEEP)], foreground=[("selected", WHITE)])
    st.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                 bordercolor=BORDER, font=(FONT, 9, "bold"), padding=(6, 4))
    st.map("Treeview.Heading", background=[("active", BORDER)])
    st.configure("Blue.Horizontal.TProgressbar", background=BLUE, troughcolor=PANEL,
                 bordercolor=BORDER, lightcolor=BLUE, darkcolor=BLUE, thickness=8)
    st.configure("TLabelframe", background=BG, bordercolor=BORDER)
    st.configure("TLabelframe.Label", background=BG, foreground=MUTED, font=(FONT, 9, "bold"))
    st.configure("TPanedwindow", background=BG)
    st.configure("Sash", sashthickness=8, background=BG)
    st.configure("TRadiobutton", background=BG, foreground=WHITE, indicatorbackground=FIELD,
                 indicatorforeground=BLUE)
    st.map("TRadiobutton", background=[("active", BG)],
           indicatorbackground=[("selected", BLUE)])
    st.configure("TEntry", fieldbackground=FIELD, foreground=WHITE, insertcolor=WHITE)
    st.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG,
                 arrowcolor=MUTED, lightcolor=PANEL, darkcolor=PANEL)
    st.map("Vertical.TScrollbar", background=[("active", BORDER)])
    root.option_add("*Menu.background", PANEL)
    root.option_add("*Menu.foreground", WHITE)
    root.option_add("*Menu.activeBackground", BLUE)
    root.option_add("*Menu.activeForeground", "white")
    root.option_add("*Menu.relief", "flat")


def dark_text(parent, **kw):
    opts = dict(bg=FIELD, fg=WHITE, insertbackground=WHITE, relief="flat",
                highlightthickness=1, highlightbackground=BORDER, highlightcolor=BLUE,
                selectbackground=BLUE_DEEP, selectforeground=WHITE, padx=8, pady=6,
                font=(FONT, 10), wrap="word")
    opts.update(kw)
    return tk.Text(parent, **opts)


class DarkDialog(tk.Toplevel if tk else object):
    """Base for small modal dialogs that match the dark theme."""
    def __init__(self, parent, title):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.configure(bg=BG)
        self.transient(parent)
        self.resizable(False, False)
        icon = getattr(parent, "_pv_icon", None)
        if icon:
            self.iconphoto(False, icon)
        self.body = ttk.Frame(self, padding=16)
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())

    def show(self):
        dark_titlebar(self)
        self.update_idletasks()
        p = self.master
        x = p.winfo_rootx() + (p.winfo_width() - self.winfo_width()) // 2
        y = p.winfo_rooty() + (p.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.wait_window()
        return self.result

    def buttons(self, ok_text, ok_cmd):
        bf = ttk.Frame(self.body)
        bf.pack(fill="x", pady=(14, 0))
        ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(bf, text=ok_text, style="Accent.TButton", command=ok_cmd).pack(side="right", padx=8)


class AskDialog(DarkDialog):
    def __init__(self, parent, title, prompt):
        super().__init__(parent, title)
        ttk.Label(self.body, text=prompt).pack(anchor="w")
        self.var = tk.StringVar()
        e = ttk.Entry(self.body, textvariable=self.var, width=40, font=(FONT, 10))
        e.pack(fill="x", pady=(6, 0), ipady=3)
        e.focus_set()
        e.bind("<Return>", lambda ev: self._ok())
        self.buttons("OK", self._ok)

    def _ok(self):
        self.result = self.var.get()
        self.destroy()


class NoteDialog(DarkDialog):
    def __init__(self, parent, project, version, count):
        super().__init__(parent, "Sync note")
        ttk.Label(self.body, text=project, style="Title.TLabel").pack(anchor="w")
        ttk.Label(self.body, text=f"{count} change(s)  ·  currently v{version}",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(self.body, text="What's new / what was done?").pack(anchor="w")
        self.txt = dark_text(self.body, height=5, width=58)
        self.txt.pack(fill="x", pady=(6, 10))
        self.bump = tk.StringVar(value="patch")
        self.preview = tk.StringVar()
        rf = ttk.Frame(self.body)
        rf.pack(anchor="w")
        ttk.Label(rf, text="Version:").pack(side="left", padx=(0, 6))
        for label, val in (("No bump", "none"), ("Patch", "patch"),
                           ("Minor", "minor"), ("Major", "major")):
            ttk.Radiobutton(rf, text=label, value=val, variable=self.bump,
                            command=lambda: self._upd(version)).pack(side="left", padx=4)
        ttk.Label(rf, textvariable=self.preview, foreground=GREEN,
                  font=(FONT, 10, "bold")).pack(side="left", padx=10)
        self._upd(version)
        self.buttons("Sync", self._ok)
        self.txt.focus_set()

    def _upd(self, version):
        self.preview.set(f"→ v{bump_version(version, self.bump.get())}")

    def _ok(self):
        self.result = (self.txt.get("1.0", "end").strip(), self.bump.get())
        self.destroy()


# ─────────────────────────────── help text ──────────────────────────────
# Simple markup: "# " = heading, "- " = bullet, "> " = tip, blank = paragraph break

HELP_TOPICS = [
    ("Getting started", """
# Welcome to PickettVault
PickettVault keeps your project folders on this PC in sync with a dedicated USB stick, keeps a copy of every file it replaces, and writes a version-numbered changelog each time you sync.

# First-time setup
- Rename your USB stick's label to PICKETVAULT (File Explorer → right-click the drive → Rename).
- Plug it in and click Detect USB. The drive appears at the top of the window.
- Click Add project, type a name (e.g. PickettPanel) and pick its folder on this PC.

# Everyday use
- Select a project on the left.
- Click Scan to preview what's changed.
- Check the list, then click Sync.
- Type a short note about what you did and choose a version bump.

> No USB handy? File → Choose vault folder… lets you use any folder as the vault.
"""),
    ("How sync works", """
# The idea
After each sync, PickettVault remembers exactly what every file looked like on both sides. Next time, it compares each side against that memory, so it knows which side actually changed.

# What each action means
- PC → USB: the file is new or changed on this PC, so it's copied to the stick.
- USB → PC: the file is new or changed on the stick (e.g. you edited it on another computer), so it's copied here.
- Delete on USB / Delete on PC: you deleted it on the other side, so it's removed here too. A snapshot is kept first.
- Skip: nothing happens to this file this time. It will show up again on the next scan.

# Changing an action
Double-click any row in the preview to change what happens to it. Normal rows switch between their action and Skip.

# Version numbers
- Patch (1.0.0 → 1.0.1): small fixes and tweaks.
- Minor (1.0.1 → 1.1.0): new features or content.
- Major (1.1.0 → 2.0.0): big releases or breaking changes.
Every sync adds an entry to CHANGELOG.md in the project folder (on both the PC and the USB) with your note, the date, the computer name and the files that changed.

# Using more than one PC
Each computer keeps its own folder location for each project. On a new PC, select the project and click Set PC folder. Projects shown in amber aren't set up on this PC yet.
"""),
    ("Conflicts & snapshots", """
# Conflicts
A conflict means the same file changed on both the PC and the USB since the last sync, so PickettVault can't know which one you want. Conflicts are highlighted red and skipped by default.

Double-click a conflict to cycle through:
- Skip: leave both alone for now.
- Keep PC version: the PC copy overwrites the USB copy.
- Keep USB version: the USB copy overwrites the PC copy.

# Snapshots – nothing is ever lost
Before any file is overwritten or deleted, the old copy is moved into:
PICKETVAULT:\\.pickettvault\\versions\\<project>\\<date_time>\\pc or usb\\

So if you pick the wrong side in a conflict, the other version is still there.

> The .pickettvault folder is hidden. To see it, in File Explorer choose View → Show → Hidden items.

# Ignored files
Temporary and system files are never synced: .git, __pycache__, node_modules, Thumbs.db, desktop.ini, Office lock files (~$…) and *.tmp.
"""),
    ("Mirror & off-site backup", """
# Why mirror?
A USB stick can be lost or fail without warning. The mirror keeps a full copy of the whole vault – projects, snapshots and changelogs – on a second drive.

# Setting it up
- Click Mirror settings… and pick a folder on your backup drive (e.g. the 3 TB desktop drive).
- Choose whether to mirror automatically after every sync. Yes is recommended.
- Let the first mirror run. It copies everything once; after that only changes are copied.

The mirror lives in <your folder>\\PICKETVAULT_MIRROR.

# Nothing is deleted from the mirror
Files removed from the USB are moved into PICKETVAULT_MIRROR\\_removed\\<date_time>\\ instead of being deleted.

# Off-site copy with Google Drive
- Install Google Drive for desktop and sign in.
- Settings → Preferences → Add folder → choose PICKETVAULT_MIRROR.
- Pick Sync with Google Drive.
It then uploads after every mirror. You'll find it under Computers on drive.google.com.

# If the USB is lost
Copy PICKETVAULT_MIRROR onto a new stick, rename the stick to PICKETVAULT, and carry on.
"""),
    ("Troubleshooting", """
# "No PICKETVAULT drive found"
- Check the stick's label is exactly PICKETVAULT.
- Unplug it, plug it back in, then click Detect USB.
- Or use File → Choose vault folder… and pick the drive by hand.

# Files from other projects show up in a scan
The project's PC folder is set to a parent folder (e.g. D:\\PICKETTECH) instead of its own folder. Select the project, click Set PC folder and pick its own folder. PickettVault now blocks folders that contain another project's folder.

# Removing a project
Select it and click Remove. Your PC folder is never touched. If the USB still has files for it, you can leave them or move them to .pickettvault\\removed_projects.

# "PC folder isn't set"
The project was created on a different computer. Select it and click Set PC folder.

# "Backup folder not found" when mirroring
The backup drive is disconnected or its drive letter changed. Reconnect it, or pick the folder again in Mirror settings….

# A file failed to copy
Usually it's open in another program (Word, KiCad, a slicer…). Close it and sync again – anything that failed is picked up on the next scan.

# Always eject safely
Use Safely Remove Hardware before unplugging the stick, especially straight after a sync.
"""),
]


class HelpWindow:
    _open = None

    @classmethod
    def show(cls, app, topic=0):
        if cls._open and cls._open.win.winfo_exists():
            cls._open.select(topic)
            cls._open.win.lift()
            return
        cls._open = cls(app, topic)

    def __init__(self, app, topic):
        self.win = tk.Toplevel(app.win)
        self.win.title(f"{APP_NAME} Help")
        self.win.configure(bg=BG)
        self.win.geometry("860x560")
        self.win.minsize(640, 400)
        if app.icon:
            self.win.iconphoto(False, app.icon)

        head = ttk.Frame(self.win, padding=(16, 12))
        head.pack(fill="x")
        app.brand(head)
        ttk.Label(head, text="Help", style="Muted.TLabel",
                  font=(FONT, 12)).pack(side="left", padx=(10, 0), pady=(8, 0))

        main = ttk.Frame(self.win, padding=(16, 0, 16, 16))
        main.pack(fill="both", expand=True)
        self.topics = tk.Listbox(main, bg=PANEL, fg=WHITE, relief="flat", width=24,
                                 highlightthickness=0, activestyle="none", font=(FONT, 10),
                                 selectbackground=BLUE, selectforeground="white",
                                 exportselection=False)
        for t, _ in HELP_TOPICS:
            self.topics.insert("end", "  " + t)
        self.topics.pack(side="left", fill="y", padx=(0, 12))
        self.topics.bind("<<ListboxSelect>>", lambda e: self._render())

        tf = ttk.Frame(main)
        tf.pack(side="left", fill="both", expand=True)
        self.text = dark_text(tf, padx=18, pady=12, spacing1=2, spacing3=2, cursor="arrow")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_configure("h", foreground=BLUE, font=(FONT, 12, "bold"),
                                spacing1=12, spacing3=4)
        self.text.tag_configure("p", foreground=WHITE, spacing3=6)
        self.text.tag_configure("li", foreground=WHITE, lmargin1=12, lmargin2=28)
        self.text.tag_configure("tip", foreground=GREEN, lmargin1=12, lmargin2=12,
                                spacing1=8, font=(FONT, 10, "italic"))
        self.win.bind("<Escape>", lambda e: self.win.destroy())
        dark_titlebar(self.win)
        self.select(topic)

    def select(self, i):
        self.topics.selection_clear(0, "end")
        self.topics.selection_set(i)
        self._render()

    def _render(self):
        sel = self.topics.curselection()
        if not sel:
            return
        title, body = HELP_TOPICS[sel[0]]
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")
        for line in body.strip("\n").splitlines():
            if line.startswith("# "):
                t.insert("end", line[2:] + "\n", "h")
            elif line.startswith("- "):
                t.insert("end", "•  " + line[2:] + "\n", "li")
            elif line.startswith("> "):
                t.insert("end", "Tip: " + line[2:] + "\n", "tip")
            elif line.strip():
                t.insert("end", line + "\n", "p")
        t.config(state="disabled")
        t.yview_moveto(0)


class App:
    def __init__(self, root):
        self.win = root
        root.title(f"{APP_NAME} v{APP_VERSION} — PICKETTECH")
        root.geometry("1180x740")
        root.minsize(900, 560)
        self.vault = None
        self.actions, self.forget = [], []
        self.plan_for = None
        self.busy = False
        self.q = queue.Queue()
        self.buttons = []
        self._load_images()
        self._build_menu()
        self._build()
        dark_titlebar(root)
        self.detect()
        root.after(100, self._poll)

    # ── branding ──
    def _load_images(self):
        self.logo = self.icon = None
        try:
            self.logo = tk.PhotoImage(data=b64(LOGO_PNG))
            self.icon = tk.PhotoImage(data=b64(ICON_PNG))
            self.win.iconphoto(True, self.icon)
            self.win._pv_icon = self.icon
        except tk.TclError:
            pass  # very old Tk without PNG support — run without images

    def brand(self, parent):
        """PICKETTECH logo + PICKETT(white) VAULT(blue) wordmark."""
        if self.logo:
            tk.Label(parent, image=self.logo, bg=BG, bd=0).pack(side="left")
            tk.Frame(parent, bg=BORDER, width=2, height=40).pack(side="left", padx=16)
        word = ttk.Frame(parent)
        word.pack(side="left")
        tk.Label(word, text="PICKETT", fg=WHITE, bg=BG, bd=0,
                 font=(FONT, 22, "bold")).pack(side="left")
        tk.Label(word, text="VAULT", fg=BLUE, bg=BG, bd=0,
                 font=(FONT, 22, "bold")).pack(side="left")

    # ── menus ──
    def _build_menu(self):
        m = tk.Menu(self.win)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Detect USB", command=self.detect, accelerator="F5")
        f.add_command(label="Choose vault folder…", command=self.choose)
        f.add_separator()
        f.add_command(label="Mirror now", command=self.mirror_now)
        f.add_command(label="Mirror settings…", command=self.mirror_settings)
        f.add_separator()
        f.add_command(label="Exit", command=self.win.destroy)
        m.add_cascade(label="File", menu=f)

        p = tk.Menu(m, tearoff=0)
        p.add_command(label="Add project…", command=self.add_project)
        p.add_command(label="Set PC folder…", command=self.set_pc)
        p.add_command(label="Remove project…", command=self.remove_project)
        p.add_separator()
        p.add_command(label="Scan", command=self.scan, accelerator="Ctrl+R")
        p.add_command(label="Sync", command=self.sync, accelerator="Ctrl+S")
        p.add_separator()
        p.add_command(label="Open PC folder", command=self.open_pc)
        p.add_command(label="Open USB folder", command=self.open_usb)
        m.add_cascade(label="Project", menu=p)

        h = tk.Menu(m, tearoff=0)
        for i, (title, _) in enumerate(HELP_TOPICS):
            h.add_command(label=title, command=lambda i=i: HelpWindow.show(self, i),
                          accelerator="F1" if i == 0 else "")
        h.add_separator()
        h.add_command(label=f"About {APP_NAME}", command=self.about)
        m.add_cascade(label="Help", menu=h)
        self.win.config(menu=m)

        self.win.bind("<F1>", lambda e: HelpWindow.show(self, 0))
        self.win.bind("<F5>", lambda e: self.detect())
        self.win.bind("<Control-r>", lambda e: self.scan())
        self.win.bind("<Control-s>", lambda e: self.sync())

    # ── layout ──
    def _btn(self, parent, text, cmd, style="TButton", **pack):
        b = ttk.Button(parent, text=text, command=cmd, style=style)
        b.pack(**pack)
        self.buttons.append(b)
        return b

    def _tree(self, parent, **kw):
        f = ttk.Frame(parent)
        tv = ttk.Treeview(f, **kw)
        sb = ttk.Scrollbar(f, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tv.pack(side="left", fill="both", expand=True)
        return f, tv

    def _build(self):
        head = ttk.Frame(self.win, padding=(16, 12, 16, 8))
        head.pack(fill="x")
        self.brand(head)
        ttk.Label(head, text=f"v{APP_VERSION}", style="Muted.TLabel").pack(side="left", padx=(8, 0), pady=(12, 0))
        ttk.Button(head, text="?  Help", style="Accent.TButton",  # stays enabled while busy
                   command=lambda: HelpWindow.show(self, 0)).pack(side="right", padx=(18, 0))
        self._btn(head, "Mirror now", self.mirror_now, side="right")
        self._btn(head, "Mirror settings…", self.mirror_settings, side="right", padx=6)

        bar = ttk.Frame(self.win, style="Panel.TFrame", padding=(16, 6))
        bar.pack(fill="x")
        self.status = tk.StringVar()
        self.mirror_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.status, style="Panel.TLabel",
                  font=(FONT, 10, "bold")).pack(side="left")
        self._btn(bar, "Detect USB", self.detect, side="left", padx=(16, 4))
        self._btn(bar, "Choose vault…", self.choose, side="left")
        ttk.Label(bar, textvariable=self.mirror_var, style="PanelMuted.TLabel").pack(side="right")

        pane = ttk.PanedWindow(self.win, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=16, pady=12)

        left = ttk.Frame(pane)
        pane.add(left, weight=1)
        ttk.Label(left, text="PROJECTS", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        pf, self.proj = self._tree(left, columns=("ver", "last"), selectmode="browse")
        self.proj.heading("#0", text="Project")
        self.proj.heading("ver", text="Version")
        self.proj.heading("last", text="Last sync")
        self.proj.column("#0", width=150)
        self.proj.column("ver", width=70, anchor="center")
        self.proj.column("last", width=120)
        self.proj.tag_configure("nopc", foreground=AMBER)
        pf.pack(fill="both", expand=True)
        self.proj.bind("<<TreeviewSelect>>", lambda e: self.on_select())
        pb = ttk.Frame(left)
        pb.pack(fill="x", pady=(8, 0))
        self._btn(pb, "+ Add project", self.add_project, style="Accent.TButton", side="left")
        self._btn(pb, "Set PC folder", self.set_pc, side="left", padx=6)
        pb2 = ttk.Frame(left)
        pb2.pack(fill="x", pady=(6, 0))
        self._btn(pb2, "Open PC", self.open_pc, side="left")
        self._btn(pb2, "Open USB", self.open_usb, side="left", padx=6)
        self._btn(pb2, "Remove", self.remove_project, side="left")

        right = ttk.Frame(pane)
        pane.add(right, weight=3)
        rt = ttk.Frame(right)
        rt.pack(fill="x")
        self.title_var = tk.StringVar(value="Select a project")
        self.sub_var = tk.StringVar()
        tb = ttk.Frame(rt)
        tb.pack(side="left", fill="x", expand=True)
        ttk.Label(tb, textvariable=self.title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(tb, textvariable=self.sub_var, style="Muted.TLabel").pack(anchor="w")
        self._btn(rt, "Sync", self.sync, style="Accent.TButton", side="right")
        self._btn(rt, "Scan", self.scan, side="right", padx=6)

        ttk.Label(right, text="Double-click a row to change what happens to it.  ·  F1 for help",
                  style="Muted.TLabel").pack(anchor="w", pady=(8, 4))
        tf, self.plan = self._tree(right, columns=("action", "detail"), height=11)
        self.plan.heading("#0", text="File")
        self.plan.heading("action", text="Action")
        self.plan.heading("detail", text="Details")
        self.plan.column("#0", width=380)
        self.plan.column("action", width=220)
        self.plan.column("detail", width=180)
        self.plan.tag_configure("conflict", background=RED_BG, foreground=RED_FG)
        self.plan.tag_configure("skip", foreground="#5D6570")
        self.plan.tag_configure("delete", foreground=RED_FG)
        self.plan.tag_configure("ok", foreground=GREEN)
        tf.pack(fill="both", expand=True)
        self.plan.bind("<Double-1>", self.on_toggle)

        pf2 = ttk.Frame(right)
        pf2.pack(fill="x", pady=(8, 0))
        self.pbar = ttk.Progressbar(pf2, mode="determinate", style="Blue.Horizontal.TProgressbar")
        self.pbar.pack(side="left", fill="x", expand=True)
        self.prog = tk.StringVar()
        ttk.Label(pf2, textvariable=self.prog, width=48, style="Muted.TLabel").pack(side="left", padx=8)

        ttk.Label(right, text="CHANGELOG", style="Section.TLabel").pack(anchor="w", pady=(12, 4))
        self.log = dark_text(right, height=7, font=(MONO, 9), state="disabled")
        self.log.pack(fill="both", expand=True)

    # ── dialogs ──
    def about(self):
        d = DarkDialog(self.win, f"About {APP_NAME}")
        top = ttk.Frame(d.body)
        top.pack(anchor="w")
        self.brand(top)
        ttk.Label(d.body, text=f"Version {APP_VERSION}", style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
        ttk.Label(d.body, text="USB project sync, snapshots, changelogs and mirror backup.\n"
                               "A PICKETTECH tool.", justify="left").pack(anchor="w", pady=(8, 0))
        tag = ttk.Frame(d.body)
        tag.pack(anchor="w", pady=(10, 0))
        for word, col in (("Learn . ", WHITE), ("Build", BLUE), (" . Innovate", WHITE)):
            tk.Label(tag, text=word, fg=col, bg=BG, font=(FONT, 10, "italic bold")).pack(side="left")
        bf = ttk.Frame(d.body)
        bf.pack(fill="x", pady=(14, 0))
        ttk.Button(bf, text="Close", style="Accent.TButton", command=d.destroy).pack(side="right")
        d.show()

    # ── background work ──
    def run_bg(self, fn, done):
        self.busy = True
        for b in self.buttons:
            b.state(["disabled"])

        def worker():
            try:
                self.q.put(("done", done, fn(), None))
            except Exception as e:
                self.q.put(("done", done, None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    _, i, n, rel = msg
                    self.pbar["maximum"], self.pbar["value"] = n, i
                    self.prog.set(f"{i}/{n}  {rel[-44:]}")
                else:
                    _, done, res, err = msg
                    self.busy = False
                    for b in self.buttons:
                        b.state(["!disabled"])
                    if err:
                        messagebox.showerror(APP_NAME, f"Something went wrong:\n\n{err}")
                    else:
                        done(res)
        except queue.Empty:
            pass
        self.win.after(100, self._poll)

    # ── vault ──
    def detect(self):
        if self.busy:
            return
        drive = find_vault_drive()
        if drive:
            self.open_vault(drive)
        else:
            self.vault = None
            self.status.set(f"●  No '{VAULT_LABEL}' drive found — plug it in, or choose a folder")
            self.update_mirror_label()
            self.refresh_projects()

    def choose(self):
        if self.busy:
            return
        path = filedialog.askdirectory(title="Choose the vault (USB root or any folder)")
        if path:
            self.open_vault(path)

    def open_vault(self, path):
        try:
            self.vault = Vault(path)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Can't open vault at {path}:\n{e}")
            return
        self.status.set(f"●  Vault: {path}      This PC: {COMPUTER}")
        self.update_mirror_label()
        self.refresh_projects()

    def need_vault(self):
        if not self.vault:
            messagebox.showinfo(APP_NAME, "Plug in the vault USB (or choose a folder) first.")
            return False
        return True

    def current(self):
        sel = self.proj.selection()
        return sel[0] if sel else None

    def refresh_projects(self, select=None):
        select = select or self.current()
        self.proj.delete(*self.proj.get_children())
        if not self.vault:
            return
        for name in sorted(self.vault.projects, key=str.lower):
            p = self.vault.projects[name]
            tags = () if self.vault.pc_dir(name) else ("nopc",)
            self.proj.insert("", "end", iid=name, text=name, tags=tags,
                             values=(f"v{p.get('version', '1.0.0')}", p.get("last_sync") or "never"))
        if select and self.proj.exists(select):
            self.proj.selection_set(select)

    def on_select(self):
        name = self.current()
        self.plan.delete(*self.plan.get_children())
        self.actions, self.forget, self.plan_for = [], [], None
        self.pbar["value"] = 0
        self.prog.set("")
        if not name:
            self.title_var.set("Select a project")
            self.sub_var.set("")
            return
        pc = self.vault.pc_dir(name)
        self.title_var.set(f"{name}   v{self.vault.projects[name].get('version', '1.0.0')}")
        self.sub_var.set(f"PC folder: {pc or '(not set on this PC — click Set PC folder)'}")
        self.show_changelog(name)

    def show_changelog(self, name):
        path = os.path.join(self.vault.usb_dir(name), CHANGELOG)
        text = "No syncs yet."
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.insert("1.0", text)
        self.log.config(state="disabled")

    # ── project buttons ──
    def add_project(self):
        if self.busy or not self.need_vault():
            return
        name = AskDialog(self.win, "Add project", "Project name (e.g. PickettPanel):").show()
        if not name:
            return
        name = name.strip()
        if not re.fullmatch(r"[\w\- .]+", name):
            messagebox.showerror(APP_NAME, "Use letters, numbers, spaces, - _ or . only.")
            return
        if name in self.vault.projects:
            messagebox.showerror(APP_NAME, f"'{name}' already exists.")
            return
        folder = filedialog.askdirectory(title=f"Folder for {name} on this PC")
        if not folder:
            return
        problem = folder_problem(self.vault, name, folder)
        if problem:
            messagebox.showerror(APP_NAME, problem)
            return
        self.vault.add_project(name, os.path.normpath(folder))
        self.refresh_projects(select=name)

    def set_pc(self):
        name = self.current()
        if not name or self.busy:
            return
        folder = filedialog.askdirectory(title=f"Folder for {name} on {COMPUTER}")
        if folder:
            problem = folder_problem(self.vault, name, folder)
            if problem:
                messagebox.showerror(APP_NAME, problem)
                return
            self.vault.set_pc_dir(name, os.path.normpath(folder))
            self.refresh_projects(select=name)
            self.on_select()

    def remove_project(self):
        name = self.current()
        if self.busy or not name:
            return
        if not messagebox.askyesno(APP_NAME, f"Remove '{name}' from PickettVault?\n\n"
                                   "Your PC folder is NOT touched."):
            return
        usb = self.vault.usb_dir(name)
        keep = True
        if os.path.isdir(usb) and os.listdir(usb):
            keep = not messagebox.askyesno(
                APP_NAME, f"The USB still has files for '{name}'.\n\n"
                "Yes = move them to the vault's hidden removed_projects folder\n"
                "No = leave them on the USB where they are")
        try:
            self.vault.remove_project(name, keep_usb_files=keep)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't remove '{name}':\n{e}")
            return
        self.refresh_projects()
        self.on_select()

    def _open(self, path):
        if path and os.path.isdir(path) and os.name == "nt":
            os.startfile(path)

    def open_pc(self):
        name = self.current()
        if name:
            self._open(self.vault.pc_dir(name))

    def open_usb(self):
        name = self.current()
        if name:
            self._open(self.vault.usb_dir(name))

    # ── scan & sync ──
    def scan(self):
        name = self.current()
        if self.busy or not name or not self.need_vault():
            return
        pc = self.vault.pc_dir(name)
        if not pc or not os.path.isdir(pc):
            messagebox.showwarning(APP_NAME, "This project's PC folder isn't set or doesn't "
                                             "exist on this computer. Use 'Set PC folder'.")
            return
        problem = folder_problem(self.vault, name, pc)
        if problem:
            messagebox.showwarning(APP_NAME, f"{name}'s PC folder needs fixing before scanning:\n\n"
                                   f"{problem}\n\nUse 'Set PC folder' to choose the right one.")
            return
        usb, state = self.vault.usb_dir(name), self.vault.load_state(name)
        self.prog.set("Scanning…")
        self.run_bg(lambda: build_plan(pc, usb, state),
                    lambda res: self.scan_done(name, res))

    def scan_done(self, name, res):
        self.actions, self.forget = res
        self.plan_for = name
        self.prog.set("")
        self.plan.delete(*self.plan.get_children())
        shown = 0
        for i, a in enumerate(self.actions):
            if a["kind"] == "same":
                continue
            self.plan.insert("", "end", iid=str(i), text=a["rel"],
                             values=(action_text(a), a["detail"]), tags=self._tags(a))
            shown += 1
        if not shown:
            self.plan.insert("", "end", iid="ok", text="✓  Everything is in sync", tags=("ok",))
        else:
            self.prog.set(f"{shown} change(s) found — review, then Sync")

    def _tags(self, a):
        if a["kind"] == "conflict":
            return ("conflict",)
        if a["choice"] == "skip":
            return ("skip",)
        return ("delete",) if a["kind"].startswith("del") else ()

    def on_toggle(self, event):
        iid = self.plan.identify_row(event.y)
        if self.busy or not iid or not iid.isdigit():
            return
        a = self.actions[int(iid)]
        if a["kind"] == "conflict":
            order = ["skip", "keep_pc", "keep_usb"]
            a["choice"] = order[(order.index(a["choice"]) + 1) % 3]
        else:
            a["choice"] = "skip" if a["choice"] == "default" else "default"
        self.plan.item(iid, values=(action_text(a), a["detail"]), tags=self._tags(a))

    def sync(self):
        name = self.current()
        if self.busy or not name:
            return
        if self.plan_for != name:
            messagebox.showinfo(APP_NAME, "Click Scan first to preview the changes.")
            return
        real = [a for a in self.actions if resolve(a) and a["kind"] != "same"]
        note = ("", "none")
        if real:
            p = self.vault.projects[name]
            note = NoteDialog(self.win, name, p.get("version", "1.0.0"), len(real)).show()
            if note is None:
                return
        pc, usb = self.vault.pc_dir(name), self.vault.usb_dir(name)
        state, snap = self.vault.load_state(name), self.vault.snap_root(name)
        actions, forget = self.actions, self.forget

        def work():
            prog = lambda i, n, rel: self.q.put(("progress", i, n, rel))
            return apply_plan(pc, usb, snap, state, actions, forget, prog)
        self.run_bg(work, lambda res: self.sync_done(name, pc, usb, note, res))

    def sync_done(self, name, pc, usb, note, res):
        new_state, done, files, errors = res
        p = self.vault.projects[name]
        if files:
            p["version"] = bump_version(p.get("version", "1.0.0"), note[1])
            try:
                write_changelog(pc, usb, name, p["version"], note[0], files, done)
                ps = stat_sig(os.path.join(pc, CHANGELOG))
                us = stat_sig(os.path.join(usb, CHANGELOG))
                if ps and us:
                    new_state[CHANGELOG] = {"pc": ps, "usb": us}
            except OSError as e:
                errors.append(f"{CHANGELOG}: {e}")
            p["last_sync"] = now_str()
            p["last_note"] = note[0]
        self.vault.save_state(name, new_state)
        self.vault.save()
        self.refresh_projects(select=name)
        self.on_select()
        msg = (f"{done['to_usb']} copied to USB, {done['to_pc']} copied to PC, "
               f"{done['deleted']} deleted.\nNow at v{p['version']}.")
        if errors:
            msg += "\n\nProblems:\n" + "\n".join(errors[:10])
            messagebox.showwarning(APP_NAME, msg)
        else:
            self.prog.set("Sync complete ✓")
            messagebox.showinfo(APP_NAME, msg)
        m = self.vault.mirror()
        if files and m and m.get("auto"):
            self.mirror_now(quiet=True)

    # ── mirror ──
    def update_mirror_label(self):
        m = self.vault.mirror() if self.vault else None
        if not m or not m.get("path"):
            self.mirror_var.set("Mirror: not set up on this PC")
            return
        auto = "auto" if m.get("auto") else "manual"
        self.mirror_var.set(f"Mirror: {os.path.join(m['path'], MIRROR_DIR)}  ·  {auto}"
                            f"  ·  last {m.get('last') or 'never'}")

    def mirror_settings(self):
        if self.busy or not self.need_vault():
            return
        folder = filedialog.askdirectory(title="Pick a folder on your backup drive")
        if not folder:
            return
        folder = os.path.normpath(folder)
        if os.path.normcase(folder).startswith(os.path.normcase(os.path.normpath(self.vault.root))):
            messagebox.showerror(APP_NAME, "The mirror must be on a different drive from the vault.")
            return
        auto = messagebox.askyesno(APP_NAME, "Mirror the whole vault automatically after every sync?")
        self.vault.set_mirror(folder, auto)
        self.update_mirror_label()
        if messagebox.askyesno(APP_NAME, "Run the first mirror now? (This can take a while.)"):
            self.mirror_now()

    def mirror_now(self, quiet=False):
        if self.busy or not self.need_vault():
            return
        m = self.vault.mirror()
        if not m or not m.get("path"):
            self.mirror_settings()
            return
        if not os.path.isdir(m["path"]):
            messagebox.showerror(APP_NAME, f"Backup folder not found:\n{m['path']}\n\nIs the drive connected?")
            return
        src, dst = self.vault.root, os.path.join(m["path"], MIRROR_DIR)

        def work():
            return mirror_vault(src, dst, lambda i, n, r: self.q.put(("progress", i, n, r)))

        def done(res):
            m["last"] = now_str()
            self.vault.save()
            self.update_mirror_label()
            self.prog.set(f"Mirror complete ✓  ({res['copied']} copied)")
            if res["errors"]:
                messagebox.showwarning(APP_NAME, "Mirror finished with problems:\n\n"
                                       + "\n".join(res["errors"][:10]))
            elif not quiet:
                messagebox.showinfo(APP_NAME, f"Mirror complete.\n{res['copied']} file(s) copied, "
                                              f"{res['removed']} moved to _removed.")
        self.prog.set("Mirroring…")
        self.run_bg(work, done)


# ───────────────────── embedded PICKETTECH artwork (PNG, base64) ─────────────────────

LOGO_PNG = """
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
e70en67rHXV1da+Vll7lzc3NGSOllEKI7DVr1lyyaNHSo2OxWJvL5XJ5PJ7hRISTSo6ZqJSSSilv
OBx57Zlnnj80Go0uTfbtYQDQ0tJyelZW1q2xWAwbN24OVFRUHBuNRh/Tdd2laXTfZZddlnbyyTPe
ME0THo9n9JQpU9KIyBk+fHi5z+cram1tXe7z+f6wfef2SR2h0GjTNFc++eS/D3njjTemWZZlZWVl
HD9t2uFFEydO+Li2tq6CiHQiarVte+6LL1a+MGfO+f/IycmZvGPHjtfXrPlswpYtW37oOI7p9/tv
evjhJ7PxNdLo7x95SxPkYTgl/xRTeIgECRGPOOnbN25HYJGO+dAR4MRfaammARwvGH2s9KZBKFvn
jlZEW1oaABBC7/VdySApLJ7hIBAQe/4Si+w/zqGcJIdApB/m9XpBRJH09PS28vJyrUdVTx60igkT
Rv8iJcWf09zcuvi88+Y/Wl5evn3Tpq3vAVCG4Trk4osv9p955qynbr75pmHPPvvirbW1ddeNHz9+
DsApsVisaeXKlTuYOXv48KH5tm2LTz9d8dDIkSPPHzRo0DMffbTOLwijmVkLd3fPy8/Pf3DOnItq
lEI3ACGl3AYAXV1dA58Y5VAC4NR457mOAgTBE00r8tSnjBoZzxxsxISPoRQbLhS4Bg1zA6BY3rhM
4UvV9Lbtu/XapXUEICU3Zarh82tsWR2djZ93FJb8oNCtizyOhYXdteujKVMKfJZuZIOAuKU2PH/s
samUlnkm4mFlrH7v+dZ//GJ+6Knf3JQR+nwZudwkYmFKc+rzv4pWl5qaejwAjsfjzRkZGaK09JjB
lmX5NE3Tdu/efU1JSclLzc0NO4igEgy31sHMYKYMIYQWCoVeLygY9Js5cy6q8fl87cwsbNvuAEAu
lzsohOB4PP74+PFjb7v00ks/9vv9l9q2vXPQoPzCe++999Dhw4e3hkKhrYZhuF944QXftm3bJum6
/gslZXzDhg13MjONHDbynRknzhi9YMGCM2bPPuUHZ5555o8BhmVZ8c+3dnqJKOooZ6umaaKxsfFD
l8s1//zzzz/S5/Od1tXV1bhkyZLrjjnmmK2dnZ3vMnNLampq5g9/ePLQnnwd3zCnwAQiVTjz6rEd
/p8Xs7QhiITTtGPn9nXPRrDuWWcvSh3AeYD/VSd2iu3YgOFy6dGWFZEXr3kWU+bpqDxAfoZv0cXy
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
"""

ICON_PNG = """
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
"""


def main():
    if tk is None:
        print("Tkinter is required — reinstall Python from python.org with Tcl/Tk ticked.")
        sys.exit(1)
    if os.name == "nt":
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    apply_theme(root)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
