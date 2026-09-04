#!/usr/bin/env python3
"""kindle_pipeline.py — Ejecuta analyze → extract → build en un paso.

Uso:
    python3 kindle_pipeline.py libro.pdf [--out-dir ./work] [--epub libro.epub]
        [--title T] [--author A] [--lang es] [--skip-pages 1-3] [--heading-sizes 18,14]
        [--ocr] [--two-columns] [--cover img | --gen-cover] [--color] [--no-build]

Para documentos no-PDF (docx, epub, html, txt) NO usar este script: convertir con pandoc a Markdown
    pandoc entrada.docx -t gfm --extract-media=work -o work/book.md
y luego ejecutar sólo kindle_build.py sobre ese book.md.

Flujo recomendado en el proyecto: correr con --no-build, revisar work/book.md (índice, títulos falsos,
párrafos rotos, imágenes basura), corregir, y luego compilar con kindle_build.py.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def sh(args: list[str]) -> None:
    print("\n$", " ".join(args))
    r = subprocess.run(args, text=True)
    if r.returncode != 0:
        sys.exit(f"Fallo en: {args[0]} {args[1]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out-dir", default="work")
    ap.add_argument("--epub")
    for flag in ("--title", "--author", "--lang", "--skip-pages", "--heading-sizes", "--render-pages", "--ocr-lang", "--cover"):
        ap.add_argument(flag)
    for flag in ("--ocr", "--two-columns", "--gen-cover", "--color", "--no-build", "--no-outline", "--bold-headings", "--no-images"):
        ap.add_argument(flag, action="store_true")
    a = ap.parse_args()

    pdf = Path(a.pdf)
    out = Path(a.out_dir); out.mkdir(parents=True, exist_ok=True)
    analysis = out / "analysis.json"
    sh([sys.executable, str(HERE / "kindle_analyze.py"), str(pdf), "--out", str(analysis)])

    ex = [sys.executable, str(HERE / "kindle_extract.py"), str(pdf), "--out", str(out), "--config", str(analysis)]
    for flag in ("title", "author", "lang", "skip_pages", "heading_sizes", "render_pages", "ocr_lang"):
        v = getattr(a, flag)
        if v:
            ex += ["--" + flag.replace("_", "-"), v]
    for flag in ("ocr", "two_columns", "no_outline", "bold_headings", "no_images"):
        if getattr(a, flag):
            ex.append("--" + flag.replace("_", "-"))
    sh(ex)

    if a.no_build:
        print(f"\nExtracción lista. Revisar {out/'book.md'} y luego:\n  python3 {HERE/'kindle_build.py'} {out/'book.md'} --out {pdf.stem}.epub --gen-cover")
        return

    epub = a.epub or str(out / f"{pdf.stem}.epub")
    bd = [sys.executable, str(HERE / "kindle_build.py"), str(out / "book.md"), "--out", epub]
    if a.cover: bd += ["--cover", a.cover]
    if a.gen_cover: bd.append("--gen-cover")
    if a.color: bd.append("--color")
    for flag in ("title", "author", "lang"):
        v = getattr(a, flag)
        if v:
            bd += ["--" + flag, v]
    sh(bd)

    rep = json.loads((out / "extract_report.json").read_text(encoding="utf-8"))
    print("\n== Resumen QA")
    print(f"Títulos: {len(rep['headings'])} | Imágenes: {len(rep['images'])} | Eliminado: {rep.get('removed_counts')}")
    print(f"Párrafos unidos entre páginas: {rep['merged_paragraphs']} | des-guionados: {rep['dehyphenated']} | OCR: {len(rep['ocr_pages'])} págs")
    for w in rep["warnings"]:
        print("AVISO:", w)


if __name__ == "__main__":
    main()
