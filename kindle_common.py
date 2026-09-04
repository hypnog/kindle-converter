"""kindle_common.py — utilidades compartidas por kindle_analyze / kindle_extract / kindle_build.

Modelo de datos:
  Line  = dict(text, x0, x1, top, bottom, size, bold, page)
  Block = dict(kind, text, level, page, top, bottom, size, bold, lines)
    kind ∈ {"heading","para","note","image","empty"}
"""
from __future__ import annotations

import re
import statistics
import subprocess
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------- patrones

RE_PAGE_NUM = re.compile(
    r"""^\s*(?:
        [\-–—•·|]?\s*\d{1,4}\s*[\-–—•·|]?            |   # 12 / - 12 - / | 12
        (?:p[aá]g(?:ina)?\.?|page|pg\.?|p\.)\s*\d{1,4}(?:\s*(?:de|of|/)\s*\d{1,4})?  |
        \d{1,4}\s*(?:de|of|/)\s*\d{1,4}               |   # 12 de 300
        [ivxlcdm]{1,7}                                    # romanos
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
RE_HYPHEN_END = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]-$")
RE_SENT_END = re.compile(r"[.!?…:;»”\"')\]]\s*$")
RE_LIST_START = re.compile(r"^\s*(?:[-–—•·▪◦*]|\d{1,3}[.)]|[a-zA-Z][.)])\s+")
RE_ONLY_PUNCT = re.compile(r"^[\W_]+$")

BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demibold", "extrabold", "-bd", ",b", "negrita")
ITALIC_HINTS = ("italic", "oblique", "-it", ",i")


def is_bold(fontname: str | None) -> bool:
    f = (fontname or "").lower()
    return any(h in f for h in BOLD_HINTS)


def is_italic(fontname: str | None) -> bool:
    f = (fontname or "").lower()
    return any(h in f for h in ITALIC_HINTS)


def norm_text(s: str) -> str:
    """Normaliza para comparar encabezados/pies repetidos: minúsculas, dígitos→#, espacios colapsados."""
    s = s.lower().strip()
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"\s+", " ", s)
    return s


def round_size(sz: float) -> float:
    return round(sz * 2) / 2  # a 0.5 pt


# ---------------------------------------------------------------- líneas

def words_to_lines(words: list[dict], page_no: int, y_tol: float = 2.5) -> list[dict]:
    """Agrupa palabras (pdfplumber extract_words con extra_attrs size/fontname) en líneas."""
    if not words:
        return []
    words = sorted(words, key=lambda w: (round(w["top"]), w["x0"]))
    lines: list[dict] = []
    cur: list[dict] = [words[0]]
    for w in words[1:]:
        if abs(w["top"] - cur[-1]["top"]) <= y_tol:
            cur.append(w)
        else:
            lines.append(_finish_line(cur, page_no))
            cur = [w]
    lines.append(_finish_line(cur, page_no))
    return lines


def _finish_line(ws: list[dict], page_no: int) -> dict:
    ws = sorted(ws, key=lambda w: w["x0"])
    # separar palabras; si el hueco es grande (columnas) igual concatenamos con espacio
    text = " ".join(w["text"] for w in ws)
    sizes = [w.get("size", 0) for w in ws if w.get("size")]
    fonts = [w.get("fontname", "") for w in ws]
    size = statistics.median(sizes) if sizes else 0.0
    nbold = sum(1 for f in fonts if is_bold(f))
    return dict(
        text=text,
        x0=min(w["x0"] for w in ws),
        x1=max(w["x1"] for w in ws),
        top=min(w["top"] for w in ws),
        bottom=max(w["bottom"] for w in ws),
        size=size,
        bold=nbold >= max(1, len(fonts) * 0.6),
        italic=sum(1 for f in fonts if is_italic(f)) >= max(1, len(fonts) * 0.6),
        page=page_no,
        nwords=len(ws),
    )


# ---------------------------------------------------------------- detección global

def body_size(lines: list[dict]) -> float:
    """Tamaño de cuerpo = moda ponderada por caracteres."""
    c: Counter = Counter()
    for ln in lines:
        if ln["size"]:
            c[round_size(ln["size"])] += len(ln["text"])
    return c.most_common(1)[0][0] if c else 10.0


def detect_repeated_edges(pages_lines: list[list[dict]], page_heights: list[float],
                          edge_frac: float = 0.10, min_ratio: float = 0.25) -> dict:
    """Encabezados/pies: textos normalizados que aparecen en la franja superior/inferior
    en ≥ min_ratio de las páginas. Devuelve {"header": set, "footer": set, "stats": {...}}."""
    n = len(pages_lines)
    top_c: Counter = Counter()
    bot_c: Counter = Counter()
    for lines, h in zip(pages_lines, page_heights):
        seen_t, seen_b = set(), set()
        for ln in lines:
            key = norm_text(ln["text"])
            if not key or len(key) > 120:
                continue
            if ln["bottom"] <= h * edge_frac:
                seen_t.add(key)
            elif ln["top"] >= h * (1 - edge_frac):
                seen_b.add(key)
        top_c.update(seen_t)
        bot_c.update(seen_b)
    thr = max(2, int(n * min_ratio))
    header = {k for k, v in top_c.items() if v >= thr}
    footer = {k for k, v in bot_c.items() if v >= thr}
    return {"header": header, "footer": footer,
            "stats": {"top": top_c.most_common(8), "bottom": bot_c.most_common(8), "threshold": thr}}


def is_page_number(text: str) -> bool:
    return bool(RE_PAGE_NUM.match(text.strip())) and len(text.strip()) <= 20


def pdf_outline(pdf_path: str | Path) -> list[dict]:
    """Marcadores (bookmarks) del PDF: [{title, page(1-based), level}]."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    out: list[dict] = []
    try:
        r = PdfReader(str(pdf_path))
        pages_idx = {p.indirect_reference.idnum: i for i, p in enumerate(r.pages) if p.indirect_reference}

        def walk(items, level):
            for it in items:
                if isinstance(it, list):
                    walk(it, level + 1)
                    continue
                try:
                    pg = r.get_destination_page_number(it)
                except Exception:
                    pg = None
                if pg is None:
                    continue
                out.append({"title": str(it.title).strip(), "page": pg + 1, "level": level})
        walk(r.outline, 1)
    except Exception:
        return []
    return out


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def which(binary: str) -> bool:
    from shutil import which as _w
    return _w(binary) is not None
