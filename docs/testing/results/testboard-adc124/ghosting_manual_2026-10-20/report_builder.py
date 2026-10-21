"""Compare the user's six manual ghosting conditions without inventing captures."""
from pathlib import Path
from datetime import datetime, timezone
from zipfile import ZipFile
from xml.etree import ElementTree as ET
from html import escape
import csv
import hashlib
import json
import re
import shutil

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import SimpleDocTemplate, Paragraph, Table, TableStyle, Spacer, PageBreak, Image

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'docs/testing/benchmarks/testboard-adc124/Ghosting Tests TestBoard_ADC124.docx'
OUT=ROOT/'docs/testing/results/testboard-adc124/ghosting_manual_2026-10-20'
PDF=ROOT/'docs/reports/exports/TestBoard_ADC124_Ghosting_Comparison_2026-10-20.pdf'
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
LSB_MV=3300/4096
CONDITIONS=[
    dict(id='G0',vmid=False,sensor_us=5,order='ADC',vmid_us=None,vmid_adc=False,sps=None,gui_interval_us=None),
    dict(id='G1',vmid=True,sensor_us=5,order='ADC',vmid_us=0,vmid_adc=True,sps=4140,gui_interval_us=5),
    dict(id='G2',vmid=True,sensor_us=5,order='MUX',vmid_us=0,vmid_adc=True,sps=5000,gui_interval_us=4),
    dict(id='G3',vmid=True,sensor_us=1,order='ADC',vmid_us=0,vmid_adc=True,sps=5375,gui_interval_us=4),
    dict(id='G4',vmid=True,sensor_us=1,order='ADC',vmid_us=0,vmid_adc=False,sps=9320,gui_interval_us=2),
    dict(id='G5',vmid=True,sensor_us=1,order='ADC',vmid_us=5,vmid_adc=False,sps=5620,gui_interval_us=4)]
LPSPI_RESULTS={
    'G0':dict(sps=6736,gui_interval_us=3),
    'G4':dict(sps=10000,gui_interval_us=2),
    'G5':dict(sps=5880,gui_interval_us=3)}


def engine_comparison():
    rows=[]
    for c in CONDITIONS:
        new=LPSPI_RESULTS.get(c['id'],{})
        old_sps=c['sps'];new_sps=new.get('sps')
        old_period=1e6/old_sps if old_sps else None
        new_period=1e6/new_sps if new_sps else None
        rows.append(dict(condition=c['id'],order=c['order'],sensor_us=c['sensor_us'],vmid=c['vmid'],
            vmid_us=c['vmid_us'],vmid_adc=c['vmid_adc'],blocking_sps=old_sps,blocking_gui_interval_us=c['gui_interval_us'],
            lpspi_sps=new_sps,lpspi_gui_interval_us=new.get('gui_interval_us'),blocking_period_us=old_period,
            lpspi_period_us=new_period,rate_increase_percent=100*(new_sps/old_sps-1) if old_sps and new_sps else None,
            period_saved_us=old_period-new_period if old_period and new_period else None,
            lpspi_ghosting='User reports unchanged' if new_sps else 'Not rerun / not reported',
            lpspi_main_amplitude='Not provided'))
    return rows


def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def text(el):
    return ' '.join(' '.join(''.join(t.text or '' for t in p.findall('.//w:t',NS)).split()) for p in el.findall('.//w:p',NS))


def write_csv(path,rows):
    with path.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)


