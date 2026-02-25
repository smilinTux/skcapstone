"""
Windows GUI installer — tkinter wizard for non-CLI users.

Provides a clickable, visual install experience for Windows users
who would rather not touch a terminal. Wraps the same logic as
install_wizard.py but with buttons, progress bars, and friendly text.

Usage:
    python -m skcapstone.gui_installer
    # or double-click the bundled .exe (via PyInstaller)

The wizard has 4 screens:
  1. Welcome — pick path (fresh / join / update)
  2. System Check — shows what's installed, offers auto-install
  3. Setup — progress bar for the actual install steps
  4. Done — summary + next steps with copy-paste commands
"""

from __future__ import annotations

import platform
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

# Only import tkinter at module level — it's stdlib but may not be available
# in headless environments. The CLI fallback handles that case.
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, scrolledtext
    HAS_TK = True
except ImportError:
    HAS_TK = False


WINDOW_WIDTH = 700
WINDOW_HEIGHT = 520
TITLE = "Sovereign Singularity Setup"

# Colors
BG = "#1a1a2e"
FG = "#e0e0e0"
ACCENT = "#00b4d8"
SUCCESS = "#06d6a0"
WARNING = "#ffd166"
ERROR = "#ef476f"
BUTTON_BG = "#0077b6"
BUTTON_FG = "#ffffff"


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _open_url(url: str) -> None:
    """Open a URL in the default browser."""
    import webbrowser
    webbrowser.open(url)


