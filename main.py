import os
import sqlite3
import multiprocessing as mp
import tkinter as tk
from tkinter import ttk, messagebox
import subprocess

import psutil
from bip_utils import (
    Bip39MnemonicGenerator,
    Bip39SeedGenerator,
    Bip44,
    Bip44Coins,
    Bip44Changes
)

DB_FILE = "database.db"
UI_POLL_MS = 150


def format_number(n: int) -> str:
    return format(n, ",").replace(",", " ")


def detect_gpu_name():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=2
        )
        if result.returncode == 0:
            lines = [x.strip() for x in result.stdout.splitlines() if x.strip()]
            if lines:
                return " | ".join(lines)
    except Exception:
        pass
    return "Not detected"


def detect_hardware():
    logical = os.cpu_count() or 4
    try:
        physical = psutil.cpu_count(logical=False) or logical
    except Exception:
        physical = logical

    try:
        ram_total_gb = round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        ram_total_gb = 0

    gpu_name = detect_gpu_name()

    return {
        "cpu_logical": logical,
        "cpu_physical": physical,
        "ram_gb": ram_total_gb,
        "gpu": gpu_name,
    }


def compute_auto_config(hw: dict):
    logical = hw["cpu_logical"]
    ram_gb = hw["ram_gb"]

    if logical <= 4:
        workers = max(1, logical - 1)
    elif logical <= 8:
        workers = max(2, logical - 2)
    else:
        workers = max(4, logical - 2)

    if ram_gb <= 8:
        db_batch_size = 200
        queue_size = 1500
    elif ram_gb <= 16:
        db_batch_size = 500
        queue_size = 4000
    else:
        db_batch_size = 1000
        queue_size = 8000

    return {
        "workers": workers,
        "db_batch_size": db_batch_size,
        "queue_size": queue_size,
    }


def generate_one_wallet():
    mnemonic = Bip39MnemonicGenerator().FromWordsNumber(24)
    seed_bytes = Bip39SeedGenerator(mnemonic).Generate()

    acct = (
        Bip44.FromSeed(seed_bytes, Bip44Coins.ETHEREUM)
        .Purpose()
        .Coin()
        .Account(0)
        .Change(Bip44Changes.CHAIN_EXT)
        .AddressIndex(0)
    )

    mnemonic_str = str(mnemonic)
    private_key = acct.PrivateKey().Raw().ToHex()
    address = acct.PublicKey().ToAddress()

    return mnemonic_str, private_key, address


def address_matches(address: str, prefix: str, suffix: str) -> bool:
    addr = address.lower().replace("0x", "")
    prefix = prefix.lower().strip()
    suffix = suffix.lower().strip()

    if prefix and suffix:
        return addr.startswith(prefix) and addr.endswith(suffix)
    if prefix:
        return addr.startswith(prefix)
    if suffix:
        return addr.endswith(suffix)
    return True