def read_source():
    with ZipFile(SOURCE) as z:
        root=ET.fromstring(z.read('word/document.xml'))
        assert not [n for n in z.namelist() if n.startswith('word/media/')]
        comments=[n for n in z.namelist() if 'comments' in n]
    body=root.find('w:body',NS)
    blocks=[];tables=[]
    for block in body:
        if block.tag.endswith('}tbl'):
            rows=[[text(c) for c in row.findall('w:tc',NS)] for row in block.findall('w:tr',NS)]
            tables.append(rows);blocks.append(dict(kind='table',table=len(tables)-1,rows=rows))
        else:
            value=''.join(t.text or '' for t in block.findall('.//w:t',NS))
            if value.strip():blocks.append(dict(kind='paragraph',text=value))
    assert len(tables)==7 and all(len(t)==9 for t in tables)
    records=[]
    for cond,table in zip(CONDITIONS,tables[1:]):
        assert len(table[0])==6 and table[0][4]=='Results'
        for row in table[1:]:
            a=int(row[0]);m=int(re.match(r'\d+',row[1])[0]);resistor=470000 if '470' in row[1] else 1000000
            mv=float(re.match(r'\s*([\d.]+)',row[5])[1])
            note=row[4];limit=re.search(r'<\s*([\d.]+)\s*%',note);approx=re.search(r'\(([\d.]+)%\)',note)
            if note=='No ghost':kind='not_observed';pct=None;short='ND'
            elif limit:kind='visual_upper_estimate';pct=float(limit[1]);short=f'<{pct:g}%'+(' N' if 'noise pattern' in note else '')
            elif approx:kind='visual_approximation';pct=float(approx[1]);short=f'~{pct:g}%'
            else:raise ValueError(note)
            records.append(dict(condition=cond['id'],array=a,mux=m,bias_ohms=resistor,pressed=row[2],tested=row[3],
                observation=note,ghost_kind=kind,ghost_percent=pct,ghost_short=short,main_pp_mV=mv,main_pp_volts=mv/1000,
                main_pp_counts_nominal=mv/LSB_MV,
                illustrative_ghost_mV=None if pct is None else mv*pct/100,
                illustrative_ghost_counts=None if pct is None else mv*pct/100/LSB_MV))
    assert len(records)==48 and len({(r['condition'],r['array'],r['mux']) for r in records})==48
    for r in records:
        expected=[[470000,1000000,470000,1000000],[1000000,470000,1000000,470000]][r['array']-1][r['mux']-1]
        assert r['bias_ohms']==expected
        assert 'ch4' in r['pressed'] and 'ch5' in r['tested']
    defaults=json.loads((ROOT/'config/boards/profiles/testboard_adc124.json').read_text())['modes']['PZT']['parameters']
    assert defaults['spi_engine']['default']=='lpspi'
    assert 'SpiEngine engine = SpiEngine::LPSPI;' in (ROOT/'Arduino_Sketches/TestBoard_ADC124/src/main.cpp').read_text()
    assert sum(r['ghost_kind']=='not_observed' for r in records)==29
    source=dict(path=str(SOURCE.relative_to(ROOT)),sha256=digest(SOURCE),blocks=blocks,
        tables=tables,comments_parts=comments,tracked_insertions=len(root.findall('.//w:ins',NS)),
        tracked_deletions=len(root.findall('.//w:del',NS)),conditions=CONDITIONS,records=records,
        user_clarifications=['SPS and sample interval copied from GUI Sampling Rate panel; SPS is per-channel rate.',
            'User confirms original DOCX tests used blocking SPI and supplies three LPSPI rerun rates in a chat image on 20 October 2026.',
            'Ghosting reported unchanged for the LPSPI reruns; no new per-route amplitudes or detailed ghosting table supplied.',
            'User uses the same sensor strip on both arrays, moving it between arrays for each test.'],
        engine_default='lpspi',original_test_engine='blocking',
        engine_historical_runtime='Engines confirmed by user; no saved hardware status log',
        strip_assignment='Same sensor strip moved between arrays for each test, confirmed by user',
        lpspi_rerun_source='User-supplied chat image and confirmation, 20 October 2026; original DOCX unchanged',
        engine_comparison=engine_comparison(),test_date='Not stated in source; report updated 20 October 2026')
    return source


def plots(records):
    plt.rcParams.update({'font.size':10,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False,'axes.spines.right':False})
    fig,ax=plt.subplots(figsize=(10.8,3.5),constrained_layout=True)
    rr=CONDITIONS[1:];bars=ax.bar([r['id'] for r in rr],[r['sps'] for r in rr],color=['#4c78a8','#72b7b2','#4c78a8','#f58518','#54a24b'])
    for b,r in zip(bars,rr):ax.text(b.get_x()+b.get_width()/2,r['sps']+120,f'{r["sps"]:,}',ha='center',fontsize=10)
    ax.set(ylabel='GUI per-channel rate (samples/s)',xlabel='Condition ID',ylim=(0,10600))
    fig.savefig(OUT/'rate_comparison.png',dpi=190,bbox_inches='tight');plt.close(fig)
    fig,ax=plt.subplots(figsize=(11.7,2.7),constrained_layout=True)
    xx=np.arange(len(CONDITIONS));w=.35
    old=[c['sps'] if c['sps'] else np.nan for c in CONDITIONS]
    new=[LPSPI_RESULTS.get(c['id'],{}).get('sps',np.nan) for c in CONDITIONS]
    for offset,values,colour,name in ((-w/2,old,'#4c78a8','Blocking'),(w/2,new,'#f58518','LPSPI')):
        bars=ax.bar(xx+offset,values,w,color=colour,label=name)
        for j,b in enumerate(bars):
            if np.isfinite(values[j]):ax.text(b.get_x()+w/2,values[j]+130,f'{int(values[j]):,}',ha='center',fontsize=9)
            else:ax.text(b.get_x()+w/2,150,'NR',ha='center',fontsize=8,color='#555555')
    ax.set(xticks=xx,xticklabels=[c['id'] for c in CONDITIONS],ylabel='GUI per-channel rate (samples/s)',xlabel='Condition ID',ylim=(0,11200))
    ax.legend(loc='upper left')
    fig.savefig(OUT/'engine_rate_comparison.png',dpi=190,bbox_inches='tight');plt.close(fig)
    fig,axs=plt.subplots(1,2,figsize=(11.7,4),constrained_layout=True)
    for a,ax in enumerate(axs,1):
        for m in range(1,5):
            rr=[next(r for r in records if r['condition']==c['id'] and r['array']==a and r['mux']==m) for c in CONDITIONS]
            ax.plot([r['condition'] for r in rr],[r['main_pp_mV'] for r in rr],'o-',label=f'MUX {m}',ms=5)
        ax.set(title=f'Array {"AB"[a-1]}',xlabel='Condition ID',ylabel='Main signal peak-to-peak (mV)',ylim=(0,100 if a==1 else 285))
    fig.legend(*axs[0].get_legend_handles_labels(),loc='outside lower center',ncol=4)
    fig.savefig(OUT/'main_amplitude_comparison.png',dpi=190,bbox_inches='tight');plt.close(fig)


