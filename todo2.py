#!/usr/bin/env python3
"""Shared color-coded deadline list (Tkinter + SQLite).

Click a deadline to open its hyperlink.
Right-click a deadline to set its color:
  green  = OK
  blue   = not urgent
  yellow = near term important
  red    = urgent non-extendable

Share the list by pointing every copy of this program at the same .db file.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sqlite3
import sys
import webbrowser
from datetime import date, datetime

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Shared Deadline List"
CONFIG_NAME = "todo_config.json"
POLL_MS = 5000

COLORS = {
    "green": {"label": "OK", "bg": "#c8e6c9", "fg": "#1b5e20"},
    "blue": {"label": "Not urgent", "bg": "#bbdefb", "fg": "#0d47a1"},
    "yellow": {"label": "Near term important", "bg": "#fff59d", "fg": "#5d4037"},
    "red": {"label": "Urgent non-extendable", "bg": "#ffcdd2", "fg": "#b71c1c"},
}
COLOR_ORDER = ("green", "blue", "yellow", "red")

COLUMNS = ("title", "assignee", "deadline", "status", "link", "updated")
HEADINGS = {
    "title": "Task",
    "assignee": "Assignee",
    "deadline": "Deadline",
    "status": "Urgency",
    "link": "Hyperlink",
    "updated": "Updated",
}
SORT_FIELD = {
    "title": "title",
    "assignee": "assignee",
    "deadline": "deadline",
    "status": "color",
    "link": "hyperlink",
    "updated": "updated_at",
}
WIDTHS = {
    "title": 260,
    "assignee": 120,
    "deadline": 210,
    "status": 180,
    "link": 240,
    "updated": 170,
}

HELP_TEXT = """Each person runs this same program.

To share deadlines
1. Put one todos.db file in a folder everyone can write to
   (a file share both computers can open).
2. On each computer choose Database and enter that same path.
   You can also start the app with the path as an argument:
     python todo_deadlines.py \\\\server\\team\\todos.db
   or set the TODO_DB environment variable.
3. Set Your name so the other person can see who changed a row.
4. The list reloads every 5 seconds. Refresh or F5 reloads now.

How to use the list
- Click a deadline to open its hyperlink.
- Right-click a deadline to change its color:
    Green  = OK
    Blue   = Not urgent
    Yellow = Near term important
    Red    = Urgent non-extendable
- Double-click a task to edit the date, link, assignee, or notes.
- Color is chosen by hand. It does not change automatically when a date gets close.
  The deadline column still shows "today", "in N days", or "N days overdue".

Put the other person's name in Assignee when a deadline is theirs.

Backup
Use File > Backup database now and then, and keep that copy off the share.

Caution
SQLite is one file, not a database server. A normal Windows or macOS file
share is usually fine for a few people editing occasionally, if the share
locks files properly.

Do not store the live database in Dropbox, OneDrive, Google Drive, or a
similar sync folder. Those tools copy the file without real locking and can
corrupt it. If the team gets larger or edits constantly, move this list to
a server database later.
"""

DB_HELP = """Paste the full path to the SQLite file everyone will share.

Examples:
  \\\\server\\team\\todos.db
  /Volumes/team/todos.db
  C:\\Users\\you\\todos.db

If the file does not exist yet, it is created. On a network share the folder
must already exist. Choosing an existing file does not erase it.

