#!/usr/bin/env python3
"""kindle_analyze.py — Diagnóstico de un PDF antes de convertirlo a EPUB.

Uso:
    python3 kindle_analyze.py libro.pdf [--out analysis.json] [--sample 40]

Qué determina:
  1. ¿Hay capa de texto? (páginas con texto vs. escaneadas → OCR)
  2. Tamaño de fuente del cuerpo y candidatos a títulos (por tamaño/negrita)
  3. Encabezados/pies repetidos y patrón de numeración de páginas
  4. Marcadores (bookmarks) del PDF → índice confiable si existen
  5. Imágenes por página (cantidad, tamaño) y páginas "full-image"
  6. Columnas (heurística por distribución de x0)
  7. Tasa de guiones de corte de línea, notas al pie (fuente chica al pie)

Salida: resumen legible en stdout + JSON con la configuración sugerida para kindle_extract.py.
El JSON es editable: ajustá `heading_sizes`, `skip_pages`, `edge_frac` etc. antes de extraer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pdfplumber

sys.path.insert(0, str(Path(__file__).parent))
from kindle_common import (body_size, detect_repeated_edges, is_bold, is_page_number,  # noqa: E402
                           pdf_outline, round_size, words_to_lines, RE_HYPHEN_END)


def sample_indices(n: int, k: int) -> list[int]:
    if n <= k:
        return list(range(n))
    step = n / k
    return sorted({int(i * step) for i in range(k)} | {0, 1, 2, n - 1, n - 2})


def analyze(pdf_path: str, sample: int = 40) -> dict:
    rep: dict = {"file": os.path.basename(pdf_path), "size_mb": round(os.path.getsize(pdf_path) / 1e6, 2)}
    with pdfplumber.open(pdf_path) as pdf:
        n = len(pdf.pages)
        rep["pages"] = n
        rep["metadata"] = {k: str(v) for k, v in (pdf.metadata or {}).items()
                           if k in ("Title", "Author", "Subject", "Creator", "Producer", "CreationDate")}
        idx = sample_indices(n, sample)
        pages_lines, heights = [], []
        chars_per_page = {}
        img_per_page = {}
        x0_hist: Counter = Counter()
        hyph_lines = total_lines = 0
        width = height = 0.0
        for i in idx:
            p = pdf.pages[i]
            width, height = p.width, p.height
            words = p.extract_words(extra_attrs=["size", "fontname"], keep_blank_chars=False,
                                    use_text_flow=False, x_tolerance=1.5, y_tolerance=2.5)
            lines = words_to_lines(words, i + 1)
            pages_lines.append(lines)
            heights.append(p.height)
            chars_per_page[i + 1] = sum(len(w["text"]) for w in words)
            imgs = [im for im in p.images if im.get("width", 0) > 20 and im.get("height", 0) > 20]
            img_per_page[i + 1] = [{"w": round(im["width"]), "h": round(im["height"]),
                                    "top": round(im["top"]), "src_w": im.get("srcsize", [0, 0])[0],
                                    "src_h": im.get("srcsize", [0, 0])[1]} for im in imgs]
            for ln in lines:
                total_lines += 1
                x0_hist[int(ln["x0"] // 20) * 20] += 1
                if RE_HYPHEN_END.search(ln["text"]):
                    hyph_lines += 1

        # --- capa de texto
        empty = [pg for pg, c in chars_per_page.items() if c < 40]
        rep["text_layer"] = {
            "sampled_pages": len(idx),
            "pages_without_text": empty,
            "scanned_ratio": round(len(empty) / max(1, len(idx)), 2),
            "verdict": ("SCANNED_OCR_REQUIRED" if len(empty) / max(1, len(idx)) > 0.7
                        else "MIXED_PARTIAL_OCR" if len(empty) / max(1, len(idx)) > 0.15
                        else "TEXT_OK"),
        }
        all_lines = [ln for pl in pages_lines for ln in pl]
        body = body_size(all_lines) if all_lines else 10.0

        # --- fuentes / títulos
        size_hist: Counter = Counter()
        size_bold_hist: Counter = Counter()
        heading_examples: dict = defaultdict(list)
        for ln in all_lines:
            s = round_size(ln["size"])
            size_hist[s] += len(ln["text"])
            if ln["bold"]:
                size_bold_hist[s] += 1
            if s >= body * 1.12 and ln["nwords"] <= 14 and len(ln["text"]) > 1:
                if len(heading_examples[s]) < 4:
                    heading_examples[s].append(f'p{ln["page"]}: {ln["text"][:70]}')
        # páginas distintas en que aparece cada tamaño (un tamaño de una sola página = portada/colofón, no nivel)
        pages_by_size: dict = defaultdict(set)
        for ln in all_lines:
            pages_by_size[round_size(ln["size"])].add(ln["page"])
        larger = sorted([s for s in heading_examples if s > body], reverse=True)
        recurrent = [s for s in larger if len(pages_by_size[s]) >= 2] or larger
        # colapsar tamaños muy próximos (≤0.5pt) y limitar a 3 niveles
        levels = []
        for s in recurrent:
            if not levels or levels[-1] - s > 0.5:
                levels.append(s)
        levels = levels[:3]
        # portada probable: página 1 con pocas líneas y el tamaño máximo del documento
        first = pages_lines[0] if pages_lines else []
        cover_like = bool(first) and len(first) <= 8 and larger and any(round_size(l["size"]) == larger[0] for l in first)
        rep["cover_page_suspected"] = cover_like
        rep["fonts"] = {
            "body_size": body,
            "size_histogram_chars": dict(sorted(size_hist.items(), reverse=True)[:12]),
            "bold_lines_by_size": dict(size_bold_hist.most_common(8)),
            "heading_candidates": {str(k): v for k, v in sorted(heading_examples.items(), reverse=True)},
            "suggested_heading_sizes": levels,  # index 0 → h1, 1 → h2, 2 → h3
            "bold_body_as_heading": size_bold_hist.get(body, 0) > 0 and size_bold_hist.get(body, 0) < len(idx) * 3,
        }

        # --- encabezados / pies / numeración
        edges = detect_repeated_edges(pages_lines, heights)
        pn_hits = 0
        pn_examples = []
        for pl, h in zip(pages_lines, heights):
            for ln in pl:
                if (ln["bottom"] <= h * 0.10 or ln["top"] >= h * 0.90) and is_page_number(ln["text"]):
                    pn_hits += 1
                    if len(pn_examples) < 5:
                        pn_examples.append(f'p{ln["page"]}: "{ln["text"]}"')
        rep["running_elements"] = {
            "headers": sorted(edges["header"]),
            "footers": sorted(edges["footer"]),
            "top_candidates": edges["stats"]["top"],
            "bottom_candidates": edges["stats"]["bottom"],
            "page_number_lines_detected": pn_hits,
            "page_number_examples": pn_examples,
        }

        # --- notas al pie: líneas chicas en el 25% inferior
        small_bottom = sum(1 for pl, h in zip(pages_lines, heights) for ln in pl
                           if ln["size"] and ln["size"] <= body * 0.85 and ln["top"] >= h * 0.75)
        rep["footnotes_suspected"] = small_bottom

        # --- columnas
        if total_lines:
            common = [k for k, v in x0_hist.most_common(6) if v > total_lines * 0.12]
            common.sort()
            two_col = len(common) >= 2 and (common[-1] - common[0]) > width * 0.30
        else:
            two_col = False
        rep["layout"] = {"page_w": round(width), "page_h": round(height), "two_columns_suspected": two_col,
                         "x0_histogram": dict(x0_hist.most_common(6))}

        # --- guiones
        rep["hyphenation_ratio"] = round(hyph_lines / max(1, total_lines), 3)

        # --- imágenes
        n_imgs = sum(len(v) for v in img_per_page.values())
        full_page = [pg for pg, ims in img_per_page.items()
                     if any(im["w"] > width * 0.85 and im["h"] > height * 0.85 for im in ims)]
        rep["images"] = {"sampled_total": n_imgs, "pages_with_images": [pg for pg, v in img_per_page.items() if v],
                         "full_page_image_pages": full_page,
                         "estimated_total": round(n_imgs * n / max(1, len(idx)))}

        # --- marcadores
        ol = pdf_outline(pdf_path)
        rep["outline"] = {"count": len(ol), "max_level": max([o["level"] for o in ol], default=0),
                          "sample": ol[:15]}

    # --- configuración sugerida para kindle_extract
    rep["suggested_config"] = {
        "body_size": body,
        "heading_sizes": levels,
        "use_outline": len(ol) >= 3,
        "edge_frac": 0.10,
        "header_keys": sorted(edges["header"]),
        "footer_keys": sorted(edges["footer"]),
        "strip_page_numbers": True,
        "dehyphenate": rep["hyphenation_ratio"] > 0.01,
        "footnote_size_max": round(body * 0.85, 1),
        "ocr": rep["text_layer"]["verdict"] != "TEXT_OK",
        "ocr_lang": "spa+eng",
        "two_columns": two_col,
        "skip_pages": [1] if rep.get("cover_page_suspected") else [],   # portada, créditos, publicidad
        "image_min_px": 60,
        "title": rep["metadata"].get("Title") or Path(pdf_path).stem,
        "author": rep["metadata"].get("Author") or "",
        "lang": "es",
    }
    rep["warnings"] = warnings(rep)
    return rep


def warnings(rep: dict) -> list[str]:
    w = []
    tl = rep["text_layer"]["verdict"]
    if tl != "TEXT_OK":
        w.append(f"Capa de texto: {tl} → requiere OCR (tesseract). Revisar idioma instalado (spa).")
    if rep.get("cover_page_suspected"):
        w.append("Página 1 parece portada: se sugiere skip_pages=[1] (usar --cover con la imagen rasterizada si se quiere conservar).")
    if rep["outline"]["count"] == 0:
        w.append("Sin marcadores PDF: el índice se construirá por heurística de tamaño de fuente. Verificar títulos.")
    if not rep["fonts"]["suggested_heading_sizes"]:
        w.append("No se detectaron tamaños de título > cuerpo. Puede que los títulos sean solo negrita o mayúsculas → activar bold_body_as_heading o marcar a mano.")
    if rep["layout"]["two_columns_suspected"]:
        w.append("Posible maquetación a dos columnas: verificar orden de lectura; usar --two-columns.")
    if rep["images"]["full_page_image_pages"]:
        w.append(f"Páginas con imagen a página completa {rep['images']['full_page_image_pages'][:6]}: probablemente portada/escaneo.")
    if rep["footnotes_suspected"] > 5:
        w.append("Notas al pie probables: se moverán al final de cada capítulo como 'Notas'.")
    if rep["size_mb"] > 45:
        w.append("PDF > 45 MB: vigilar tamaño del EPUB (límite 50 MB por email / 200 MB web).")
    return w


def print_summary(rep: dict) -> None:
    print(f"== {rep['file']} — {rep['pages']} págs, {rep['size_mb']} MB")
    print(f"Texto: {rep['text_layer']['verdict']} (sin texto: {rep['text_layer']['pages_without_text'][:10]})")
    f = rep["fonts"]
    print(f"Cuerpo: {f['body_size']} pt | Títulos sugeridos (h1,h2,h3): {f['suggested_heading_sizes']}")
    for s, ex in f["heading_candidates"].items():
        print(f"   {s} pt → {ex}")
    r = rep["running_elements"]
    print(f"Encabezados repetidos: {r['headers']}\nPies repetidos: {r['footers']}")
    print(f"Líneas de nº de página detectadas: {r['page_number_lines_detected']} {r['page_number_examples']}")
    print(f"Marcadores PDF: {rep['outline']['count']} (niveles: {rep['outline']['max_level']})")
    print(f"Imágenes (muestra): {rep['images']['sampled_total']} | est. total {rep['images']['estimated_total']}")
    print(f"Columnas dobles: {rep['layout']['two_columns_suspected']} | guiones: {rep['hyphenation_ratio']} | notas: {rep['footnotes_suspected']}")
    print("Avisos:")
    for w in rep["warnings"]:
        print("  -", w)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default=None, help="JSON de salida (default: <pdf>.analysis.json)")
    ap.add_argument("--sample", type=int, default=40, help="páginas a muestrear")
    a = ap.parse_args()
    rep = analyze(a.pdf, a.sample)
    out = a.out or (Path(a.pdf).with_suffix(".analysis.json"))
    Path(out).write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print_summary(rep)
    print(f"\nJSON → {out}")


if __name__ == "__main__":
    main()