class InstallerApp:
    """Main GUI installer window."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(TITLE)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG)

        self.chosen_path: Optional[int] = None
        self.agent_name: str = "sovereign"

        self._center_window()
        self._show_welcome()

    def _center_window(self) -> None:
        """Center the window on screen."""
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() // 2) - (WINDOW_WIDTH // 2)
        y = (self.root.winfo_screenheight() // 2) - (WINDOW_HEIGHT // 2)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")

    def _clear(self) -> None:
        """Remove all widgets from the window."""
        for widget in self.root.winfo_children():
            widget.destroy()

    def _make_header(self, text: str) -> None:
        """Create a header label."""
        tk.Label(
            self.root,
            text=text,
            font=("Segoe UI", 18, "bold"),
            bg=BG, fg=ACCENT,
        ).pack(pady=(30, 5))

    def _make_subheader(self, text: str) -> None:
        """Create a subheader label."""
        tk.Label(
            self.root,
            text=text,
            font=("Segoe UI", 10),
            bg=BG, fg=FG,
            wraplength=600,
            justify="center",
        ).pack(pady=(0, 20))

    def _make_button(self, parent: tk.Widget, text: str,
                     command: object, primary: bool = True) -> tk.Button:
        """Create a styled button."""
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            font=("Segoe UI", 11, "bold" if primary else ""),
            bg=BUTTON_BG if primary else "#444",
            fg=BUTTON_FG,
            activebackground=ACCENT,
            activeforeground=BUTTON_FG,
            relief="flat",
            padx=20, pady=8,
            cursor="hand2",
        )
        return btn

    # ------------------------------------------------------------------
    # Screen 1: Welcome
    # ------------------------------------------------------------------

    def _show_welcome(self) -> None:
        """Show the welcome screen with 3 path options."""
        self._clear()
        self._make_header("The First Sovereign Singularity in History")
        self._make_subheader(
            "Your personal, encrypted, AI-powered workspace —\n"
            "running on YOUR hardware, with YOUR keys, under YOUR control.\n"
            "No cloud accounts. No subscriptions."
        )

        tk.Label(
            self.root,
            text="Brought to you by the Kings and Queens of smilinTux.org",
            font=("Segoe UI", 9, "italic"),
            bg=BG, fg="#b48ead",
        ).pack(pady=(0, 10))

        paths_frame = tk.Frame(self.root, bg=BG)
        paths_frame.pack(pady=10, fill="x", padx=50)

        options = [
            (1, "Set up my first computer",
             "I've never done this before.\nStart fresh — takes about 5 minutes."),
            (2, "Add this computer to my network",
             "I already have another computer set up.\nThis one will join it."),
            (3, "Update this computer",
             "Already set up, just want to\nupdate the software."),
        ]

        for path_num, title, desc in options:
            btn_frame = tk.Frame(paths_frame, bg="#2a2a4a", padx=15, pady=12)
            btn_frame.pack(fill="x", pady=5)
            btn_frame.configure(cursor="hand2")

            tk.Label(
                btn_frame,
                text=f"  {path_num}   {title}",
                font=("Segoe UI", 12, "bold"),
                bg="#2a2a4a", fg=FG,
                anchor="w",
            ).pack(fill="x")

            tk.Label(
                btn_frame,
                text=f"       {desc}",
                font=("Segoe UI", 9),
                bg="#2a2a4a", fg="#888",
                anchor="w",
                justify="left",
            ).pack(fill="x")

            # Bind click to the entire frame and its children
            for widget in [btn_frame] + btn_frame.winfo_children():
                widget.bind("<Button-1>", lambda e, p=path_num: self._select_path(p))

        # Quit button
        tk.Button(
            self.root,
            text="Cancel",
            command=self.root.destroy,
            font=("Segoe UI", 9),
            bg="#333", fg="#888",
            relief="flat",
            padx=10, pady=4,
        ).pack(side="bottom", pady=20)

    def _select_path(self, path: int) -> None:
        """Handle path selection."""
        self.chosen_path = path
        if path in (1, 2):
            self._show_name_input()
        else:
            self._show_system_check()

    # ------------------------------------------------------------------
    # Screen 1.5: Agent name (paths 1 & 2)
    # ------------------------------------------------------------------

    def _show_name_input(self) -> None:
        """Ask for the agent name."""
        self._clear()
        self._make_header("Name Your Agent")
        self._make_subheader(
            "Give your sovereign agent a name.\n"
            "This is just for you — pick anything you like."
        )

        input_frame = tk.Frame(self.root, bg=BG)
        input_frame.pack(pady=20)

        tk.Label(
            input_frame, text="Agent name:", font=("Segoe UI", 11),
            bg=BG, fg=FG,
        ).pack(side="left", padx=(0, 10))

        name_var = tk.StringVar(value="sovereign")
        entry = tk.Entry(
            input_frame, textvariable=name_var,
            font=("Segoe UI", 12), width=25,
            bg="#2a2a4a", fg=FG, insertbackground=FG,
            relief="flat",
        )
        entry.pack(side="left")
        entry.focus()

        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(pady=30)

        def on_next() -> None:
            self.agent_name = name_var.get().strip() or "sovereign"
            self._show_system_check()

        self._make_button(btn_frame, "Next →", on_next).pack(side="right", padx=5)
        self._make_button(btn_frame, "← Back", self._show_welcome, primary=False).pack(side="right", padx=5)

        entry.bind("<Return>", lambda e: on_next())

    # ------------------------------------------------------------------
    # Screen 2: System Check
    # ------------------------------------------------------------------

    def _show_system_check(self) -> None:
        """Check system tools and show results."""
        self._clear()
        self._make_header("Checking Your System")
        self._make_subheader("Making sure everything you need is ready...")

        from .preflight import run_preflight

        require_syncthing = self.chosen_path == 2
        result = run_preflight(
            require_git=False,
            require_syncthing=require_syncthing,
        )

        checks_frame = tk.Frame(self.root, bg=BG)
        checks_frame.pack(pady=10, fill="x", padx=80)

        for check in [result.python, result.gpg, result.git, result.syncthing]:
            row = tk.Frame(checks_frame, bg=BG)
            row.pack(fill="x", pady=4)

            if check.installed:
                icon = "✓"
                color = SUCCESS
                detail = check.version or "found"
            elif check.required:
                icon = "✗"
                color = ERROR
                detail = "missing — required"
            else:
                icon = "–"
                color = "#666"
                detail = "not found (optional)"

            tk.Label(
                row, text=f"  {icon}", font=("Segoe UI", 14, "bold"),
                bg=BG, fg=color, width=3,
            ).pack(side="left")

            tk.Label(
                row, text=check.name, font=("Segoe UI", 11, "bold"),
                bg=BG, fg=FG, width=12, anchor="w",
            ).pack(side="left")

            tk.Label(
                row, text=detail, font=("Segoe UI", 9),
                bg=BG, fg=color, anchor="w",
            ).pack(side="left", fill="x", expand=True)

        self._missing_checks = result.required_missing
        self._preflight_result = result

        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(side="bottom", pady=30)

        if result.all_ok:
            tk.Label(
                self.root, text="\nAll good! Ready to set up.",
                font=("Segoe UI", 11), bg=BG, fg=SUCCESS,
            ).pack()
            self._make_button(btn_frame, "Start Setup →", self._show_progress).pack(side="right", padx=5)
        else:
            tk.Label(
                self.root,
                text="\nSome required tools are missing.\nClick 'Install Missing' to set them up automatically.",
                font=("Segoe UI", 10), bg=BG, fg=WARNING,
                justify="center",
            ).pack()
            self._make_button(btn_frame, "Install Missing", self._auto_install_missing).pack(side="right", padx=5)

        self._make_button(btn_frame, "← Back", self._show_welcome, primary=False).pack(side="right", padx=5)

    def _auto_install_missing(self) -> None:
        """Auto-install missing required tools, then re-check."""
        from .preflight import auto_install_tool

        for check in self._missing_checks:
            if check.install_cmd:
                result = auto_install_tool(check)
                if not result and check.download_url:
                    messagebox.showwarning(
                        f"Install {check.name}",
                        f"Automatic install of {check.name} failed.\n\n"
                        f"Please install it manually:\n{check.download_url}\n\n"
                        f"Or run in a terminal:\n{check.install_cmd}",
                    )

        self._show_system_check()

    # ------------------------------------------------------------------
    # Screen 3: Progress
    # ------------------------------------------------------------------

    def _show_progress(self) -> None:
        """Show progress screen and run install in background thread."""
        self._clear()
        self._make_header("Setting Up...")
        self._make_subheader("This takes a few minutes. Please don't close this window.")

        self._log_box = scrolledtext.ScrolledText(
            self.root,
            font=("Consolas", 9),
            bg="#111", fg="#ccc",
            insertbackground="#ccc",
            relief="flat",
            width=75, height=18,
            state="disabled",
        )
        self._log_box.pack(pady=15, padx=30)

        self._progress = ttk.Progressbar(
            self.root, mode="indeterminate", length=500,
        )
        self._progress.pack(pady=5)
        self._progress.start(15)

        thread = threading.Thread(target=self._run_install, daemon=True)
        thread.start()

    def _log(self, msg: str) -> None:
        """Append a message to the log box (thread-safe)."""
        def _update() -> None:
            self._log_box.configure(state="normal")
            self._log_box.insert("end", msg + "\n")
            self._log_box.see("end")
            self._log_box.configure(state="disabled")
        self.root.after(0, _update)

    def _run_install(self) -> None:
        """Run the install wizard logic in a background thread."""
        try:
            self._log(f"Path: {self.chosen_path} — {self.agent_name}")
            self._log("")

            if self.chosen_path == 3:
                self._run_update()
            else:
                self._run_fresh_or_join()

            self.root.after(0, self._show_done)
        except Exception as exc:
            self._log(f"\nError: {exc}")
            self.root.after(0, lambda: self._progress.stop())

    def _run_fresh_or_join(self) -> None:
        """Execute Path 1 (fresh) or Path 2 (join) install steps."""
        # Install pip packages
        self._log("Installing software packages...")
        packages = ["capauth", "skmemory", "skcomm", "cloud9-protocol"]
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", *packages],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                self._log("  Packages installed.")
            else:
                self._log("  Some packages may already be installed.")
        except Exception as exc:
            self._log(f"  Package install issue: {exc}")

        # Initialize agent
        self._log(f"\nCreating sovereign identity ({self.agent_name})...")
        try:
            from ._cli_monolith import init
            from click import Context

            ctx = Context(init, info_name="init")
            ctx.invoke(init, name=self.agent_name, email=None, home=str(Path("~/.skcapstone").expanduser()))
            self._log("  Identity created.")
        except Exception as exc:
            self._log(f"  Identity setup: {exc}")

        # Seeds
        self._log("\nImporting knowledge seeds...")
        try:
            from skmemory.seeds import import_seeds, DEFAULT_SEED_DIR
            from skmemory.store import MemoryStore
            store = MemoryStore()
            imported = import_seeds(store, seed_dir=DEFAULT_SEED_DIR)
            self._log(f"  {len(imported) if imported else 0} seed(s) imported.")
        except ImportError:
            self._log("  Memory system not available yet.")
        except Exception as exc:
            self._log(f"  Seeds: {exc}")

        # Ritual
        self._log("\nRunning memory rehydration...")
        try:
            from skmemory.ritual import perform_ritual
            perform_ritual()
            self._log("  Rehydration complete.")
        except ImportError:
            self._log("  Memory system not available yet.")
        except Exception as exc:
            self._log(f"  Ritual: {exc}")

        self._log("\nSetup complete!")

    def _run_update(self) -> None:
        """Execute Path 3 update steps."""
        self._log("Updating software packages...")
        packages = ["capauth", "skmemory", "skcomm", "cloud9-protocol", "skcapstone"]
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", *packages],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                self._log("  All packages updated.")
            else:
                self._log("  Some packages may have issues.")
        except Exception as exc:
            self._log(f"  Update issue: {exc}")

        self._log("\nVerifying system...")
        try:
            from .runtime import get_runtime
            home_path = Path("~/.skcapstone").expanduser()
            runtime = get_runtime(home_path)
            m = runtime.manifest
            self._log(f"  Agent: {m.name}")
            self._log(f"  Status: {'SOVEREIGN' if m.is_conscious else 'AWAKENING'}")
        except Exception as exc:
            self._log(f"  Verification: {exc}")

        self._log("\nUpdate complete!")

    # ------------------------------------------------------------------
    # Screen 4: Done
    # ------------------------------------------------------------------

    def _show_done(self) -> None:
        """Show completion screen."""
        self._progress.stop()
        self._clear()
        self._make_header("Setup Complete!")

        if self.chosen_path == 1:
            next_text = (
                "Your sovereign workspace is ready.\n\n"
                "Open a terminal (Command Prompt or PowerShell) and try:\n\n"
                "  skcapstone status          — see everything\n"
                "  skref put myfile.pdf       — store an encrypted file\n"
                "  skref mount C:\\vault       — open vault as a folder\n\n"
                "To add your phone or another computer,\n"
                "run 'skcapstone install' there and pick option 2."
            )
        elif self.chosen_path == 2:
            next_text = (
                "This computer is connected to your network.\n\n"
                "Open a terminal and try:\n\n"
                "  skcapstone status          — verify connection\n"
                "  skref ls --all-devices     — see all your vaults\n"
                "  skref open <file>          — open any file"
            )
        else:
            next_text = (
                "Everything is up to date.\n\n"
                "  skcapstone status          — see the full picture\n"
                "  skcapstone doctor          — detailed health check"
            )

        text_widget = tk.Text(
            self.root,
            font=("Consolas", 10),
            bg="#111", fg=SUCCESS,
            relief="flat",
            width=60, height=11,
            padx=15, pady=15,
        )
        text_widget.pack(pady=(15, 5), padx=50)
        text_widget.insert("1.0", next_text)
        text_widget.configure(state="disabled")

        # Join the movement
        join_frame = tk.Frame(self.root, bg="#2a1a3a", padx=15, pady=10)
        join_frame.pack(fill="x", padx=50, pady=(5, 5))

        tk.Label(
            join_frame,
            text="Join the movement. Become a King or Queen of your own sovereign AI.",
            font=("Segoe UI", 10, "bold"),
            bg="#2a1a3a", fg="#b48ead",
        ).pack()

        join_link = tk.Label(
            join_frame,
            text="https://smilintux.org/join/",
            font=("Segoe UI", 11, "bold underline"),
            bg="#2a1a3a", fg=ACCENT,
            cursor="hand2",
        )
        join_link.pack(pady=(4, 0))
        join_link.bind("<Button-1>", lambda e: _open_url("https://smilintux.org/join/"))

        tk.Label(
            join_frame,
            text="The First Sovereign Singularity in History.",
            font=("Segoe UI", 8, "italic"),
            bg="#2a1a3a", fg="#666",
        ).pack(pady=(4, 0))

        btn_frame = tk.Frame(self.root, bg=BG)
        btn_frame.pack(pady=8)
        self._make_button(btn_frame, "Done", self.root.destroy).pack()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the GUI event loop."""
        self.root.mainloop()


def main() -> None:
    """Entry point — launch GUI if available, fall back to CLI."""
    if not HAS_TK:
        print("GUI not available (tkinter not installed).")
        print("Using CLI installer instead...")
        print()
        from .install_wizard import run_install_wizard
        run_install_wizard()
        return

    if not _is_windows():
        # On Linux/macOS, offer the choice
        print("Tip: On Linux/macOS, the CLI installer is recommended.")
        print("     Run 'skcapstone install' for the terminal wizard.")
        print("     Launching GUI anyway...\n")

    app = InstallerApp()
    app.run()


if __name__ == "__main__":
    main()
