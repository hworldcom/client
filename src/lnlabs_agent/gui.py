# src/lnlabs_agent/gui.py
"""
Simple Tkinter GUI for the LNLabs Agent:
- Enter pairing code to pair once
- Start/Stop the agent loop
- Shows live log lines
Package with PyInstaller using --windowed / -w to get a double-clickable app.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
import traceback

from lnlabs_agent.core import (
    load_token,
    save_token,
    clear_token,
    pair_with_code,
    AgentRunner,
    configure_api_base,
    get_api_base,
    known_api_environments,
    current_api_environment,
)

class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("LNLabs Agent")
        self.minsize(460, 380)

        # Layout base
        self.columnconfigure(0, weight=1)
        frm = ttk.Frame(self, padding=12)
        frm.grid(row=0, column=0, sticky="nsew")
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(0, weight=0)

        # Environment selector (shown only if multiple environments are configured)
        self.env_map = known_api_environments()
        self.env_var = tk.StringVar(value="")
        self.env_box: ttk.Combobox | None = None
        row = 0
        if len(self.env_map) > 1:
            ttk.Label(frm, text="Environment:").grid(row=row, column=0, sticky="w")
            self.env_box = ttk.Combobox(frm, textvariable=self.env_var, state="readonly")
            self.env_box["values"] = list(self.env_map.keys())
            self.env_box.bind("<<ComboboxSelected>>", self.on_env_change)
            self.env_box.grid(row=row, column=1, sticky="ew")
            row += 1
        else:
            self.env_var.set(current_api_environment())

        # API base display (read-only)
        ttk.Label(frm, text="API base URL:").grid(row=row, column=0, sticky="w", pady=(6, 0))
        self.api_var = tk.StringVar(value=get_api_base())
        ttk.Entry(frm, textvariable=self.api_var, state="readonly").grid(row=row, column=1, sticky="ew", pady=(6, 0))
        row += 1

        # Status
        ttk.Label(frm, text="Status:").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.status_var = tk.StringVar(value="Not paired")
        ttk.Label(frm, textvariable=self.status_var).grid(row=row, column=1, sticky="w", pady=(8, 0))
        row += 1

        # Pairing code input
        ttk.Label(frm, text="Pairing code:").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.code_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.code_var).grid(row=row, column=1, sticky="ew", pady=(8, 0))
        row += 1

        # Buttons
        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 8))
        self.btn_pair   = ttk.Button(btns, text="Pair", command=self.on_pair)
        self.btn_unpair = ttk.Button(btns, text="Unpair", command=self.on_unpair)
        self.btn_start  = ttk.Button(btns, text="Start Agent", command=self.on_start, state="disabled")
        self.btn_stop   = ttk.Button(btns, text="Stop Agent",  command=self.on_stop,  state="disabled")
        for i, w in enumerate([self.btn_pair, self.btn_unpair, self.btn_start, self.btn_stop]):
            w.grid(row=0, column=i, padx=(0,6))
        row += 1

        # Log box
        ttk.Label(frm, text="Log:").grid(row=row, column=0, sticky="w")
        log_container = ttk.Frame(frm)
        log_container.grid(row=row + 1, column=0, columnspan=2, sticky="nsew")
        frm.rowconfigure(row + 1, weight=1)
        log_container.columnconfigure(0, weight=1)
        log_container.rowconfigure(0, weight=1)

        self.log = tk.Text(log_container, wrap="word")
        y_scroll = ttk.Scrollbar(log_container, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=y_scroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")

        # Runner
        self.runner: AgentRunner | None = None

        # Ensure API base reflects initial selection
        self.refresh_api_display()
        if self.env_box is not None:
            current_alias = current_api_environment()
            if current_alias in self.env_map:
                self.env_var.set(current_alias)
                self.env_box.set(current_alias)

        # Initialize from token
        if load_token():
            self.status_var.set("Paired (token present)")
            self.btn_start.config(state="normal")

        # Handle close
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------- helpers -------------
    def log_line(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def refresh_api_display(self) -> None:
        self.api_var.set(get_api_base())
        alias = current_api_environment()
        self.env_var.set(alias)
        if self.env_box is not None and alias in self.env_map:
            self.env_box.set(alias)

    def on_env_change(self, *_args) -> None:
        env = self.env_var.get().strip()
        if not env:
            return
        try:
            configure_api_base(env=env)
            self.refresh_api_display()
            self.log_line(f"Environment switched to '{env}' → {get_api_base()}")
        except ValueError as e:
            messagebox.showerror("Environment", str(e))

    # ------------- actions -------------
    def on_pair(self) -> None:
        code = self.code_var.get().strip()
        if not code:
            messagebox.showwarning("Pair", "Enter the pairing code from the website.")
            return
        self.log_line(f"Pairing using {get_api_base()}")
        try:
            tok = pair_with_code(code)
            save_token(tok)
            self.status_var.set("Paired (token saved)")
            self.btn_start.config(state="normal")
            self.log_line("Paired successfully.")
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Pair", f"Pairing failed:\n{e}")

    def on_unpair(self) -> None:
        self.on_stop()
        clear_token()
        self.status_var.set("Not paired")
        self.btn_start.config(state="disabled")
        self.log_line("Unpaired and token cleared.")

    def on_start(self) -> None:
        if self.runner and self.runner.is_alive():
            return
        tok = load_token()
        if not tok:
            messagebox.showwarning("Start", "Not paired yet.")
            return
        self.log_line(f"Starting agent against {get_api_base()}")
        self.runner = AgentRunner(tok, on_log=self.log_line)
        self.runner.start()
        self.status_var.set("Running")
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self.log_line("Agent started.")

    def on_stop(self) -> None:
        if self.runner:
            self.runner.stop()
            self.runner.join(timeout=3)
            self.runner = None
            self.log_line("Agent stopped.")
        if load_token():
            self.status_var.set("Paired (stopped)")
            self.btn_start.config(state="normal")
        else:
            self.status_var.set("Not paired")
            self.btn_start.config(state="disabled")
        self.btn_stop.config(state="disabled")

    def on_close(self) -> None:
        self.on_stop()
        self.destroy()

if __name__ == "__main__":
    # Run GUI directly:  python -m src.lnlabs_agent.gui
    app = App()
    app.mainloop()
