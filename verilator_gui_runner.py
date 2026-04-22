from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "Verilator Build Assistant"
WORKFLOW_BINARY = "SV binary (--binary)"
WORKFLOW_CPP = "C++ testbench (--cc --exe --build)"
WORKFLOW_LINT = "Lint only (--lint-only)"
TRACE_NONE = "None"
TRACE_VCD = "VCD (--trace-vcd)"
TRACE_FST = "FST (--trace-fst)"
WAVE_EXTENSIONS = (".fst", ".vcd", ".vpd")


class ListPicker(ttk.LabelFrame):
    def __init__(
        self,
        master: tk.Misc,
        title: str,
        filetypes: list[tuple[str, str]] | None = None,
        allow_multiple: bool = True,
        add_mode: str = "files",
    ):
        super().__init__(master, text=title, padding=8)
        self.filetypes = filetypes or [("All files", "*.*")]
        self.allow_multiple = allow_multiple
        self.add_mode = add_mode

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.listbox = tk.Listbox(self, height=6, exportselection=False)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=self.scrollbar.set)

        self.button_frame = ttk.Frame(self)
        self.add_button = ttk.Button(self.button_frame, text="Add", command=self.add_items)
        self.remove_button = ttk.Button(self.button_frame, text="Remove", command=self.remove_selected)
        self.clear_button = ttk.Button(self.button_frame, text="Clear", command=self.clear)

        self.listbox.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.button_frame.grid(row=0, column=2, sticky="ns", padx=(8, 0))

        self.add_button.pack(fill="x")
        self.remove_button.pack(fill="x", pady=4)
        self.clear_button.pack(fill="x")

        if self.add_mode == "dirs":
            self.add_button.configure(text="Add folder")

    def add_items(self) -> None:
        if self.add_mode == "dirs":
            selected = filedialog.askdirectory(title=self["text"], mustexist=True)
            values = (selected,) if selected else ()
        elif self.allow_multiple:
            values = filedialog.askopenfilenames(title=self["text"], filetypes=self.filetypes)
        else:
            selected = filedialog.askopenfilename(title=self["text"], filetypes=self.filetypes)
            values = (selected,) if selected else ()

        existing = set(self.get_items())
        for value in values:
            if value and value not in existing:
                self.listbox.insert(tk.END, value)
                existing.add(value)

    def remove_selected(self) -> None:
        indexes = list(self.listbox.curselection())
        indexes.reverse()
        for index in indexes:
            self.listbox.delete(index)

    def clear(self) -> None:
        self.listbox.delete(0, tk.END)

    def get_items(self) -> list[str]:
        return list(self.listbox.get(0, tk.END))

    def set_items(self, items: list[str]) -> None:
        self.clear()
        for item in items:
            self.listbox.insert(tk.END, item)


class VerilatorApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1260x900")
        self.root.minsize(1040, 700)

        self.output_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.running_process: subprocess.Popen[str] | None = None

        cpu_count = max(1, os.cpu_count() or 1)
        self.verilator_var = tk.StringVar(value=shutil.which("verilator") or "verilator")
        self.workflow_var = tk.StringVar(value=WORKFLOW_BINARY)
        self.top_var = tk.StringVar()
        self.mdir_var = tk.StringVar(value="obj_dir")
        self.exe_var = tk.StringVar(value="Vsim")
        self.jobs_var = tk.IntVar(value=cpu_count)
        self.threads_var = tk.IntVar(value=1)
        self.wall_var = tk.BooleanVar(value=True)
        self.timing_var = tk.BooleanVar(value=True)
        self.coverage_var = tk.BooleanVar(value=False)
        self.no_assert_var = tk.BooleanVar(value=False)
        self.trace_var = tk.StringVar(value=TRACE_NONE)
        self.run_after_build_var = tk.BooleanVar(value=False)
        self.auto_scroll_var = tk.BooleanVar(value=True)
        self.extra_args_var = tk.StringVar()
        self.run_args_var = tk.StringVar()
        self.gtkwave_var = tk.StringVar(value=shutil.which("gtkwave") or "gtkwave")
        self.wave_file_var = tk.StringVar()
        self.wave_save_var = tk.StringVar()
        self.open_gtkwave_after_run_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        self._bind_events()
        self._apply_workflow_state()
        self.refresh_command_preview()
        self.root.after(120, self._poll_output_queue)

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        container = ttk.Frame(self.root, padding=10)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(container)
        self.notebook.grid(row=0, column=0, sticky="nsew")

        self.project_tab = ttk.Frame(self.notebook, padding=10)
        self.build_tab = ttk.Frame(self.notebook, padding=10)
        self.output_tab = ttk.Frame(self.notebook, padding=10)

        self.notebook.add(self.project_tab, text="Project")
        self.notebook.add(self.build_tab, text="Build and run")
        self.notebook.add(self.output_tab, text="Output")

        self._build_project_tab()
        self._build_build_tab()
        self._build_output_tab()

        bottom = ttk.Frame(container)
        bottom.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        bottom.columnconfigure(1, weight=1)

        left_actions = ttk.Frame(bottom)
        left_actions.grid(row=0, column=0, sticky="w")
        ttk.Button(left_actions, text="Check Verilator", command=self.check_verilator).pack(side="left")
        ttk.Button(left_actions, text="Refresh command", command=self.refresh_command_preview).pack(side="left", padx=(8, 0))
        ttk.Button(left_actions, text="Copy command", command=self.copy_command).pack(side="left", padx=(8, 0))
        ttk.Button(left_actions, text="Save preset", command=self.save_preset).pack(side="left", padx=(8, 0))
        ttk.Button(left_actions, text="Load preset", command=self.load_preset).pack(side="left", padx=(8, 0))

        self.status_label = ttk.Label(bottom, textvariable=self.status_var, anchor="center")
        self.status_label.grid(row=0, column=1, sticky="ew", padx=12)

        right_actions = ttk.Frame(bottom)
        right_actions.grid(row=0, column=2, sticky="e")
        ttk.Button(right_actions, text="Open output tab", command=lambda: self.notebook.select(self.output_tab)).pack(side="left")
        ttk.Button(right_actions, text="Stop", command=self.stop_process).pack(side="left", padx=(8, 0))
        self.run_executable_button = ttk.Button(right_actions, text="Run executable", command=self.run_executable)
        self.run_executable_button.pack(side="left", padx=(8, 0))
        self.open_gtkwave_button = ttk.Button(right_actions, text="Open GTKWave", command=self.open_gtkwave)
        self.open_gtkwave_button.pack(side="left", padx=(8, 0))
        self.run_button = ttk.Button(right_actions, text="Build", command=self.run_command)
        self.run_button.pack(side="left", padx=(8, 0))

    def _build_project_tab(self) -> None:
        self.project_tab.columnconfigure(0, weight=1)
        self.project_tab.columnconfigure(1, weight=1)
        self.project_tab.rowconfigure(1, weight=1)

        config = ttk.LabelFrame(self.project_tab, text="General", padding=10)
        config.grid(row=0, column=0, columnspan=2, sticky="ew")
        for col in range(7):
            config.columnconfigure(col, weight=1 if col in (1, 3, 5) else 0)

        ttk.Label(config, text="Verilator").grid(row=0, column=0, sticky="w")
        ttk.Entry(config, textvariable=self.verilator_var).grid(row=0, column=1, sticky="ew", padx=(6, 10))
        ttk.Label(config, text="Workflow").grid(row=0, column=2, sticky="w")
        workflow_combo = ttk.Combobox(
            config,
            textvariable=self.workflow_var,
            state="readonly",
            values=[WORKFLOW_BINARY, WORKFLOW_CPP, WORKFLOW_LINT],
        )
        workflow_combo.grid(row=0, column=3, sticky="ew", padx=(6, 10))
        ttk.Label(config, text="Top module").grid(row=0, column=4, sticky="w")
        ttk.Entry(config, textvariable=self.top_var).grid(row=0, column=5, sticky="ew", padx=(6, 0))
        ttk.Label(config, text="Output directory").grid(row=1, column=0, sticky="w", pady=(10, 0))
        output_dir_row = ttk.Frame(config)
        output_dir_row.grid(row=1, column=1, sticky="ew", padx=(6, 10), pady=(10, 0))
        output_dir_row.columnconfigure(0, weight=1)
        ttk.Entry(output_dir_row, textvariable=self.mdir_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_dir_row, text="Browse", command=self.choose_output_directory).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(config, text="Executable name").grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Entry(config, textvariable=self.exe_var).grid(row=1, column=3, sticky="ew", padx=(6, 10), pady=(10, 0))
        ttk.Label(config, text="Build jobs").grid(row=1, column=4, sticky="w", pady=(10, 0))
        ttk.Spinbox(config, from_=1, to=256, textvariable=self.jobs_var, width=8).grid(row=1, column=5, sticky="w", padx=(6, 0), pady=(10, 0))

        self.sources_frame = ListPicker(
            self.project_tab,
            "Verilog/SystemVerilog sources",
            [("HDL files", "*.sv *.svh *.v *.vh"), ("All files", "*.*")],
            allow_multiple=True,
        )
        self.sources_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0), padx=(0, 5))

        self.argfiles_frame = ListPicker(
            self.project_tab,
            "Argument files (-f)",
            [("Argument files", "*.f *.vf *.args *.txt"), ("All files", "*.*")],
            allow_multiple=True,
        )
        self.argfiles_frame.grid(row=1, column=1, sticky="nsew", pady=(10, 0), padx=(5, 0))

        lower = ttk.Frame(self.project_tab)
        lower.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        lower.columnconfigure(0, weight=1)
        lower.columnconfigure(1, weight=1)

        self.includes_frame = ListPicker(
            lower,
            "Include directories",
            [("All files", "*.*")],
            allow_multiple=True,
            add_mode="dirs",
        )
        self.includes_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 5))

        self.cpp_frame = ListPicker(
            lower,
            "C/C++ testbench files",
            [("C/C++ files", "*.c *.cc *.cpp *.cxx"), ("All files", "*.*")],
            allow_multiple=True,
        )
        self.cpp_frame.grid(row=0, column=1, sticky="nsew", padx=(5, 0))

    def _build_build_tab(self) -> None:
        self.build_tab.columnconfigure(0, weight=1)
        self.build_tab.columnconfigure(1, weight=1)
        self.build_tab.rowconfigure(2, weight=1)

        options = ttk.LabelFrame(self.build_tab, text="Common options", padding=10)
        options.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        options.columnconfigure(0, weight=1)
        options.columnconfigure(1, weight=1)

        ttk.Checkbutton(options, text="-Wall", variable=self.wall_var).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(options, text="Timing enabled", variable=self.timing_var).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(options, text="Coverage (--coverage)", variable=self.coverage_var).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(options, text="Disable assert (--no-assert)", variable=self.no_assert_var).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Checkbutton(options, text="Run executable after build", variable=self.run_after_build_var).grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Checkbutton(options, text="Auto-scroll output", variable=self.auto_scroll_var).grid(row=2, column=1, sticky="w", pady=(8, 0))

        ttk.Label(options, text="Trace").grid(row=3, column=0, sticky="w", pady=(12, 0))
        ttk.Combobox(
            options,
            textvariable=self.trace_var,
            state="readonly",
            values=[TRACE_NONE, TRACE_VCD, TRACE_FST],
        ).grid(row=3, column=1, sticky="ew", pady=(12, 0))

        ttk.Label(options, text="Threads").grid(row=4, column=0, sticky="w", pady=(12, 0))
        ttk.Spinbox(options, from_=1, to=256, textvariable=self.threads_var, width=8).grid(row=4, column=1, sticky="w", pady=(12, 0))

        advanced = ttk.LabelFrame(self.build_tab, text="Arguments", padding=10)
        advanced.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        advanced.columnconfigure(0, weight=1)

        ttk.Label(advanced, text="Extra Verilator args").grid(row=0, column=0, sticky="w")
        ttk.Entry(advanced, textvariable=self.extra_args_var).grid(row=1, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(advanced, text="Executable args").grid(row=2, column=0, sticky="w", pady=(12, 0))
        ttk.Entry(advanced, textvariable=self.run_args_var).grid(row=3, column=0, sticky="ew", pady=(6, 0))

        gtkwave = ttk.LabelFrame(self.build_tab, text="GTKWave", padding=10)
        gtkwave.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        gtkwave.columnconfigure(1, weight=1)
        gtkwave.columnconfigure(3, weight=1)

        ttk.Label(gtkwave, text="GTKWave executable").grid(row=0, column=0, sticky="w")
        gtk_row = ttk.Frame(gtkwave)
        gtk_row.grid(row=0, column=1, sticky="ew", padx=(6, 10))
        gtk_row.columnconfigure(0, weight=1)
        ttk.Entry(gtk_row, textvariable=self.gtkwave_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(gtk_row, text="Browse", command=self.choose_gtkwave_executable).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(gtkwave, text="Wave dump file").grid(row=0, column=2, sticky="w")
        wave_row = ttk.Frame(gtkwave)
        wave_row.grid(row=0, column=3, sticky="ew")
        wave_row.columnconfigure(0, weight=1)
        ttk.Entry(wave_row, textvariable=self.wave_file_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(wave_row, text="Browse", command=self.choose_wave_file).grid(row=0, column=1, padx=(8, 0))

        ttk.Label(gtkwave, text="GTKWave save file").grid(row=1, column=0, sticky="w", pady=(10, 0))
        save_row = ttk.Frame(gtkwave)
        save_row.grid(row=1, column=1, sticky="ew", padx=(6, 10), pady=(10, 0))
        save_row.columnconfigure(0, weight=1)
        ttk.Entry(save_row, textvariable=self.wave_save_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(save_row, text="Browse", command=self.choose_wave_save_file).grid(row=0, column=1, padx=(8, 0))

        ttk.Checkbutton(gtkwave, text="Open GTKWave after run", variable=self.open_gtkwave_after_run_var).grid(row=1, column=2, sticky="w", pady=(10, 0))
        gtkwave_actions = ttk.Frame(gtkwave)
        gtkwave_actions.grid(row=1, column=3, sticky="e", pady=(10, 0))
        ttk.Button(gtkwave_actions, text="Use newest wave", command=self.use_newest_wave_file).pack(side="left")
        ttk.Button(gtkwave_actions, text="Open GTKWave", command=self.open_gtkwave).pack(side="left", padx=(8, 0))

        info = ttk.LabelFrame(self.build_tab, text="Workflow notes", padding=10)
        info.grid(row=2, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        info.columnconfigure(0, weight=1)

        text = (
            "SV binary is the easiest path when your top file is already a testbench. "
            "Use C++ testbench when you want --cc --exe --build with one or more C/C++ files. "
            "Use lint only for syntax and elaboration checks without building an executable. "
            "After a successful run, the tool can auto-detect the newest wave dump and open GTKWave."
        )
        ttk.Label(info, text=text, wraplength=960, justify="left").grid(row=0, column=0, sticky="w")

    def _build_output_tab(self) -> None:
        self.output_tab.columnconfigure(0, weight=1)
        self.output_tab.rowconfigure(1, weight=1)

        preview_frame = ttk.LabelFrame(self.output_tab, text="Generated commands", padding=10)
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.preview = tk.Text(preview_frame, height=10, wrap="word")
        self.preview.grid(row=0, column=0, sticky="nsew")
        preview_scroll = ttk.Scrollbar(preview_frame, orient="vertical", command=self.preview.yview)
        preview_scroll.grid(row=0, column=1, sticky="ns")
        self.preview.configure(yscrollcommand=preview_scroll.set)

        log_frame = ttk.LabelFrame(self.output_tab, text="Build log", padding=10)
        log_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log = tk.Text(log_frame, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scroll.set)

    def _bind_events(self) -> None:
        variables = [
            self.verilator_var,
            self.workflow_var,
            self.top_var,
            self.mdir_var,
            self.exe_var,
            self.jobs_var,
            self.threads_var,
            self.wall_var,
            self.timing_var,
            self.coverage_var,
            self.no_assert_var,
            self.trace_var,
            self.run_after_build_var,
            self.extra_args_var,
            self.run_args_var,
            self.gtkwave_var,
            self.wave_file_var,
            self.wave_save_var,
            self.open_gtkwave_after_run_var,
        ]
        for variable in variables:
            variable.trace_add("write", self._on_state_change)

    def _on_state_change(self, *_args) -> None:
        self._apply_workflow_state()
        self.refresh_command_preview()

    def _apply_workflow_state(self) -> None:
        workflow = self.workflow_var.get()
        lint = workflow == WORKFLOW_LINT
        cpp = workflow == WORKFLOW_CPP
        state = "normal" if cpp else "disabled"
        for widget in (self.cpp_frame.listbox, self.cpp_frame.add_button, self.cpp_frame.remove_button, self.cpp_frame.clear_button):
            widget.configure(state=state)
        if lint and self.run_after_build_var.get():
            self.run_after_build_var.set(False)
        if lint and self.open_gtkwave_after_run_var.get():
            self.open_gtkwave_after_run_var.set(False)
        if lint and self.exe_var.get():
            self.exe_var.set("")
        if workflow in (WORKFLOW_BINARY, WORKFLOW_CPP) and not self.exe_var.get():
            self.exe_var.set("Vsim")
        run_state = "disabled" if lint else "normal"
        self.run_executable_button.configure(state=run_state)
        self.open_gtkwave_button.configure(state="normal")

    def choose_output_directory(self) -> None:
        selected = filedialog.askdirectory(title="Choose output directory")
        if selected:
            self.mdir_var.set(selected)

    def choose_gtkwave_executable(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose GTKWave executable",
            filetypes=[("Executable files", "*"), ("All files", "*.*")],
        )
        if selected:
            self.gtkwave_var.set(selected)

    def choose_wave_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose wave dump file",
            filetypes=[("Wave files", "*.fst *.vcd *.vpd"), ("All files", "*.*")],
        )
        if selected:
            self.wave_file_var.set(selected)

    def choose_wave_save_file(self) -> None:
        selected = filedialog.askopenfilename(
            title="Choose GTKWave save file",
            filetypes=[("GTKWave save files", "*.gtkw *.sav"), ("All files", "*.*")],
        )
        if selected:
            self.wave_save_var.set(selected)

    def _append_log(self, text: str) -> None:
        self.log.insert(tk.END, text)
        if self.auto_scroll_var.get():
            self.log.see(tk.END)

    def _set_preview_text(self, text: str) -> None:
        self.preview.configure(state="normal")
        self.preview.delete("1.0", tk.END)
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _shell_join(self, parts: list[str]) -> str:
        return " ".join(shlex.quote(part) for part in parts)

    def _parse_extra_args(self, raw: str) -> list[str]:
        return shlex.split(raw) if raw.strip() else []

    def _normalize_path(self, raw: str) -> Path:
        return Path(raw).expanduser()

    def _expected_executable_path(self) -> Path:
        output_dir = self._normalize_path(self.mdir_var.get().strip() or "obj_dir")
        executable = self.exe_var.get().strip() or "Vsim"
        suffix = ".exe" if os.name == "nt" else ""
        return output_dir / f"{executable}{suffix}"

    def _current_executable_path(self) -> str:
        found = self._find_built_executable()
        if found is not None:
            return str(found)
        return str(self._expected_executable_path())

    def _find_built_executable(self) -> Path | None:
        if self.workflow_var.get() == WORKFLOW_LINT:
            return None
        expected = self._expected_executable_path()
        if expected.exists():
            return expected
        output_dir = expected.parent
        if not output_dir.exists() or not output_dir.is_dir():
            return None
        candidates: list[Path] = []
        for entry in output_dir.iterdir():
            if not entry.is_file():
                continue
            if os.name == "nt":
                if entry.suffix.lower() == ".exe":
                    candidates.append(entry)
            else:
                if os.access(entry, os.X_OK):
                    candidates.append(entry)
        if not candidates:
            return None
        requested = self.exe_var.get().strip()
        if requested:
            for candidate in candidates:
                if candidate.name == requested or candidate.stem == requested:
                    return candidate
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return candidates[0]

    def _iter_wave_candidates(self, directory: Path) -> list[Path]:
        if not directory.exists() or not directory.is_dir():
            return []
        found: list[Path] = []
        try:
            for path in directory.rglob("*"):
                if path.is_file() and path.suffix.lower() in WAVE_EXTENSIONS:
                    found.append(path)
        except Exception:
            return []
        return found

    def _find_newest_wave_in_directory(self, directory: Path) -> Path | None:
        candidates = self._iter_wave_candidates(directory)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return candidates[0]

    def _wave_search_roots(self) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()

        def add(path: Path | None) -> None:
            if path is None:
                return
            try:
                key = str(path.resolve())
            except Exception:
                key = str(path)
            if key in seen:
                return
            seen.add(key)
            roots.append(path)

        add(self._normalize_path(self.mdir_var.get().strip() or "obj_dir"))
        explicit = self.wave_file_var.get().strip()
        if explicit:
            explicit_path = self._normalize_path(explicit)
            if explicit_path.is_dir():
                add(explicit_path)
            else:
                add(explicit_path.parent)
        for source in self.sources_frame.get_items():
            add(self._normalize_path(source).parent)
        add(Path.cwd())
        return roots

    def _resolve_wave_file(self, prefer_manual: bool = True) -> Path | None:
        explicit = self.wave_file_var.get().strip()
        if prefer_manual and explicit:
            explicit_path = self._normalize_path(explicit)
            checks = [explicit_path]
            output_dir = self._normalize_path(self.mdir_var.get().strip() or "obj_dir")
            if not explicit_path.is_absolute():
                checks.append(output_dir / explicit_path)
                checks.append(Path.cwd() / explicit_path)
            for candidate in checks:
                if candidate.is_file() and candidate.suffix.lower() in WAVE_EXTENSIONS:
                    return candidate
                if candidate.is_dir():
                    found = self._find_newest_wave_in_directory(candidate)
                    if found is not None:
                        return found

        candidates: list[Path] = []
        for root in self._wave_search_roots():
            found = self._find_newest_wave_in_directory(root)
            if found is not None:
                candidates.append(found)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        return candidates[0]

    def _refresh_wave_field_from_detection(self, announce: bool = False) -> Path | None:
        detected = self._resolve_wave_file(prefer_manual=True)
        if detected is None:
            if announce:
                self._append_log("\nNo wave dump file detected.\n")
                self.status_var.set("Wave file not found")
            return None
        current = self.wave_file_var.get().strip()
        if current != str(detected):
            self.wave_file_var.set(str(detected))
        if announce:
            self._append_log(f"\nUsing wave dump file: {detected}\n")
            self.status_var.set("Wave file ready")
        return detected

    def use_newest_wave_file(self) -> None:
        self.notebook.select(self.output_tab)
        self._refresh_wave_field_from_detection(announce=True)

    def build_command(self) -> list[str]:
        workflow = self.workflow_var.get()
        command: list[str] = [self.verilator_var.get().strip() or "verilator"]

        if workflow == WORKFLOW_BINARY:
            command.append("--binary")
        elif workflow == WORKFLOW_CPP:
            command.extend(["--cc", "--exe", "--build"])
        elif workflow == WORKFLOW_LINT:
            command.append("--lint-only")

        sources = self.sources_frame.get_items()
        argfiles = self.argfiles_frame.get_items()
        includes = self.includes_frame.get_items()
        cpp_files = self.cpp_frame.get_items()

        for source in sources:
            command.append(source)
        for argfile in argfiles:
            command.extend(["-f", argfile])
        for include in includes:
            command.append(f"+incdir+{include}")

        top = self.top_var.get().strip()
        if top:
            command.extend(["--top-module", top])

        output_dir = self.mdir_var.get().strip()
        if output_dir:
            command.extend(["--Mdir", output_dir])

        if self.wall_var.get():
            command.append("-Wall")
        if not self.timing_var.get():
            command.append("--no-timing")
        if self.coverage_var.get():
            command.append("--coverage")
        if self.no_assert_var.get():
            command.append("--no-assert")

        trace_mode = self.trace_var.get()
        if trace_mode == TRACE_VCD:
            command.append("--trace-vcd")
        elif trace_mode == TRACE_FST:
            command.append("--trace-fst")

        threads = max(1, int(self.threads_var.get()))
        command.extend(["--threads", str(threads)])

        if workflow in (WORKFLOW_BINARY, WORKFLOW_CPP):
            executable = self.exe_var.get().strip()
            if executable:
                command.extend(["-o", executable])

        if workflow in (WORKFLOW_BINARY, WORKFLOW_CPP):
            jobs = max(1, int(self.jobs_var.get()))
            command.extend(["--build-jobs", str(jobs)])

        if workflow == WORKFLOW_CPP:
            command.extend(cpp_files)

        command.extend(self._parse_extra_args(self.extra_args_var.get()))
        return command

    def build_gtkwave_command(self, wave_path: Path | None = None) -> list[str]:
        command = [self.gtkwave_var.get().strip() or "gtkwave"]
        wave_target = wave_path or self._resolve_wave_file(prefer_manual=True)
        if wave_target is not None:
            command.append(str(wave_target))
        save_path = self.wave_save_var.get().strip()
        if save_path:
            command.append(save_path)
        return command

    def refresh_command_preview(self) -> None:
        try:
            parts = ["Build:\n" + self._shell_join(self.build_command())]
            if self.workflow_var.get() != WORKFLOW_LINT:
                run_command = [self._current_executable_path()]
                run_command.extend(self._parse_extra_args(self.run_args_var.get()))
                parts.append("Run executable:\n" + self._shell_join(run_command))
            gtk_command = self.build_gtkwave_command()
            if len(gtk_command) > 1 or self.wave_save_var.get().strip():
                parts.append("GTKWave:\n" + self._shell_join(gtk_command))
            else:
                parts.append("GTKWave:\n" + (self.gtkwave_var.get().strip() or "gtkwave") + " <wave-file>")
            self._set_preview_text("\n\n".join(parts))
            self.status_var.set("Command ready")
        except Exception as exc:
            self._set_preview_text(f"Unable to build command:\n{exc}")
            self.status_var.set("Command error")

    def copy_command(self) -> None:
        text = self.preview.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Command copied to clipboard")

    def check_verilator(self) -> None:
        command = [self.verilator_var.get().strip() or "verilator", "--version"]
        try:
            completed = subprocess.run(command, capture_output=True, text=True, check=True)
            message = completed.stdout.strip() or completed.stderr.strip() or "Verilator detected"
            self.status_var.set(message)
            self._append_log(message + "\n")
        except Exception as exc:
            messagebox.showerror("Verilator check", f"Unable to run Verilator:\n{exc}")
            self.status_var.set("Verilator not available")

    def _validate_before_run(self) -> bool:
        if not self.sources_frame.get_items():
            messagebox.showwarning("Missing sources", "Add at least one Verilog or SystemVerilog source file.")
            self.notebook.select(self.project_tab)
            return False
        if self.workflow_var.get() == WORKFLOW_CPP and not self.cpp_frame.get_items():
            messagebox.showwarning("Missing C/C++ files", "The C++ testbench workflow needs at least one C or C++ file.")
            self.notebook.select(self.project_tab)
            return False
        return True

    def run_command(self) -> None:
        if self.running_process is not None:
            messagebox.showinfo("Build already running", "A build is already running.")
            return
        if not self._validate_before_run():
            return

        self.refresh_command_preview()
        self.notebook.select(self.output_tab)
        command = self.build_command()

        self.log.delete("1.0", tk.END)
        self._append_log("$ " + self._shell_join(command) + "\n\n")
        self.status_var.set("Building")
        self.run_button.configure(state="disabled")
        self.run_executable_button.configure(state="disabled")
        self.open_gtkwave_button.configure(state="disabled")

        def worker() -> None:
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
                self.running_process = process
                if process.stdout is not None:
                    for line in process.stdout:
                        self.output_queue.put(("log", line))
                return_code = process.wait()
                self.output_queue.put(("finished", str(return_code)))
            except Exception as exc:
                self.output_queue.put(("error", str(exc)))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def stop_process(self) -> None:
        if self.running_process is None:
            self.status_var.set("No active build")
            return
        try:
            self.running_process.terminate()
            self.status_var.set("Stopping")
        except Exception as exc:
            messagebox.showerror("Stop build", f"Unable to stop the build:\n{exc}")

    def run_executable(self) -> None:
        if self.workflow_var.get() == WORKFLOW_LINT:
            self.status_var.set("Lint workflow has no executable")
            return
        self.notebook.select(self.output_tab)
        self._run_executable()

    def _run_executable(self) -> None:
        detected = self._find_built_executable()
        if detected is None:
            expected = self._expected_executable_path()
            self._append_log(f"\nNo runnable executable found in {expected.parent}\n")
            self._append_log(f"Expected executable: {expected}\n")
            self.status_var.set("Executable not found")
            return

        run_command = [str(detected)]
        run_command.extend(self._parse_extra_args(self.run_args_var.get()))
        self._append_log("\n$ " + self._shell_join(run_command) + "\n\n")
        if detected != self._expected_executable_path():
            self._append_log(f"Using detected executable: {detected}\n\n")

        try:
            completed = subprocess.run(run_command, capture_output=True, text=True)
            if completed.stdout:
                self._append_log(completed.stdout)
            if completed.stderr:
                self._append_log(completed.stderr)
            self._append_log(f"\nProcess exited with code {completed.returncode}\n")
            if completed.returncode == 0:
                self.status_var.set("Run finished")
                if self.open_gtkwave_after_run_var.get():
                    self._open_gtkwave_after_run()
            else:
                self.status_var.set("Run finished with errors")
        except Exception as exc:
            self._append_log(f"\nUnable to run executable: {exc}\n")
            self.status_var.set("Run error")

    def _open_gtkwave_after_run(self) -> None:
        detected = self._refresh_wave_field_from_detection(announce=True)
        if detected is None:
            self._append_log("GTKWave was not opened because no wave dump file was found.\n")
            return
        self._open_gtkwave(detected)

    def open_gtkwave(self) -> None:
        self.notebook.select(self.output_tab)
        detected = self._refresh_wave_field_from_detection(announce=True)
        if detected is None:
            return
        self._open_gtkwave(detected)

    def _open_gtkwave(self, wave_path: Path) -> None:
        command = self.build_gtkwave_command(wave_path)
        self._append_log("\n$ " + self._shell_join(command) + "\n\n")
        try:
            subprocess.Popen(command)
            self.status_var.set("GTKWave started")
        except Exception as exc:
            self._append_log(f"\nUnable to start GTKWave: {exc}\n")
            self.status_var.set("GTKWave error")

    def _poll_output_queue(self) -> None:
        while True:
            try:
                kind, payload = self.output_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self._append_log(payload)
            elif kind == "finished":
                self.running_process = None
                self.run_button.configure(state="normal")
                self.run_executable_button.configure(state="normal" if self.workflow_var.get() != WORKFLOW_LINT else "disabled")
                self.open_gtkwave_button.configure(state="normal")
                code = int(payload)
                if code == 0:
                    self._append_log(f"\nBuild finished successfully with exit code {code}\n")
                    self.status_var.set("Build finished")
                    if self.workflow_var.get() != WORKFLOW_LINT and self.run_after_build_var.get():
                        self._run_executable()
                else:
                    self._append_log(f"\nBuild failed with exit code {code}\n")
                    self.status_var.set("Build failed")
            elif kind == "error":
                self.running_process = None
                self.run_button.configure(state="normal")
                self.run_executable_button.configure(state="normal" if self.workflow_var.get() != WORKFLOW_LINT else "disabled")
                self.open_gtkwave_button.configure(state="normal")
                self._append_log(f"\nUnable to start process: {payload}\n")
                self.status_var.set("Run error")

        self.root.after(120, self._poll_output_queue)

    def _collect_preset(self) -> dict[str, object]:
        return {
            "verilator": self.verilator_var.get(),
            "workflow": self.workflow_var.get(),
            "top_module": self.top_var.get(),
            "output_dir": self.mdir_var.get(),
            "executable": self.exe_var.get(),
            "build_jobs": int(self.jobs_var.get()),
            "threads": int(self.threads_var.get()),
            "wall": bool(self.wall_var.get()),
            "timing": bool(self.timing_var.get()),
            "coverage": bool(self.coverage_var.get()),
            "no_assert": bool(self.no_assert_var.get()),
            "trace": self.trace_var.get(),
            "run_after_build": bool(self.run_after_build_var.get()),
            "auto_scroll": bool(self.auto_scroll_var.get()),
            "extra_args": self.extra_args_var.get(),
            "run_args": self.run_args_var.get(),
            "gtkwave": self.gtkwave_var.get(),
            "wave_file": self.wave_file_var.get(),
            "wave_save": self.wave_save_var.get(),
            "open_gtkwave_after_run": bool(self.open_gtkwave_after_run_var.get()),
            "sources": self.sources_frame.get_items(),
            "argfiles": self.argfiles_frame.get_items(),
            "includes": self.includes_frame.get_items(),
            "cpp_files": self.cpp_frame.get_items(),
        }

    def save_preset(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save preset",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            Path(path).write_text(json.dumps(self._collect_preset(), indent=2), encoding="utf-8")
            self.status_var.set("Preset saved")
        except Exception as exc:
            messagebox.showerror("Save preset", f"Unable to save preset:\n{exc}")

    def load_preset(self) -> None:
        path = filedialog.askopenfilename(
            title="Load preset",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.verilator_var.set(str(data.get("verilator", self.verilator_var.get())))
            self.workflow_var.set(str(data.get("workflow", self.workflow_var.get())))
            self.top_var.set(str(data.get("top_module", "")))
            self.mdir_var.set(str(data.get("output_dir", self.mdir_var.get())))
            self.exe_var.set(str(data.get("executable", self.exe_var.get())))
            self.jobs_var.set(int(data.get("build_jobs", self.jobs_var.get())))
            self.threads_var.set(int(data.get("threads", self.threads_var.get())))
            self.wall_var.set(bool(data.get("wall", self.wall_var.get())))
            self.timing_var.set(bool(data.get("timing", self.timing_var.get())))
            self.coverage_var.set(bool(data.get("coverage", self.coverage_var.get())))
            self.no_assert_var.set(bool(data.get("no_assert", self.no_assert_var.get())))
            self.trace_var.set(str(data.get("trace", self.trace_var.get())))
            self.run_after_build_var.set(bool(data.get("run_after_build", self.run_after_build_var.get())))
            self.auto_scroll_var.set(bool(data.get("auto_scroll", self.auto_scroll_var.get())))
            self.extra_args_var.set(str(data.get("extra_args", "")))
            self.run_args_var.set(str(data.get("run_args", "")))
            self.gtkwave_var.set(str(data.get("gtkwave", self.gtkwave_var.get())))
            self.wave_file_var.set(str(data.get("wave_file", "")))
            self.wave_save_var.set(str(data.get("wave_save", "")))
            self.open_gtkwave_after_run_var.set(bool(data.get("open_gtkwave_after_run", self.open_gtkwave_after_run_var.get())))
            self.sources_frame.set_items(list(data.get("sources", [])))
            self.argfiles_frame.set_items(list(data.get("argfiles", [])))
            self.includes_frame.set_items(list(data.get("includes", [])))
            self.cpp_frame.set_items(list(data.get("cpp_files", [])))
            self._apply_workflow_state()
            self.refresh_command_preview()
            self.status_var.set("Preset loaded")
        except Exception as exc:
            messagebox.showerror("Load preset", f"Unable to load preset:\n{exc}")


def main() -> None:
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.1)
    except tk.TclError:
        pass
    style = ttk.Style()
    if "clam" in style.theme_names():
        style.theme_use("clam")
    VerilatorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
