from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import queue
import threading
import webbrowser

from . import __version__, batch
from .journal import write_json


TERMINAL = {"completed", "cached", "failed", "qa_failed", "cancelled", "analyzed", "discovered"}


@dataclass
class GuiState:
    records: dict[str, dict] = field(default_factory=dict)
    total: int = 0
    active: bool = False
    status: str = "Ready"
    exit_code: int | None = None

    def begin(self):
        self.records.clear()
        self.total, self.active, self.status, self.exit_code = 0, True, "Starting", None

    def accept(self, event: dict):
        kind = event.get("type")
        if kind == "planned":
            self.total = event["total"]
        elif kind == "job":
            self.records[event["source"]] = event
        elif kind in {"finished", "error"}:
            self.active = False
            self.exit_code = event.get("exit_code", 2)
        self.status = event.get("message") or event.get("status") or self.status

    @property
    def completed(self):
        return sum(r.get("status") in TERMINAL for r in self.records.values())


def build_argv(settings: dict, mode: str, *, resume=False, retry_failed=False) -> list[str]:
    argv = [settings["input_root"], settings["output_root"], "--profile", settings["profile"],
            "--workers", str(settings.get("workers", 1)), "--retries", str(settings.get("retries", 0))]
    for name in ("class", "subject", "chapter", "material"):
        value = str(settings.get(name, "")).strip()
        if value and value != "All":
            argv += [f"--{name}-filter", value.lower() if name == "subject" else value]
    if mode == "dry":
        argv.append("--dry-run")
    elif mode == "analyze":
        argv.append("--analyze-only")
        if settings.get("plan_previews", True):
            argv.append("--preview-plans")
    if settings.get("contact_sheets", True):
        argv.append("--contact-sheets")
    for option in ("overwrite", "no_cache"):
        if settings.get(option):
            argv.append("--" + option.replace("_", "-"))
    if resume:
        argv.append("--resume")
    if retry_failed:
        argv.append("--retry-failed")
    return argv


