"""Render the two report sources to PDFs and preserve source/QA provenance.

Run from any directory with the repository's Python environment. This script
does not access hardware, change firmware, or implement acquisition calculators.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
from pathlib import Path
import re
import statistics
import subprocess

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    Preformatted,
)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = HERE
QA = ROOT / 'tmp' / 'pdfs' / 'adc_timing'
BLUE = colors.HexColor('#153C5B')
TEAL = colors.HexColor('#087F8C')
LIGHT = colors.HexColor('#F0F5F8')
WIDTH = 7.25 * inch

BODY = ParagraphStyle('body', fontName='Helvetica', fontSize=9.1, leading=12.2,
                      spaceAfter=7, textColor=colors.HexColor('#243342'))
H1 = ParagraphStyle('title', parent=BODY, fontSize=23, leading=27,
                    textColor=BLUE, spaceAfter=13)
H2 = ParagraphStyle('h2', parent=BODY, fontSize=14, leading=17,
                    textColor=BLUE, spaceBefore=7, spaceAfter=8, keepWithNext=True)
H3 = ParagraphStyle('h3', parent=BODY, fontSize=10.4, leading=13,
                    textColor=TEAL, spaceBefore=5, spaceAfter=6, keepWithNext=True)
CELL = ParagraphStyle('cell', parent=BODY, fontSize=8.1, leading=10.5,
                      spaceAfter=0)
HEAD = ParagraphStyle('head', parent=CELL, textColor=colors.white,
                      fontName='Helvetica-Bold')
CODE = ParagraphStyle('code', fontName='Courier', fontSize=8, leading=11,
                      spaceBefore=4, spaceAfter=8, backColor=LIGHT,
                      borderPadding=7, textColor=BLUE)


def inline(s: str) -> str:
    s = html.escape(s)
    def link(match):
        label, target = match.groups()
        if target.startswith('https://'):
            return f'<a href="{target}" color="#087F8C">{label}</a>'
        return label
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', link, s)
    s = re.sub(r'`([^`]+)`', r'<font name="Courier" size="7.6">\1</font>', s)
    return re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s)


def table(lines: list[str]) -> Table:
    rows = [[v.strip() for v in line.strip().strip('|').split('|')]
            for line in lines]
    rows = [r for r in rows if not all(re.fullmatch(r':?-+:?', c) for c in r)]
    n = len(rows[0])
    # Balance prose tables while preserving room for the first identifying column.
    scores = [max(len(re.sub(r'[*`]', '', r[i])) for r in rows) ** .45 for i in range(n)]
    scores[0] *= 1.08
    colwidths = [WIDTH * s / sum(scores) for s in scores]
    cells = [[Paragraph(inline(v), HEAD if j == 0 else CELL) for v in row]
             for j, row in enumerate(rows)]
    t = Table(cells, colWidths=colwidths, repeatRows=1, hAlign='LEFT')
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), BLUE),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 7),
        ('RIGHTPADDING', (0, 0), (-1, -1), 7),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, LIGHT]),
        ('LINEBELOW', (0, 0), (-1, 0), .7, TEAL),
        ('LINEBELOW', (0, -1), (-1, -1), .4, colors.HexColor('#CAD6DF')),
    ]))
    return t


def story(source: str) -> list:
    lines = source.splitlines()
    result = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line == '<!-- pagebreak -->':
            result.append(PageBreak()); i += 1
        elif line.startswith('```'):
            block = []; i += 1
            while not lines[i].startswith('```'):
                block.append(lines[i]); i += 1
            result.append(Preformatted('\n'.join(block), CODE)); i += 1
        elif line.startswith('|'):
            block = []
            while i < len(lines) and lines[i].startswith('|'):
                block.append(lines[i]); i += 1
            result.extend([table(block), Spacer(1, 9)])
        elif line.startswith('#'):
            level = len(line) - len(line.lstrip('#'))
            result.append(Paragraph(inline(line.lstrip('#').strip()),
                                    {1: H1, 2: H2, 3: H3}[level])); i += 1
        else:
            text = [line]; i += 1
            is_list = bool(re.match(r'(- |\d+\. )', line))
            if not is_list:
                while i < len(lines) and lines[i] and not lines[i].startswith(('#', '|', '```', '<!--')):
                    text.append(lines[i]); i += 1
            result.append(Paragraph(inline(' '.join(text)), BODY))
    return result


def footer(title):
    def draw(canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(TEAL)
        canvas.setLineWidth(.7)
        canvas.line(45, 755, 567, 755)
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(BLUE)
        canvas.drawString(45, 763, title)
        canvas.setFillColor(colors.HexColor('#62788B'))
        canvas.drawString(45, 25, '21 October 2026 | Firmware review + existing measurements')
        canvas.drawRightString(567, 25, str(doc.page))
        canvas.restoreState()
    return draw


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_sources():
    """Check source links and independently recompute reported arithmetic."""
    links = []
    for p in HERE.glob('*_CAPTURE_TIMING_REPORT.md'):
        for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', p.read_text(encoding='utf-8')):
            if not target.startswith('https://'):
                resolved = (p.parent / target).resolve()
                assert resolved.is_file(), (p.name, target)
                links.append(resolved)
    with (ROOT / 'Arduino_Sketches/TestBoard_7953/benchmarks/results/live_usb_production_adc_10_20/benchmark_results.csv').open(encoding='utf-8-sig') as f:
        rows = [r for r in csv.DictReader(f) if r['result_scope'] == 'repetition'
                and r['route_set'] == 'all_four_full' and r['spi_clock_hz'] == '20000000']
    expected = {('manual', 'blocking'): (86, 88), ('manual', 'dma'): (150, 151),
                ('manual', 'lpspi'): (58, 59), ('auto1', 'blocking'): (85, 86),
                ('auto1', 'dma'): (144, 146), ('auto1', 'lpspi'): (60, 61)}
    observed = {}
    for key, values in expected.items():
        group = [r for r in rows if (r['adcseq'], r['spiengine']) == key]
        assert len(group) == 3
        found = tuple(statistics.median(float(r[k]) for r in group)
                      for k in ['duration_median_us', 'block_period_median_us'])
        assert found == values
        observed['/'.join(key)] = found
    # ADC124 current wait-only budgets: words + CS + sensor + VMID + switching.
    conditions = {'G0': (6736, 50 + 2.8 + 14*5 + 14*.4),
                  'G4': (10000, 50 + 2.8 + 14*1 + 28*.4),
                  'G5': (5880, 50 + 2.8 + 14*1 + 14*5 + 28*.4)}
    with (ROOT / 'docs/testing/results/testboard-adc124/ghosting_manual_2026-10-20/engine_rate_comparison.csv').open(encoding='utf-8-sig') as f:
        rates = {r['condition']: r for r in csv.DictReader(f)}
    for k, (rate, _) in conditions.items():
        assert float(rates[k]['lpspi_sps']) == rate
        assert abs(float(rates[k]['lpspi_period_us']) - 1e6/rate) < 1e-9
    periods = {k: {'reciprocal_period_us': 1e6/rate, 'budget_us': budget,
                   'residual_us': 1e6/rate - budget} for k, (rate, budget) in conditions.items()}
    return links, {'7953_csv_medians_us': observed, 'adc124_rate_budgets': periods,
                   'adc124_added_wait_us': 1e6/5880 - 1e6/10000}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    QA.mkdir(parents=True, exist_ok=True)
    links, checks = verify_sources()
    head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    inputs = sorted(set(links))
    manifest = {'review_date': '2026-10-21', 'repository_head': head,
                'hardware_access': False, 'raw_binary_redecode': False,
                'source_files': [{'path': p.relative_to(ROOT).as_posix(), 'sha256': sha(p)} for p in inputs]}
    (HERE / 'capture_timing_source_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    outputs = []
    for p in sorted(HERE.glob('*_CAPTURE_TIMING_REPORT.md')):
        text = p.read_text(encoding='utf-8')
        assert text.isascii(), p
        output = OUT / (p.stem + '.pdf')
        doc = SimpleDocTemplate(str(output), pagesize=(612, 792),
                               leftMargin=45, rightMargin=45, topMargin=50, bottomMargin=44,
                               title=text.splitlines()[0][2:], author='ADC Streamer engineering review')
        doc.build(story(text), onFirstPage=footer(text.splitlines()[0][2:]),
                  onLaterPages=footer(text.splitlines()[0][2:]))
        outputs.append({'path': output.relative_to(ROOT).as_posix(), 'sha256': sha(output)})
    checks['outputs'] = outputs
    (QA / 'verification.json').write_text(json.dumps(checks, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(checks, indent=2))


if __name__ == '__main__':
    main()
