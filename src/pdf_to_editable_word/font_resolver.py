from __future__ import annotations


class FontResolver:
    """Conservative PDF-font to Windows-font mapping with stable metric fallbacks."""

    _MAPPINGS = (
        (("helvetica", "arial", "univers", "sans"), "Arial"),
        (("times", "georgia", "serif"), "Times New Roman"),
        (("courier", "mono", "consolas"), "Courier New"),
        (("simsun", "song", "simhei", "kai", "fangsong", "cjk"), "SimSun"),
    )

    def resolve(self, source_name: str) -> str:
        normalized = source_name.casefold().replace("-", "").replace("_", "")
        for aliases, fallback in self._MAPPINGS:
            if any(alias.replace("-", "").replace("_", "") in normalized for alias in aliases):
                return fallback
        return "Arial"
