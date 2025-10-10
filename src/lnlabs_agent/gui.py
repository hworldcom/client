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
    API_BASE,
    load_token,
    save_token,
    clear_token,
    pair_with_code,
    AgentRunner,
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

        # Server label (read-only)
        ttk.Label(frm, text="Server:").grid(row=0, column=0, sticky="w")
        self.api_var = tk.StringVar(value=API_BASE)
        ttk.Entry(frm, textvariable=self.api_var, state="readonly").grid(row=0, column=1, sticky="ew")

        # Status
        ttk.Label(frm, text="Status:").grid(row=1, column=0, sticky="w", pady=(8,0))
        self.status_var = tk.StringVar(value="Not paired")
        ttk.Label(frm, textvariable=self.status_var).grid(row=1, column=1, sticky="w", pady=(8,0))

        # Pairing code input
        ttk.Label(frm, text="Pairing code:").grid(row=2, column=0, sticky="w", pady=(8,0))
        self.code_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.code_var).grid(row=2, column=1, sticky="ew", pady=(8,0))

        # Buttons
        btns = ttk.Frame(frm)
        btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4,8))
        self.btn_pair   = ttk.Button(btns, text="Pair", command=self.on_pair)
        self.btn_unpair = ttk.Button(btns, text="Unpair", command=self.on_unpair)
        self.btn_start  = ttk.Button(btns, text="Start Agent", command=self.on_start, state="disabled")
        self.btn_stop   = ttk.Button(btns, text="Stop Agent",  command=self.on_stop,  state="disabled")
        for i, w in enumerate([self.btn_pair, self.btn_unpair, self.btn_start, self.btn_stop]):
            w.grid(row=0, column=i, padx=(0,6))

        # Log box
        ttk.Label(frm, text="Log:").grid(row=4, column=0, sticky="w")
        self.log = tk.Text(frm, height=12)
        self.log.grid(row=5, column=0, columnspan=2, sticky="nsew")
        frm.rowconfigure(5, weight=1)

        # Runner
        self.runner: AgentRunner | None = None

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

    # ------------- actions -------------
    def on_pair(self) -> None:
        code = self.code_var.get().strip()
        if not code:
            messagebox.showwarning("Pair", "Enter the pairing code from the website.")
            return
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
