#!/usr/bin/env python3
"""kindle_build.py — Markdown (book.md) → EPUB3 listo para Send to Kindle.

Uso:
    python3 kindle_build.py work/book.md --out libro.epub
        [--cover portada.jpg | --gen-cover] [--color] [--max-px 1600] [--toc-depth 3]
        [--title T] [--author A] [--lang es]

Hace:
  1. Optimiza imágenes referenciadas en el .md: RGBA→RGB, escala de grises (salvo --color),
     lado mayor ≤ max-px (Paperwhite/Oasis 1236×1648; Scribe 1860×2480), JPEG q85 o PNG paleta si es line-art.
  2. Inyecta CSS compatible con el renderizador Kindle (KF8/Enhanced Typesetting):
     sin fuentes fijas, sangría 1.2em, títulos con page-break-before, imágenes max-width 100%.
  3. pandoc → EPUB3 con nav TOC (--toc), un XHTML por capítulo (--split-level 1), metadatos dc:.
     Fallback: constructor EPUB propio (zip) si pandoc no está disponible.
  4. Valida: mimetype primero y sin compresión, nav.xhtml con entradas, tamaño (aviso >50 MB / error >200 MB).
  5. Escribe build_report.json junto al EPUB.
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from kindle_common import which  # noqa: E402

KINDLE_CSS = """
/* Kindle-safe CSS: unidades relativas, sin fuentes embebidas, sin posicionamiento */
body { margin: 0; padding: 0; text-align: left; }
h1, h2, h3, h4 { text-align: left; font-weight: bold; page-break-after: avoid; margin: 1em 0 0.6em 0; }
h1 { font-size: 1.6em; page-break-before: always; margin-top: 2em; }
h2 { font-size: 1.3em; margin-top: 1.5em; }
h3 { font-size: 1.1em; }
h4 { font-size: 1em; font-style: italic; }
p { margin: 0; text-indent: 1.2em; line-height: 1.4; text-align: justify; }
h1 + p, h2 + p, h3 + p, h4 + p, blockquote p, li p, .noindent, p.first { text-indent: 0; }
blockquote { margin: 0.8em 1.5em; font-style: italic; }
ul, ol { margin: 0.6em 0 0.6em 1.2em; }
li { margin-bottom: 0.3em; }
img { max-width: 100%; height: auto; }
figure { margin: 1em 0; text-align: center; page-break-inside: avoid; }
figcaption { font-size: 0.85em; font-style: italic; margin-top: 0.3em; }
table { border-collapse: collapse; margin: 1em 0; font-size: 0.9em; }
td, th { border: 1px solid #999; padding: 0.2em 0.4em; vertical-align: top; }
hr { border: 0; text-align: center; margin: 1.5em 0; }
hr:after { content: "* * *"; }
code, pre { font-family: monospace; font-size: 0.9em; }
pre { white-space: pre-wrap; }
.notes { font-size: 0.9em; }
"""

IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


# ---------------------------------------------------------------- imágenes

def optimize_image(src: Path, dst_dir: Path, max_px: int, color: bool) -> tuple[Path, dict]:
    im = Image.open(src)
    info = {"src": src.name, "orig_px": list(im.size), "orig_kb": round(src.stat().st_size / 1024)}
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    if max(im.size) > max_px:
        r = max_px / max(im.size)
        im = im.resize((max(1, int(im.width * r)), max(1, int(im.height * r))), Image.LANCZOS)
    if not color:
        im = im.convert("L")
    # line-art / capturas con pocos colores → PNG paleta; fotos → JPEG
    sample = im.resize((min(200, im.width), min(200, im.height)))
    ncolors = len(sample.getcolors(maxcolors=4096) or [None] * 4097)
    if ncolors <= 48:
        dst = dst_dir / (src.stem + ".png")
        im.convert("P", palette=Image.ADAPTIVE, colors=max(2, min(64, ncolors))).save(dst, optimize=True)
    else:
        dst = dst_dir / (src.stem + ".jpg")
        im.save(dst, "JPEG", quality=85, optimize=True, progressive=False)
    info.update({"dst": dst.name, "px": list(im.size), "kb": round(dst.stat().st_size / 1024)})
    return dst, info


def gen_cover(title: str, author: str, dst: Path) -> Path:
    from PIL import ImageDraw, ImageFont
    W, H = 1600, 2560
    im = Image.new("RGB", (W, H), (20, 20, 20))
    d = ImageDraw.Draw(im)
    try:
        f1 = ImageFont.truetype("DejaVuSerif-Bold.ttf", 120)
        f2 = ImageFont.truetype("DejaVuSerif.ttf", 70)
    except Exception:
        f1 = f2 = ImageFont.load_default()
    d.rectangle([80, 80, W - 80, H - 80], outline=(200, 200, 200), width=6)
    y = 700
    for line in wrap(title, 18):
        w = d.textlength(line, font=f1)
        d.text(((W - w) / 2, y), line, fill=(245, 245, 245), font=f1); y += 150
    if author:
        y += 120
        for line in wrap(author, 28):
            w = d.textlength(line, font=f2)
            d.text(((W - w) / 2, y), line, fill=(190, 190, 190), font=f2); y += 95
    im.save(dst, "JPEG", quality=88)
    return dst


def wrap(text: str, n: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > n and cur:
            lines.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines


# ---------------------------------------------------------------- pandoc / fallback

def build_pandoc(md: Path, epub: Path, css: Path, cover: Path | None, toc_depth: int, meta: dict) -> None:
    cmd = ["pandoc", str(md), "-o", str(epub), "--to", "epub3", "--toc", f"--toc-depth={toc_depth}",
           "--split-level=1", "--css", str(css), f"--resource-path={md.parent}",
           "--epub-title-page=false", "-M", f"lang={meta.get('lang', 'es')}"]
    if meta.get("title"):
        cmd += ["-M", f"title={meta['title']}"]
    if meta.get("author"):
        cmd += ["-M", f"author={meta['author']}"]
    if cover:
        cmd += ["--epub-cover-image", str(cover)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr)
    if r.stderr.strip():
        print("pandoc:", r.stderr.strip()[:500], file=sys.stderr)


def build_fallback(md: Path, epub: Path, css_text: str, cover: Path | None, meta: dict) -> None:
    """EPUB3 mínimo sin pandoc: markdown → HTML, split por <h1>, nav.xhtml + toc.ncx."""
    import markdown as mdlib
    text = md.read_text(encoding="utf-8")
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    body = mdlib.markdown(text, extensions=["extra"])
    parts = re.split(r"(?=<h1[ >])", body)
    chapters = [p for p in parts if p.strip()]
    uid = f"urn:uuid:{uuid.uuid4()}"
    title, author, lang = meta.get("title", "Sin título"), meta.get("author", ""), meta.get("lang", "es")
    manifest, spine, nav_items, ncx_points = [], [], [], []
    files: dict[str, bytes] = {}
    for i, ch in enumerate(chapters, 1):
        fname = f"ch{i:03d}.xhtml"
        heads = re.findall(r"<h([1-3])[^>]*>(.*?)</h\1>", ch, flags=re.S)
        for j, (lvl, htxt) in enumerate(heads):
            anchor = f"h{i}_{j}"
            ch = ch.replace(f"<h{lvl}>{htxt}</h{lvl}>", f'<h{lvl} id="{anchor}">{htxt}</h{lvl}>', 1)
            nav_items.append((int(lvl), re.sub("<[^>]+>", "", htxt), f"{fname}#{anchor}"))
        files[f"OEBPS/{fname}"] = (f'<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" '
                                   f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}"><head><title>{html.escape(title)}</title>'
                                   f'<link rel="stylesheet" type="text/css" href="style.css"/></head><body>{ch}</body></html>').encode()
        manifest.append(f'<item id="c{i}" href="{fname}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    for img in md.parent.glob("images_opt/*"):
        mt = "image/jpeg" if img.suffix == ".jpg" else "image/png"
        files[f"OEBPS/images_opt/{img.name}"] = img.read_bytes()
        manifest.append(f'<item id="{img.stem}" href="images_opt/{img.name}" media-type="{mt}"/>')
    cover_meta = ""
    if cover:
        files["OEBPS/cover.jpg"] = cover.read_bytes()
        manifest.append('<item id="cover-image" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
        cover_meta = '<meta name="cover" content="cover-image"/>'
    nav_html = "".join(f'<li><a href="{href}">{html.escape(t)}</a></li>' for lvl, t, href in nav_items if lvl <= 2)
    files["OEBPS/nav.xhtml"] = (f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" '
                                f'xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Índice</title></head><body>'
                                f'<nav epub:type="toc" id="toc"><h1>Índice</h1><ol>{nav_html}</ol></nav></body></html>').encode()
    for k, (lvl, t, href) in enumerate(nav_items, 1):
        if lvl <= 2:
            ncx_points.append(f'<navPoint id="np{k}" playOrder="{k}"><navLabel><text>{html.escape(t)}</text></navLabel>'
                              f'<content src="{href}"/></navPoint>')
    files["OEBPS/toc.ncx"] = (f'<?xml version="1.0" encoding="utf-8"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
                              f'<head><meta name="dtb:uid" content="{uid}"/></head><docTitle><text>{html.escape(title)}</text></docTitle>'
                              f'<navMap>{"".join(ncx_points)}</navMap></ncx>').encode()
    files["OEBPS/style.css"] = css_text.encode()
    files["OEBPS/content.opf"] = (
        f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">{uid}</dc:identifier>'
        f'<dc:title>{html.escape(title)}</dc:title><dc:creator>{html.escape(author)}</dc:creator><dc:language>{lang}</dc:language>'
        f'<meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>{cover_meta}</metadata>'
        f'<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        f'<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>'
        f'<item id="css" href="style.css" media-type="text/css"/>{"".join(manifest)}</manifest>'
        f'<spine toc="ncx">{"".join(spine)}</spine></package>').encode()
    with zipfile.ZipFile(epub, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   b'<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        for k, v in files.items():
            z.writestr(k, v, compress_type=zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------- validación

def validate(epub: Path) -> dict:
    rep: dict = {"file": epub.name, "size_mb": round(epub.stat().st_size / 1e6, 2), "problems": []}
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        first = z.infolist()[0]
        if first.filename != "mimetype" or first.compress_type != zipfile.ZIP_STORED:
            rep["problems"].append("mimetype no es la primera entrada sin compresión")
        opf = [n for n in names if n.endswith(".opf")]
        nav = [n for n in names if n.endswith("nav.xhtml")]
        if not opf: rep["problems"].append("falta content.opf")
        if not nav: rep["problems"].append("falta nav.xhtml (TOC)")
        else:
            navx = z.read(nav[0]).decode("utf-8", "ignore")
            toc = re.search(r'<nav[^>]*epub:type="toc".*?</nav>', navx, flags=re.S)
            rep["toc_entries"] = len(re.findall(r"<a ", toc.group(0))) if toc else 0
            if rep["toc_entries"] == 0:
                rep["problems"].append("TOC vacío: el Kindle no tendrá índice navegable")
        rep["xhtml_files"] = len([n for n in names if n.endswith(".xhtml") and "nav" not in n])
        rep["images"] = len([n for n in names if re.search(r"\.(jpe?g|png|gif)$", n, re.I)])
        if opf:
            o = z.read(opf[0]).decode("utf-8", "ignore")
            rep["title"] = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", o, re.S).group(1) if "<dc:title" in o else None
            rep["has_cover"] = 'properties="cover-image"' in o or 'name="cover"' in o
            if not re.search(r"<dc:language[^>]*>\w", o):
                rep["problems"].append("sin dc:language")
    if rep["size_mb"] > 200:
        rep["problems"].append("EPUB > 200 MB: excede Send to Kindle web")
    elif rep["size_mb"] > 50:
        rep["problems"].append("EPUB > 50 MB: no se puede enviar por email ni app de escritorio; usar web (≤200 MB) o reducir imágenes")
    return rep


# ---------------------------------------------------------------- main

def read_front_matter(md_text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", md_text, flags=re.S)
    meta = {}
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md")
    ap.add_argument("--out", required=True)
    ap.add_argument("--cover"); ap.add_argument("--gen-cover", action="store_true")
    ap.add_argument("--color", action="store_true", help="conservar color (Kindle Colorsoft / apps)")
    ap.add_argument("--max-px", type=int, default=1600)
    ap.add_argument("--toc-depth", type=int, default=3)
    ap.add_argument("--title"); ap.add_argument("--author"); ap.add_argument("--lang")
    ap.add_argument("--force-fallback", action="store_true")
    a = ap.parse_args()

    md = Path(a.md).resolve()
    work = md.parent
    text = md.read_text(encoding="utf-8")
    meta = read_front_matter(text)
    for k in ("title", "author", "lang"):
        if getattr(a, k):
            meta[k] = getattr(a, k)
    meta.setdefault("title", md.stem); meta.setdefault("lang", "es")

    # imágenes
    opt_dir = work / "images_opt"
    if opt_dir.exists():
        shutil.rmtree(opt_dir)
    opt_dir.mkdir()
    img_report, missing = [], []

    def _sub(m):
        alt, rel = m.group(1), m.group(2)
        src = (work / rel).resolve()
        if not src.exists():
            missing.append(rel); return ""
        dst, info = optimize_image(src, opt_dir, a.max_px, a.color)
        img_report.append(info)
        return f"![{alt}](images_opt/{dst.name})"

    text2 = IMG_RE.sub(_sub, text)
    md_opt = work / "book_build.md"
    md_opt.write_text(text2, encoding="utf-8")
    css = work / "kindle.css"; css.write_text(KINDLE_CSS, encoding="utf-8")

    cover = None
    if a.cover:
        cover, _ = optimize_image(Path(a.cover), work, 2560, True)
        cover = Path(shutil.copy(cover, work / "cover.jpg")) if cover.suffix != ".jpg" else cover
    elif a.gen_cover:
        cover = gen_cover(meta["title"], meta.get("author", ""), work / "cover.jpg")

    epub = Path(a.out).resolve()
    epub.parent.mkdir(parents=True, exist_ok=True)
    engine = "pandoc"
    if which("pandoc") and not a.force_fallback:
        try:
            build_pandoc(md_opt, epub, css, cover, a.toc_depth, meta)
        except RuntimeError as e:
            print("pandoc falló, usando fallback:", str(e)[:300], file=sys.stderr)
            engine = "fallback"; build_fallback(md_opt, epub, KINDLE_CSS, cover, meta)
    else:
        engine = "fallback"; build_fallback(md_opt, epub, KINDLE_CSS, cover, meta)

    rep = validate(epub)
    rep.update({"engine": engine, "images_optimized": img_report, "images_missing": missing,
                "images_total_kb": sum(i["kb"] for i in img_report), "meta": meta})
    (epub.parent / (epub.stem + ".build_report.json")).write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"EPUB → {epub} ({rep['size_mb']} MB, motor {engine})")
    print(f"TOC: {rep.get('toc_entries')} entradas | capítulos xhtml: {rep['xhtml_files']} | imágenes: {rep['images']} "
          f"({rep['images_total_kb']} KB) | portada: {rep.get('has_cover')}")
    for p in rep["problems"]:
        print("PROBLEMA:", p)
    if missing:
        print("Imágenes no encontradas:", missing)


if __name__ == "__main__":
    main()