class BrandingApp:
    def __init__(self, root, *, settings_path=None, runner=None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.runner = runner or batch.main
        appdata = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        self.settings_path = Path(settings_path) if settings_path else appdata / "PDFBranding/gui_settings.json"
        base = Path(__file__).resolve().parent.parent
        defaults = {"input_root": str(base / "foundation_notes"), "output_root": str(base / "v3_output_350"),
                    "profile": str(base / "profiles/sskem.json"), "class": "All", "subject": "All",
                    "chapter": "", "material": "", "workers": 1, "retries": 0,
                    "plan_previews": True, "contact_sheets": True, "overwrite": False, "no_cache": False}
        try:
            saved_settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(saved_settings, dict):
                defaults.update({k: v for k, v in saved_settings.items() if k in defaults})
        except (OSError, ValueError, TypeError):
            pass
        self.vars = {name: (tk.BooleanVar(value=value) if isinstance(value, bool)
                           else tk.IntVar(value=value) if isinstance(value, int)
                           else tk.StringVar(value=str(value))) for name, value in defaults.items()}
        self.state, self.events, self.cancel_event = GuiState(), queue.Queue(), threading.Event()
        self.thread = None
        self.generation = 0
        self.closing = False
        self.preview_files, self.preview_position, self.photo = [], 0, None
        self.status_var, self.progress_var = tk.StringVar(value="Ready"), tk.DoubleVar(value=0)
        root.title(f"PDF Branding {__version__}")
        root.geometry("1140x850")
        root.minsize(900, 680)
        root.protocol("WM_DELETE_WINDOW", self.request_close)
        self._build()
        self.load_previous_results()
        root.after(80, self.poll)

    def settings(self) -> dict:
        return {name: value.get() for name, value in self.vars.items()}

    def _build(self):
        tk, ttk = self.tk, self.ttk
        frame = ttk.Frame(self.root, padding=12)
        frame.pack(fill="both", expand=True)
        folders = ttk.LabelFrame(frame, text="Library and branding profile", padding=8)
        folders.pack(fill="x")
        for row, (label, name) in enumerate((("Input folder", "input_root"), ("Output folder", "output_root"), ("JSON profile", "profile"))):
            ttk.Label(folders, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(folders, textvariable=self.vars[name]).grid(row=row, column=1, sticky="ew", padx=8)
            ttk.Button(folders, text="Browse", command=lambda n=name: self.browse(n)).grid(row=row, column=2)
        folders.columnconfigure(1, weight=1)
        filters = ttk.Frame(frame, padding=(0, 8))
        filters.pack(fill="x")
        for col, (label, name, values) in enumerate((("Class", "class", ["All", "6", "7", "8", "9", "10"]),
                                                    ("Subject", "subject", ["All", "Physics", "Mathematics", "Chemistry", "Biology"]))):
            ttk.Label(filters, text=label).grid(row=0, column=col * 2, padx=(0, 5))
            ttk.Combobox(filters, textvariable=self.vars[name], values=values, state="readonly", width=14).grid(row=0, column=col * 2 + 1, padx=(0, 10))
        ttk.Label(filters, text="Chapter").grid(row=0, column=4)
        ttk.Entry(filters, textvariable=self.vars["chapter"], width=25).grid(row=0, column=5, padx=5, sticky="ew")
        ttk.Label(filters, text="Material").grid(row=0, column=6)
        ttk.Entry(filters, textvariable=self.vars["material"], width=18).grid(row=0, column=7, padx=5)
        ttk.Label(filters, text="Workers").grid(row=1, column=0, pady=(8, 0))
        ttk.Spinbox(filters, from_=1, to=8, textvariable=self.vars["workers"], width=5).grid(row=1, column=1, sticky="w", pady=(8, 0))
        ttk.Label(filters, text="Retries").grid(row=1, column=2, pady=(8, 0))
        ttk.Spinbox(filters, from_=0, to=5, textvariable=self.vars["retries"], width=5).grid(row=1, column=3, sticky="w", pady=(8, 0))
        options = ttk.Frame(filters)
        options.grid(row=1, column=4, columnspan=4, sticky="w", pady=(8, 0))
        for label, name in (("Plan previews", "plan_previews"), ("Contact sheets", "contact_sheets"),
                            ("Overwrite", "overwrite"), ("Ignore cache", "no_cache")):
            ttk.Checkbutton(options, text=label, variable=self.vars[name]).pack(side="left", padx=6)
        filters.columnconfigure(5, weight=1)
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(0, 8))
        self.run_buttons = []
        for label, command in (("1. Classify", lambda: self.start("dry")),
                               ("2. Analyze + review", lambda: self.start("analyze")),
                               ("3. Process + QA", lambda: self.start("process")),
                               ("Resume", lambda: self.start("process", resume=True)),
                               ("Retry failures", lambda: self.start("process", retry_failed=True))):
            button = ttk.Button(actions, text=label, command=command)
            button.pack(side="left", padx=(0, 6))
            self.run_buttons.append(button)
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left")
        ttk.Button(actions, text="Open output folder", command=self.open_output).pack(side="right")
        self.progress = ttk.Progressbar(frame, variable=self.progress_var, maximum=1)
        self.progress.pack(fill="x")
        ttk.Label(frame, textvariable=self.status_var).pack(anchor="w", pady=(3, 6))
        paned = ttk.Panedwindow(frame, orient="vertical")
        paned.pack(fill="both", expand=True)
        results_frame = ttk.Frame(paned)
        columns = ("status", "class", "subject", "material", "attempts", "seconds")
        self.results = ttk.Treeview(results_frame, columns=columns, show="tree headings", height=9)
        self.results.heading("#0", text="Source PDF")
        self.results.column("#0", width=470, minwidth=200)
        for name, label, width in (("status", "Status", 95), ("class", "Class", 45), ("subject", "Subject", 90),
                                   ("material", "Family", 105), ("attempts", "Attempts", 65), ("seconds", "Seconds", 65)):
            self.results.heading(name, text=label)
            self.results.column(name, width=width, stretch=False)
        scroll = ttk.Scrollbar(results_frame, orient="vertical", command=self.results.yview)
        self.results.configure(yscrollcommand=scroll.set)
        self.results.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.results.tag_configure("failed", foreground="#aa1724")
        self.results.tag_configure("qa_failed", foreground="#aa1724")
        self.results.tag_configure("completed", foreground="#176a35")
        self.results.bind("<<TreeviewSelect>>", lambda _: self.show_selected_preview())
        paned.add(results_frame, weight=2)
        preview_frame = ttk.LabelFrame(paned, text="Source, plan and output review", padding=6)
        toolbar = ttk.Frame(preview_frame)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Previous", command=lambda: self.change_preview(-1)).pack(side="left")
        ttk.Button(toolbar, text="Next", command=lambda: self.change_preview(1)).pack(side="left", padx=5)
        self.preview_label = ttk.Label(toolbar, text="Select a result to inspect its review")
        self.preview_label.pack(side="left", padx=8)
        ttk.Button(toolbar, text="Open PDF", command=self.open_pdf).pack(side="right")
        ttk.Button(toolbar, text="Open review", command=self.open_review).pack(side="right", padx=5)
        self.canvas = tk.Canvas(preview_frame, background="#edf0f3", highlightthickness=0, height=285)
        self.canvas.pack(fill="both", expand=True, pady=(6, 0))
        self.canvas.bind("<Configure>", lambda _: self.render_preview())
        paned.add(preview_frame, weight=3)
        from tkinter.scrolledtext import ScrolledText
        self.log = ScrolledText(frame, height=6, wrap="word")
        self.log.pack(fill="x", pady=(8, 0))

    def browse(self, name):
        from tkinter import filedialog
        if name == "profile":
            chosen = filedialog.askopenfilename(filetypes=[("JSON profiles", "*.json")])
        else:
            chosen = filedialog.askdirectory()
        if chosen:
            self.vars[name].set(chosen)
            if name == "output_root":
                self.load_previous_results()

    def start(self, mode, *, resume=False, retry_failed=False):
        from tkinter import messagebox
        if self.state.active or (self.thread is not None and self.thread.is_alive()):
            return
        try:
            settings = self.settings()
            if not Path(settings["input_root"]).is_dir() or not Path(settings["profile"]).is_file():
                messagebox.showerror("Missing input", "Choose an existing input folder and JSON branding profile.")
                return
            argv = build_argv(settings, mode, resume=resume, retry_failed=retry_failed)
            batch.build_parser().parse_args(argv)
            write_json(self.settings_path, settings)
        except (ValueError, OSError, SystemExit, self.tk.TclError) as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return
        self.cancel_event.clear()
        self.generation += 1
        generation = self.generation
        self.state.begin()
        self.results.delete(*self.results.get_children())
        self.log.delete("1.0", "end")
        self.set_running(True)
        self.status_var.set("Starting")
        self.progress_var.set(0)

        def worker():
            def publish(event):
                # The runner still owns its lock until it returns. Its completion
                # notification cannot enable another GUI run prematurely.
                if event.get("type") == "finished":
                    return
                self.events.put({**event, "generation": generation})
            try:
                code = self.runner(argv, event_callback=publish, cancel_event=self.cancel_event)
                self.events.put({"type": "finished", "exit_code": code,
                                 "generation": generation,
                                 "status": "cancelled" if code == 130 else "failed" if code else "completed",
                                 "message": f"Batch finished with exit code {code}"})
            except BaseException as exc:
                self.events.put({"type": "error", "exit_code": 2, "message": str(exc), "generation": generation})
        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()

    def set_running(self, running):
        for button in self.run_buttons:
            button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def cancel(self):
        self.cancel_event.set()
        self.status_var.set("Cancellation requested. Active documents finish safely; remaining jobs can resume.")

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event.get("generation", self.generation) != self.generation:
                    continue
                self.state.accept(event)
                if event.get("type") == "job":
                    self.upsert_result(event)
                    if event.get("status") in {"running", *TERMINAL}:
                        line = f"{event.get('status')}: {Path(event['source']).name}"
                        if event.get("message"):
                            line += " - " + event["message"]
                        self.log.insert("end", line + "\n")
                        for issue in event.get("qa_issues") or []:
                            self.log.insert("end", f"  Page {issue.get('page_number')}: {issue['code']} - {issue['message']}\n")
                        self.log.see("end")
                if event.get("type") in {"error", "finished"}:
                    self.set_running(False)
                    self.log.insert("end", event.get("message", "") + "\n")
                self.progress.configure(maximum=max(self.state.total, 1))
                self.progress_var.set(min(self.state.completed, self.state.total))
                self.status_var.set(f"{self.state.completed}/{self.state.total} - {self.state.status}" if self.state.total else self.state.status)
        except queue.Empty:
            pass
        if self.closing and not self.state.active:
            self.root.destroy()
            return
        self.root.after(80, self.poll)

    def upsert_result(self, record):
        self.state.records[record["source"]] = record
        key = hashlib.sha1(record["source"].encode()).hexdigest()
        values = (record["status"], record.get("class_name", ""), record.get("subject", ""),
                  record.get("material_type", ""), record.get("attempts", 0), record.get("elapsed_seconds", ""))
        if self.results.exists(key):
            self.results.item(key, text=Path(record["source"]).name, values=values, tags=(record["status"],))
        else:
            self.results.insert("", "end", iid=key, text=Path(record["source"]).name, values=values, tags=(record["status"],))

    def selected_record(self):
        selection = self.results.selection()
        if not selection:
            return None
        key = selection[0]
        return next((r for source, r in self.state.records.items()
                     if hashlib.sha1(source.encode()).hexdigest() == key), None)

    def load_previous_results(self):
        if self.state.active:
            return
        path = Path(self.vars["output_root"].get()) / "batch_manifest.json"
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(manifest, dict) or not isinstance(manifest.get("jobs"), list):
            return
        self.state.records.clear()
        self.results.delete(*self.results.get_children())
        for record in manifest.get("jobs", []):
            if isinstance(record, dict) and isinstance(record.get("source"), str) and isinstance(record.get("status"), str):
                self.upsert_result(record)
        self.status_var.set(f"Loaded {len(self.state.records)} saved job records")

    def show_selected_preview(self):
        record = self.selected_record()
        self.preview_files, self.preview_position = [], 0
        if record and record.get("review_index"):
            directory = Path(record["review_index"]).parent
            self.preview_files = sorted(directory.glob("contact_*.png"))
        if not self.preview_files:
            self.canvas.delete("all")
            self.preview_label.configure(text="No review yet; enable Contact sheets and analyze or process")
            return
        self.render_preview()

    def change_preview(self, amount):
        if self.preview_files:
            self.preview_position = (self.preview_position + amount) % len(self.preview_files)
            self.render_preview()

    def render_preview(self):
        if not self.preview_files:
            return
        from PIL import Image, ImageTk
        try:
            with Image.open(self.preview_files[self.preview_position]) as image:
                image = image.convert("RGB")
                image.thumbnail((max(200, self.canvas.winfo_width() - 12), max(160, self.canvas.winfo_height() - 12)))
                self.photo = ImageTk.PhotoImage(image)
            self.canvas.delete("all")
            self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                     image=self.photo, anchor="center")
            self.preview_label.configure(text=f"Contact sheet {self.preview_position + 1}/{len(self.preview_files)}")
        except (OSError, self.tk.TclError) as exc:
            self.preview_label.configure(text=str(exc))

    def open_review(self):
        record = self.selected_record()
        if record and record.get("review_index") and Path(record["review_index"]).is_file():
            webbrowser.open(Path(record["review_index"]).resolve().as_uri())

    def open_pdf(self):
        record = self.selected_record()
        if record:
            path = Path(record.get("output") or record["source"])
            if path.is_file():
                if os.name == "nt":
                    os.startfile(path)
                else:
                    webbrowser.open(path.resolve().as_uri())

    def open_output(self):
        path = Path(self.vars["output_root"].get()).resolve()
        if path.is_dir():
            if os.name == "nt":
                os.startfile(path)
            else:
                webbrowser.open(path.as_uri())

    def request_close(self):
        if self.state.active:
            self.closing = True
            self.cancel()
        else:
            self.root.destroy()


def main() -> int:
    try:
        import tkinter as tk
        root = tk.Tk()
    except Exception as exc:
        print(f"Tkinter GUI unavailable: {exc}")
        return 1
    BrandingApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
