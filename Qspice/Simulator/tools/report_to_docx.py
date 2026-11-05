"""MS Word version (report.docx) of a sweep report written by basic_sine_sweep.py or basic_fixed_force_sweep.py.

    python Qspice/Simulator/tools/report_to_docx.py                 # every results/basic_model_sine_sweep* folder that has a report.md
    python Qspice/Simulator/tools/report_to_docx.py results/basic_model_sine_sweep_noise_0p4mV
    python Qspice/Simulator/tools/report_to_docx.py results/basic_model_fixed_force_R20M_noise0p4mV_100ph_<date>_<time>

Reads report.md (headings, tables, bullets, paragraphs with **bold** / *italic* / `code`, figures) and the figures it points to, and writes
report.docx next to it: A4, title, numbered figures with captions, tables with a repeated header row, page numbers. Needs python-docx.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from PIL import Image

RESULTS = Path(__file__).resolve().parents[1] / "results"
PAGE_W_CM, MARGIN_CM = 21.0, 2.0
TEXT_W_CM = PAGE_W_CM - 2 * MARGIN_CM
MAX_FIG_H_CM = 21.0
INK, MUTED, HEAD_FILL, BAND_FILL = RGBColor(0x1F, 0x1F, 0x1F), RGBColor(0x5A, 0x5A, 0x5A), "DCE6F2", "F5F7FA"
INLINE = re.compile(r"(\*\*.+?\*\*|`.+?`|(?<![\w*])\*[^*\s][^*]*?\*(?![\w*]))")


def typography(text: str) -> str:
    """ASCII units of the report -> the symbols a Word reader expects."""
    text = re.sub(r"(?<=\d) us\b", " µs", text)
    text = re.sub(r"(?<=\d) uN\b", " µN", text)
    text = re.sub(r"(?<=\d) deg\b", "°", text)
    text = re.sub(r"\bMohm\b", "MΩ", text)
    text = re.sub(r"(?<=[\w)]) x (?=[\w(])", " × ", text)
    text = re.sub(r"\btau\b", "τ", text)
    text = re.sub(r"\bsigma\b", "σ", text)
    text = text.replace("+-", "±").replace("->", "→")
    return text.replace(" .. ", " to ")


def add_runs(par, text: str, size: float | None = None, bold: bool = False, color: RGBColor | None = None, code_ok: bool = True) -> None:
    for part in INLINE.split(text):
        if not part:
            continue
        run_bold, run_italic, font = bold, False, None
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            part, run_bold = typography(part[2:-2].replace("`", "")), True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2 and code_ok:
            part, font = part[1:-1], "Consolas"
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            part, run_italic = part[1:-1], True
        else:
            part = typography(part)
        run = par.add_run(part)
        run.bold, run.italic = run_bold, run_italic
        if font:
            run.font.name = font
            run.font.size = Pt((size or 10.5) - 1)
            run._element.rPr.rFonts.set(qn("w:eastAsia"), font)
        elif size:
            run.font.size = Pt(size)
        if color is not None:
            run.font.color.rgb = color


def shade(cell, fill: str) -> None:
    tc_pr = cell._element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.append(shd)


def set_borders(table, color: str = "BFC7D1") -> None:
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    table._tbl.tblPr.append(borders)


def row_props(row, header: bool) -> None:
    pr = row._tr.get_or_add_trPr()
    pr.append(OxmlElement("w:cantSplit"))
    if header:
        pr.append(OxmlElement("w:tblHeader"))


def cell_margins(table, top: int = 40, bottom: int = 40, side: int = 70) -> None:
    mar = OxmlElement("w:tblCellMar")
    for edge, w in (("top", top), ("left", side), ("bottom", bottom), ("right", side)):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:w"), str(w))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    table._tbl.tblPr.append(mar)


def split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def add_table(doc: Document, lines: list[str]) -> None:
    header, align = split_row(lines[0]), split_row(lines[1])
    body = [split_row(ln) for ln in lines[2:]]
    ncol = len(header)
    no_header = not any(header)                                   # the setup table: label | value, no header row
    if not no_header and len(body) > 40 and ncol <= 3:            # a long narrow table (the sampling phases): side-by-side blocks of <= 34 rows
        blocks = -(-len(body) // 34)
        per = -(-len(body) // blocks)
        header, align = header * blocks, split_row(lines[1]) * blocks
        body = [sum((body[b * per + r] if b * per + r < len(body) else [""] * ncol for b in range(blocks)), []) for r in range(per)]
        ncol *= blocks
    rows = body if no_header else [header] + body
    # column widths in proportion to the longest word / text of the column (bounded), summing to the text width
    weight = []
    for j in range(ncol):
        cells = [r[j] if j < len(r) else "" for r in rows]
        longest_word = max((len(w) for c in cells for w in c.replace("`", "").split()), default=3)
        weight.append(max(min(max(len(c) for c in cells) * 0.35, 40), longest_word, 6))
    widths = [TEXT_W_CM * w / sum(weight) for w in weight]
    size = 9 if ncol <= 4 else 8
    table = doc.add_table(rows=len(rows), cols=ncol)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    set_borders(table)
    cell_margins(table)
    for i, r in enumerate(rows):
        is_head = i == 0 and not no_header
        row_props(table.rows[i], is_head)
        for j in range(ncol):
            cell = table.cell(i, j)
            cell.width = Cm(widths[j])
            par = cell.paragraphs[0]
            par.paragraph_format.space_after = par.paragraph_format.space_before = Pt(0)
            par.paragraph_format.keep_with_next = i < len(rows) - 1 and len(rows) <= 40     # a short table stays on one page
            right = j < len(align) and align[j].endswith(":")
            par.alignment = WD_ALIGN_PARAGRAPH.RIGHT if (right and not is_head) else (WD_ALIGN_PARAGRAPH.CENTER if is_head and j else WD_ALIGN_PARAGRAPH.LEFT)
            add_runs(par, (r[j] if j < len(r) else "") or " ", size=size, bold=is_head or (no_header and j == 0))   # an empty cell keeps the row height
            if is_head:
                shade(cell, HEAD_FILL)
            elif no_header and j == 0:
                shade(cell, BAND_FILL)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def add_field(par, instr: str) -> None:
    for kind, text in (("begin", None), (None, instr), ("end", None)):
        run = par.add_run()
        run.font.size = Pt(8.5)
        run.font.color.rgb = MUTED
        if kind:
            fld = OxmlElement("w:fldChar")
            fld.set(qn("w:fldCharType"), kind)
            run._r.append(fld)
        else:
            it = OxmlElement("w:instrText")
            it.set(qn("xml:space"), "preserve")
            it.text = text
            run._r.append(it)


def figure_captions(lines: list[str]) -> dict[str, str]:
    """`figures/x.png`: description  (the bullet list of the report) -> {"figures/x.png": "description"}"""
    out = {}
    for ln in lines:
        m = re.match(r"- `(figures/[^`]+)`: (.+)", ln)
        if m:
            out[m.group(1)] = m.group(2)[0].upper() + m.group(2)[1:].rstrip(".")
    return out


def build_docx(root: Path, file_name: str = "report.docx", header: bool = True, md_file: str = "report.md") -> Path:
    """``root / md_file`` -> ``root / file_name``; ``header``: the page header with the folder name."""
    lines = (root / md_file).read_text(encoding="utf-8").splitlines()
    captions = figure_captions(lines)
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(PAGE_W_CM), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(MARGIN_CM)
    sec.top_margin, sec.bottom_margin = Cm(2.0), Cm(1.8)

    normal = doc.styles["Normal"]
    normal.font.name, normal.font.size = "Calibri", Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Calibri")
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.1
    for name, size, before in (("Heading 1", 16, 16), ("Heading 2", 13, 12), ("Heading 3", 11.5, 10)):
        st = doc.styles[name]
        st.font.name, st.font.size, st.font.bold = "Calibri", Pt(size), True
        st.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)
        st.paragraph_format.space_before, st.paragraph_format.space_after = Pt(before), Pt(4)
        st.paragraph_format.keep_with_next = True
    title = doc.styles["Title"]
    title.font.name, title.font.size, title.font.bold = "Calibri", Pt(22), True
    title.font.color.rgb = RGBColor(0x1F, 0x3A, 0x5F)

    # header / footer
    if header:
        add_runs(sec.header.paragraphs[0], f"Basic_Model – {root.name}", size=8.5, color=MUTED)
    foot = sec.footer.paragraphs[0]
    foot.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    add_runs(foot, "Page ", size=8.5, color=MUTED)
    add_field(foot, "PAGE")
    add_runs(foot, " of ", size=8.5, color=MUTED)
    add_field(foot, "NUMPAGES")

    fig_n, i = 0, 0
    first_heading = True
    while i < len(lines):
        ln = lines[i]
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("#"):
            level = len(ln) - len(ln.lstrip("#"))
            text = typography(ln.lstrip("#").strip()).replace(" x ", " × ")
            if level == 1 and first_heading:
                doc.add_paragraph(text, style="Title")
                sub = doc.add_paragraph()
                add_runs(sub, f"Generated {date.today().isoformat()} from {root.name}", size=9.5, color=MUTED)
                first_heading = False
            else:
                doc.add_heading(text, level=min(level, 3))
            i += 1
        elif ln.startswith("|"):
            j = i
            while j < len(lines) and lines[j].startswith("|"):
                j += 1
            add_table(doc, lines[i:j])
            i = j
        elif ln.startswith("!["):
            m = re.match(r"!\[(.*?)\]\((.+?)\)", ln)
            path = root / m.group(2)
            if path.exists():
                fig_n += 1
                w, h = Image.open(path).size
                width_cm = min(TEXT_W_CM, MAX_FIG_H_CM * w / h)
                par = doc.add_paragraph()
                par.alignment = WD_ALIGN_PARAGRAPH.CENTER
                par.paragraph_format.keep_with_next = True
                par.paragraph_format.space_after = Pt(2)
                par.add_run().add_picture(str(path), width=Cm(width_cm))
                cap = doc.add_paragraph()
                cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
                add_runs(cap, f"Figure {fig_n}. " + captions.get(m.group(2), m.group(1)), size=9, color=MUTED)
                cap.paragraph_format.space_after = Pt(10)
            i += 1
        elif ln.startswith("- "):
            if re.match(r"- `figures/", ln):                       # the figure list: its text became the captions
                i += 1
                while i < len(lines) and lines[i].startswith("  "):
                    i += 1
                continue
            # a bullet with its wrapped lines (2 spaces), sub-bullets ("  - ", wrapped with 4 spaces) and a paragraph that continues the
            # bullet after its sub-bullets (2 spaces again)
            items, i = [["b1", ln[2:].strip()]], i + 1
            while i < len(lines) and lines[i].startswith("  "):
                s = lines[i]
                if s.startswith("  - "):
                    items.append(["b2", s[4:].strip()])
                elif s.startswith("    ") or items[-1][0] != "b2":
                    items[-1][1] += " " + s.strip()
                else:
                    items.append(["p", s.strip()])
                i += 1
            for kind, text in items:
                par = doc.add_paragraph(style="List Bullet 2" if kind == "b2" else "List Bullet" if kind == "b1" else "Normal")
                par.paragraph_format.space_after = Pt(4 if kind != "b2" else 2)
                if kind == "p":
                    par.paragraph_format.left_indent = Cm(0.63)
                add_runs(par, text)
        else:
            text, i = ln.strip(), i + 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "- ", "![")):
                text += " " + lines[i].strip()
                i += 1
            par = doc.add_paragraph()
            add_runs(par, text)
            nxt = next((x for x in lines[i:] if x.strip()), "")
            par.paragraph_format.keep_with_next = nxt.startswith("|")        # an introduction stays with its table
    out = root / file_name
    doc.save(out)
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="*", help="result folders with a report.md (default: every results/basic_model_sine_sweep* folder)")
    ap.add_argument("--name", default="report.docx", help="file name of the Word file in the result folder")
    ap.add_argument("--no-header", action="store_true", help="no page header (the page footer with the page numbers stays)")
    ap.add_argument("--md", default="report.md", help="markdown file of the result folder to convert")
    args = ap.parse_args(argv)
    roots = [Path(a) for a in args.folders] or sorted(p for p in RESULTS.glob("basic_model_sine_sweep*") if (p / "report.md").exists())
    if not roots:
        print("no report.md found")
        return 1
    for root in roots:
        print(f"wrote {build_docx(root, args.name, not args.no_header, args.md)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
