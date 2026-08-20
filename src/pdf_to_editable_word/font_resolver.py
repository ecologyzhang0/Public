from __future__ import annotations

import re


class FontResolver:
    """Conservative PDF-font to Windows-font mapping with stable metric fallbacks."""

    _MAPPINGS = (
        (("microsoftyahei", "yahei"), "Microsoft YaHei"),
        (("simhei",), "SimHei"),
        (("kaiti", "kai"), "KaiTi"),
        (("fangsong",), "FangSong"),
        (("simsun", "stsong", "song", "cjk"), "SimSun"),
        (("calibri",), "Calibri"),
        (("cambria",), "Cambria"),
        (("helvetica", "arial", "univers", "sans"), "Arial"),
        (("times", "georgia", "serif"), "Times New Roman"),
        (("courier", "mono", "consolas"), "Courier New"),
    )

    def resolve(self, source_name: str) -> str:
        cleaned = re.sub(r"^[A-Za-z]{6}\+", "", source_name).strip()
        normalized = cleaned.casefold().replace("-", "").replace("_", "").replace(" ", "")
        for aliases, fallback in self._MAPPINGS:
            if any(alias.replace("-", "").replace("_", "") in normalized for alias in aliases):
                return fallback
        # Prefer a non-subset name from the PDF when Windows may already have it.
        return cleaned or "Arial"