def make_report(source):
    rows=source['records'];lookup={(r['condition'],r['array'],r['mux']):r for r in rows}
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Body2',fontName='Helvetica',fontSize=10.4,leading=14.3,spaceAfter=8))
    styles.add(ParagraphStyle(name='Small2',fontName='Helvetica',fontSize=8.8,leading=11.7,spaceAfter=7))
    styles.add(ParagraphStyle(name='Cell2',fontName='Helvetica',fontSize=8.4,leading=11.1))
    for key in ('Title','Heading1','Heading2'):styles[key].textColor=colors.black
    width,height=landscape(A4);usable=width-84;story=[]
    def p(s,small=False):story.append(Paragraph(s,styles['Small2' if small else 'Body2']))
    def page(title):
        if story:story.append(PageBreak())
        story.append(Paragraph(title,styles['Heading1']))
    def label(s):story.append(Paragraph(s,styles['Heading2']))
    def table(headers,data,widths=None):
        cell=lambda s:Paragraph(escape(str(s)),styles['Cell2'])
        t=Table([[cell(h) for h in headers]]+[[cell(v) for v in r] for r in data],colWidths=widths or [usable/len(headers)]*len(headers),repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e3edf4')),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f6f8fa')]),
            ('GRID',(0,0),(-1,-1),.35,colors.HexColor('#d9d9d9')),('VALIGN',(0,0),(-1,-1),'MIDDLE'),
            ('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),
            ('TOPPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
        story.extend([t,Spacer(1,9)])
    def image(name,maxheight):
        from PIL import Image as PI
        path=OUT/name
        with PI.open(path) as im:w,h=im.size
        scale=min(usable/w,maxheight/h)
        story.extend([Image(str(path),width=w*scale,height=h*scale),Spacer(1,7)])
    def rate_delta(a,b):return 100*(CONDITIONS[b]['sps']/CONDITIONS[a]['sps']-1)
    def cfg(c):return f'{c["order"]} scan, sensor settle {c["sensor_us"]} us, VMID '+('off' if not c['vmid'] else f'{c["vmid_us"]} us, VMID ADC scan '+('on' if c['vmid_adc'] else 'off'))

    page('TestBoard ADC124 manual ghosting comparison')
    p('<b>Report updated 20 October 2026.</b> Six original blocking-SPI conditions with 48 array/MUX observations, plus three LPSPI rerun rates supplied by the user. The original notes do not specify acquisition dates or repeated-press counts.')
    label('Main conclusions')
    p('Visiting VMID between external MUX addresses is the strongest observed ghost-suppression measure. With VMID off, Array A shows approximately 90% ghosting on every MUX; Array B shows approximately 30-50%. VMID-on conditions reduce this to no visible ghost or small reported residuals.')
    p('ADC conversions while connected to VMID are not essential for substantial suppression in these tests. The wait-only conditions G4 and G5 both suppress the large ghosts. A programmed VMID delay of 0 us still includes a finite reference visit and firmware switching time.')
    p('G4 is the fastest reported condition at 10,000 samples/s per channel with LPSPI, versus 9,320 with blocking (+7.3%). In the original blocking tests its Array B main signals were 2-5 times smaller than in G3. Manual excitation was uncontrolled, and LPSPI main-signal amplitudes were not supplied, so signal preservation still needs checking.')
    p('G5 is a conservative validation candidate: ADC scan, 1 us sensor settle, 5 us VMID settle, no ADC scan at VMID. It reaches 5,880 samples/s per channel with LPSPI versus 5,620 with blocking (+4.6%). Original blocking observations had larger Array B signals and trace residuals; the user reports unchanged ghosting in the LPSPI reruns.')
    label('Engine verification')
    p('The GUI board profile and firmware have now been changed to default to <b>LPSPI</b>, verified from current source. The user confirms that the original DOCX results were blocking-SPI tests and that the three new rates are LPSPI reruns. No saved device-status logs accompany either session.')
    p('The percentages are visual estimates from manual presses. "No ghost" means none was observed under those conditions; it is not a measured zero or a calibrated rejection limit.',True)

    page('Test method and physical channel mapping')
    p('Each full octo sensor TOP element was pressed manually while observing the adjacent Octo 7 element on the same external MUX. This tests the transition from external MUX address 4 to address 5. MUX 4 also has an Octo 7 sensor at address 6, allowing observation of a second following address. SPI clock was 16 MHz throughout.')
    table(['PCB array','MUX / ADC input','Bias resistor','Pressed sensor at address 4','Observed Octo 7 sensor'],[
        ['AB'[int(r[0])-1],re.match(r'\d+',r[1])[0], '470 kohm' if '470' in r[1] else '1 Mohm',r[2].replace('(ch4)',''),r[3].replace('ch5','address 5').replace('ch6','address 6').replace('_ L','_L')] for r in source['tables'][0][1:]], [64,105,89,220,usable-478])
    p('<b>Terminology:</b> source "ch4/ch5/ch6" labels are external MUX addresses 4/5/6. MUX number 1-4 identifies a chip and its corresponding ADC input IN1-IN4. These are separate selections. Array 1 = Array A; Array 2 = Array B.')
    p('ADC scan completes one array before scanning the other. MUX scan switches both arrays to the same address together and shares the settling wait, then reads the ADCs sequentially. These are the intended firmware semantics; the historical test binary was not archived with the notes.',True)
    p('The user confirms that the same sensor strip was moved between Array A and Array B for each test. This removes different-strip identity as an explanation for the array comparison, while manual pressure still varies. The source notes that coupling among elements of the same sensor is difficult to distinguish from mechanical coupling.',True)

    page('Original blocking test conditions and timing')
    table(['ID','Scan order','Sensor settle us','VMID settle us','ADC scan at VMID','GUI per-channel SPS','GUI Sample Interval us','Per-channel period us'],[
        [c['id'],c['order'],c['sensor_us'],'off' if not c['vmid'] else c['vmid_us'],'n/a' if not c['vmid'] else 'yes' if c['vmid_adc'] else 'no',c['sps'] or 'not stated',c['gui_interval_us'] or 'not stated','not available' if not c['sps'] else f'{1e6/c["sps"]:.2f}'] for c in CONDITIONS], [41,65,83,82,104,112,102,usable-589])
    image('rate_comparison.png',190)
    p('<b>Legend:</b> bars are the original blocking GUI per-channel rates. G0 has no recorded blocking rate. The channel period is calculated as 1,000,000 / SPS; it is not the GUI Sample Interval.',True)
    p('The user confirmed the values came from the GUI Sampling Rate panel. Current GUI code displays Sample Interval from the MCU average time per serialized sample, while per-channel rate comes from measured sweep timing. With 50 acquired channels, the reported 2-5 us intervals are consistent with approximate average code spacing; one sensor is revisited every 107-242 us in G1-G5.',True)

    page('Blocking and LPSPI sampling comparison')
    p('The user reran G0, G4 and G5 with LPSPI and reports unchanged ghosting. Rates and GUI intervals below reproduce the supplied comparison table. Empty cells mean no result was supplied; no missing rate is inferred.')
    table(['ID','Blocking SPS per channel','Blocking GUI interval us','LPSPI SPS per channel','LPSPI GUI interval us','Rate increase percent','Cycle time saved us'],[
        [r['condition'],r['blocking_sps'] or '',r['blocking_gui_interval_us'] or '',r['lpspi_sps'] or '',r['lpspi_gui_interval_us'] or '',
         '' if r['rate_increase_percent'] is None else f'{r["rate_increase_percent"]:+.2f}',
         '' if r['period_saved_us'] is None else f'{r["period_saved_us"]:.2f}'] for r in source['engine_comparison']],
        [41,110,125,110,125,118,129])
    image('engine_rate_comparison.png',172)
    p('<b>Legend:</b> blue = blocking, orange = LPSPI. NR marks a missing measurement, not zero speed. Compare identical condition IDs across engines; G0 and G4 have different sensor waits, so their rates do not isolate VMID overhead.',True)
    p('For G4 and G5, LPSPI saves 7.30 and 7.87 us per cycle. The smaller percentage gain with a 5 us VMID wait is consistent with a larger share of fixed analog waiting. Unchanged visible ghosting supports treating engine choice mainly as a throughput change in these reruns; it does not establish identical waveforms or amplitudes.',True)

    page('Original blocking ghosting observations')
    table(['Condition','A MUX 1','A MUX 2','A MUX 3','A MUX 4','B MUX 1','B MUX 2','B MUX 3','B MUX 4'],[
        [c['id']]+[lookup[(c['id'],a,m)]['ghost_short'] for a in (1,2) for m in range(1,5)] for c in CONDITIONS], [69]+[(usable-69)/8]*8)
    p('<b>Legend:</b> ND = source says "No ghost"; no numerical detection threshold was measured. ~ = approximate visual fraction. &lt;1%, &lt;2% and &lt;5% reproduce the reported visual estimates, not statistical upper confidence bounds. N = source also reports a changed noise pattern.')
    p('The detailed matrix is from the original blocking notes. The user reports unchanged ghosting for the LPSPI G0/G4/G5 reruns; no additional per-path numerical ghost measurements were supplied.',True)
    label('What the comparison establishes')
    p('G0 to G1 is the closest VMID-off/on comparison: both use ADC scan and 5 us sensor settling. Array A changes from approximately 90% on all four MUXes to ND. Array B changes from approximately 30-50% to ND on three MUXes and &lt;5% on MUX 3. VMID adds switching and conversion time, so this comparison establishes the benefit of the complete VMID visit rather than isolating the reference capacitor.')
    p('Array B / MUX 3 is the persistent residual path: a small ghost is noted in every VMID-on condition. Array A / MUX 3 has a trace observation only in G2. Source remarks about a changed noise pattern on B / MUX 4 should remain separate from a measured ghost amplitude.')
    p('With VMID off, A / MUX 4 also shows ghosting on the second following sensor at external address 6. B / MUX 4 shows hardly any ghost there. This is consistent with a longer scan-memory tail on A, although the second-address magnitude was not quantified.')

    page('Original blocking main signal amplitudes')
    image('main_amplitude_comparison.png',290)
    p('<b>Legend:</b> colour = MUX number. Points are original blocking main-signal peak-to-peak amplitudes in mV, not ghost amplitude or noise. No LPSPI amplitudes were supplied. Condition IDs are categorical; lines only help follow a MUX across tests. Presses were not identical or force-controlled.',True)
    table(['Array B path','G3 with ADC scan mV p-p','G4 no ADC scan mV p-p','G4 / G3','G5 with 5 us VMID mV p-p'],[
        [f'MUX {m}',lookup[('G3',2,m)]['main_pp_mV'],lookup[('G4',2,m)]['main_pp_mV'],f'{lookup[("G4",2,m)]["main_pp_mV"]/lookup[("G3",2,m)]["main_pp_mV"]:.2f}',lookup[('G5',2,m)]['main_pp_mV']] for m in range(1,5)])
    p('The smaller G4 signals may result from different presses, acquisition-history effects or reduced measured response. The notes cannot distinguish these explanations. A lower visible ghost fraction at a smaller signal can also approach the ADC/noise floor; speed alone is insufficient to choose G4.',True)

    # Remaining interpretation, references and source tables are appended below.
    append_interpretation(source,story,p,page,label,table,lookup,rate_delta,cfg)
    def footer(canvas,doc):
        canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#586676'))
        canvas.drawString(42,21,'TestBoard ADC124 | Manual ghosting comparison | 20 October 2026')
        canvas.drawRightString(width-42,21,str(doc.page))
    PDF.parent.mkdir(parents=True,exist_ok=True)
    SimpleDocTemplate(str(PDF),pagesize=(width,height),leftMargin=42,rightMargin=42,topMargin=34,bottomMargin=37,
        title='TestBoard ADC124 manual ghosting comparison',author='TestBoard ADC124 analysis').build(story,onFirstPage=footer,onLaterPages=footer)


def append_interpretation(source,story,p,page,label,table,lookup,rate_delta,cfg):
    page('Effects of settling within the blocking tests')
    pairs=[('G1 to G2',1,2,'ADC scan to MUX scan'),('G1 to G3',1,3,'Sensor settle 5 to 1 us'),
           ('G3 to G4',3,4,'Remove ADC scan at VMID'),('G4 to G5',4,5,'Add 5 us wait at VMID'),
           ('G3 to G5',3,5,'Replace VMID ADC scan by 5 us wait')]
    table(['Comparison','Changed setting','Per-channel rate before / after','Rate change','Period change us'],[
        [name,change,f'{CONDITIONS[a]["sps"]:,} / {CONDITIONS[b]["sps"]:,}',f'{rate_delta(a,b):+.1f}%',f'{1e6/CONDITIONS[b]["sps"]-1e6/CONDITIONS[a]["sps"]:+.2f}'] for name,a,b,change in pairs], [90,235,164,90,95])
    p('<b>Scan order G1 to G2.</b> Shared MUX settling increases the recorded rate by 20.8% at 5 us sensor settle. Both retain strong suppression, but G2 adds a trace observation on A / MUX 3 and still reports &lt;5% on B / MUX 3. The notes do not test MUX scan at 1 us or without VMID conversions.')
    p('<b>Sensor settle G1 to G3.</b> Reducing the programmed sensor wait to 1 us increases rate by 29.8% without a large observed ghost returning. B / MUX 3 changes from &lt;5% to &lt;1%, while B / MUX 4 has a trace noise-pattern observation. These qualitative observations do not establish that shorter settling improves analog accuracy.')
    p('<b>VMID conversions G3 to G4.</b> Removing them increases rate by 73.4%. Array A remains ND; Array B has reported residuals below 1-2%. The concurrent decrease in major-signal amplitudes prevents interpreting this as a demonstrated fidelity improvement.')
    p('<b>VMID wait G4 to G5.</b> Rate decreases by 39.7%, but observed Array B signals return to 150-250 mV on MUXes 1-3 and 200 mV on MUX 4. B / MUXes 1-2 become ND. G5 is also 4.6% faster than G3 and has the same reported ghost categories, making wait-only VMID a credible alternative to VMID conversions.')
    p('Calculated G4/G5 periods are 107.30 and 177.94 us, a difference of 70.64 us. If all 50 sensor routes are scanned over seven addresses per array, ADC order has 14 VMID visits: 14 x 5 us = 70 us. This close match supports the intended timing interpretation; it is not an independent GPIO timing measurement.',True)
    p('The LPSPI G4/G5 periods are 100.00 and 170.07 us, giving 70.07 us for the same added wait. This independently reproduces the expected 70 us contribution at GUI precision. G5\'s rounded GUI interval changes from 4 to 3 us across engines, but the per-channel SPS gain is 4.6%, not 33%; use SPS-derived periods for the engine comparison.',True)

    page('Electrical interpretation of the array differences')
    label('The results support scan memory as a major contributor')
    p('A previous sensor can leave charge on a MUX common node and associated buffer/input capacitances. Connecting the next high-impedance sensor can redistribute that charge and shift the next reading. A VMID visit drives the shared path toward a known reference before the next sensor is connected. This is a plausible explanation for the strong address-4 to address-5 dependence and the improvement with wait-only VMID.')
    p('The data do not locate the entire effect in one component. MUX parasitics, OPA365 response, sensor/cable capacitance and ADC acquisition history can contribute. Removing VMID ADC conversions shows they are not necessary for substantial suppression under the tested conditions; it does not prove the ADC contributes nothing.')
    table(['Typical datasheet parameter at 3.3 V','Array A TMUX1108','Array B TMUX1308A'],[
        ['On capacitance', '65 pF','11 pF'],['On resistance','4 ohm','120 ohm'],
        ['Sensor-side off-state leakage magnitude at 25 C','1 pA','1 nA']], [350,200,208])
    p('The larger stated on capacitance of TMUX1108 makes greater capacitive memory on A plausible. Its lower off-state leakage also preserves stored charge more effectively. TMUX1308A can have less capacitive memory while losing more stored sensor voltage during long isolation. Thus strong ghosting on A and stronger long-hold discharge on B can coexist. This is an interpretation consistent with both test sets, not a component-level proof. [2, 3]')
    p('These are typical manufacturer values under their specified test conditions, not measured PCB capacitance or leakage. The leakage test voltages differ. On resistance alone does not predict ghosting into a capacitive, high-impedance sensor path. The user confirms the same strip was used on both arrays for these manual tests, strengthening the board-path comparison while leaving manual-excitation variability.',True)
    label('The resistor comparison is inconclusive')
    p('Both 470 kohm and 1 Mohm paths on Array A show approximately 90% ghosting with VMID off. On B, only MUX 1 is estimated at 50%; the other three are 30%. These observations do not isolate a consistent resistor-value effect because MUX position, individual sensor and applied pressure vary together.')

    page('Configuration choices and required confirmation')
    label('Use G5 as the conservative validation starting point')
    p('Keep SPI at 16 MHz, ADC scan, 1 us sensor settle, VMID enabled with a 5 us wait, and no ADC conversions at VMID. The reported rate is 5,880 samples/s per channel with LPSPI, versus 5,620 with blocking. Original blocking results combine larger main signals and trace residuals on B / MUXes 3-4; the LPSPI rerun reportedly has unchanged ghosting. Confirm signal preservation with LPSPI before treating this as a validated production setting.')
    label('Treat G4 as the speed candidate')
    p('G4 reaches 10,000 samples/s per channel with LPSPI, versus 9,320 with blocking, at 1 us sensor settle and no explicit VMID delay or VMID conversions. The user reports unchanged ghosting across engines. Before selecting it, verify main amplitude, waveform and integrated response under identical excitation. A 0 us programmed wait leaves finite analog dwell from switching and firmware execution.')
    label('Run the following comparisons with controlled excitation')
    p('<b>1.</b> Compare G3, G4 and G5 using a repeatable actuator trajectory or calibrated charge injection that preserves representative sensor impedance. Record the pressed path and all other paths together. Use at least ten events per condition and reverse or randomize condition order to expose drift.')
    p('<b>2.</b> Measure positive and negative ghost waveforms relative to each victim baseline, including delayed responses at following addresses. Compute a consistent ghost-to-source amplitude ratio, main-signal retention and trial-to-trial variation. Record pre-event noise to establish a detection limit.')
    p('<b>3.</b> Repeat selected tests with scan order reversed or with the stimulus address changed. A response that follows read order supports electrical scan memory; one that follows physical neighbours may include mechanical coupling. Fixed capacitors or unconnected dummy channels help separate the two.')
    p('<b>4.</b> Sweep wait-only VMID delay through 0, 0.5, 1, 2 and 5 us where firmware resolution permits. Test the MUX scan equivalent of G5. Log actual requested and achieved timing, active SPI engine, firmware identity, all routes and strip assignment. Keep excitation and conditioning history fixed.')

    page('Measurement limits units and references')
    p('Manual presses vary in force, displacement and speed. The document contains observations and main-signal peak-to-peak values, without raw traces, event counts, uncertainty intervals or a quantitative ghost-detection floor. It cannot establish statistically significant differences among sub-percent categories, a complete 50-by-50 ghost matrix, noise SD/PSD, or calibrated signal gain.')
    p('At the nominal 3.3 V reference, one 12-bit ADC count is 0.805664 mV. A visually estimated 1% ghost on a 40 mV main signal corresponds to only 0.4 mV, about 0.50 count; on a 250 mV signal it corresponds to 2.5 mV, about 3.10 counts. These products illustrate scale only. The source does not establish that its percentages use identical peak-to-peak windows, so they are not validated voltage bounds.')
    p('The result tables use ND for an absent visible ghost, never zero percent. Statements such as &lt;1% retain their original observational status. Main-signal peak-to-peak amplitudes must not be relabelled as ghost amplitudes or noise levels. G0\'s blocking rate is missing; its LPSPI rate is 6,736 samples/s. LPSPI rates for G1-G3 and all LPSPI main amplitudes remain unreported.')
    label('Sources and verification')
    p('[1] Ghosting Tests TestBoard_ADC124.docx, supplied manual notes: one mapping table, six result tables, 48 result rows. The appendix reproduces every observation and main-signal value. Source SHA-256: '+source['sha256']+'.',True)
    p('[2] Texas Instruments, TMUX1108 datasheet, sections 6.6 and 7.3.6. '
      '<link href="https://www.ti.com/lit/ds/symlink/tmux1108.pdf" color="#1e5a80">TI TMUX1108 datasheet</link>. '
      '[3] Texas Instruments, TMUX1308A datasheet, sections 6.6 and 7.7. '
      '<link href="https://www.ti.com/lit/ds/symlink/tmux1308a.pdf" color="#1e5a80">TI TMUX1308A datasheet</link>.',True)
    p('[4] Repository rechecked 20 October 2026 after the user update: config/boards/profiles/testboard_adc124.json now defaults to lpspi; Arduino_Sketches/TestBoard_ADC124/src/main.cpp initializes SpiEngine::LPSPI. The earlier blocking defaults and their file hashes are preserved in report_revisions/blocking_only. Current firmware uses wait-only VMID, so one present-day binary does not describe every historical row.',True)
    p('[5] data_processing/timing_display.py, update_timing_display: Sample Interval displays the MCU average sample time; per-channel rate uses actual sweep timing. User clarification confirms the document values were copied from that GUI panel. No board configuration was changed or hardware test run for this report.',True)
    p('[6] User chat image and clarification, 20 October 2026: original tests used blocking; LPSPI reruns G0 = 6,736 SPS / 3 us, G4 = 10,000 SPS / 2 us, G5 = 5,880 SPS / 3 us; ghosting unchanged. The user also confirms moving the same strip between arrays for each test. These additions are stored separately from the unchanged DOCX observations.',True)

    for cond,raw_table in zip(CONDITIONS,source['tables'][1:]):
        page(f'Appendix {cond["id"]} complete manual observations')
        p('<b>Original blocking parameters:</b> '+cfg(cond)+'. SPI 16 MHz. '+('Blocking rate not recorded.' if not cond['sps'] else f'Blocking GUI rate {cond["sps"]:,} samples/s per channel; GUI Sample Interval {cond["gui_interval_us"]} us.'))
        if cond['id'] in LPSPI_RESULTS:
            new=LPSPI_RESULTS[cond['id']]
            p(f'<b>LPSPI rerun:</b> {new["sps"]:,} samples/s per channel; GUI interval {new["gui_interval_us"]} us; ghosting reported unchanged. The detailed amplitudes and observations below remain the original blocking measurements.',True)
        rr=[lookup[(cond['id'],a,m)] for a in (1,2) for m in range(1,5)]
        table(['Array','MUX / bias','Pressed external address 4','Observed external address 5 or 6','Source ghost observation','Main signal mV p-p'],[
            ['AB'[r['array']-1],f'{r["mux"]} / '+('470k' if r['bias_ohms']==470000 else '1M'),
             r['pressed'].replace('(ch4)','').strip(),r['tested'].replace('ch5','addr 5').replace('ch6','addr 6').replace('_ L','_L'),
             r['observation'],f'{r["main_pp_mV"]:g}'] for r in rr], [45,80,157,161,231,84])
        p('The source observations are reproduced without assigning numerical zeros to "No ghost". MUX number identifies the same-numbered ADC input. "Second channel" in the MUX 4 remarks means the second following external address, address 6, rather than ADC IN2.',True)
        if cond['id']=='G0':
            p('Conclusion: strong ghosts occur on every observed path, with a longer visible tail on Array A / MUX 4. The signal amplitudes vary across manual presses, so percentages should be treated as approximate.')
        elif cond['id']=='G1':
            p('Conclusion: adding the VMID visit strongly suppresses the G0 ghosts at unchanged 5 us sensor settle. B / MUX 3 retains a small reported residual below 5%. Zero programmed VMID wait includes the additional time spent performing ADC conversions at VMID.')
        elif cond['id']=='G2':
            p('Conclusion: shared MUX settling raises the recorded rate versus G1. A / MUX 3 has a trace observation and B / MUX 3 remains below 5%. Lower main amplitudes on several paths cannot be assigned to scan mode without repeatable excitation.')
        elif cond['id']=='G3':
            p('Conclusion: 1 us sensor settle retains strong suppression and raises rate versus G1. The two residual observations are on B / MUXes 3-4. This establishes useful behaviour in this manual test, not full analog settling or calibrated accuracy.')
        elif cond['id']=='G4':
            p('Conclusion: substantial suppression survives without VMID ADC conversions, at the highest recorded rate. All four B paths have trace observations and smaller main signals than G3. Confirm main-signal preservation with a controlled input before adopting this condition.')
        else:
            p('Conclusion: a 5 us reference wait without VMID ADC conversions produces larger observed main signals on B and removes its MUX 1-2 visible residuals relative to G4. It is a conservative validation candidate; tiny remaining observations are not calibrated rejection limits.')


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    source=read_source()
    (OUT/'source_records.json').write_text(json.dumps(source,indent=2,ensure_ascii=False),encoding='utf-8')
    write_csv(OUT/'manual_observations.csv',source['records'])
    write_csv(OUT/'conditions.csv',CONDITIONS)
    write_csv(OUT/'engine_rate_comparison.csv',source['engine_comparison'])
    (OUT/'lpspi_rerun_source.json').write_text(json.dumps(dict(source=source['lpspi_rerun_source'],results=LPSPI_RESULTS,
        ghosting='User reports unchanged for the three rerun conditions',main_amplitudes='Not supplied',
        original_engine='blocking',current_default_engine='lpspi'),indent=2))
    plots(source['records'])
    make_report(source)
    assert digest(SOURCE)==source['sha256']
    inputs=[SOURCE,ROOT/'config/boards/profiles/testboard_adc124.json',ROOT/'Arduino_Sketches/TestBoard_ADC124/src/main.cpp',ROOT/'data_processing/timing_display.py',Path(__file__)]
    verification=dict(created_utc=datetime.now(timezone.utc).isoformat(),source_unchanged=True,source_tables=7,
        result_conditions=6,observations=48,all_manual_rows_transcribed=True,no_ghost_not_encoded_as_zero=True,
        gui_default_engine='lpspi',firmware_default_engine='lpspi',original_test_engine='blocking',
        lpspi_rerun_rates=3,engines_confirmed_by_user=True,historical_engine_runtime_verified=False,
        hardware_tests_run=False,pdf=dict(path=str(PDF.relative_to(ROOT)),sha256=digest(PDF),visual_review_pending=True),
        input_sha256={str(p.relative_to(ROOT)):digest(p) for p in inputs})
    (OUT/'verification.json').write_text(json.dumps(verification,indent=2))
    shutil.copy2(__file__,OUT/'report_builder.py')
    (OUT/'README.md').write_text('''# Manual ghosting comparison updated 20 October 2026

The original DOCX contains six blocking-SPI conditions and 48 array/MUX observations.
The user subsequently supplied three LPSPI rerun rates and reported unchanged ghosting.
The same sensor strip was moved between arrays for each test. Manual presses were uncontrolled.

## Engine comparison

- G0, ADC scan, sensor settle 5 us, VMID off: LPSPI 6,736 SPS/channel; blocking rate missing.
- G4, ADC scan, sensor settle 1 us, VMID wait 0 us, no VMID ADC scan: blocking 9,320 to LPSPI 10,000 SPS/channel (+7.30%).
- G5, ADC scan, sensor settle 1 us, VMID wait 5 us, no VMID ADC scan: blocking 5,620 to LPSPI 5,880 SPS/channel (+4.63%).
- LPSPI G1-G3 rates and all new main-signal amplitudes were not supplied; missing values remain blank.
- Current GUI and firmware defaults are now LPSPI. The original tests were blocking, confirmed by the user.
- Ghosting is qualitatively unchanged for the reruns; this does not establish equal main-signal fidelity.
- The 5 us wait adds 70.64 us per blocking cycle and 70.07 us per LPSPI cycle, consistent with 14 shared-within-array waits in ADC order.

## Conclusions and evidence

VMID visits strongly suppress the large observed ghosts. VMID ADC conversions are not essential for substantial suppression.
G5 remains the conservative validation candidate at 5,880 LPSPI SPS/channel. G4 reaches 10,000 but still needs a matched-input amplitude check.
No ghost is not encoded as zero percent. Source DOCX unchanged; no hardware configuration or acquisition was performed for this revision.

- source_records.json: original source paragraphs/tables, 48 records and subsequent user clarifications.
- manual_observations.csv: original blocking observations and amplitudes only.
- conditions.csv: original blocking configuration/rate data.
- engine_rate_comparison.csv and lpspi_rerun_source.json: new engine results and provenance.
- report_revisions/blocking_only: preserved previous PDF, builder, verification and analysis files.
- verification.json and pdf_quality.json: input hashes, table checks and visual QA.
- PDF: docs/reports/exports/TestBoard_ADC124_Ghosting_Comparison_2026-10-20.pdf.

Rebuild: `.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_ghosting.py`
''')
    print(json.dumps(verification,indent=2))


if __name__=='__main__':main()