Do not put this file in a Dropbox, OneDrive, or Google Drive sync folder.
Those tools can corrupt a live SQLite database.
"""


def app_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def has_display() -> bool:
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def config_path() -> str:
    return os.path.join(app_dir(), CONFIG_NAME)


def is_network_path(path: str) -> bool:
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    if os.name == "nt" and len(path) >= 2 and path[1] == ":":
        try:
            import ctypes

            drive_type = ctypes.windll.kernel32.GetDriveTypeW(path[:2] + "\\")
            if drive_type == 4:  # DRIVE_REMOTE
                return True
        except Exception:
            pass
    parts = os.path.abspath(path).split(os.sep)
    return len(parts) > 2 and parts[1].lower() in {"volumes", "mnt", "media", "net", "nfs"}


def load_settings() -> tuple[str, str]:
    data: dict = {}
    path = config_path()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        db_path = sys.argv[1]
    elif os.environ.get("TODO_DB", "").strip():
        db_path = os.environ["TODO_DB"]
    else:
        db_path = data.get("db_path") or os.path.join(app_dir(), "todos.db")
    name = (data.get("display_name") or "").strip() or getpass.getuser()
    return db_path, name


def save_settings(db_path: str, display_name: str) -> None:
    payload = {"db_path": db_path, "display_name": display_name}
    with open(config_path(), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def configure_connection(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("PRAGMA busy_timeout=5000")
    network = is_network_path(path)
    try:
        if network:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
        else:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() == "wal":
                conn.execute("PRAGMA synchronous=NORMAL")
            else:
                conn.execute("PRAGMA synchronous=FULL")
    except sqlite3.DatabaseError:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            assignee TEXT DEFAULT '',
            deadline TEXT DEFAULT '',
            color TEXT DEFAULT 'green',
            hyperlink TEXT DEFAULT '',
            notes TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            updated_by TEXT DEFAULT ''
        )
        """
    )
    have = {row[1] for row in conn.execute("PRAGMA table_info(todos)")}
    additions = {
        "assignee": "TEXT DEFAULT ''",
        "deadline": "TEXT DEFAULT ''",
        "color": "TEXT DEFAULT 'green'",
        "hyperlink": "TEXT DEFAULT ''",
        "notes": "TEXT DEFAULT ''",
        "updated_at": "TEXT DEFAULT ''",
        "updated_by": "TEXT DEFAULT ''",
    }
    for name, declaration in additions.items():
        if name not in have:
            conn.execute(f"ALTER TABLE todos ADD COLUMN {name} {declaration}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_todos_deadline ON todos (deadline)")


