from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .converter import PdfToWordConverter


class ConverterWindow(tk.Tk):
    """Small native Windows UI with no third-party UI runtime."""

    def __init__(self) -> None:
        super().__init__()
        self.title("PDF 转可编辑 Word")
        self.minsize(640, 360)
        self.resizable(False, False)

        self.source_path: Path | None = None
        self.output_path = Path.home() / "Documents"
        self.latest_docx: Path | None = None
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
        self.open_button = ttk.Button(actions, text="打开 Word 文件", command=self._open_result, state="disabled")
        self.open_button.pack(side="left", padx=(10, 0))

    def _choose_pdf(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 PDF", filetypes=[("PDF 文件", "*.pdf")], initialdir=str(Path.home())
        )
        if not selected:
            return
        self.source_path = Path(selected)
        self.source_label.set(self.source_path.name)
        self.status_label.set("已准备好在本机转换。")
        self.convert_button.configure(state="normal")
        self.open_button.configure(state="disabled")

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择输出文件夹", initialdir=str(self.output_path))
        if selected:
            self.output_path = Path(selected)
            self.output_label.set(str(self.output_path))

    def _start_conversion(self) -> None:
        if self.source_path is None or self.is_converting:
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
            converter = PdfToWordConverter(lambda percent, message: self.events.put(("progress", (percent, message))))
            docx, qa, report = converter.convert(self.source_path, self.output_path)  # type: ignore[arg-type]
            self.events.put(("completed", (docx, qa.status, report)))
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
                    docx, _qa_status, report = payload  # type: ignore[misc]
                    self.latest_docx = docx
                    self.progress_value.set(100)
                    self.status_label.set(f"转换完成，请在 Word 中检查版式。质检报告：{report.name}")
                    self.open_button.configure(state="normal")
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
        self.convert_button.configure(state="normal" if self.source_path else "disabled")
        self.choose_pdf_button.configure(state="normal")
        self.choose_output_button.configure(state="normal")

    def _open_result(self) -> None:
        if self.latest_docx is None:
            return
        if hasattr(os, "startfile"):
            os.startfile(self.latest_docx)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("文件已生成", str(self.latest_docx))


def main() -> int:
    window = ConverterWindow()
    window.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
