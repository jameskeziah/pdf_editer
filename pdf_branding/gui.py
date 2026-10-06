from __future__ import annotations

import queue
import threading
from pathlib import Path

from . import batch


def main() -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        print(f"Tkinter GUI is unavailable: {exc}")
        return 1

    root = tk.Tk()
    root.title("PDF Branding V3.2")
    root.geometry("940x690")

    input_var = tk.StringVar()
    output_var = tk.StringVar()
    profile_var = tk.StringVar(value=str(Path(__file__).resolve().parent.parent / "profiles" / "sskem.json"))
    class_var = tk.StringVar(value="All")
    subject_var = tk.StringVar(value="All")
    chapter_var = tk.StringVar()
    material_var = tk.StringVar()
    workers_var = tk.IntVar(value=1)
    preview_var = tk.BooleanVar(value=True)
    q: queue.Queue[str] = queue.Queue()

    def choose_dir(var):
        value = filedialog.askdirectory()
        if value:
            var.set(value)

    def choose_profile():
        value = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if value:
            profile_var.set(value)

    def append_log():
        try:
            while True:
                line = q.get_nowait()
                if line:
                    log.insert("end", line + "\n")
                    log.see("end")
        except queue.Empty:
            pass
        root.after(150, append_log)

    def build_argv(mode: str) -> list[str]:
        argv = [input_var.get(), output_var.get(), "--profile", profile_var.get(), "--workers", str(workers_var.get())]
        if class_var.get() != "All":
            argv += ["--class-filter", class_var.get()]
        if subject_var.get() != "All":
            argv += ["--subject-filter", subject_var.get().lower()]
        if chapter_var.get().strip():
            argv += ["--chapter-filter", chapter_var.get().strip()]
        if material_var.get().strip():
            argv += ["--material-filter", material_var.get().strip()]
        if mode == "dry":
            argv.append("--dry-run")
        elif mode == "analyze":
            argv.append("--analyze-only")
            if preview_var.get():
                argv.append("--preview-plans")
        return argv

    def run(mode: str):
        if not input_var.get() or not output_var.get():
            messagebox.showerror("Missing folder", "Choose input and output folders first.")
            return
        argv = build_argv(mode)
        for b in buttons.winfo_children():
            b.configure(state="disabled")

        def worker():
            import contextlib, io
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                    code = batch.main(argv)
                q.put(buf.getvalue().rstrip())
                q.put(f"Finished with exit code {code}")
            except Exception as exc:
                q.put(f"ERROR: {exc}")
            finally:
                root.after(0, lambda: [b.configure(state="normal") for b in buttons.winfo_children()])

        threading.Thread(target=worker, daemon=True).start()

    frame = ttk.Frame(root, padding=14)
    frame.pack(fill="both", expand=True)
    rows = [
        ("Input folder", input_var, lambda: choose_dir(input_var)),
        ("Output folder", output_var, lambda: choose_dir(output_var)),
        ("Branding profile", profile_var, choose_profile),
    ]
    for row, (label, var, cmd) in enumerate(rows):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(frame, textvariable=var, width=80).grid(row=row, column=1, columnspan=4, sticky="ew", padx=8)
        ttk.Button(frame, text="Browse", command=cmd).grid(row=row, column=5)

    ttk.Label(frame, text="Class").grid(row=3, column=0, sticky="w", pady=6)
    ttk.Combobox(frame, textvariable=class_var, values=["All", "6", "7", "8", "9", "10"], state="readonly", width=12).grid(row=3, column=1, sticky="w")
    ttk.Label(frame, text="Subject").grid(row=3, column=2, sticky="e", padx=(14, 4))
    ttk.Combobox(frame, textvariable=subject_var, values=["All", "Physics", "Mathematics", "Chemistry", "Biology"], state="readonly", width=16).grid(row=3, column=3, sticky="w")
    ttk.Label(frame, text="Workers").grid(row=3, column=4, sticky="e", padx=(14, 4))
    ttk.Spinbox(frame, from_=1, to=8, textvariable=workers_var, width=5).grid(row=3, column=5, sticky="w")

    ttk.Label(frame, text="Chapter contains").grid(row=4, column=0, sticky="w", pady=6)
    ttk.Entry(frame, textvariable=chapter_var, width=30).grid(row=4, column=1, columnspan=2, sticky="ew", padx=(0, 8))
    ttk.Label(frame, text="Material contains").grid(row=4, column=3, sticky="e", padx=(14, 4))
    ttk.Entry(frame, textvariable=material_var, width=24).grid(row=4, column=4, sticky="ew")
    ttk.Checkbutton(frame, text="Plan previews", variable=preview_var).grid(row=4, column=5, sticky="w", padx=8)

    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=6, sticky="w", pady=12)
    ttk.Button(buttons, text="1. Dry Run", command=lambda: run("dry")).pack(side="left", padx=4)
    ttk.Button(buttons, text="2. Analyze Only", command=lambda: run("analyze")).pack(side="left", padx=4)
    ttk.Button(buttons, text="3. Process + QA", command=lambda: run("process")).pack(side="left", padx=4)

    ttk.Label(frame, text="Recommended workflow: Dry Run → Analyze Only + plan previews → Process + QA").grid(row=6, column=0, columnspan=6, sticky="w", pady=(0, 6))
    log = tk.Text(frame, wrap="none", height=28)
    log.grid(row=7, column=0, columnspan=6, sticky="nsew")
    frame.columnconfigure(1, weight=1)
    frame.columnconfigure(4, weight=1)
    frame.rowconfigure(7, weight=1)
    append_log()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