def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mnemonic TEXT NOT NULL,
            private_key TEXT NOT NULL,
            address TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def writer_process(db_queue: mp.Queue, stop_event: mp.Event, db_batch_size: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA synchronous=NORMAL;")

    buffer_rows = []

    def flush():
        nonlocal buffer_rows
        if not buffer_rows:
            return
        cur.executemany("""
            INSERT INTO wallets (mnemonic, private_key, address)
            VALUES (?, ?, ?)
        """, buffer_rows)
        conn.commit()
        buffer_rows.clear()

    while True:
        try:
            item = db_queue.get(timeout=0.3)
        except Exception:
            item = None

        if item is None:
            if stop_event.is_set():
                flush()
                break
        else:
            buffer_rows.append(item)
            if len(buffer_rows) >= db_batch_size:
                flush()

    conn.close()


def worker_process(prefix, suffix, db_queue, result_queue, stop_event, counter, counter_lock):
    local_count = 0

    while not stop_event.is_set():
        mnemonic, private_key, address = generate_one_wallet()

        db_queue.put((mnemonic, private_key, address))
        local_count += 1

        if local_count >= 100:
            with counter_lock:
                counter.value += local_count
            local_count = 0

        if address_matches(address, prefix, suffix):
            if local_count:
                with counter_lock:
                    counter.value += local_count
            result_queue.put((mnemonic, private_key, address))
            stop_event.set()
            return

    if local_count:
        with counter_lock:
            counter.value += local_count


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Vanity ETH Generator")
        self.root.geometry("1600x980")
        self.root.minsize(1450, 900)
        self.root.configure(bg="#0a0f1a")

        self.ctx = mp.get_context("spawn")
        self.stop_event = None
        self.db_queue = None
        self.result_queue = None
        self.writer = None
        self.workers = []
        self.counter = None
        self.counter_lock = None
        self.running = False

        self.hw = detect_hardware()
        self.auto_cfg = compute_auto_config(self.hw)

        self.colors = {
            "bg": "#0a0f1a",
            "sidebar": "#0d1423",
            "panel": "#121b2f",
            "panel2": "#17223b",
            "panel3": "#1d2a47",
            "entry": "#0b1324",
            "border": "#2b3b63",
            "text": "#ecf3ff",
            "muted": "#93a6ca",
            "green": "#16a34a",
            "red": "#dc2626",
            "blue": "#2563eb",
            "purple": "#7c3aed",
            "cyan": "#0891b2",
            "gold": "#f59e0b",
            "pink": "#db2777",
        }

        self.setup_style()
        self.build_ui()
        init_db()
        self.apply_auto_config()
        self.refresh_db_view()

    def setup_style(self):
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(
            "Modern.Treeview",
            background=self.colors["entry"],
            foreground=self.colors["text"],
            fieldbackground=self.colors["entry"],
            rowheight=32,
            borderwidth=0,
            font=("Consolas", 10)
        )
        style.configure(
            "Modern.Treeview.Heading",
            background=self.colors["panel3"],
            foreground=self.colors["text"],
            relief="flat",
            font=("Segoe UI", 10, "bold")
        )
        style.map(
            "Modern.Treeview",
            background=[("selected", self.colors["purple"])],
            foreground=[("selected", "white")]
        )

    def card(self, parent, bg=None, padx=16, pady=16):
        return tk.Frame(
            parent,
            bg=bg or self.colors["panel"],
            highlightbackground=self.colors["border"],
            highlightthickness=1,
            bd=0,
            padx=padx,
            pady=pady
        )

    def label(self, parent, text="", size=11, bold=False, fg=None, bg=None, textvariable=None, anchor="w", wraplength=None, justify="left"):
        return tk.Label(
        parent,
        text=text,
        textvariable=textvariable,
        fg=fg or self.colors["text"],
        bg=bg or parent.cget("bg"),
        font=("Segoe UI", size, "bold" if bold else "normal"),
        anchor=anchor,
        wraplength=wraplength,
        justify=justify
    )

    def entry(self, parent, textvariable=None, width=20, center=False):
        return tk.Entry(
            parent,
            textvariable=textvariable,
            width=width,
            bg=self.colors["entry"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["purple"],
            justify="center" if center else "left",
            font=("Consolas", 11)
        )

    def button(self, parent, text, command, bg, width=14):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg="white",
            activebackground=bg,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=14,
            pady=10,
            cursor="hand2",
            font=("Segoe UI", 10, "bold"),
            width=width
        )

    def build_ui(self):
        main = tk.Frame(self.root, bg=self.colors["bg"])
        main.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(main, bg=self.colors["sidebar"], width=260)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        content = tk.Frame(main, bg=self.colors["bg"])
        content.pack(side="left", fill="both", expand=True, padx=18, pady=18)

        self.build_sidebar()
        self.build_content(content)

    def build_sidebar(self):
        top = tk.Frame(self.sidebar, bg=self.colors["sidebar"])
        top.pack(fill="x", padx=18, pady=(22, 18))

        self.label(
            top,
            "VANITY ETH",
            size=22,
            bold=True,
            bg=self.colors["sidebar"]
        ).pack(anchor="w")
        self.label(
            top,
            "Generator Suite",
            size=11,
            fg=self.colors["muted"],
            bg=self.colors["sidebar"]
        ).pack(anchor="w", pady=(4, 0))

        nav = self.card(self.sidebar, bg=self.colors["panel2"], padx=14, pady=14)
        nav.pack(fill="x", padx=14, pady=(0, 14))

        self.button(nav, "Generator", lambda: self.show_page("generator"), self.colors["blue"], width=18).pack(fill="x", pady=4)
        self.button(nav, "Database", lambda: self.show_page("database"), self.colors["purple"], width=18).pack(fill="x", pady=4)

        stats = self.card(self.sidebar, padx=14, pady=14)
        stats.pack(fill="x", padx=14, pady=(0, 14))

        self.label(stats, "Live Counter", size=12, bold=True).pack(anchor="w")
        self.attempts_var = tk.StringVar(value="0")
        tk.Label(
            stats,
            textvariable=self.attempts_var,
            bg=stats.cget("bg"),
            fg=self.colors["gold"],
            font=("Consolas", 24, "bold"),
            anchor="w"
        ).pack(fill="x", pady=(8, 0))

        self.label(stats, "Status", size=10, fg=self.colors["muted"]).pack(anchor="w", pady=(14, 0))
        self.status_var = tk.StringVar(value="Ready")
        tk.Label(
            stats,
            textvariable=self.status_var,
            bg=stats.cget("bg"),
            fg=self.colors["text"],
            wraplength=210,
            justify="left",
            font=("Segoe UI", 10, "bold")
        ).pack(fill="x", pady=(6, 0))

        hw = self.card(self.sidebar, bg=self.colors["panel2"], padx=14, pady=14)
        hw.pack(fill="x", padx=14, pady=(0, 14))

        self.label(hw, "Hardware", size=12, bold=True, bg=self.colors["panel2"]).pack(anchor="w")
        self.hw_cpu_var = tk.StringVar()
        self.hw_ram_var = tk.StringVar()
        self.hw_gpu_var = tk.StringVar()

        self.label(hw, textvariable=self.hw_cpu_var, bg=self.colors["panel2"]).pack(anchor="w", pady=(8, 0))
        self.label(hw, textvariable=self.hw_ram_var, bg=self.colors["panel2"]).pack(anchor="w", pady=(4, 0))
        self.label(hw, textvariable=self.hw_gpu_var, bg=self.colors["panel2"], wraplength=210).pack(anchor="w", pady=(4, 0))

        tools = self.card(self.sidebar, padx=14, pady=14)
        tools.pack(fill="x", padx=14, pady=(0, 14))

        self.button(tools, "Auto Config", self.apply_auto_config, self.colors["cyan"], width=18).pack(fill="x", pady=4)
        self.button(tools, "Refresh DB", self.refresh_db_view, self.colors["pink"], width=18).pack(fill="x", pady=4)

    def build_content(self, parent):
        header = self.card(parent, bg=self.colors["panel2"], padx=20, pady=18)
        header.pack(fill="x", pady=(0, 14))

        self.label(
            header,
            "Ethereum Vanity Wallet Generator",
            size=26,
            bold=True,
            bg=self.colors["panel2"]
        ).pack(anchor="w")

        self.label(
            header,
            "Auto-detection • Multi-core CPU • Prefix / Suffix matching • Searchable database viewer",
            size=10,
            fg=self.colors["muted"],
            bg=self.colors["panel2"]
        ).pack(anchor="w", pady=(6, 0))

        self.pages = tk.Frame(parent, bg=self.colors["bg"])
        self.pages.pack(fill="both", expand=True)

        self.page_generator = tk.Frame(self.pages, bg=self.colors["bg"])
        self.page_database = tk.Frame(self.pages, bg=self.colors["bg"])

        for page in (self.page_generator, self.page_database):
            page.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.build_generator_page()
        self.build_database_page()
        self.show_page("generator")

    def build_generator_page(self):
        top = tk.Frame(self.page_generator, bg=self.colors["bg"])
        top.pack(fill="x", pady=(0, 14))

        settings = self.card(top)
        settings.pack(side="left", fill="both", expand=True, padx=(0, 8))

        actions = self.card(top, bg=self.colors["panel2"])
        actions.pack(side="left", fill="y", padx=(8, 0))

        self.label(settings, "Search Configuration", size=14, bold=True).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 14))

        self.label(settings, "Prefix (start)", fg=self.colors["muted"]).grid(row=1, column=0, sticky="w", padx=(0, 8), pady=6)
        self.prefix_var = tk.StringVar()
        self.entry(settings, self.prefix_var, width=28).grid(row=1, column=1, sticky="ew", padx=(0, 16), pady=6)

        self.label(settings, "Suffix (end)", fg=self.colors["muted"]).grid(row=1, column=2, sticky="w", padx=(0, 8), pady=6)
        self.suffix_var = tk.StringVar()
        self.entry(settings, self.suffix_var, width=28).grid(row=1, column=3, sticky="ew", pady=6)

        self.label(settings, "CPU Workers", fg=self.colors["muted"]).grid(row=2, column=0, sticky="w", padx=(0, 8), pady=6)
        self.workers_var = tk.StringVar()
        self.entry(settings, self.workers_var, width=12).grid(row=2, column=1, sticky="w", pady=6)

        self.label(settings, "DB Batch", fg=self.colors["muted"]).grid(row=2, column=2, sticky="w", padx=(0, 8), pady=6)
        self.batch_var = tk.StringVar()
        self.entry(settings, self.batch_var, width=12).grid(row=2, column=3, sticky="w", pady=6)

        self.label(settings, "Queue Size", fg=self.colors["muted"]).grid(row=3, column=0, sticky="w", padx=(0, 8), pady=6)
        self.queue_var = tk.StringVar()
        self.entry(settings, self.queue_var, width=12).grid(row=3, column=1, sticky="w", pady=6)

        self.label(
            settings,
            "Allowed characters: 0-9 and a-f • Search is performed on address without 0x",
            size=9,
            fg=self.colors["muted"]
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(12, 0))

        settings.grid_columnconfigure(1, weight=1)
        settings.grid_columnconfigure(3, weight=1)

        self.label(actions, "Run Controls", size=14, bold=True, bg=self.colors["panel2"]).pack(anchor="w", pady=(0, 14))
        self.btn_generate = self.button(actions, "Generate / Search", self.start_search, self.colors["green"], width=18)
        self.btn_generate.pack(fill="x", pady=4)

        self.btn_stop = self.button(actions, "Stop", self.stop_search, self.colors["red"], width=18)
        self.btn_stop.pack(fill="x", pady=4)
        self.btn_stop.config(state="disabled")

        self.btn_auto_inline = self.button(actions, "Auto Configure", self.apply_auto_config, self.colors["cyan"], width=18)
        self.btn_auto_inline.pack(fill="x", pady=4)

        self.btn_refresh_inline = self.button(actions, "Refresh DB", self.refresh_db_view, self.colors["purple"], width=18)
        self.btn_refresh_inline.pack(fill="x", pady=4)

        result = self.card(self.page_generator)
        result.pack(fill="both", expand=True)

        self.label(result, "Generated Wallet", size=14, bold=True).pack(anchor="w", pady=(0, 14))

        self.label(result, "Seed phrase (24 words)", fg=self.colors["muted"]).pack(anchor="w")
        self.mnemonic_text = tk.Text(
            result,
            height=6,
            bg=self.colors["entry"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            relief="flat",
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["purple"],
            font=("Consolas", 11),
            wrap="word",
            padx=12,
            pady=10
        )
        self.mnemonic_text.pack(fill="x", pady=(6, 10))

        row1 = tk.Frame(result, bg=result.cget("bg"))
        row1.pack(fill="x", pady=(0, 14))
        self.button(row1, "Copy Seed", self.copy_mnemonic, self.colors["cyan"], width=14).pack(side="left")

        self.private_key_var = tk.StringVar()
        self.address_var = tk.StringVar()

        self.label(result, "Private key (without 0x)", fg=self.colors["muted"]).pack(anchor="w")
        self.entry(result, self.private_key_var, width=130).pack(fill="x", pady=(6, 10))

        row2 = tk.Frame(result, bg=result.cget("bg"))
        row2.pack(fill="x", pady=(0, 14))
        self.button(row2, "Copy Private Key", self.copy_private_key, self.colors["purple"], width=18).pack(side="left")

        self.label(result, "Ethereum address", fg=self.colors["muted"]).pack(anchor="w")
        self.entry(result, self.address_var, width=130).pack(fill="x", pady=(6, 10))

        row3 = tk.Frame(result, bg=result.cget("bg"))
        row3.pack(fill="x")
        self.button(row3, "Copy Address", self.copy_address, self.colors["pink"], width=14).pack(side="left")

    def build_database_page(self):
        toolbar = self.card(self.page_database, bg=self.colors["panel2"])
        toolbar.pack(fill="x", pady=(0, 14))

        self.label(toolbar, "Database Viewer", size=14, bold=True, bg=self.colors["panel2"]).grid(row=0, column=0, sticky="w")
        self.db_count_var = tk.StringVar(value="0 rows")
        self.label(toolbar, textvariable=self.db_count_var, size=11, bold=True, fg=self.colors["gold"], bg=self.colors["panel2"]).grid(row=0, column=5, sticky="e")

        self.label(toolbar, "Search", fg=self.colors["muted"], bg=self.colors["panel2"]).grid(row=1, column=0, sticky="w", pady=(12, 0))
        self.db_search_var = tk.StringVar()
        search_entry = self.entry(toolbar, self.db_search_var, width=38)
        search_entry.grid(row=1, column=1, sticky="ew", padx=(10, 14), pady=(12, 0))
        search_entry.bind("<Return>", lambda event: self.search_db())

        self.button(toolbar, "Search", self.search_db, self.colors["blue"], width=10).grid(row=1, column=2, padx=(0, 8), pady=(12, 0))
        self.button(toolbar, "Clear", self.clear_db_search, self.colors["red"], width=10).grid(row=1, column=3, padx=(0, 8), pady=(12, 0))

        self.limit_var = tk.StringVar(value="200")
        limit_box = ttk.Combobox(toolbar, textvariable=self.limit_var, values=["200", "1000", "5000"], width=10, state="readonly")
        limit_box.grid(row=1, column=4, padx=(0, 8), pady=(12, 0))
        limit_box.bind("<<ComboboxSelected>>", lambda event: self.refresh_db_view())

        self.button(toolbar, "Refresh", self.refresh_db_view, self.colors["cyan"], width=10).grid(row=1, column=5, pady=(12, 0), sticky="e")

        toolbar.grid_columnconfigure(1, weight=1)

        table_card = self.card(self.page_database)
        table_card.pack(fill="both", expand=True)

        columns = ("id", "created_at", "address", "private_key", "mnemonic")
        self.db_tree = ttk.Treeview(table_card, columns=columns, show="headings", style="Modern.Treeview")

        self.db_tree.heading("id", text="ID")
        self.db_tree.heading("created_at", text="Created At")
        self.db_tree.heading("address", text="Address")
        self.db_tree.heading("private_key", text="Private Key")
        self.db_tree.heading("mnemonic", text="Mnemonic")

        self.db_tree.column("id", width=70, anchor="center")
        self.db_tree.column("created_at", width=170, anchor="center")
        self.db_tree.column("address", width=350, anchor="w")
        self.db_tree.column("private_key", width=500, anchor="w")
        self.db_tree.column("mnemonic", width=900, anchor="w")

        self.db_tree.bind("<Double-1>", self.on_db_double_click)

        yscroll = ttk.Scrollbar(table_card, orient="vertical", command=self.db_tree.yview)
        xscroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.db_tree.xview)

        self.db_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)

        self.db_tree.pack(side="left", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        xscroll.pack(side="bottom", fill="x")

    def show_page(self, page_name: str):
        if page_name == "generator":
            self.page_generator.lift()
        else:
            self.page_database.lift()

    def apply_auto_config(self):
        self.hw = detect_hardware()
        self.auto_cfg = compute_auto_config(self.hw)

        self.workers_var.set(str(self.auto_cfg["workers"]))
        self.batch_var.set(str(self.auto_cfg["db_batch_size"]))
        self.queue_var.set(str(self.auto_cfg["queue_size"]))

        self.hw_cpu_var.set(f"CPU: {self.hw['cpu_physical']}C / {self.hw['cpu_logical']}T")
        self.hw_ram_var.set(f"RAM: {self.hw['ram_gb']} GB")
        self.hw_gpu_var.set(f"GPU: {self.hw['gpu']}")
        self.status_var.set("Hardware detected and auto configuration applied")

    def validate_inputs(self):
        prefix = self.prefix_var.get().strip()
        suffix = self.suffix_var.get().strip()
        allowed_hex = set("0123456789abcdefABCDEF")

        if prefix and not all(c in allowed_hex for c in prefix):
            messagebox.showerror("Error", "Prefix must contain only 0-9 and a-f.")
            return None

        if suffix and not all(c in allowed_hex for c in suffix):
            messagebox.showerror("Error", "Suffix must contain only 0-9 and a-f.")
            return None

        try:
            workers = int(self.workers_var.get().strip())
            if workers < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Invalid CPU worker count.")
            return None

        try:
            db_batch_size = int(self.batch_var.get().strip())
            if db_batch_size < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Invalid DB batch size.")
            return None

        try:
            queue_size = int(self.queue_var.get().strip())
            if queue_size < 100:
                raise ValueError
        except ValueError:
            messagebox.showerror("Error", "Invalid queue size.")
            return None

        return prefix, suffix, workers, db_batch_size, queue_size

    def start_search(self):
        if self.running:
            return

        values = self.validate_inputs()
        if values is None:
            return

        prefix, suffix, workers, db_batch_size, queue_size = values

        self.mnemonic_text.delete("1.0", tk.END)
        self.private_key_var.set("")
        self.address_var.set("")
        self.attempts_var.set("0")
        self.status_var.set("Starting search...")

        self.stop_event = self.ctx.Event()
        self.db_queue = self.ctx.Queue(maxsize=queue_size)
        self.result_queue = self.ctx.Queue()
        self.counter = self.ctx.Value("Q", 0)
        self.counter_lock = self.ctx.Lock()

        self.writer = self.ctx.Process(
            target=writer_process,
            args=(self.db_queue, self.stop_event, db_batch_size),
            daemon=True
        )
        self.writer.start()

        self.workers = []
        for _ in range(workers):
            p = self.ctx.Process(
                target=worker_process,
                args=(
                    prefix,
                    suffix,
                    self.db_queue,
                    self.result_queue,
                    self.stop_event,
                    self.counter,
                    self.counter_lock
                ),
                daemon=True
            )
            p.start()
            self.workers.append(p)

        self.running = True
        self.btn_generate.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.btn_auto_inline.config(state="disabled")
        self.status_var.set("Search running...")

        self.root.after(UI_POLL_MS, self.poll_state)

    def poll_state(self):
        if not self.running:
            return

        try:
            self.attempts_var.set(format_number(self.counter.value))
        except Exception:
            pass

        try:
            mnemonic, private_key, address = self.result_queue.get_nowait()
            self.finish_success(mnemonic, private_key, address)
            return
        except Exception:
            pass

        alive = any(p.is_alive() for p in self.workers)
        if not alive and self.stop_event.is_set():
            self.finish_stopped()
            return

        self.status_var.set(f"Searching... {self.attempts_var.get()} wallets tested")
        self.root.after(UI_POLL_MS, self.poll_state)

    def finish_success(self, mnemonic, private_key, address):
        self.stop_event.set()

        self.mnemonic_text.delete("1.0", tk.END)
        self.mnemonic_text.insert(tk.END, mnemonic)
        self.private_key_var.set(private_key)
        self.address_var.set(address)

        self.cleanup_processes()
        self.status_var.set("Wallet found and saved to database.db")
        self.refresh_db_view()

    def finish_stopped(self):
        self.cleanup_processes()
        self.status_var.set("Search stopped, tested wallets saved")
        self.refresh_db_view()

    def stop_search(self):
        if not self.running:
            return
        self.status_var.set("Stop requested...")
        self.stop_event.set()

    def cleanup_processes(self):
        for p in self.workers:
            p.join(timeout=0.5)
            if p.is_alive():
                p.terminate()

        if self.writer is not None:
            self.writer.join(timeout=1.0)
            if self.writer.is_alive():
                self.writer.terminate()

        self.workers = []
        self.writer = None
        self.running = False
        self.btn_generate.config(state="normal")
        self.btn_stop.config(state="disabled")
        self.btn_auto_inline.config(state="normal")

    def refresh_db_view(self):
        try:
            limit = int(self.limit_var.get()) if hasattr(self, "limit_var") else 200
        except Exception:
            limit = 200

        query = self.db_search_var.get().strip() if hasattr(self, "db_search_var") else ""
        self.load_db_rows(limit=limit, query=query)

    def search_db(self):
        self.load_db_rows(limit=int(self.limit_var.get()), query=self.db_search_var.get().strip())

    def clear_db_search(self):
        self.db_search_var.set("")
        self.refresh_db_view()

    def load_db_rows(self, limit=200, query=""):
        try:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM wallets")
            total = cur.fetchone()[0]
            self.db_count_var.set(f"{format_number(total)} rows")

            for item in self.db_tree.get_children():
                self.db_tree.delete(item)

            if query:
                like = f"%{query}%"
                sql = """
                    SELECT id, created_at, address, private_key, mnemonic
                    FROM wallets
                    WHERE CAST(id AS TEXT) LIKE ?
                       OR address LIKE ?
                       OR private_key LIKE ?
                       OR mnemonic LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                """
                cur.execute(sql, (like, like, like, like, int(limit)))
            else:
                sql = """
                    SELECT id, created_at, address, private_key, mnemonic
                    FROM wallets
                    ORDER BY id DESC
                    LIMIT ?
                """
                cur.execute(sql, (int(limit),))

            rows = cur.fetchall()
            conn.close()

            for row in rows:
                self.db_tree.insert("", "end", values=row)

            if query:
                self.status_var.set(f"Database search loaded: {len(rows)} result(s)")
        except Exception as e:
            messagebox.showerror("Database Error", str(e))

    def on_db_double_click(self, event):
        selected = self.db_tree.selection()
        if not selected:
            return

        values = self.db_tree.item(selected[0], "values")
        if not values or len(values) < 5:
            return

        _, _, address, private_key, mnemonic = values

        self.mnemonic_text.delete("1.0", tk.END)
        self.mnemonic_text.insert(tk.END, mnemonic)
        self.private_key_var.set(private_key)
        self.address_var.set(address)

        self.show_page("generator")
        self.status_var.set("Wallet loaded from database")

    def copy_mnemonic(self):
        value = self.mnemonic_text.get("1.0", tk.END).strip()
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.root.update()
            self.status_var.set("Seed copied")

    def copy_private_key(self):
        value = self.private_key_var.get().strip()
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.root.update()
            self.status_var.set("Private key copied")

    def copy_address(self):
        value = self.address_var.get().strip()
        if value:
            self.root.clipboard_clear()
            self.root.clipboard_append(value)
            self.root.update()
            self.status_var.set("Address copied")


if __name__ == "__main__":
    mp.freeze_support()
    root = tk.Tk()
    app = App(root)
    root.mainloop()