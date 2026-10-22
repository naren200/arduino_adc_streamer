"""Explanation-first composition of the measured PCB diagnostics report.

Acquisition and statistics are unchanged. Each test has a procedure page followed
by its evidence, a complete legend and a bounded interpretation.
"""
from pathlib import Path
import csv,json
from xml.sax.saxutils import escape
import numpy as np
from reportlab.platypus import SimpleDocTemplate,Paragraph,Table,TableStyle,Image,PageBreak,Spacer
from reportlab.platypus.tableofcontents import TableOfContents
from reportlab.lib.pagesizes import A4,landscape
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.lib import colors

LSB=3.3/4096

class ReaderDoc(SimpleDocTemplate):
    def afterFlowable(self,flowable):
        if isinstance(flowable,Paragraph) and flowable.style.name=='Chapter':
            key=flowable.bookmark
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.getPlainText(),key,0)
            self.notify('TOCEntry',(0,flowable.getPlainText(),self.page,key))

def write_report(x,out,pdf,figdir,noise,spikes,correlations,timing):
    out,figdir=Path(out),Path(figdir)
    extension_folder=out.parent/'vmid_long_hold_2026-10-15'
    extension_path=extension_folder/'analysis.json'
    extension=json.loads(extension_path.read_text()) if extension_path.exists() else None
    if extension:
        assert not extension['partial'] and len(extension['captures'])==63
        assert extension['lifecycle']['state']=='production_restored_verified'
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle('Text',fontName='Helvetica',fontSize=10,leading=14,spaceAfter=8))
    styles.add(ParagraphStyle('Fine',fontName='Helvetica',fontSize=8.4,leading=11,spaceAfter=6))
    styles.add(ParagraphStyle('Chapter',fontName='Helvetica-Bold',fontSize=17,leading=21,textColor=colors.HexColor('#174f73'),spaceAfter=12,keepWithNext=True))
    styles.add(ParagraphStyle('ResultTitle',parent=styles['Chapter'],fontSize=15,leading=18))
    styles.add(ParagraphStyle('Label',fontName='Helvetica-Bold',fontSize=10,leading=13,textColor=colors.HexColor('#174f73'),spaceBefore=5,spaceAfter=4,keepWithNext=True))
    styles.add(ParagraphStyle('CellText',fontName='Helvetica',fontSize=8.2,leading=10.5))
    styles.add(ParagraphStyle('TOCLine',fontName='Helvetica',fontSize=9.5,leading=12,spaceBefore=4))
    story=[];width=758;chapters=[];cell_style=styles['CellText']
    def p(t,fine=False):story.append(Paragraph(t,styles['Fine' if fine else 'Text']))
    def label(t):story.append(Paragraph(t,styles['Label']))
    def page(t,key=None):
        if story:story.append(PageBreak())
        z=Paragraph(t,styles['Chapter' if key else 'ResultTitle'])
        if key:z.bookmark=key
        story.append(z)
    def table(head,rows,widths=None,compact=False):
        cells=[[Paragraph(escape(str(c)).replace('\n','<br/>'),cell_style) for c in r] for r in [head]+rows]
        z=Table(cells,colWidths=widths or [width/len(head)]*len(head),repeatRows=1,hAlign='LEFT')
        z.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e5edf4')),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f3f6f8')]),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),2 if compact else 4),('BOTTOMPADDING',(0,0),(-1,-1),2 if compact else 4)]))
        z.spaceAfter=9;story.append(z)
    def fig(name,height=315):
        path=Path(name)
        if path.suffix!='.png':path=figdir/(str(name)+'.png')
        story.append(Image(str(path),width=width,height=height,kind='proportional'))
    def n(v,dp=2):return '-' if v is None or not np.isfinite(v) else f'{v:,.{dp}f}'
    def center(name,a,m,field='baseline_center_counts'):
        return next(r[field] for r in x['mux_spreads'] if r['name']==name and r['array']==a and r['mux']==m)
    def val(name,a,m,address=2,repeat=0,field='baseline_counts'):
        return next(r[field] for r in x['channels'] if r['name']==name and r['array']==a and r['mux']==m and r['address']==address and r['repeat']==repeat)
    def csvrows(name):
        with (out/(name+'.csv')).open(newline='') as f:return list(csv.DictReader(f))
    common=[('SPI / arrays','LPSPI; requested 16 MHz; both arrays unless stated otherwise.'),
            ('Recordings','Three recordings per condition; about 3.5 s each. Exclude the first 1 s; analyze the following 2.5 s.')]
    def guide(num,title,purpose,steps,params,measured,legend,recordings=True):
        full=f'{num:02d}. {title}';page(full,f'test-{num:02d}')
        label('Purpose');p(purpose)
        label('What was done')
        for i,t in enumerate(steps,1):p(f'<b>{i}.</b> {t}')
        label('Test parameters');table(['Parameter','Setting'],params+(common if recordings else []),[152,606])
        label('What was measured');p(measured)
        label('How to read the results');p(legend)
        chapters.append(dict(number=num,title=title,purpose=purpose,steps=steps,parameters=params+(common if recordings else []),measured=measured,legend=legend,conclusions=[]))
    def results(num,title,figure=None,conclusions=(),legend=None,height=315):
        page(f'{num:02d}. {title} - results')
        if figure:fig(figure,height)
        if legend:label('Legend and axes');p(legend,True)
        if conclusions:
            label('Conclusions from these results')
            for t in conclusions:p(t)
            chapters[-1]['conclusions'].extend(conclusions)
    def conclude(*texts):
        label('Conclusions from these results')
        for t in texts:p(t)
        chapters[-1]['conclusions'].extend(texts)

    total=sum(r['cycles'] for r in x['captures']);received=sum(r['received'] for r in x['captures']);dropped=sum(r['dropped'] for r in x['captures'])
    story.append(Paragraph('TestBoard_ADC124<br/>PCB behaviour tests',styles['Title']))
    p('<b>Tests acquired 15 October 2026. Explanatory report revision: 15 October 2026.</b>')
    p('Connected PZT strips, left untouched. Temporary diagnostic firmware; LPSPI only. '
      +('The original matrix is retained; Tests 21-23 add new long-VMID recordings from the same setup.' if extension else
        'This revision explains the existing recordings. No hardware tests were repeated for the rewrite.'))
    table(['Completed work','Result'],[
        ['Original test matrix',f'{len(x["programs"])} programmed conditions / {len(x["captures"])} complete recordings'],
        *([['Added VMID tests','Tests 21-23: 63 additional recordings / 21 programmed conditions; combined 825 recordings.']] if extension else []),
        ['Original acquisition',f'{total:,} cycles acquired; {received:,} received; {dropped:,} counted whole-cycle USB discards'],
        ['Integrity','Zero transfer errors, data errors, USB-write errors and controlled timing overruns'],
        ['Board afterwards','Original production firmware and settings restored; acquisition stopped']],[180,578])
    label('What this report is trying to explain')
    p('Why baseline readings differ between arrays and MUXes; how external switching, sensor connection time, '
      'ADC read order and conversion activity change those readings; and whether quiet recordings contain bursts or isolated spikes.')
    label('Main findings')
    p('External MUX address 6 provides an unintentional reference visit on MUXes 1-3. Holding an address gives near-midscale baselines. '
      'Array B loses much more baseline after a long disconnected interval. Repeated conversions shift readings even with VMID held continuously. '
      'Isolated spikes remain in the VMID-reset recording. The tests identify operating-history effects; they do not identify a single defective component.')
    p('Each numbered test starts with its purpose, exact procedure, parameters, measurement and legend. '
      'The following results page gives the graphs or tables and explains what can, and cannot, be concluded.')
    if extension:p('Added Tests 21-23 cover a 20-second timed VMID hold, ten consecutive conversions per selected MUX path, and regular full scans with 1, 5 and 10 ms VMID waits.')

    page('Contents')
    toc=TableOfContents(tableStyle=TableStyle([('TOPPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),0)]),dotsMinLevel=0)
    toc.levelStyles=[styles['TOCLine']];story.append(toc)

    page('Terminology and the electrical path','terminology')
    p('<b>Sensor connection to external MUX input at an address to MUX common output to OPA365 buffer to ADC input.</b>')
    table(['Term','Meaning used throughout this report'],[
        ['Array A / Array B','Each array has four external MUXes, four buffers and one ADC. ADC A measures Array A; ADC B measures Array B.'],
        ['MUX number 1, 2, 3 or 4','Which external MUX chip within an array. Its buffered output is wired to the same-numbered ADC input: MUX 1 to ADC IN1, ..., MUX 4 to ADC IN4.'],
        ['External MUX address 0-7','Which input pin is connected to the common output of each external MUX. Address numbering is zero-based. Address 5 means the sixth input pin; it does not mean ADC IN4 or MUX 5.'],
        ['ADC input IN1-IN4','Which buffered MUX output the ADC converts. Changing ADC input does not change the external MUX address.'],
        ['VMID','Nominal 1.65 V reference/bias. Selecting external MUX address 7 connects VMID through each external MUX.'],
        ['MUX EN disabled','External MUX signal paths are open. Buffers and ADCs remain powered; the MUX-output bias resistors remain connected to VMID. This is not ADC chip-select or ADC power-off.'],
        ['ADC A first / ADC B first','At one external MUX address, which ADC is read first on the shared SPI bus. The other ADC is then read.']],[160,598])
    p('Within an array, all four external MUXes share address/enable control. In these two-array tests, the arrays normally change address '
      'together. The SPI reads are sequential, not simultaneous.',True)

    page('Board setup and the special wiring of address 6','setup')
    table(['Item','Array A','Array B'],[
        ['External MUX type','TMUX1108','TMUX1308A'],['Bias resistor: MUX 1 / 2 / 3 / 4','470 kohm / 1 Mohm / 470 kohm / 1 Mohm','1 Mohm / 470 kohm / 1 Mohm / 470 kohm'],
        ['Buffer / ADC','OPA365 / ADC124S101','OPA365 / ADC124S101'],['External MUX addresses 0-5','Sensor inputs on MUXes 1-4','Sensor inputs on MUXes 1-4'],
        ['External MUX address 6','VMID on MUXes 1-3; sensor on MUX 4','VMID on MUXes 1-3; sensor on MUX 4'],
        ['External MUX address 7','VMID on every MUX','VMID on every MUX']])
    p('On MUXes 1-3, the address-6 input is physically tied to the address-7 VMID input. '
      'Consequently, a full address-0-to-6 scan still connects those MUXes to VMID once per cycle even when software says VMID off. '
      'MUX 4 instead has a seventh sensor at address 6. There are 25 sensors per array, 50 total.')
    p('Both arrays use the 3.3 V TPS7A2033PDBVR LDO. REF2033 supplies nominal 3.3 V ADC reference/supply and 1.65 V VMID. '
      'The user reports 100 nF at the REF2033 VMID pad, and 150 pF to ground plus a nearby 100 ohm isolation resistor at each MUX VMID input. '
      'The precise resistor/capacitor net ordering has not been verified from a schematic; no inferred schematic is used here.')
    p('The PZT strips remained attached and untouched. Table vibration was not monitored. '
      'MUX type is tied to array location, ADC, buffers and layout; an array difference is not proof that the MUX type alone caused it.')

    page('Timing, statistics and graph conventions','methods')
    table(['Term / symbol','Exact interpretation'],[
        ['Phase slot','A fixed interval beginning before external MUX switching. It includes switching guards, the configured wait and any ADC reads. A 100 µs slot is not 100 µs added after the reads.'],
        ['Cycle period','Start-to-start interval for repeating the full programmed sequence. Any remaining time includes encoding/enqueue and waiting; the last analog connection persists unless explicitly disabled.'],
        ['Settling delay d','The programmed wait after a switching request and before ADC reads. The wait still executes if the address did not change. ADC B normally starts later than ADC A because they share SPI.'],
        ['Baseline','For each route and recording: median ADC code. For repeated conditions: median of the three recording medians.'],
        ['MUX baseline center','Median of six separately measured sensor baselines at external MUX addresses 0-5, for one MUX. It is not a temporal SD.'],
        ['Baseline spread','Maximum baseline minus minimum baseline across the specified inputs or external MUX addresses. This is spatial variation, not temporal noise.'],
        ['SD / peak-to-peak','Temporal sample standard deviation (ddof=1) / maximum minus minimum recorded code. Repeated-condition tables use the median of per-record statistics.'],
        ['Units','1 count = 3.3 / 4096 V = 0.805664 mV nominally. Midscale 2048 counts corresponds to nominal VMID. mV values are code conversions, not separate voltmeter measurements.'],
        ['Lines and circles','Unless stated otherwise, circles are measured summary points. Lines connect them for readability; they are not additional measurements or time waveforms.']],[164,594])
    p('SPI frequencies are requested settings; SCLK edges were not measured. Timing markers bracket driver operations, not exact analog sample apertures. '
      'Diagnostic logging adds overhead, so these rates are not production sampling-rate benchmarks.',True)

    guide(1,'Full scans with and without external MUX address 6',
        'Check whether the hidden VMID visit at external MUX address 6 explains why MUX 4 behaves differently from MUXes 1-3.',[
        'Run ascending external MUX addresses 0-6 with software VMID off. Read ADC IN1-IN4 at addresses 0-5; read only ADC IN4 at address 6.',
        'Repeat using only external MUX addresses 0-5, with software VMID off.',
        'Repeat the full 0-6 sequence, adding external MUX address 7 and four discarded conversions per ADC after every address.'],
        [('Timing','No fixed phase slots or cycle period: run freely. Settling delays 1, 5 and 50 µs; graph below shows 5 µs.')],
        'For each MUX, compare the baseline center and max-minus-min baseline spread across the same external MUX addresses 0-5 in all conditions.',
        'X = MUX number, also its ADC input number. Left = baseline center; right = spread across addresses 0-5. Upper row = Array A; lower = Array B. '
        'Each line is a complete acquisition condition, not a separate sensor.')
    results(1,'Full scans with and without address 6','boundary_fast_centers',[
        f'On Array A / MUX 2, removing address 6 reduces spread from {center("fast_full_5",1,2,"spread_counts"):g} to {center("fast_core_5",1,2,"spread_counts"):g} counts; '
        f'MUX 4 stays at {center("fast_core_5",1,4,"spread_counts"):g} counts. Hidden reference visits explain much of the MUX-4 distinction.',
        'Array B retains a large baseline deficit without address 6. The hidden visit does not explain its entire deficit. '
        'Removing address 6 also changes cycle time and sensor connection duty; Test 3 controls the phase durations. It loses both arrays’ seventh MUX-4 sensors, so this is not a full-50-sensor production fix.'],
        'Blue = addresses 0-6, software VMID off. Orange = addresses 0-5, software VMID off. Green = addresses 0-6 with explicit VMID visits between addresses.')

    guide(2,'Baseline spread between ADC inputs at each external MUX address',
        'Determine how differently the four buffered MUX outputs read when their external MUXes select the same address.',[
        'Use the same three acquisition conditions as Test 1, separately at settling delays 1, 5 and 50 µs.',
        'At each external MUX address 0-5, obtain a baseline for ADC IN1, IN2, IN3 and IN4 in the same array.',
        'Subtract the smallest of those four baselines from the largest.'],
        [('Timing','Same freely running recordings as Test 1; this section is another analysis of those data, not another acquisition.')],
        'One max-minus-min baseline difference per array, condition and external MUX address. Individual baselines are estimated over time before taking the difference.',
        'X = external MUX address, not time or ADC input. Y = spread among ADC IN1-IN4 in counts. Columns = settling delay; rows = array. '
        'The four ADC inputs are separate physical paths, each connected to a different sensor at that address.')
    results(2,'Baseline spread at each external MUX address','address_spreads',[
        'At short delays with addresses 0-6 and software VMID off, Array A’s spread generally falls as the address advances, while Array B’s generally rises. '
        'This is an address/history-dependent mismatch between paths; it is not a rising or falling noise SD.',
        'Removing address 6 or adding explicit VMID changes these shapes. A sequence of points does not show charge accumulating in one sensor: successive addresses select different sensors, and the predecessor history also changes.'],
        'Blue = addresses 0-6, software VMID off; orange = only addresses 0-5, off; green = full scan with explicit VMID between addresses. Lines join different external MUX addresses.')

    guide(3,'Boundary controls with equal phase slots',
        'Compare different connections between the end of one sweep and the start of the next, without changing the nominal cycle duration.',[
        'Select external MUX addresses 0, 1, 2, 3, 4, 5 in order. At each, switch both arrays together, wait d, then read ADC A IN1-IN4 followed by ADC B IN1-IN4.',
        'Give each address a 100 µs phase slot. Add one 100 µs boundary slot in one of the five states explained on the next page.',
        'Repeat the seven-slot sequence every 710 µs; leave the boundary state in place during the remaining cycle overhead/wait.'],
        [('Timing / delays','Seven 100 µs slots; period 710 µs. Separate runs for d = 1, 5 and 50 µs.'),
         ('Recorded inputs','Only external MUX addresses 0-5, all four ADC inputs on both arrays. Boundary conversions, when present, are discarded.')],
        'For each MUX, the median of its six sensor baselines at external MUX addresses 0-5.',
        'X = MUX number; Y = MUX baseline center in counts. Rows = array; columns = d. The boundary is applied once per sweep, not between every pair of addresses.')
    page('03. Boundary-state legend - what each connection means')
    table(['Graph line','Exact action during the final 100 µs slot'],[
        ['Hold MUX address 5','Keep every MUX enabled at external MUX address 5. No ADC reads. Address 5 therefore remains connected for about 200 µs across its normal and boundary slots, plus the remaining cycle overhead/wait.'],
        ['Select MUX address 6','Select address 6 on every MUX; no ADC reads. MUXes 1-3 connect VMID; MUX 4 connects its seventh sensor.'],
        ['VMID; no ADC conversions','Select external MUX address 7 on every MUX. Its analog VMID connection is present, but no conversion clocks are sent during this slot.'],
        ['VMID; ADC conversions','Make the same address-7 VMID connection. After wait d, read IN1-IN4 on each ADC and discard those eight results.'],
        ['MUX EN disabled','Open both arrays’ external MUX signal paths. No address-pin change to 7 and no ADC reads. The buffers remain biased to VMID through their resistors.']],[174,584])
    p('“No ADC conversions” does not mean the ADC is powered off. “VMID” here describes an analog connection through the external MUX. '
      'The equal slot durations separate that connection from the extra conversion activity. The hold control deliberately gives address 5 longer connection time.')
    results(3,'Boundary controls with equal phase slots','boundary_matched',[
        'At 5 µs on Array A / MUX 2, the baseline center is 2047 counts when holding MUX address 5, versus 2043 counts with a VMID boundary. '
        'Array B’s 5 µs curves nearly overlap. The boundary state has a stronger effect on A in this schedule.',
        'Adding ADC conversions to the otherwise equal VMID boundary changes centers by at most 0.5 count across these tested delays. '
        'This applies to a 100 µs boundary, not automatically to a much shorter production VMID interval.'])

    guide(4,'Fixed-address acquisition: external switching removed',
        'Check whether the large scanning offsets persist when a sensor stays connected, and whether cycling ADC inputs contributes additional offsets.',[
        'Select one external MUX address on all eight MUXes and leave them enabled there for the whole recording. No VMID visits or EN toggles occur during the steady recording.',
        'In the four-input variant, every 710 µs read ADC A IN1-IN4, then ADC B IN1-IN4. The external address stays fixed.',
        'In separate one-input runs, read only IN1 on each ADC, then only IN2, IN3 or IN4. All eight external MUXes still remain connected.',
        'Repeat at external MUX addresses 0, 2 and 5. Also hold address 4 with four-input reads. Every plotted address is a separate recording, not a scan within one recording.'],
        [('Timing','100 µs read-phase slot; period 710 µs. The connection persists for the full period. A 5 µs wait executes before every read even without switching.')],
        'Baseline and raw temporal SD for each selected path. The table compares the same address-2 sensor under full scanning, held/four-input and held/one-input acquisition.',
        'Graph X = fixed external MUX address tested in separate runs; Y = baseline. Each colour is one MUX number. The one-input table column combines four separate selected-ADC-input recordings.')
    results(4,'Fixed-address acquisition','held',[
        'All single-ADC-input, held-address-2 baselines are about 2048 counts. Four-input held baselines are about 2048-2050 counts. '
        'The large scanning deficits therefore depend on dynamic operating history.',
        'Holding also gives the sensor continuous bias connection. Four-input versus one-input reads change both ADC input switching and conversion count (eight versus two per cycle). '
        'The differences cannot be assigned uniquely to one of those effects.'],
        'Each MUX line joins independent held-address recordings. Those connecting lines do not mean the firmware swept the addresses during a recording. '
        'Some MUX curves coincide exactly and cover one another; they are not missing measurements.')
    page('04. Same external MUX address 2 - numerical comparison')
    p('Every baseline / SD pair below is in counts and refers to the same physical address-2 sensor on that row. '
      '“Only this ADC input” means one selected input on each ADC per recording; it does not mean only one array was active.')
    table(['Array / MUX / ADC input','Scanning 0-6: baseline / SD','Held address 2, four ADC inputs','Held address 2, only this ADC input'],[
        [f'{"AB"[a-1]} / MUX {m} / IN{m}',n(val('fast_full_5',a,m))+' / '+n(val('fast_full_5',a,m,field='sd_counts'),3),
         n(val('held_a2_mask15',a,m))+' / '+n(val('held_a2_mask15',a,m,field='sd_counts'),3),
         n(val(f'held_a2_mask{1<<(m-1)}',a,m))+' / '+n(val(f'held_a2_mask{1<<(m-1)}',a,m,field='sd_counts'),3)] for a in (1,2) for m in range(1,5)])
    p('The scanning comparator is the freely running 5 µs / requested 16 MHz / software-VMID-off sequence from Test 1. '
      'Its cadence and sensor connection duty differ from the held tests. Near-zero recorded SD can occur when the ADC remains in one code; it does not establish zero analog noise.')

    guide(5,'First eight conversions after a VMID connection',
        'Separate recovery with elapsed time from recovery associated with actually performing ADC conversions after reconnecting a sensor.',[
        'Use one array and one ADC input per recording; disable the other array. All four MUXes in the active array still share the same address changes.',
        'Select external MUX address 7, wait 100 µs, and keep that reference phase 120 µs long. Perform no conversions there.',
        'Select external MUX address 2, wait d, then read the same selected ADC input eight times consecutively. Retain every result; keep this phase 100 µs long.',
        'In separate controls, replace the eight reads with one read after waiting d + seven nominal word times, or that wait plus another 1 µs. Repeat this VMID-to-address-2 cycle.'],
        [('Timing / clock','Period 250 µs; reference slot 120 µs; sensor slot 100 µs. At 16 MHz: d = 1, 5, 50 µs. At 8 MHz: d = 1, 5 µs.'),
         ('Wait-only controls','Extra wait = 7 µs at requested 16 MHz, or 14 µs at requested 8 MHz. The second endpoint adds a further 1 µs.')],
        'A separate median code for each of readings 1-8, across repeated cycles, then across three recordings. This is a repeatedly driven steady cycle, not a board-startup charging measurement.',
        'Panels identify the array and MUX number / ADC input. Colour identifies initial wait d. Circles joined by lines are the eight-read test; x and hollow diamond are independent single-read controls. Detailed marker key follows.')
    page('05. Recovery marker key - the controls are real measurements')
    table(['Symbol','What was acquired','Why / where plotted'],[
        ['Circle joined by a line','Eight consecutive conversions of one unchanged ADC input after switching from VMID to external MUX address 2.','X = actual conversion position 1 through 8. These are medians over cycles, not eight points from one selected transient.'],
        ['x','One conversion after an extra seven nominal word times of waiting, with no conversions during that wait.','Compare its code with the eighth circle. Plotted just right of 8 only to keep symbols separate; it is not a ninth conversion.'],
        ['Hollow diamond','One conversion after the x-control wait plus another 1 µs.','An additional wait endpoint to check sensitivity to imperfect nominal timing matching. Also plotted just right of 8 for visibility.']],[130,326,302])
    p('<b>About the old report’s apparent asterisks:</b> the old figure used an x and a + at exactly the same horizontal position. '
      'When they overlapped, they could look like a *. They were measured wait-only controls, not error bars or statistical significance marks. '
      'This revision replaces + with a hollow diamond and separates the control positions horizontally.')
    p('Marker colour uses the same initial delay as its corresponding curve. The comparison controls are not connected to the eight-reading curve. '
      'Their horizontal offset is graphical only: it does not encode elapsed time.')
    p('These endpoints are approximately time matched using the requested word period. Driver overhead and actual sample apertures were not independently measured, '
      'so a difference from the eighth reading is evidence to investigate conversion activity, not proof that all of the difference is an ADC-specific settling error.')
    for clock in (16000000,8000000):
        results(5,f'First eight conversions - requested {clock//1000000} MHz','recovery_'+str(clock),[
            'Codes generally rise through the eight-reading burst. Across both clock settings and all paths, the eighth baseline is 2-9 counts above the first '
            '(about 1.6-7.3 mV). Waiting alone does not reproduce every eighth-reading endpoint.',
            'More conversions also change reference/supply loading and buffer activity. Test 14 shows a roughly two-count within-burst change even with VMID held continuously, '
            'so these sensor curves cannot be interpreted as sensor settling alone.'],
            'Colour = initial wait. Circles/line = eight consecutive reads. x = single read after extra waiting. Hollow diamond = single read after another 1 µs. '
            'Control markers to the right of position 8 are not extra burst readings.')
    page('05. Recovery comparison - numerical endpoints')
    p('All codes are baseline medians. “Wait” and “wait + 1 µs” are separate single-conversion recordings, as defined in the marker key. '
      'Gain = eighth reading minus first reading. Exact burst-start markers remain in channels.csv.')
    table(['Array / MUX','d (µs)','Requested MHz','First','Eighth','Wait','Wait + 1 µs','Gain (counts)'],[
        [f'{"AB"[int(r["array"])-1]} / {r["mux"]}',r['name'].split('_')[-2],r['name'].split('_')[-3][:-6],n(float(r['first'])),n(float(r['eighth'])),n(float(r['wait'])),n(float(r['wait_plus'])),n(float(r['gain_counts']))]
        for r in csvrows('recovery_comparisons')])

    guide(6,'External MUX address order with equal phase slots',
        'Check whether a baseline pattern follows a physical external MUX address or changes when that address has a different predecessor.',[
        'Read all four ADC inputs on both arrays at the same six external MUX addresses 0-5, using each of five acquisition orders listed below.',
        'After the six read slots, either keep the final address connected for one more slot or select external MUX address 7 without ADC conversions.',
        'Plot the results sorted by physical external MUX address, irrespective of the order in which they were acquired.'],
        [('Orders','Ascending 0-1-2-3-4-5; descending 5-4-3-2-1-0; rotated 3-4-5-0-1-2; Gray 0-2-3-1-5-4; permuted 2-5-0-3-1-4.'),
         ('Timing','d = 5 µs; six 100 µs read slots + one 100 µs boundary slot; period 710 µs.')],
        'Individual sensor baselines versus physical external MUX address. The figure uses Array A / MUX 2 and Array B / MUX 1 as examples; all paths are in channels.csv.',
        'Colour = acquisition order, not a different sensor. X = physical external MUX address sorted 0-5. Left panels hold the last address at the boundary; right panels connect VMID there. Lines do not show chronological sample order.')
    results(6,'External MUX address order','order',[
        'Several baselines change with acquisition order. A peak at a physical address therefore cannot always be treated as a fixed sensor or resistor offset.',
        'Gray order changes one address bit per transition, including wrap, but also changes the analog predecessor. '
        'These results do not separately identify GPIO feedthrough versus analog charge history.'])

    guide(7,'Fast external MUX address order and placement of the cycle gap',
        'Test predecessor order and the location of the logging gap in a freely running sequence closer to the short-dwell scan.',[
        'Run full address sequences 0-1-2-3-4-5-6, 6-5-4-3-2-1-0 and 3-4-5-6-0-1-2, all with software VMID off.',
        'Also run only addresses 0-5 in ascending or Gray order. Read IN1-IN4 at 0-5 and only IN4 at address 6.',
        'Allow encoding/enqueue at each cycle end. Rotating the full sequence moves this gap from after address 6 to after address 2 while preserving cyclic address order.'],
        [('Timing','d = 5 µs; no fixed phase slots or cycle throttle. These are diagnostic, not production, sampling rates.')],
        'Sensor baselines at the same external MUX addresses, with selected MUXes plotted. See the manifest for measured cycle periods.',
        'Each line names the complete acquisition sequence. X is sorted physical external MUX address 0-5, not elapsed order. Address-6 readings are omitted from the plot so all curves compare the same sensors.')
    results(7,'Fast order and cycle-gap placement','fast_order',[
        'Reversing order changes the address-dependent baseline profile. Rotating the sequence also changes some baselines, even though cyclic predecessors remain the same: '
        'where the longer cycle-boundary interval occurs matters.',
        'The large Array B deficit persists. A different scan order is not by itself a demonstrated fix, and these graphs do not measure signal fidelity or ghosting.'])

    guide(8,'ADC array order and ADC input order',
        'Check whether a path reads differently when it is converted earlier or later after the same external address selection.',[
        'Use external MUX addresses 0-5, with both arrays switching together. Add an address-7 boundary slot without conversions.',
        'Compare ADC A then ADC B, each reading IN1-IN4; ADC B then ADC A, each IN1-IN4; and ADC A then ADC B, each reading IN4-IN1.',
        'Keep the phase slots and cycle period equal across these conditions.'],
        [('Timing','Six 100 µs sensor slots + 100 µs VMID boundary; period 710 µs. Sensor wait d = 1, 5, 50 µs. Boundary wait 5 µs.')],
        'For each MUX, the median baseline across external MUX addresses 0-5.',
        'X = MUX number, also ADC input number; Y = baseline center. A-first/B-first says which ADC’s four-read burst occurs first. IN4-IN1 reverses ADC input order, not external MUX addresses.')
    results(8,'ADC array and input order','read_order',[
        'Swapping which ADC is read first changes baseline centers by up to 5 counts in these matched-slot tests. The ADC read second has additional elapsed time and preceding conversion activity.',
        'Reversing ADC inputs changes both position in the burst and the preceding ADC input. These effects remain coupled; '
        'the result does not establish a universal direction or an independently measured optimal delay.'])

    guide(9,'One active array versus both arrays',
        'Check whether activity in the other array affects readings beyond the simple delay caused by reading it first.',[
        'Use the same addresses and timing as Test 8. Record Array A alone, Array B alone and both arrays.',
        'When only one array is active, leave the other array’s external MUX EN disabled and do not read its ADC.',
        'Compare the single-array result with the dual-array condition in which that same ADC is read first: A alone versus A-first, B alone versus B-first.'],
        [('Timing','Period 710 µs; six 100 µs sensor slots + 100 µs VMID boundary. d = 1, 5, 50 µs; ADC input order IN1-IN4.')],
        'MUX baseline centers and the difference “one active array minus both arrays with this ADC first.”',
        'The difference column is in counts. This matched-first comparison reduces the extra-wait difference. It does not make all bus, GPIO or reference-current activity identical.')
    results(9,'One active array versus both arrays')
    table(['Array / MUX','d (µs)','Both; ADC A first','Both; ADC B first','Only this array','Only - matched-first both'],[
        [f'{"AB"[a-1]} / {m}',d,n(center(f'readorder_{d}_banks3_first0_order228',a,m)),n(center(f'readorder_{d}_banks3_first1_order228',a,m)),
         n(center(f'readorder_{d}_banks{1<<(a-1)}_first0_order228',a,m)),
         n(center(f'readorder_{d}_banks{1<<(a-1)}_first0_order228',a,m)-center(f'readorder_{d}_banks3_first{a-1}_order228',a,m))]
        for a in (1,2) for m in range(1,5) for d in (1,5,50)],compact=True)
    conclude('The matched-first differences are at most 0.5 count in this schedule. This reduces the case for a large independent penalty simply from having the other array active. '
      'It does not remove the shared conversion-history effect demonstrated directly in Test 14. Single-array GPIO handling also differs slightly.')

    for num,family,title,sensor_slot,ref_slot,period,delays,figure in (
        (10,'reset_control','VMID connection versus extra conversions: 50 µs interval',100,50,920,(1,5,50),'reset_control'),
        (11,'reset_short','VMID connection versus extra conversions: 20 µs interval',25,20,300,(1,5),'reset_short')):
        guide(num,title,
            'Determine whether an analog VMID connection changes baselines, and whether reading/discarding ADC values during that same connection adds an effect.',[
            f'At external MUX address 0, wait d and read IN1-IN4 on ADC A then ADC B. Keep the sensor phase {sensor_slot} µs long, then disable both external MUX banks.',
            f'Use the next {ref_slot} µs phase in one of three states: stay disabled; select address 7 without conversions; or select address 7, wait 5 µs and read/discard four conversions per ADC. Disable at phase end.',
            'Repeat that sensor/reference pair for external MUX addresses 1-5. Thus the reference or disabled interval occurs after every address, not just at the cycle boundary.'],
            [('Timing',f'Sensor slot {sensor_slot} µs; reference/disabled slot {ref_slot} µs; cycle period {period} µs; d = '+', '.join(map(str,delays))+' µs.'),
             ('EN and wait','Every sensor slot ends with EN disabled. Every reference slot also ends disabled. VMID wait is fixed at 5 µs; it does not follow d in this test.')],
            'Median sensor baseline across external MUX addresses 0-5, for each MUX. Only sensor reads contribute to the plotted baselines.',
            'Blue “Disabled interval” = no selected external input between sensor slots. Orange “VMID; no ADC” = analog address-7 connection. '
            'Green “VMID + discarded ADC” = same reference connection with conversion activity. X = MUX number; columns = sensor wait d.')
        results(num,title,figure,[
            'The two VMID variants nearly overlap: adding conversions during the otherwise equal connection changes median MUX centers by at most 1 count across this test’s conditions.',
            'The analog VMID connection itself can change baselines, particularly on Array A, and does not uniformly move every path closer to 2048. '
            f'These controlled {ref_slot} µs intervals do not prove that production VMID conversions can be removed safely at a shorter dwell or faster cadence.'])

    guide(12,'MUX isolation and reconnection at an unchanged address',
        'Check how a longer disconnected interval affects the next sensor reading, without requiring an external address-bit change at reconnection.',[
        'Use one array and one ADC input per recording; disable the other array. Connect external MUX address 2 for a 100.1 ms preconditioning phase without ADC reads.',
        'Disable the active array’s external MUXes while keeping their cached address at 2. Wait the programmed interval, then finish a slot that is another 100 µs longer.',
        'Re-enable at the same external MUX address 2, wait 1 µs and retain eight consecutive conversions of the same ADC input. Repeat the whole cycle.'],
        [('Programmed off-waits','1, 10, 100, 1000 ms. Actual disabled slots are approximately 1.1, 10.1, 100.1, 1000.1 ms, plus small control overhead.'),
         ('Cycle / recordings','Period = programmed off-wait + 100.5 ms. Read slot 100 µs. Three recordings per path/interval; at least six cycles each; no initial warm-up exclusion.'),
         ('SPI / initial condition','LPSPI, requested 16 MHz. Pre-disconnection sensor voltage was not recorded; complete charging was not established.')],
        'First and eighth code on reconnection. The comparison is an early reconnect response, not a direct voltage measurement while the sensor is disconnected.',
        'X = programmed off-wait in ms on a logarithmic axis; the actual disabled slot adds 0.1 ms. Blue = first conversion; orange = eighth. Grey dotted line = separate continuously held-address baseline.',recordings=False)
    results(12,'Isolation and reconnection','leakage',[
        'Between the shortest and longest programmed off-waits, first readings change by -1 to 0 counts on Array A, versus -480 to -363 counts on Array B '
        '(about -387 to -292 mV). This is a strong array-dependent retention/reconnection difference.',
        'Address pins are unchanged at reconnection, so an address-bit transition is not required for the early-reading deficit. '
        'EN effects, sensor/MUX/PCB leakage, charge sharing and ADC history remain combined. These data do not identify a leakage resistance or sensor capacitance.'])
    page('12. Isolation and reconnection - endpoint table')
    p('Column labels use the programmed wait; actual disabled slots add about 0.1 ms. Values are median codes across three recordings. '
      'The last column shows repetition variation, not temporal noise within a recording.')
    table(['Array / MUX','First, 1 ms wait','First, 1000 ms wait','Change counts / mV','Eighth, 1000 ms wait','First-code range across reps'],[
        [f'{"AB"[a-1]} / {m}',n(val(f'leak_b{a-1}_m{m-1}_1000',a,m)),n(val(f'leak_b{a-1}_m{m-1}_1000000',a,m)),
         n(val(f'leak_b{a-1}_m{m-1}_1000000',a,m)-val(f'leak_b{a-1}_m{m-1}_1000',a,m))+' / '+n((val(f'leak_b{a-1}_m{m-1}_1000000',a,m)-val(f'leak_b{a-1}_m{m-1}_1000',a,m))*LSB*1000),
         n(val(f'leak_b{a-1}_m{m-1}_1000000',a,m,repeat=7)),n(val(f'leak_b{a-1}_m{m-1}_1000000',a,m,field='baseline_rep_range_counts'))]
        for a in (1,2) for m in range(1,5)])
    p('Between cycles the input remains connected during the read-slot remainder, cycle padding and the next preconditioning phase. '
      'The experiment supplies a repeatable periodic history, not a guaranteed fully charged sensor before every off interval.')

    guide(13,'Known-voltage comparison of external MUX addresses 6 and 7',
        'Check readings while selecting two different external input pins that are physically connected to the same VMID net on MUXes 1-3.',[
        'Read only ADC IN1-IN3. In separate recordings, hold external MUX address 6, hold address 7, or alternate 6 then 7.',
        'Use both arrays at delays 1, 5 and 50 µs. Add single-array alternating controls at 5 µs.',
        'Do not read ADC IN4: its external MUX address 6 is a sensor, not the reference. MUX 4 still follows the shared address pins physically.'],
        [('Timing','100 µs phase slots; period 710 µs. Held cases read three inputs per ADC once per cycle; alternating cases read them twice.')],
        'Baseline at addresses 6 and 7 and raw SD while alternating. Nominal VMID is 2048 counts.',
        'Table rows identify array, MUX number and settling delay. Both alternating columns refer to the same physical reference voltage reached through different external MUX input pins.')
    results(13,'Known-voltage control')
    table(['d (µs)','Array / MUX','Hold address 6','Hold address 7','Alternate: address 6','Alternate: address 7','Alternate SD: 6 / 7'],[
        [d,f'{"AB"[a-1]} / {m}',n(val(f'known_held6_{d}_banks3',a,m,6)),n(val(f'known_held7_{d}_banks3',a,m,7)),
         n(val(f'known_toggle67_{d}_banks3',a,m,6)),n(val(f'known_toggle67_{d}_banks3',a,m,7)),
         n(val(f'known_toggle67_{d}_banks3',a,m,6,field='sd_counts'),3)+' / '+n(val(f'known_toggle67_{d}_banks3',a,m,7,field='sd_counts'),3)]
        for d in (1,5,50) for a in (1,2) for m in (1,2,3)])
    conclude('These known-reference connections read about 2048-2050 counts. The large sensor-scan deficits do not appear under this low-impedance reference condition. '
      'One- or two-count differences remain dependent on timing and read position. Because MUX 4 still moves between its sensor and VMID, this is not a pure isolated GPIO-feedthrough measurement.')

    guide(14,'Repeated conversions while continuously connected to VMID',
        'Check whether an eight-reading change can occur without either external MUX address switching, ADC input switching or sensor charging.',[
        'Hold external MUX address 7 continuously on the active array or arrays. Keep the ADC input selection fixed at IN1.',
        'Every 250 µs, use a 120 µs no-read reference phase, then read IN1 eight times in a 100 µs slot. The read phase includes a 1 µs wait.',
        'Compare Array A only, Array B only, both arrays with ADC A’s eight reads first, and both with ADC B’s eight reads first. In single-array runs, disable the other array’s MUXes.',
        'Also record one conversion per active ADC per cycle; the graph’s dotted reference uses the corresponding single-array one-read run.'],
        [('Timing','Period 250 µs; no-read phase includes a 100 µs wait in a 120 µs slot; read phase 100 µs. Requested 16 MHz.')],
        'Mean code and temporal SD separately for each conversion position. Means reveal sub-count changes in code occupancy; plotted values are medians of three per-record means/SDs.',
        'X = conversion position within this ADC’s burst. Blue = only this array; orange = both, ADC A first; green = both, ADC B first. Grey dotted baseline in the mean panels = one conversion/cycle with this array alone.')
    results(14,'Continuously connected VMID','reference_burst',[
        'The first-read ADC rises by about two counts across its eight conversions. The ADC read second starts about two counts higher; reversing array order transfers that behaviour to the other ADC.',
        'A shared conversion-history effect contributes even without sensor or address transitions. Reference/supply changes, buffer drive, ADC behaviour and digital coupling remain candidates; '
        'this is not proof of a failed ADC or a requirement to discard a fixed number of samples.'])
    page('14. Reference-only endpoints and what a code change could mean')
    table(['Activity','Array','First mean','Eighth mean','Eighth - first counts'],[
        [r['name'].replace('refburst_banks1_first0_n8','A only').replace('refburst_banks2_first0_n8','B only').replace('refburst_banks3_first0_n8','A then B').replace('refburst_banks3_first1_n8','B then A'),
         'AB'[int(r['array'])-1],n(float(r['first_mean']),4),n(float(r['eighth_mean']),4),n(float(r['change_counts']),4)] for r in csvrows('reference_burst_comparisons')])
    label('Interpretation of the reference ratio')
    p('Near midscale, the ADC code is approximately 4096 × input voltage / reference voltage. A code rise can therefore come from the buffered VMID input rising, '
      'the ADC reference falling, or both. The ADC uses VA as its analog supply and reference.')
    p('For scale only: +3 counts could mean the input rose about 2.42 mV with a fixed 3.3 V reference, or the reference fell about 4.83 mV with the input fixed at 1.65 V. '
      'These are equivalent-voltage calculations, not measured rail changes. Shared internal reference generation does not guarantee that fast output-load disturbances cancel.')
    p('Simultaneous probing of ADC reference/supply, VMID and buffer output is needed to distinguish those candidates. '
      'The same code shift can have several electrical explanations.')

    long_names={'long_scan':'Scan; no explicit VMID','long_scan_vmid':'Scan; VMID after each address','long_held':'Hold MUX address 2'}
    long_steps=[
        'Record addresses 0-5 with no explicit VMID: six 100 µs read slots. The last address 5 remains connected for the rest of the 1 ms cycle.',
        'Record addresses 0-5 with VMID after every address: alternate 100 µs sensor slots and 50 µs address-7 slots; read/discard four conversions per ADC during each VMID slot. VMID remains connected during the cycle remainder.',
        'Record with external MUX address 2 held continuously: one four-input read burst per ADC every 1 ms. Keep the external connection unchanged between reads.'
    ]
    long_params=[('Timing / SPI','LPSPI, requested 16 MHz; sensor wait 5 µs; explicit VMID wait 5 µs; cycle period 1000 µs; both arrays.'),
        ('Recordings','One separate recording per condition: 2 s initial exclusion, then 120 s analyzed. Conditions occur at different times; no vibration monitoring.')]
    guide(15,'Long recordings: time behaviour and sustained disturbances',
        'Look for drifting baselines, bursts and steps that would not be represented by a single SD number.',long_steps,long_params,
        'For each array and condition, select the sensor route with the largest raw SD. Plot all analyzed data as minimum/maximum envelopes over successive 100 ms bins.',
        'Rows = the three conditions above; columns = arrays. Each panel identifies its selected MUX number and external MUX address. Y = code minus that route’s full-record median. '
        'Blue shading covers each bin’s minimum to maximum; it is not a confidence interval or averaged trace.',recordings=False)
    results(15,'Long-record time traces','long_time',[
        'The no-explicit-VMID record contains a late burst and a baseline step. It is not adequately described as stationary white noise.',
        'The later VMID and held records have less sustained variation, but these are separate times and one recording per condition. '
        'The difference does not establish the cause or prove that VMID suppresses disturbances. Selecting by SD can miss a rare large spike; Test 16 therefore selects independently by range.'])

    guide(16,'Long recordings: isolated spikes and maximum excursions',
        'Find large brief excursions even when they contribute too little to make their route the highest-SD example.',[
        'Use all three existing long recordings from Test 15; do not remove spikes.',
        'For each array and condition, choose the sensor route with the largest full-record peak-to-peak range, independently of the SD selection.',
        'Find its sample furthest from the median and plot every recorded point in a 25 ms window around that event.'],long_params,
        'Full-record peak-to-peak and raw SD for the selected route, in counts and nominal mV, plus the event’s sampled time shape.',
        'Dots are individual samples, about 1 ms apart; connecting lines are visual aids. Y = deviation from median. X = milliseconds within the 25 ms window; the starting time since acquisition began appears below each panel. '
        'The labelled peak-to-peak is the full-record range, not necessarily the span visible in that one window.',recordings=False)
    results(16,'Largest excursions','long_spikes',[
        'The largest route ranges are 128 counts (103.1 mV) in the no-explicit-VMID scan, 72 counts (58.0 mV) with VMID, and 2 counts held. '
        'The VMID condition still contains a brief large spike despite low sustained SD.',
        'Valid framing confirms these are acquired ADC codes rather than undecoded transport bytes. It does not distinguish electrical interference from a real PZT response to vibration.'])
    page('16. Largest excursions - numerical comparison')
    table(['Condition','Array / MUX / external address','Raw range counts / mV','Raw SD counts / mV'],[
        [long_names[r['name']],f'{"AB"[r["array"]-1]} / MUX {r["mux"]} / address {r["address"]}',n(r['ptp_counts'],0)+' / '+n(r['ptp_counts']*LSB*1000,3),n(r['sd_counts'],4)+' / '+n(r['sd_counts']*LSB*1000,4)] for r in spikes])
    p('A low SD does not guarantee no rare excursions. Conversely, one-count toggling is expected when an analog level lies near an ADC code boundary. '
      'The sample rate limits the event shape visible here; sub-millisecond behaviour was not captured.')

    guide(17,'Long recordings: power spectral density',
        'Show whether fluctuations are broadly distributed with frequency or concentrated at low frequencies or particular frequencies.',[
        'Use the same highest-SD routes as Test 15, with every steady-state sample retained for the raw curve.',
        'Separately identify the longest continuous quiet interval common to all routes in that recording, using the screening method in Test 18.',
        'Compute Welch spectra using up to 2048 samples per window, subtracting the window mean. Use only uninterrupted received sequences; do not interpolate across missing cycles.'],
        long_params+[('Spectrum','About 1 ksample/s per route; frequency axis extends to about 500 Hz. Constant-detrended Welch windows, up to 2048 points.')],
        'Power spectral density in counts squared per Hz, for the full raw record and the selected quiet interval.',
        'Blue = raw; orange = longest quiet interval. X = frequency in Hz; Y is logarithmic PSD. A flat spectrum can be consistent with white noise, '
        'but a PSD alone does not prove whiteness or identify a source. A constant-code quiet interval has zero recorded PSD and is annotated instead of drawn at a fictitious floor.',recordings=False)
    results(17,'Power spectral density','long_psd',[
        'The no-explicit-VMID burst/step contributes strong low-frequency power, especially on Array B. Removing sustained disturbed intervals changes that spectrum.',
        'Some remaining spectra are broad and low, but isolated events, quantization and limited duration prevent declaring all inputs purely white-noise sources. '
        'The displayed routes are selected examples; full raw/quiet route statistics are exported.'])

    guide(18,'Long recordings: raw versus quiet noise statistics',
        'Report ordinary quiet behaviour separately from full-record behaviour while keeping spikes visible in the raw statistics.',[
        'Compute raw SD and peak-to-peak from every steady-state sample in each long recording.',
        'Screen coherent separated-code states and one-second periods of unusually high SD, using the detailed rules on the following page.',
        'Take the longest remaining uninterrupted received interval shared by all routes. Compute quiet statistics on exactly that interval; do not join different baseline states or choose the lowest SD interval.'],long_params,
        'Raw and quiet SD/range. The results table uses the highest-raw-SD route in each array/condition; all routes and exact intervals are in the companion CSV/JSON files.',
        'Quiet means a defined screened time interval, not a guarantee of Gaussian or white noise. Isolated spikes inside that interval remain. '
        'A zero recorded SD means constant ADC codes, not zero physical analog noise.',recordings=False)
    page('18. Quiet-interval selection - exact rules')
    table(['Stage','Rule'],[
        ['Separated-state screen','Anchor each route to a dominant one-code state (60% occupancy) or adjacent two-code state (80% occupancy), allowing a one-count margin. A separated excursion requires at least 1% tail mass and a histogram peak with at least 0.5% of samples and three times the edge density.'],
        ['Common rejection','At least two routes must depart in the same cycle. Reject that cycle and one neighbouring cycle on each side, identically for every route.'],
        ['Sustained-variance screen','For each route, compute SD in one-second bins. Flag a bin if its SD exceeds max(0.75 count, four times that route’s median bin SD). Reject bins flagged by any route and pad by one bin on each side.'],
        ['Continuous interval','From what remains after both screens, choose the longest uninterrupted received interval. Split at missing sequence numbers. Apply its same start/end to every route.'],
        ['Audit files','long_quiet_metadata.json records thresholds and selected interval. long_quiet_channels.csv records every route’s raw/quiet statistics. Raw records and raw statistics retain all events.']],[164,594])
    p('The method screens sustained or coherent disturbed states; it is not a complete detector for every isolated spike. '
      'Do not interpret quiet statistics as the worst-case excursions. Test 16 supplies the independent range check.')
    results(18,'Raw and quiet noise statistics')
    table(['Condition','Array / MUX / address','Raw SD counts / mV','Raw range counts / mV','Quiet SD counts / mV','Quiet range counts','Quiet interval s'],[
        [long_names[r['name']],f'{"AB"[r["array"]-1]} / {r["mux"]} / {r["address"]}',n(r['sd_counts'],4)+' / '+n(r['sd_counts']*LSB*1000,4),
         n(r['ptp_counts'],0)+' / '+n(r['ptp_counts']*LSB*1000,3),n(r['quiet_sd_counts'],4)+' / '+n(r['quiet_sd_volts']*1000,4),n(r['quiet_ptp_counts'],0),n(r['quiet_start_s'],3)+' to '+n(r['quiet_end_s'],3)] for r in noise])
    conclude('Quiet intervals can have much smaller SD than the full disturbed record. The chosen no-VMID Array-A route is constant-code in its quiet interval. '
      'The VMID examples near 0.5 count SD mostly toggle between adjacent codes; this is not by itself evidence of a 0.5-count continuous analog-noise floor.',
      'These are SD-selected routes. For example, the 72-count VMID spike occurs on a different Array-B route and is therefore shown in Test 16, not hidden by this table’s small ranges.')

    guide(19,'Correlation between measured paths',
        'Look for fluctuations that occur together on different paths, while avoiding correlation caused only by a slow shared drift.',[
        'Use the long recordings from Test 15. Compare pairs acquired at the same external MUX address phase, requiring route SD above 0.05 count.',
        'Compute ordinary correlation over the full steady-state record.',
        'Subtract each route’s own mean separately in each one-second bin and compute correlation again. Show the 14 pairs with largest absolute within-second correlation.'],long_params,
        'Dimensionless Pearson correlation: +1 means rising/falling together; -1 means opposite directions; 0 means little linear association.',
        'A route label such as A / MUX 2 / address 5 identifies the array, external MUX chip and external MUX address. '
        'Within-second means removing each one-second mean; it is not simultaneous sampling or a proof of coupling.',recordings=False)
    results(19,'Shared correlations')
    def route_label(s):
        a,m,addr=s.split(':');return f'{"AB"[int(a)-1]} / MUX {m} / address {addr}'
    strong=sorted(correlations,key=lambda r:abs(r['within_second_correlation']),reverse=True)[:14]
    table(['Condition','Path 1','Path 2','Raw correlation','Within-second correlation'],[
        [long_names[r['name']],route_label(r['route1']),route_label(r['route2']),n(r['raw_correlation'],3),n(r['within_second_correlation'],3)] for r in strong])
    conclude('Some pairs share measurable variation, including opposite-direction changes. Correlation narrows what to inspect, but does not distinguish a common reference disturbance, '
      'sensor/table motion or electrical coupling. Many pairs were screened, and ADC inputs were read sequentially; the largest values are descriptive, not a significance test.')

    guide(20,'Timing stability and association with code changes',
        'Check whether the long-record code events coincide with variable software delay or variable ADC-burst duration.',[
        'Use the same highest-SD routes as Test 15. Retrieve CPU-tick markers after MUX switching guards, immediately before the array’s ADC burst and immediately after that burst.',
        'Calculate wait-to-burst delay and full burst duration for each cycle, using integer tick differences before converting units.',
        'Measure their total ranges and correlate each with the selected ADC code.'],long_params,
        'Median delay in µs, full-record timing range in ns, and code/timing correlation. The burst duration covers the array’s whole read batch, not just the selected input.',
        'A dash for correlation means the timing variable was constant, so correlation is undefined. These are firmware markers, not oscilloscope pin edges or exact sample-aperture times.',recordings=False)
    results(20,'Timing stability')
    table(['Condition / array','MUX / address','Median delay µs','Delay range ns','Burst-duration range ns','Code/delay correlation','Code/duration correlation'],[
        [long_names[r['name']]+' / '+'AB'[r['array']-1],f'{r["mux"]} / {r["address"]}',n(r['delay_median_us'],4),n(r['delay_span_ns'],2),n(r['duration_span_ns'],2),n(r['code_delay_correlation'],4),n(r['code_duration_correlation'],4)] for r in timing])
    conclude('Logged timing is stable to a few or tens of nanoseconds, and its correlations with the selected codes are near zero. '
      'This does not support variable software burst timing as the main explanation for the large recorded events. '
      'Stable software timing does not rule out electrical USB, reference/supply or digital coupling.')

    if extension:
        from pcb_diagnostics_vmid_report import append_vmid_tests
        append_vmid_tests(extension,extension_folder,guide,results,page,p,label,table,conclude,n)

    page('Original matrix: acquisition checks, restoration and evidence files','integrity')
    p('Purpose: confirm that the plotted codes belong to fully decoded recordings, and that the temporary experiment did not leave the board running diagnostic firmware.')
    table(['Check','Result'],[
        ['Expected / complete recordings',f'{x["expected_captures"]} / {len(x["captures"])}'],
        ['Acquired / received / discarded cycles',f'{total:,} / {received:,} / {dropped:,}; every recording accounts for all acquired cycles'],
        ['Transfer / data / USB-write errors','0 / 0 / 0'],['Timing overruns','0'],
        ['Frame validation','ADG1 schema, per-conversion labels, monotonic sequence and FNV-1a checksum checked'],
        ['Firmware checks','82 focused regression tests passed; tagged diagnostic model checked 810 configurations × three cycles; production and diagnostic builds passed'],
        ['Restored state',x['lifecycle']['state']+'; '+x['lifecycle']['final_status']['firmware_revision']+'; stopped'],
        ['Original settings','Restored, including original engine/scan order; not forced to the diagnostic LPSPI settings']],[180,578])
    p('One separate smoke test is excluded. An incomplete long-isolation attempt hit a host timeout; its bytes are preserved separately, the reader was corrected, '
      'production was restored, and the condition was repeated. Only complete recordings are included.')
    p('Conclusion: no recorded framing fault or controlled timing overrun explains the observed ADC-code events. '
      'Quiet sensor data alone do not independently prove analog channel identity; physical labels rely on the documented wiring and validated digital sequencing.')

    page('How to find the underlying data','files')
    table(['File / location','Contents'],[
        ['docs/testing/results/testboard-adc124/pcb_diagnostics_2026-10-15','This report’s analysis, tables, figures, reader explanations and verification record.'],
        ['channels.csv / channel_repetitions.csv','Per array, MUX number, external MUX address and repeated conversion: baseline, SD, range, counts/volts, repetition variation and timing markers.'],
        ['captures.csv / analysis.json','All conditions, programmed steps, measured cycle periods and acquisition accounting.'],
        ['long_noise.csv / long_spikes.csv','SD-selected examples versus independently peak-to-peak-selected examples.'],
        ['long_quiet_channels.csv / long_quiet_metadata.json','Every long-record route’s quiet statistics and exact screening intervals/thresholds.'],
        ['correlations.csv / timing_correlations.csv','Pairwise and code/timing associations used in Tests 19 and 20.'],
        ['docs/testing/datasets/testboard-adc124/pcb_diagnostics_2026-10-15','Original raw bytes, decoded arrays/labels, capture metadata, command/upload logs, production backup and diagnostic firmware/tool snapshots.'],
        ['report_revisions/original','The earlier PDF, builder and verification record, preserved before this explanatory rewrite.']],[300,458])
    if extension:
        p('Tests 1-20 retain the original recordings. New Tests 21-23 have separate raw/analysis folders listed in their acquisition-evidence page. '
          'The temporary diagnostic image was updated for those tests, then original production firmware/settings were restored.')
    else:
        p('Conclusions in this revision use the same captured data. No firmware upload, sensor manipulation or new recording was required to clarify the document.')

    page('Device references and limits of component attribution','references')
    p('Purpose: distinguish the observed board behaviour from claims that would need a component-level measurement. '
      'The following manufacturer documents are background references, not measurements of this assembled board.')
    for title,url in [('ADC124S101 data sheet','https://www.ti.com/lit/ds/symlink/adc124s101.pdf'),('TMUX1108 data sheet','https://www.ti.com/lit/ds/symlink/tmux1108.pdf'),
        ('TMUX1308A data sheet','https://www.ti.com/lit/ds/symlink/tmux1308a.pdf'),('OPA365 data sheet','https://www.ti.com/lit/ds/symlink/opa365.pdf'),
        ('REF2033 family data sheet','https://www.ti.com/lit/pdf/sbos600'),('High-impedance MUX settling application note','https://www.ti.com/lit/pdf/SCDA054')]:
        p(f'<a href="{url}" color="#174f73">{escape(title)}</a> - {url}',True)
    p('ADC124S101’s command selects the next conversion input. Its data sheet does not require a power-up dummy conversion. '
      'A measured improvement after more conversions is therefore not evidence of a digital requirement to discard one reading. '
      'The OPA365 buffer also separates the external high-resistance MUX node from the ADC sampling capacitor.')
    p('Conclusion: these automatic controls establish sensitivity to operating history. They do not separately measure charge injection, PZT capacitance, leakage, '
      'reference ripple, buffer stability or physical GPIO/SPI waveforms. Controlled component substitution or probing is needed before assigning one component as the cause.')

    page('Appendix: original programmed conditions and measured cycle periods','manifest')
    p('This appendix is an audit index, not a graph legend. Program names are machine identifiers linked to raw recording filenames. '
      'Arrays A, B or A+B identify enabled arrays. A requested period of “free” means no programmed throttle; measured values include diagnostic overhead. '
      'Clock values are requested MHz. Phase count includes read and no-read steps.')
    capby={}
    for r in x['captures']:capby.setdefault(r['name'],[]).append(r)
    # Smaller audit-table text leaves explanatory chapters at readable body sizes.
    cell_style=ParagraphStyle('AuditCell',parent=styles['CellText'],fontSize=7,leading=9)
    table(['Program identifier','Family','Reps','MHz','Arrays','Phases','Requested period µs','Measured median period µs'],[
        [name,prog['family'],len(capby[name]),prog['clock']//1000000,{1:'A',2:'B',3:'A+B'}[prog['arrays']],len(prog['steps']),prog['period'] or 'free',
         n(float(np.median([r['period_median_us'] for r in capby[name] if r['period_median_us'] is not None])),2)] for name,prog in x['programs'].items()],
         [208,104,32,32,42,40,96,104],compact=True)
    def footer(canvas,doc):
        canvas.setFont('Helvetica',7);canvas.setFillColor(colors.HexColor('#526575'))
        canvas.drawString(42,23,'TestBoard_ADC124 | 15 October 2026 | Explanatory revision')
        canvas.drawRightString(800,23,str(doc.page))
    ReaderDoc(str(pdf),pagesize=landscape(A4),leftMargin=42,rightMargin=42,topMargin=32,bottomMargin=38,
        title='TestBoard_ADC124 PCB behaviour tests - explained',author='TestBoard_ADC124 diagnostic analysis').multiBuild(story,onFirstPage=footer,onLaterPages=footer)
    (out/'chapter_explanations.json').write_text(json.dumps(chapters,indent=2),encoding='utf-8')