def connect_db(path: str) -> tuple[sqlite3.Connection, str]:
    raw = path.strip().strip('"')
    if not raw:
        raise sqlite3.OperationalError("Database path is empty.")
    norm = os.path.abspath(os.path.expanduser(raw))
    parent = os.path.dirname(norm)
    if parent and not os.path.isdir(parent):
        if is_network_path(norm):
            raise sqlite3.OperationalError(
                f"Cannot find the shared folder:\n{parent}\n\nMount the share, then try again."
            )
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(norm, timeout=5, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        configure_connection(conn, norm)
        ensure_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn, norm


_ALLOWED_PREFIXES = ("http://", "https://", "mailto:", "file://")
_DOMAIN_RE = re.compile(r"^(www\.)?[\w.-]+\.[A-Za-z]{2,}([/:?#].*)?$")
_HOST_RE = re.compile(r"^(localhost|(\d{1,3}\.){3}\d{1,3})(:\d+)?(/.*)?$", re.IGNORECASE)


def normalize_url(url: str) -> str | None:
    """Return a safe URL, '' if blank, or None if it should not be opened."""
    url = url.strip()
    if not url:
        return ""
    if len(url) > 2048 or any(ch in url for ch in "\r\n\t"):
        return None
    lower = url.lower()
    if lower.startswith(_ALLOWED_PREFIXES):
        return url
    if "://" in url or lower.startswith(("javascript:", "data:")):
        return None
    if _HOST_RE.match(url):
        return "http://" + url
    if _DOMAIN_RE.match(url):
        return "https://" + url
    return None


def parse_deadline(text: str) -> str | None:
    """Return ISO date, '' if blank, or None if invalid."""
    text = text.strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def format_deadline(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        due = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return value
    delta = (due - date.today()).days
    if delta == 0:
        relative = "today"
    elif delta == 1:
        relative = "tomorrow"
    elif delta == -1:
        relative = "yesterday"
    elif delta > 1:
        relative = f"in {delta} days"
    else:
        relative = f"{-delta} days overdue"
    return f"{value}  ({relative})"


def one_line(text: str) -> str:
    return " ".join((text or "").split())


def apply_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("Treeview", rowheight=26)
    style.configure("Treeview.Heading", padding=(4, 6))
    style.map(
        "Treeview",
        background=[("selected", "#455a64")],
        foreground=[("selected", "#ffffff")],
    )


class ItemDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, title: str, initial: dict | None = None):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.result: dict | None = None
        initial = initial or {}

        self.title_var = tk.StringVar(value=initial.get("title", ""))
        self.assignee_var = tk.StringVar(value=initial.get("assignee", ""))
        self.deadline_var = tk.StringVar(value=initial.get("deadline", ""))
        self.color_var = tk.StringVar(value=initial.get("color", "green"))
        self.link_var = tk.StringVar(value=initial.get("hyperlink", ""))
        self.notes_var = tk.StringVar(value=initial.get("notes", ""))

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        self._add_field(frame, "Task", self.title_var, required=True)
        self._add_field(frame, "Assignee", self.assignee_var)
        self._add_field(frame, "Deadline (YYYY-MM-DD)", self.deadline_var)

        row = len(frame.grid_slaves()) // 2
        ttk.Label(frame, text="Urgency").grid(row=row, column=0, sticky="w", padx=(0, 8), pady=6)
        urgency_values = [COLORS[name]["label"] for name in COLOR_ORDER]
        urgency_combo = ttk.Combobox(
            frame,
            values=urgency_values,
            textvariable=self.color_var,
            width=30,
            state="readonly",
        )
        urgency_combo.grid(row=row, column=1, sticky="ew", pady=6)
        self.color_var.set(COLORS.get(initial.get("color"), COLORS["green"])["label"])

        self._add_field(frame, "Hyperlink", self.link_var)

        ttk.Label(frame, text="Notes").grid(row=5, column=0, sticky="nw", padx=(0, 8), pady=(8, 4))
        notes = tk.Text(frame, width=60, height=6, wrap="word")
        notes.insert("1.0", initial.get("notes", ""))
        notes.grid(row=5, column=1, sticky="ew", pady=(8, 4))
        self.notes_widget = notes

        button_row = ttk.Frame(frame)
        button_row.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(button_row, text="Save", command=self._save).pack(side="right", padx=(6, 0))
        ttk.Button(button_row, text="Cancel", command=self.destroy).pack(side="right")

        self.bind("<Return>", lambda event: self._save())
        self.bind("<Escape>", lambda event: self.destroy())
        self.title_var.trace_add("write", lambda *_: self._sync_title())
        self._sync_title()

    def _add_field(self, parent: ttk.Frame, label: str, variable: tk.StringVar, required: bool = False) -> None:
        row = len(parent.grid_slaves()) // 2
        ttk.Label(parent, text=label + (" *" if required else "")).grid(
            row=row, column=0, sticky="w", padx=(0, 8), pady=6
        )
        entry = ttk.Entry(parent, textvariable=variable, width=60)
        entry.grid(row=row, column=1, sticky="ew", pady=6)
        if required:
            entry.focus_set()

    def _sync_title(self) -> None:
        title = (self.title_var.get() or "").strip()
        self.title(title or "New item")

    def _save(self) -> None:
        title = one_line(self.title_var.get())
        if not title:
            messagebox.showerror("Missing task", "Please enter a task title.")
            self.title_var.set("")
            self.focus_force()
            return

        deadline = parse_deadline(self.deadline_var.get())
        if deadline is None and self.deadline_var.get().strip():
            messagebox.showerror("Bad date", "Deadline must be blank or in YYYY-MM-DD format.")
            return

        url = normalize_url(self.link_var.get())
        if url is None and self.link_var.get().strip():
            messagebox.showerror("Bad hyperlink", "Hyperlink must be a valid http, https, mailto, or local file URL.")
            return

        selected_color = None
        for name in COLOR_ORDER:
            if COLORS[name]["label"] == self.color_var.get():
                selected_color = name
                break
        selected_color = selected_color or "green"

        self.result = {
            "title": title,
            "assignee": one_line(self.assignee_var.get()),
            "deadline": deadline or "",
            "color": selected_color,
            "hyperlink": url if url is not None else "",
            "notes": self.notes_widget.get("1.0", "end").strip(),
        }
        self.destroy()


class TodoApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x650")
        self.minsize(900, 500)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.db_path, self.display_name = load_settings()
        self.conn: sqlite3.Connection | None = None
        self.current_sort = "deadline"
        self.sort_desc = False
        self._init_ui()
        self._connect_db()
        self.refresh()
        self.after(POLL_MS, self.poll_loop)

    def _init_ui(self) -> None:
        apply_style(self)
        self.menu = tk.Menu(self)
        self.config(menu=self.menu)

        file_menu = tk.Menu(self.menu, tearoff=0)
        file_menu.add_command(label="Set database…", command=self.choose_database)
        file_menu.add_command(label="Backup database now", command=self.backup_database)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        self.menu.add_cascade(label="File", menu=file_menu)

        help_menu = tk.Menu(self.menu, tearoff=0)
        help_menu.add_command(label="Help", command=self.show_help)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=lambda: messagebox.showinfo(APP_NAME, "Shared Deadline List\n\nStay aligned on tasks and deadlines across a shared SQLite database."))
        self.menu.add_cascade(label="Help", menu=help_menu)

        content = ttk.Frame(self, padding=10)
        content.pack(fill="both", expand=True)

        top = ttk.Frame(content)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Your name:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.name_var = tk.StringVar(value=self.display_name)
        self.name_entry = ttk.Entry(top, textvariable=self.name_var, width=24)
        self.name_entry.grid(row=0, column=1, sticky="w")
        self.name_entry.bind("<FocusOut>", self._persist_name)
        self.name_entry.bind("<Return>", self._persist_name)

        ttk.Label(top, text="Database:").grid(row=0, column=2, sticky="w", padx=(8, 8))
        self.db_var = tk.StringVar(value=self.db_path)
        self.db_entry = ttk.Entry(top, textvariable=self.db_var, width=70)
        self.db_entry.grid(row=0, column=3, sticky="ew")
        self.db_entry.bind("<Return>", lambda _event: self.apply_database_path())
        self.db_entry.bind("<FocusOut>", lambda _event: self.apply_database_path())
        ttk.Button(top, text="Choose…", command=self.choose_database).grid(row=0, column=4, sticky="w", padx=(8, 0))
        ttk.Button(top, text="Refresh", command=self.refresh).grid(row=0, column=5, sticky="w", padx=(8, 0))
        top.columnconfigure(3, weight=1)

        action_bar = ttk.Frame(content)
        action_bar.pack(fill="x", pady=(0, 8))
        ttk.Button(action_bar, text="New task", command=self.new_task).pack(side="left")
        ttk.Button(action_bar, text="Edit", command=self.edit_selected_task).pack(side="left", padx=(8, 0))
        ttk.Button(action_bar, text="Delete", command=self.delete_selected_task).pack(side="left", padx=(8, 0))

        self.tree = ttk.Treeview(
            content,
            columns=COLUMNS,
            show="headings",
            selectmode="browse",
            height=25,
        )
        self.tree.pack(fill="both", expand=True)

        for column in COLUMNS:
            self.tree.heading(column, text=HEADINGS[column], command=lambda c=column: self.sort_by(c))
            self.tree.column(column, width=WIDTHS[column], anchor="w", stretch=False)

        y_scroll = ttk.Scrollbar(content, orient="vertical", command=self.tree.yview)
        y_scroll.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=y_scroll.set)

        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<ButtonRelease-1>", self.on_deadline_click)
        self.bind("<F5>", lambda _event: self.refresh())

        self.context_menu = tk.Menu(self, tearoff=0)
        for color_name in COLOR_ORDER:
            self.context_menu.add_command(
                label=f"{COLORS[color_name]['label']}",
                command=lambda name=color_name: self.set_row_color(name),
            )

        self._style_tags()

    def _style_tags(self) -> None:
        for color_name in COLOR_ORDER:
            colors = COLORS[color_name]
            self.tree.tag_configure(color_name, background=colors["bg"], foreground=colors["fg"])

    def _persist_name(self, _event=None) -> None:
        self.display_name = self.name_var.get().strip() or getpass.getuser()
        self.name_var.set(self.display_name)
        save_settings(self.db_path, self.display_name)

    def _connect_db(self) -> None:
        if self.conn is not None:
            self.conn.close()
        try:
            self.conn, self.db_path = connect_db(self.db_var.get())
        except Exception as exc:
            messagebox.showerror("Database error", str(exc))
            self.conn, self.db_path = connect_db(self.db_path)
        self.db_var.set(self.db_path)
        save_settings(self.db_path, self.display_name)

    def poll_loop(self) -> None:
        if self.winfo_exists():
            self.refresh()
            self.after(POLL_MS, self.poll_loop)

    def sort_by(self, column: str) -> None:
        if self.current_sort == column:
            self.sort_desc = not self.sort_desc
        else:
            self.current_sort = column
            self.sort_desc = False
        self.refresh()

    def refresh(self) -> None:
        if self.conn is None:
            self._connect_db()
        try:
            order_field = SORT_FIELD.get(self.current_sort, "deadline")
            direction = "DESC" if self.sort_desc else "ASC"
            rows = self.conn.execute(
                f"SELECT * FROM todos ORDER BY {order_field} {direction}, title ASC"
            ).fetchall()
        except sqlite3.Error as exc:
            messagebox.showerror("Database error", f"Could not read the list: {exc}")
            return

        self.tree.delete(*self.tree.get_children())
        for item in rows:
            status_name = item["color"] if item["color"] in COLORS else "green"
            values = (
                item["title"],
                item["assignee"],
                format_deadline(item["deadline"]),
                COLORS[status_name]["label"],
                item["hyperlink"],
                item["updated_at"] or "",
            )
            task_id = str(item["id"])
            self.tree.insert("", "end", iid=task_id, values=values, tags=(status_name,))

    def on_click(self, _event=None) -> None:
        item_id = self.tree.focus()
        if item_id:
            self.tree.selection_set(item_id)

    def on_deadline_click(self, event=None) -> None:
        region = self.tree.identify_region(event.x, event.y) if event else "cell"
        if region != "cell":
            return
        column = self.tree.identify_column(event.x)
        if column != "#3":
            return
        item_id = self.tree.focus()
        if not item_id:
            return
        task = self.conn.execute("SELECT hyperlink FROM todos WHERE id = ?", (int(item_id),)).fetchone()
        if task is None or not task["hyperlink"]:
            return
        url = normalize_url(task["hyperlink"])
        if url:
            webbrowser.open_new_tab(url)

    def on_double_click(self, _event=None) -> None:
        selected = self.tree.focus()
        if not selected:
            return
        self.edit_task(int(selected))

    def on_right_click(self, event) -> None:
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        self.tree.selection_set(item_id)
        self.context_menu.post(event.x_root, event.y_root)

    def set_row_color(self, color_name: str) -> None:
        selected = self.tree.focus()
        if not selected:
            return
        task_id = int(selected)
        if task_id <= 0:
            return
        self.conn.execute(
            "UPDATE todos SET color = ?, updated_at = ?, updated_by = ? WHERE id = ?",
            (color_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), self.display_name, task_id),
        )
        self.conn.commit()
        self.refresh()

    def apply_database_path(self) -> None:
        value = self.db_var.get().strip()
        if not value:
            return
        try:
            self._connect_db()
            self.refresh()
        except Exception as exc:
            messagebox.showerror("Database error", str(exc))

    def choose_database(self) -> None:
        initial = os.path.dirname(self.db_var.get()) or app_dir()
        path = filedialog.askopenfilename(
            title="Choose shared database",
            initialdir=initial,
            filetypes=[("SQLite database", "*.db *.sqlite *.sqlite3"), ("All files", "*.*")],
        )
        if not path:
            return
        self.db_var.set(path)
        self._connect_db()
        self.refresh()

    def backup_database(self) -> None:
        if not self.db_path:
            return
        target = filedialog.asksaveasfilename(
            title="Backup database",
            defaultextension=".db",
            initialfile="todos-backup.db",
            filetypes=[("SQLite database", "*.db *.sqlite *.sqlite3"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            with sqlite3.connect(self.db_path) as source, sqlite3.connect(target) as dest:
                source.backup(dest)
            messagebox.showinfo("Backup created", f"Database backup saved to:\n{target}")
        except Exception as exc:  # pragma: no cover - UI error path
            messagebox.showerror("Backup failed", str(exc))

    def show_help(self) -> None:
        messagebox.showinfo("Help", HELP_TEXT)

    def nd(self, text: str) -> str:
        return one_line(text)

    def new_task(self) -> None:
        dialog = ItemDialog(self, "New task")
        self.wait_window(dialog)
        if not dialog.result:
            return
        payload = dialog.result
        self.conn.execute(
            """
            INSERT INTO todos (title, assignee, deadline, color, hyperlink, notes, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["title"],
                payload["assignee"],
                payload["deadline"],
                payload.get("color", "green"),
                payload["hyperlink"],
                payload["notes"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self.display_name,
            ),
        )
        self.conn.commit()
        self.refresh()

    def edit_task(self, task_id: int) -> None:
        task = self.conn.execute("SELECT * FROM todos WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            return
        dialog = ItemDialog(
            self,
            "Edit task",
            {
                "title": task["title"],
                "assignee": task["assignee"],
                "deadline": task["deadline"],
                "color": task["color"],
                "hyperlink": task["hyperlink"],
                "notes": task["notes"],
            },
        )
        self.wait_window(dialog)
        if not dialog.result:
            return
        payload = dialog.result
        self.conn.execute(
            """
            UPDATE todos
            SET title = ?, assignee = ?, deadline = ?, color = ?, hyperlink = ?, notes = ?, updated_at = ?, updated_by = ?
            WHERE id = ?
            """,
            (
                payload["title"],
                payload["assignee"],
                payload["deadline"],
                payload.get("color", "green"),
                payload["hyperlink"],
                payload["notes"],
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                self.display_name,
                task_id,
            ),
        )
        self.conn.commit()
        self.refresh()

    def edit_selected_task(self) -> None:
        selected = self.tree.focus()
        if not selected:
            messagebox.showinfo("Select a task", "Choose a task first.")
            return
        self.edit_task(int(selected))

    def delete_selected_task(self) -> None:
        selected = self.tree.focus()
        if not selected:
            messagebox.showinfo("Select a task", "Choose a task first.")
            return
        task_id = int(selected)
        task = self.conn.execute("SELECT title FROM todos WHERE id = ?", (task_id,)).fetchone()
        if task is None:
            return
        if messagebox.askyesno("Delete task", f"Delete '{task['title']}'?"):
            self.conn.execute("DELETE FROM todos WHERE id = ?", (task_id,))
            self.conn.commit()
            self.refresh()

    def on_close(self) -> None:
        self._persist_name()
        if self.conn is not None:
            self.conn.close()
        self.destroy()


def main() -> None:
    if not has_display():
        print(
            "Tkinter GUI requires a display server. "
            "Set DISPLAY or WAYLAND_DISPLAY, or run this on a desktop/VM with a graphical session.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        root = TodoApp()
        root.mainloop()
    except tk.TclError as exc:
        print(f"Tkinter error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

