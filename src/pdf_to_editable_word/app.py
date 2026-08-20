from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .converter import PdfToWordConverter


def unique_output_stem(source: Path, output_dir: Path, reserved_stems: set[str]) -> str:
    """Return a Word filename stem that cannot overwrite this batch or prior output."""

    stem = source.stem
    candidate = stem
    sequence = 2
    while candidate.casefold() in reserved_stems or (output_dir / f"{candidate}.docx").exists():
        candidate = f"{stem} ({sequence})"
        sequence += 1
    reserved_stems.add(candidate.casefold())
    return candidate


class ConverterWindow(tk.Tk):
    """Small native Windows UI with no third-party UI runtime."""

    def __init__(self) -> None:
        super().__init__()
        self.title("PDF 转可编辑 Word")
        self.minsize(640, 360)
        self.resizable(False, False)

        self.source_paths: list[Path] = []
        self.output_path = Path.home() / "Documents"
        self.latest_docxs: list[Path] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.is_converting = False

        self.source_label = tk.StringVar(value="尚未选择 PDF")
        self.output_label = tk.StringVar(value=str(self.output_path))
        self.status_label = tk.StringVar(value="选择 PDF 后开始转换。")
        self.progress_value = tk.IntVar(value=0)
        self._build()
        self.after(100, self._handle_events)

    def _build(self) -> None:
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Body.TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        root = ttk.Frame(self, padding=28)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)

        ttk.Label(root, text="PDF 转可编辑 Word", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            root,
            text="本地转换，文字可编辑，检测到的印章保留为透明图片。",
            style="Body.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 24))

        ttk.Label(root, text="PDF 文件", style="Body.TLabel").grid(row=2, column=0, sticky="w")
        ttk.Label(root, textvariable=self.source_label, style="Body.TLabel").grid(
            row=2, column=1, sticky="w", padx=12
        )
        self.choose_pdf_button = ttk.Button(root, text="选择 PDF", command=self._choose_pdf)
        self.choose_pdf_button.grid(row=2, column=2, sticky="e")

        ttk.Label(root, text="输出文件夹", style="Body.TLabel").grid(row=3, column=0, sticky="w", pady=(18, 0))
        ttk.Label(root, textvariable=self.output_label, style="Body.TLabel").grid(
            row=3, column=1, sticky="w", padx=12, pady=(18, 0)
        )
        self.choose_output_button = ttk.Button(root, text="更改", command=self._choose_output)
        self.choose_output_button.grid(row=3, column=2, sticky="e", pady=(18, 0))

        ttk.Separator(root).grid(row=4, column=0, columnspan=3, sticky="ew", pady=26)
        self.progress = ttk.Progressbar(root, maximum=100, variable=self.progress_value)
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew")
        ttk.Label(root, textvariable=self.status_label, style="Body.TLabel").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 22)
        )

        actions = ttk.Frame(root)
        actions.grid(row=7, column=0, columnspan=3, sticky="w")
        self.convert_button = ttk.Button(
            actions, text="转换", command=self._start_conversion, style="Primary.TButton", state="disabled"
        )
        self.convert_button.pack(side="left")
        self.open_button = ttk.Button(actions, text="打开输出文件夹", command=self._open_result, state="disabled")
        self.open_button.pack(side="left", padx=(10, 0))

    def _choose_pdf(self) -> None:
        selected = filedialog.askopenfilenames(
            title="选择一个或多个 PDF", filetypes=[("PDF 文件", "*.pdf")], initialdir=str(Path.home())
        )
        if not selected:
            return
        self.source_paths = [Path(path) for path in selected]
        if len(self.source_paths) == 1:
            self.source_label.set(self.source_paths[0].name)
        else:
            self.source_label.set(f"已选择 {len(self.source_paths)} 个 PDF")
        self.status_label.set("已准备好在本机逐个转换。")
        self.convert_button.configure(state="normal")
        self.open_button.configure(state="disabled")
        self.latest_docxs = []

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择输出文件夹", initialdir=str(self.output_path))
        if selected:
            self.output_path = Path(selected)
            self.output_label.set(str(self.output_path))

    def _start_conversion(self) -> None:
        if not self.source_paths or self.is_converting:
            return
        self.is_converting = True
        self.progress_value.set(0)
        self.status_label.set("正在准备转换...")
        self.convert_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.choose_pdf_button.configure(state="disabled")
        self.choose_output_button.configure(state="disabled")
        threading.Thread(target=self._convert_worker, daemon=True).start()

    def _convert_worker(self) -> None:
        try:
            total = len(self.source_paths)
            converter = PdfToWordConverter()
            outputs: list[Path] = []
            failures: list[tuple[str, str]] = []
            validation_failures: list[tuple[str, Path]] = []
            reserved_stems: set[str] = set()
            for index, source_path in enumerate(self.source_paths):
                source_name = source_path.name

                def on_progress(
                    percent: int, message: str, current: int = index, current_name: str = source_name
                ) -> None:
                    overall_percent = int(((current + percent / 100) / total) * 100)
                    self.events.put(
                        ("progress", (overall_percent, f"正在转换 {current + 1}/{total}：{current_name} - {message}"))
                    )

                converter.progress = on_progress
                output_stem = unique_output_stem(source_path, self.output_path, reserved_stems)
                try:
                    docx, qa, report = converter.convert(
                        source_path, self.output_path, output_stem=output_stem
                    )
                    outputs.append(docx)
                    if qa.status == "FAIL":
                        validation_failures.append((source_path.name, report))
                except Exception as error:
                    failures.append((source_path.name, str(error)))

            if outputs:
                self.events.put(("completed", (outputs, failures, validation_failures)))
            else:
                details = "\n".join(f"{name}: {reason}" for name, reason in failures)
                self.events.put(("failed", details or "没有可转换的 PDF 文件。"))
        except Exception as error:
            self.events.put(("failed", str(error)))

    def _handle_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    percent, message = payload  # type: ignore[misc]
                    self.progress_value.set(percent)
                    self.status_label.set(message)
                elif event == "completed":
                    outputs, failures, validation_failures = payload  # type: ignore[misc]
                    self.latest_docxs = outputs
                    self.progress_value.set(100)
                    result = f"已完成 {len(outputs)} 个文件，请在 Word 中检查版式。"
                    if failures:
                        result += f" {len(failures)} 个文件转换失败。"
                    if validation_failures:
                        result += f" {len(validation_failures)} 个文件未通过可编辑文本自检。"
                    self.status_label.set(result)
                    self.open_button.configure(state="normal")
                    if failures:
                        failed_names = "\n".join(name for name, _reason in failures)
                        messagebox.showwarning("部分文件转换失败", f"以下文件未转换成功：\n{failed_names}")
                    if validation_failures:
                        failed_names = "\n".join(name for name, _report in validation_failures)
                        messagebox.showwarning(
                            "可编辑文本自检未通过",
                            f"以下 Word 已生成，但部分文字未通过自检；请查看同名 .conversion.json 报告：\n{failed_names}",
                        )
                    self._finish_conversion()
                elif event == "failed":
                    self.status_label.set("转换失败。")
                    messagebox.showerror("转换失败", str(payload))
                    self._finish_conversion()
        except queue.Empty:
            pass
        self.after(100, self._handle_events)

    def _finish_conversion(self) -> None:
        self.is_converting = False
        self.convert_button.configure(state="normal" if self.source_paths else "disabled")
        self.choose_pdf_button.configure(state="normal")
        self.choose_output_button.configure(state="normal")

    def _open_result(self) -> None:
        if not self.latest_docxs:
            return
        if hasattr(os, "startfile"):
            os.startfile(self.output_path)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("文件已生成", str(self.output_path))


def main() -> int:
    window = ConverterWindow()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
