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
        self.root.geometry("1220x820")
        self.root.minsize(980, 640)

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
        self.run_button = ttk.Button(right_actions, text="Build", command=self.run_command)
        self.run_button.pack(side="left", padx=(8, 0))

    def _build_project_tab(self) -> None:
        self.project_tab.columnconfigure(0, weight=1)
        self.project_tab.columnconfigure(1, weight=1)
        self.project_tab.rowconfigure(1, weight=1)

        config = ttk.LabelFrame(self.project_tab, text="General", padding=10)
        config.grid(row=0, column=0, columnspan=2, sticky="ew")
        for col in range(6):
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
        ttk.Entry(config, textvariable=self.top_var).grid(row=0, column=5, sticky="ew", padx=(6, 0), pady=0)

        ttk.Label(config, text="Output directory").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(config, textvariable=self.mdir_var).grid(row=1, column=1, sticky="ew", padx=(6, 10), pady=(10, 0))
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

        info = ttk.LabelFrame(self.build_tab, text="Workflow notes", padding=10)
        info.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        info.columnconfigure(0, weight=1)

        text = (
            "SV binary is the easiest path when your top file is already a testbench. "
            "Use C++ testbench when you want --cc --exe --build with one or more C/C++ files. "
            "Use lint only for syntax and elaboration checks without building an executable."
        )
        ttk.Label(info, text=text, wraplength=900, justify="left").grid(row=0, column=0, sticky="w")

    def _build_output_tab(self) -> None:
        self.output_tab.columnconfigure(0, weight=1)
        self.output_tab.rowconfigure(1, weight=1)

        preview_frame = ttk.LabelFrame(self.output_tab, text="Generated commands", padding=10)
        preview_frame.grid(row=0, column=0, sticky="nsew")
        preview_frame.columnconfigure(0, weight=1)
        preview_frame.rowconfigure(0, weight=1)

        self.preview = tk.Text(preview_frame, height=8, wrap="word")
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
        if lint and self.exe_var.get():
            self.exe_var.set("")
        if workflow == WORKFLOW_BINARY and not self.exe_var.get():
            self.exe_var.set("Vsim")
        if workflow == WORKFLOW_CPP and not self.exe_var.get():
            self.exe_var.set("Vsim")
        run_state = "disabled" if lint else "normal"
        self.run_executable_button.configure(state=run_state)

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

    def _expected_executable_path(self) -> Path:
        output_dir = Path(self.mdir_var.get().strip() or "obj_dir")
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

    def refresh_command_preview(self) -> None:
        try:
            command = self.build_command()
            display = self._shell_join(command)
            if self.workflow_var.get() != WORKFLOW_LINT:
                run_command = [self._current_executable_path()]
                run_command.extend(self._parse_extra_args(self.run_args_var.get()))
                display += "\n\n" + self._shell_join(run_command)
            self._set_preview_text(display)
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
            self.status_var.set("Run finished")
        except Exception as exc:
            self._append_log(f"\nUnable to run executable: {exc}\n")
            self.status_var.set("Run error")

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
