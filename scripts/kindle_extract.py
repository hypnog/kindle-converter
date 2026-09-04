#!/usr/bin/env python3
"""kindle_extract.py — PDF → Markdown estructurado + imágenes (insumo de kindle_build.py).

Uso:
    python3 kindle_extract.py libro.pdf --out ./work [--config libro.analysis.json]
        [--title T] [--author A] [--lang es] [--skip-pages 1,2,300-310]
        [--heading-sizes 18,14,12] [--no-outline] [--ocr] [--ocr-lang spa+eng]
        [--two-columns] [--render-pages 5,17]   # rasterizar páginas enteras como figura (gráficos vectoriales)

Salida en --out:
    book.md            Markdown con front-matter YAML (title/author/lang) — REVISAR antes de compilar
    images/            figuras recortadas (PNG) referenciadas desde book.md
    extract_report.json  qué se eliminó, títulos detectados, páginas OCR, avisos

Pipeline por página:
    palabras (pdfplumber, con size/fontname) → líneas → filtro encabezado/pie/nº página
    → clasificación (título por tamaño/negrita/marcador PDF, nota al pie por tamaño chico al pie, párrafo)
    → bloques por proximidad vertical → intercalado de imágenes por posición
Post-proceso global: unión de párrafos cortados entre páginas, des-guionado, notas al final de cada capítulo.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).parent))
from kindle_common import (RE_HYPHEN_END, RE_LIST_START, RE_ONLY_PUNCT, RE_SENT_END,  # noqa: E402
                           body_size, detect_repeated_edges, is_page_number, norm_text,
                           pdf_outline, round_size, words_to_lines, which)


# ---------------------------------------------------------------- helpers

def parse_pages(spec: str | None) -> set[int]:
    s: set[int] = set()
    if not spec:
        return s
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            s.update(range(int(a), int(b) + 1))
        else:
            s.add(int(part))
    return s


def ocr_page(pdf_path: str, page_no: int, lang: str, dpi: int = 300) -> list[dict]:
    """Devuelve 'words' compatibles (text,x0,x1,top,bottom,size,fontname) vía tesseract."""
    import pypdfium2 as pdfium
    import pytesseract
    pdf = pdfium.PdfDocument(pdf_path)
    page = pdf[page_no - 1]
    scale = dpi / 72
    img = page.render(scale=scale).to_pil()
    data = pytesseract.image_to_data(img, lang=lang, output_type=pytesseract.Output.DICT)
    words = []
    for i, txt in enumerate(data["text"]):
        txt = txt.strip()
        if not txt or int(data["conf"][i]) < 30:
            continue
        x, y, w, h = (data[k][i] / scale for k in ("left", "top", "width", "height"))
        words.append(dict(text=txt, x0=x, x1=x + w, top=y, bottom=y + h, size=round(h * 0.75, 1), fontname="OCR"))
    return words


def render_page_pil(pdf_path: str, page_no: int, dpi: int = 200):
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(pdf_path)
    return pdf[page_no - 1].render(scale=dpi / 72).to_pil(), dpi / 72


def fuzzy_match(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_text(a), norm_text(b)).ratio()


# ---------------------------------------------------------------- núcleo

class Extractor:
    def __init__(self, pdf_path: str, cfg: dict, out_dir: Path):
        self.pdf_path = pdf_path
        self.cfg = cfg
        self.out = out_dir
        self.img_dir = out_dir / "images"
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self.report: dict = {"removed": defaultdict(list), "removed_counts": {}, "headings": [], "ocr_pages": [],
                             "images": [], "warnings": [], "merged_paragraphs": 0, "dehyphenated": 0}
        self.outline = pdf_outline(pdf_path) if cfg.get("use_outline", True) else []
        self.outline_by_page: dict[int, list[dict]] = defaultdict(list)
        for o in self.outline:
            self.outline_by_page[o["page"]].append(o)
        self.max_outline_level = max([o["level"] for o in self.outline], default=0)

    # --- clasificación de líneas
    def heading_level(self, ln: dict, body: float) -> int:
        """0 = no es título. Marcador PDF > tamaño de fuente > negrita corta."""
        text = ln["text"].strip()
        if not text or RE_ONLY_PUNCT.match(text) or ln["nwords"] > 18:
            return 0
        for o in self.outline_by_page.get(ln["page"], []):
            if fuzzy_match(text, o["title"]) >= 0.72 or norm_text(o["title"]).startswith(norm_text(text)[:25]):
                return min(o["level"], 3)
        sizes = self.cfg.get("heading_sizes") or []
        s = round_size(ln["size"])
        for lvl, hs in enumerate(sizes, start=1):
            if abs(s - hs) <= 0.5 and s > body:
                # si hay outline, los tamaños quedan por debajo de sus niveles
                return min(lvl + (self.max_outline_level if self.outline else 0), 4)
        if (self.cfg.get("bold_body_as_heading") and ln["bold"] and ln["nwords"] <= 12
                and not RE_SENT_END.search(text) and not RE_LIST_START.match(text) and s >= body - 0.5):
            return min(len(sizes) + 1 + (self.max_outline_level if self.outline else 0), 4)
        return 0

    def is_running(self, ln: dict, h: float, hdr: set, ftr: set) -> str | None:
        ef = self.cfg.get("edge_frac", 0.10)
        key = norm_text(ln["text"])
        at_top, at_bottom = ln["bottom"] <= h * ef, ln["top"] >= h * (1 - ef)
        if at_top and key in hdr:
            return "header"
        if at_bottom and key in ftr:
            return "footer"
        if (at_top or at_bottom) and self.cfg.get("strip_page_numbers", True) and is_page_number(ln["text"]):
            return "page_number"
        # números sueltos en cualquier margen (folios laterales)
        if is_page_number(ln["text"]) and ln["nwords"] == 1 and (at_top or at_bottom):
            return "page_number"
        return None

    # --- bloques
    def lines_to_blocks(self, lines: list[dict], body: float, h: float) -> list[dict]:
        blocks: list[dict] = []
        fn_max = self.cfg.get("footnote_size_max", body * 0.85)
        # interlineado típico y borde derecho del bloque de texto en esta página
        gaps = sorted(b["top"] - a["bottom"] for a, b in zip(lines, lines[1:])
                      if 0 <= b["top"] - a["bottom"] <= body * 2 and abs(a["size"] - b["size"]) < 1)
        normal_gap = gaps[len(gaps) // 2] if gaps else body * 0.4
        para_gap = normal_gap * 1.6 + 1.0
        x1s = sorted(ln["x1"] for ln in lines if ln["nwords"] >= 6)
        right_edge = x1s[int(len(x1s) * 0.9)] if x1s else None
        for ln in lines:
            lvl = self.heading_level(ln, body)
            is_note = (ln["size"] and ln["size"] <= fn_max and ln["top"] >= h * 0.70 and lvl == 0)
            kind = "heading" if lvl else ("note" if is_note else "para")
            prev = blocks[-1] if blocks else None
            gap = (ln["top"] - prev["bottom"]) if prev else 99
            same_kind = prev is not None and prev["kind"] == kind and (kind != "heading" or prev["level"] == lvl)
            ragged = (prev is not None and right_edge is not None and kind == "para"
                      and prev["x1"] < right_edge - body * 2.5 and RE_SENT_END.search(prev["text"])
                      and ln["text"][:1].isupper())
            indented = (prev is not None and kind == "para" and ln["x0"] - prev["last_x0"] > body * 0.8
                        and RE_SENT_END.search(prev["text"]))
            starts_new_para = RE_LIST_START.match(ln["text"]) is not None or ragged or indented
            if same_kind and gap <= para_gap and not starts_new_para and abs(ln["size"] - prev["size"]) <= 1.0:
                prev["text"] = join_lines(prev["text"], ln["text"], self.cfg.get("dehyphenate", True), self.report)
                prev["bottom"] = ln["bottom"]
                prev["x1"], prev["last_x0"] = ln["x1"], ln["x0"]
                prev["lines"] += 1
            else:
                blocks.append(dict(kind=kind, level=lvl, text=ln["text"], page=ln["page"], top=ln["top"],
                                   bottom=ln["bottom"], size=ln["size"], bold=ln["bold"], italic=ln["italic"],
                                   x0=ln["x0"], x1=ln["x1"], last_x0=ln["x0"], lines=1))
        return blocks

    # --- imágenes
    def page_images(self, p, page_no: int, pw: float, ph: float) -> list[dict]:
        min_px = self.cfg.get("image_min_px", 60)
        imgs = []
        cands = [im for im in p.images if im.get("width", 0) >= min_px * 0.5 and im.get("height", 0) >= min_px * 0.5]
        # descartar fondos a página completa salvo que se pida
        cands = [im for im in cands if not (im["width"] > pw * 0.9 and im["height"] > ph * 0.9)
                 or self.cfg.get("keep_full_page_images")]
        if not cands:
            return imgs
        pil, scale = render_page_pil(self.pdf_path, page_no, dpi=self.cfg.get("image_dpi", 200))
        for k, im in enumerate(cands, 1):
            x0, top, x1, bottom = im["x0"], im["top"], im["x1"], im["bottom"]
            box = (max(0, int(x0 * scale)), max(0, int(top * scale)),
                   min(pil.width, int(x1 * scale)), min(pil.height, int(bottom * scale)))
            if box[2] - box[0] < min_px or box[3] - box[1] < min_px:
                continue
            crop = pil.crop(box)
            if _is_blank(crop):
                continue
            name = f"p{page_no:04d}_{k}.png"
            crop.save(self.img_dir / name, optimize=True)
            imgs.append(dict(kind="image", page=page_no, top=top, bottom=bottom, path=f"images/{name}",
                             w=crop.width, h=crop.height))
            self.report["images"].append({"page": page_no, "file": name, "px": [crop.width, crop.height]})
        return imgs

    def render_full_pages(self, pages: set[int]) -> dict[int, dict]:
        out = {}
        for pg in sorted(pages):
            pil, _ = render_page_pil(self.pdf_path, pg, dpi=self.cfg.get("image_dpi", 200))
            name = f"p{pg:04d}_full.png"
            pil.save(self.img_dir / name, optimize=True)
            out[pg] = dict(kind="image", page=pg, top=0, bottom=0, path=f"images/{name}", w=pil.width, h=pil.height)
            self.report["images"].append({"page": pg, "file": name, "px": [pil.width, pil.height], "full_page": True})
        return out

    # --- recorrido
    def run(self) -> list[dict]:
        cfg = self.cfg
        skip = set(cfg.get("skip_pages", []))
        render_pages = set(cfg.get("render_pages", []))
        all_blocks: list[dict] = []
        with pdfplumber.open(self.pdf_path) as pdf:
            n = len(pdf.pages)
            # pasada 1: líneas por página (con OCR donde haga falta)
            pages_lines, heights, widths = [], [], []
            for i, p in enumerate(pdf.pages, start=1):
                if i in skip:
                    pages_lines.append([]); heights.append(p.height); widths.append(p.width); continue
                words = p.extract_words(extra_attrs=["size", "fontname"], x_tolerance=1.5, y_tolerance=2.5)
                if sum(len(w["text"]) for w in words) < 40 and cfg.get("ocr"):
                    try:
                        words = ocr_page(self.pdf_path, i, cfg.get("ocr_lang", "spa+eng"))
                        self.report["ocr_pages"].append(i)
                    except Exception as e:  # tesseract/idioma ausente
                        self.report["warnings"].append(f"OCR falló en p{i}: {e}")
                if cfg.get("two_columns"):
                    mid = p.width / 2
                    left = words_to_lines([w for w in words if w["x1"] <= mid + 5], i)
                    right = words_to_lines([w for w in words if w["x0"] > mid - 5], i)
                    lines = left + right
                else:
                    lines = words_to_lines(words, i)
                pages_lines.append(lines); heights.append(p.height); widths.append(p.width)

            body = cfg.get("body_size") or body_size([ln for pl in pages_lines for ln in pl])
            if not cfg.get("header_keys") and not cfg.get("footer_keys"):
                edges = detect_repeated_edges(pages_lines, heights)
                hdr, ftr = edges["header"], edges["footer"]
            else:
                hdr, ftr = set(cfg.get("header_keys", [])), set(cfg.get("footer_keys", []))
            full_imgs = self.render_full_pages(render_pages)

            # pasada 2: filtrar, clasificar, intercalar imágenes
            for i, p in enumerate(pdf.pages, start=1):
                if i in skip:
                    continue
                h, w = heights[i - 1], widths[i - 1]
                kept = []
                for ln in pages_lines[i - 1]:
                    why = self.is_running(ln, h, hdr, ftr)
                    if why:
                        if len(self.report["removed"][why]) < 40:
                            self.report["removed"][why].append(f'p{i}: {ln["text"][:60]}')
                        self.report["removed_counts"][why] = self.report["removed_counts"].get(why, 0) + 1
                        continue
                    kept.append(ln)
                blocks = self.lines_to_blocks(kept, body, h)
                if i in full_imgs:
                    blocks = [full_imgs[i]] + [b for b in blocks if b["kind"] == "heading"]
                elif not cfg.get("no_images"):
                    blocks += self.page_images(p, i, w, h)
                blocks.sort(key=lambda b: (b["top"], 0 if b["kind"] != "image" else 1))
                all_blocks.extend(blocks)
        self.report["removed"] = dict(self.report["removed"])
        return postprocess(all_blocks, self.report)


def _is_blank(img, thr: float = 0.985) -> bool:
    g = img.convert("L")
    hist = g.histogram()
    total = sum(hist)
    return total == 0 or (hist[-1] + hist[0]) / total > thr


def join_lines(a: str, b: str, dehyph: bool, report: dict) -> str:
    a, b = a.rstrip(), b.lstrip()
    if dehyph and RE_HYPHEN_END.search(a) and b and b[0].islower():
        report["dehyphenated"] += 1
        return a[:-1] + b
    return a + " " + b


def postprocess(blocks: list[dict], report: dict) -> list[dict]:
    """Une párrafos cortados entre páginas; agrupa notas al final del capítulo."""
    out: list[dict] = []
    pending_notes: list[dict] = []

    def flush_notes():
        if pending_notes:
            out.append(dict(kind="notes", items=[n["text"] for n in pending_notes]))
            pending_notes.clear()

    for b in blocks:
        if b["kind"] == "note":
            pending_notes.append(b)
            continue
        if b["kind"] == "heading" and b["level"] <= 2:
            flush_notes()
        if b["kind"] == "para" and out and out[-1]["kind"] == "para":
            prev = out[-1]
            cont_lower = b["text"][:1].islower() or b["text"][:1] in ",;:)»”"
            if (not RE_SENT_END.search(prev["text"]) and (cont_lower or RE_HYPHEN_END.search(prev["text"]))
                    and not RE_LIST_START.match(b["text"])):
                prev["text"] = join_lines(prev["text"], b["text"], True, report)
                report["merged_paragraphs"] += 1
                continue
        out.append(b)
    flush_notes()
    for b in out:
        if b["kind"] == "heading":
            report["headings"].append({"level": b["level"], "text": b["text"], "page": b["page"]})
    return out


# ---------------------------------------------------------------- salida markdown

def clean_text(t: str) -> str:
    t = re.sub(r"\s+", " ", t).strip()
    t = t.replace("ﬁ", "fi").replace("ﬂ", "fl").replace("ﬀ", "ff").replace("ﬃ", "ffi")
    t = re.sub(r"\s+([,.;:!?»)])", r"\1", t)
    t = re.sub(r"([(«])\s+", r"\1", t)
    return t


def md_escape(t: str) -> str:
    return re.sub(r"^(\s*)([#>+\-*]|\d+\.)(\s)", r"\1\\\2\3", t)


def to_markdown(blocks: list[dict], meta: dict) -> str:
    lines = ["---", f'title: "{meta["title"].replace(chr(34), chr(39))}"']
    if meta.get("author"):
        lines.append(f'author: "{meta["author"].replace(chr(34), chr(39))}"')
    lines += [f'lang: {meta.get("lang", "es")}', "---", ""]
    for b in blocks:
        if b["kind"] == "heading":
            lines += ["#" * b["level"] + " " + clean_text(b["text"]), ""]
        elif b["kind"] == "para":
            t = md_escape(clean_text(b["text"]))
            if b.get("italic") and b.get("lines", 1) <= 3:
                t = f"*{t}*"
            lines += [t, ""]
        elif b["kind"] == "image":
            lines += [f'![]({b["path"]})', ""]
        elif b["kind"] == "notes":
            lines += ["#### Notas", ""] + [f"- {clean_text(n)}" for n in b["items"]] + [""]
    return "\n".join(lines)


# ---------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", help="JSON de kindle_analyze.py (usa suggested_config)")
    ap.add_argument("--title"); ap.add_argument("--author"); ap.add_argument("--lang")
    ap.add_argument("--skip-pages"); ap.add_argument("--render-pages")
    ap.add_argument("--heading-sizes", help="ej. 18,14,12 (h1,h2,h3)")
    ap.add_argument("--body-size", type=float)
    ap.add_argument("--no-outline", action="store_true")
    ap.add_argument("--ocr", action="store_true"); ap.add_argument("--ocr-lang")
    ap.add_argument("--two-columns", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--keep-full-page-images", action="store_true")
    ap.add_argument("--bold-headings", action="store_true", help="líneas cortas en negrita = títulos")
    ap.add_argument("--edge-frac", type=float)
    a = ap.parse_args()

    cfg: dict = {}
    if a.config:
        j = json.loads(Path(a.config).read_text(encoding="utf-8"))
        cfg = j.get("suggested_config", j)
        cfg["bold_body_as_heading"] = j.get("fonts", {}).get("bold_body_as_heading", False)
    if a.title: cfg["title"] = a.title
    if a.author: cfg["author"] = a.author
    if a.lang: cfg["lang"] = a.lang
    if a.skip_pages: cfg["skip_pages"] = sorted(parse_pages(a.skip_pages))
    if a.render_pages: cfg["render_pages"] = sorted(parse_pages(a.render_pages))
    if a.heading_sizes: cfg["heading_sizes"] = [float(x) for x in a.heading_sizes.split(",")]
    if a.body_size: cfg["body_size"] = a.body_size
    if a.no_outline: cfg["use_outline"] = False
    if a.ocr: cfg["ocr"] = True
    if a.ocr_lang: cfg["ocr_lang"] = a.ocr_lang
    if a.two_columns: cfg["two_columns"] = True
    if a.no_images: cfg["no_images"] = True
    if a.keep_full_page_images: cfg["keep_full_page_images"] = True
    if a.bold_headings: cfg["bold_body_as_heading"] = True
    if a.edge_frac: cfg["edge_frac"] = a.edge_frac
    cfg.setdefault("title", Path(a.pdf).stem); cfg.setdefault("lang", "es")
    if cfg.get("ocr") and not which("tesseract"):
        print("AVISO: OCR pedido pero tesseract no está instalado.", file=sys.stderr)

    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    ex = Extractor(a.pdf, cfg, out)
    blocks = ex.run()
    md = to_markdown(blocks, cfg)
    (out / "book.md").write_text(md, encoding="utf-8")
    ex.report["config_used"] = cfg
    ex.report["stats"] = {"blocks": len(blocks), "headings": len(ex.report["headings"]),
                          "images": len(ex.report["images"]), "chars": len(md)}
    if len(ex.report["headings"]) == 0:
        ex.report["warnings"].append("0 títulos detectados → sin índice. Ajustar --heading-sizes / --bold-headings o marcar títulos a mano en book.md.")
    (out / "extract_report.json").write_text(json.dumps(ex.report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"book.md: {len(md)} chars | títulos: {len(ex.report['headings'])} | imágenes: {len(ex.report['images'])}"
          f" | párrafos unidos: {ex.report['merged_paragraphs']} | des-guionados: {ex.report['dehyphenated']}")
    print("Índice detectado:")
    for h in ex.report["headings"][:60]:
        print("  " + "  " * (h["level"] - 1) + f"{h['text'][:70]}  (p{h['page']})")
    for w in ex.report["warnings"]:
        print("AVISO:", w)


if __name__ == "__main__":
    main()
