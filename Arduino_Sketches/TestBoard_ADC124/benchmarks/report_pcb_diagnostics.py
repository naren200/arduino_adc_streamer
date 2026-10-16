"""Scientific figures and PDF for automatic PCB diagnostics (raw codes retained)."""
from pathlib import Path
import argparse,json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import welch
from reportlab.platypus import SimpleDocTemplate,Paragraph,Table,TableStyle,Image,PageBreak,Spacer
from reportlab.lib.pagesizes import A4,landscape
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.lib import colors
from matplotlib.lines import Line2D
import pcb_diagnostics as d
from analyze_pcb_diagnostics import OUT,LSB,csv_write,steady,longest_quiet,burst_window_screen
from adc124_quiet_windows import between_bursts

PDF=d.ROOT/'docs/reports/exports/TestBoard_ADC124_PCB_Diagnostics_2026-10-15.pdf'

def build(out=OUT,pdf=PDF,allow_partial=False):
    x=json.loads((out/'analysis.json').read_text())
    if not allow_partial:
        assert not x['partial'] and x['lifecycle']['state']=='production_restored_verified'
        assert len(x['captures'])==x['expected_captures'] and all(not any(r[k] for k in ('overruns','transfer_errors','data_errors','usb_write_errors')) for r in x['captures'])
    ch=x['channels'];sp=x['mux_spreads'];ads=x['address_spreads']
    raw_root=Path(x.get('raw_root',str(d.RAW)))
    figdir=out/'figures';figdir.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size':8,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle('Body',fontSize=9,leading=12,spaceAfter=7))
    styles.add(ParagraphStyle('Small',fontSize=7.3,leading=9.6,spaceAfter=5))
    styles['Heading1'].fontSize=15;styles['Heading1'].leading=19
    styles['Heading1'].textColor=colors.HexColor('#174f73')
    story=[];width=758
    def p(text,small=False):story.append(Paragraph(text,styles['Small' if small else 'Body']))
    def h(text):story.append(Paragraph(text,styles['Heading1']))
    def page(title):story.append(PageBreak());h(title)
    def table(head,rows,widths=None):
        cell=ParagraphStyle('Cell',fontSize=7,leading=8.5)
        t=Table([[Paragraph(str(c),cell) for c in row] for row in [head]+rows],
            colWidths=widths or [width/len(head)]*len(head),repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e5edf4')),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f4f6f8')]),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3)]))
        t.spaceAfter=6
        story.append(t)
    def chart(fig,name,height=330):
        for ax in fig.axes:
            if ax.get_yscale() == 'linear':ax.ticklabel_format(axis='y',style='plain',useOffset=False)
            if ax.get_xlabel().startswith('MUX'):ax.set_xticks(range(1,5))
        path=figdir/(name+'.png');fig.savefig(path,dpi=180,bbox_inches='tight');plt.close(fig)
        story.append(Image(str(path),width=width,height=height,kind='proportional'))
    def n(v,dp=2):return '-' if v is None or not np.isfinite(v) else f'{v:,.{dp}f}'
    def rows(name,arr=None,mux=None,addr=None,repeat=0):
        return [r for r in ch if r['name']==name and (arr is None or r['array']==arr)
            and (mux is None or r['mux']==mux) and (addr is None or r['address']==addr)
            and (repeat is None or r['repeat']==repeat)]
    def value(name,arr,mux,addr,repeat=0,field='baseline_counts'):
        r=rows(name,arr,mux,addr,repeat);return r[0][field] if r else np.nan
    def center(name,arr,mux,field='baseline_center_counts'):
        r=next((r for r in sp if r['name']==name and r['array']==arr and r['mux']==mux),{})
        return r.get(field,np.nan)
    def as_spread(name,arr):
        rr=sorted([r for r in ads if r['name']==name and r['array']==arr],key=lambda r:r['address'])
        return [r['address'] for r in rr],[r['spread_counts'] for r in rr]
    def line_routes(ax,name,arr,mux,label,**kw):
        rr=sorted(rows(name,arr,mux),key=lambda r:r['address'])
        rr=[r for r in rr if r['address']<6]
        ax.plot([r['address'] for r in rr],[r['baseline_counts'] for r in rr],'o-',label=label,**kw)
    total=sum(r['cycles'] for r in x['captures']);received=sum(r['received'] for r in x['captures']);dropped=sum(r['dropped'] for r in x['captures'])
    story.append(Paragraph('TestBoard_ADC124 - automatic PCB diagnostics',styles['Title']))
    p('15 October 2026 | Connected PZT strips, untouched | LPSPI only | Separate temporary diagnostic firmware')
    h('Switching history, recovery and acquisition controls')
    p(f'{len(x["captures"])} captures across {len(x["programs"])} microprograms; three repetitions except three 120-second noise records. '
      f'{total:,} acquired cycles, {received:,} received, {dropped:,} accounted whole-cycle USB discards. '
      'Diagnostic timing is measured separately from production sampling performance.')
    table(['Hardware','Array A','Array B'],[
        ['External MUX','TMUX1108','TMUX1308A'],['Bias resistance MUX1 / 2 / 3 / 4','470k / 1M / 470k / 1M','1M / 470k / 1M / 470k'],
        ['Buffer / ADC','OPA365 / ADC124S101','OPA365 / ADC124S101'],
        ['External MUX address 6','MUX1-3 tied to VMID; MUX4 sensor','MUX1-3 tied to VMID; MUX4 sensor']])
    p('Both MUX banks use the 3.3 V TPS7A2033PDBVR LDO. REF2033 provides ADC reference 3.3 V and VMID 1.65 V. '
      'There is 100 nF near the reference VMID pad, and 150 pF to ground plus a nearby 100 ohm isolation resistor at each MUX VMID input. '
      'Exact electrical capacitor/resistor ordering has not been established from a schematic. Nominal 1 count = 0.805664 mV.',True)
    p('Raw datasets, frozen firmware/settings, complete per-conversion labels, channel statistics and capture accounting accompany this PDF. '
      'Baseline uniformity, temporal SD and closeness to nominal VMID are different measurements. All SPI frequencies are requested settings; '
      'SCLK was not electrically measured. Logged burst markers include driver overhead.',True)

    page('Measured comparisons and practical implications')
    table(['Array/MUX','Full 0-6, off: center / spread','Only 0-5, off: center / spread','Held addr2, single ADC input'],[
        [f'{"AB"[a-1]}/{m}',n(center('fast_full_5',a,m))+' / '+n(center('fast_full_5',a,m,'spread_counts')),
         n(center('fast_core_5',a,m))+' / '+n(center('fast_core_5',a,m,'spread_counts')),
         n(value(f'held_a2_mask{1<<(m-1)}',a,m,2))] for a in (1,2) for m in range(1,5)])
    p('5 us / 16 MHz; all values in counts. Centers/spreads compare the same six physical addresses. '
      'Removing address 6 removes the periodic VMID connection only on MUX1-3; MUX4 had a seventh sensor there. '
      'This is direct evidence about reset history, with a remaining sweep-period/duty change. The separate equal-slot boundary controls address that timing confound.')
    p('If held-address baselines return near midscale while scanning baselines do not, a large static leakage/reference offset is insufficient '
      'to explain the scan deficit. Switching history, charge redistribution and read timing remain contributors. '
      'The sensor is also continuously connected during the held condition, so its charging duty changes.')
    summary_path=out/'interpretation.json'
    if summary_path.exists():
        for text in json.loads(summary_path.read_text())['paragraphs']:p(text)

    page('Controls and interpretation limits')
    for text in x['notes']:p(text)
    p('A fixed-address acquisition removes external switching but changes sensor connection duty. It is an empirical static reference, '
      'not proof of accuracy under multiplexed operation. Reversing scan order changes both physical predecessors and digital address transitions. '
      'A sequence comparison alone cannot isolate digital feedthrough. MUX type remains confounded with array, ADC, buffer and layout.')
    p('All initial samples are saved. Baseline/noise statistics exclude the configured warm-up only; leakage tests use no warm-up. '
      'Baseline = median of samples, then median of repetition medians. Repeated-setting SD and peak-to-peak are medians of per-record raw statistics; '
      'worst-repetition SD and peak-to-peak are also retained in channels.csv. No spikes are removed from the raw data. '
      'Sub-count SD is quantization-dependent and is not a direct measurement of intrinsic analog noise.',True)
    p('A separate one-second smoke capture is preserved but excluded from matrix totals. One incomplete long-isolation attempt '
      'was archived after a host timeout; the reader was corrected, production restored, and the capture repeated. '
      'Only complete, fully accounted captures enter the tables.',True)
    table(['Family','Programs','Captures'],[[family,sum(p['family']==family for p in x['programs'].values()),count] for family,count in sorted(x['families'].items())])

    page('Fast-sequence reproduction and removal of address 6')
    fig,axs=plt.subplots(2,2,figsize=(12,5.2),constrained_layout=True)
    for i,arr in enumerate((1,2)):
        for kind,label in (('full','Original 0-6 / VMID off'),('core','Only 0-5 / VMID off'),('full_vmid','Original 0-6 / VMID on')):
            name=f'fast_{kind}_5'
            axs[i,0].plot(range(1,5),[center(name,arr,m) for m in range(1,5)],'o-',label=label)
            axs[i,1].plot(range(1,5),[center(name,arr,m,'spread_counts') for m in range(1,5)],'o-',label=label)
        axs[i,0].set(title=f'Array {"AB"[i]} / median baseline',ylabel='Counts',xlabel='MUX number (ADC input number)')
        axs[i,1].set(title=f'Array {"AB"[i]} / spread across sensor addresses',ylabel='Counts',xlabel='MUX number (ADC input number)')
        for ax in axs[i]:ax.set_xticks(range(1,5));ax.legend(fontsize=7)
    chart(fig,'boundary_fast_centers');p('5 us settling, requested 16 MHz. Centers/spreads use addresses 0-5 in every condition. '
        'Removing address 6 removes the hidden VMID visit on MUX1-3 as well as MUX4\'s seventh sensor; sweep duration and exposure also change.',True)

    page('Spread between ADC inputs at each external address')
    fig,axs=plt.subplots(2,3,figsize=(12,5.2),constrained_layout=True)
    for i,arr in enumerate((1,2)):
        for j,delay in enumerate((1,5,50)):
            for kind,label in (('full','0-6 off'),('core','0-5 off'),('full_vmid','0-6 on')):
                a,v=as_spread(f'fast_{kind}_{delay}',arr);axs[i,j].plot(a,v,'o-',label=label)
            axs[i,j].set(title=f'Array {"AB"[i]} / {delay} us',xlabel='External MUX address',ylabel='Max-min IN1-4 (counts)');axs[i,j].legend(fontsize=7)
    chart(fig,'address_spreads');p('Every point compares four independently estimated baselines at the same address; this is not temporal noise.',True)

    page('Boundary controls with equal phase slots')
    fig,axs=plt.subplots(2,3,figsize=(12,5.2),constrained_layout=True)
    for i,arr in enumerate((1,2)):
        for j,delay in enumerate((1,5,50)):
            for kind,label in (('hold','Hold MUX address 5'),('address6','Select MUX address 6'),('vmid','VMID; no ADC conversions'),('vmid_adc','VMID; ADC conversions'),('disabled','MUX EN disabled')):
                axs[i,j].plot(range(1,5),[center(f'boundary_{kind}_{delay}',arr,m) for m in range(1,5)],'o-',label=label)
            axs[i,j].set(title=f'Array {"AB"[i]} / {delay} us',xlabel='MUX number',ylabel='Median baseline (counts)');axs[i,j].set_xticks(range(1,5));axs[i,j].legend(fontsize=6)
    chart(fig,'boundary_matched');p('Six sensor slots plus one boundary slot, each 100 us; requested period 710 us. '
        'The boundary state is intentionally changed. Hold-sensor-5 control exposes that sensor for longer. '
        'ADC-on versus ADC-off VMID variants share the same boundary-slot duration, but conversions occur early within that slot.',True)

    page('Fixed-address acquisition: external switching removed')
    table(['Array','MUX','Bias resistor','Scan 0-6 baseline / SD','Held addr2, four ADC inputs','Held addr2, one ADC input'],[
        ['AB'[a-1],m,('470k' if ((a==1 and m in (1,3))or(a==2 and m in (2,4))) else '1M'),
         n(value('fast_full_5',a,m,2))+' / '+n(value('fast_full_5',a,m,2,field='sd_counts'),3),
         n(value('held_a2_mask15',a,m,2))+' / '+n(value('held_a2_mask15',a,m,2,field='sd_counts'),3),
         n(value(f'held_a2_mask{1<<(m-1)}',a,m,2))+' / '+n(value(f'held_a2_mask{1<<(m-1)}',a,m,2,field='sd_counts'),3)]
        for a in (1,2) for m in range(1,5)])
    fig,axs=plt.subplots(1,2,figsize=(12,3.4),constrained_layout=True)
    for i,a in enumerate((1,2)):
        for m in range(1,5):
            axs[i].plot((0,2,4,5),[value(f'held_a{addr}_mask15',a,m,addr) for addr in (0,2,4,5)],'o-',label=f'MUX{m}')
        axs[i].set(title=f'Array {"AB"[i]} / held-address baselines',xlabel='External MUX address',ylabel='Counts');axs[i].legend(fontsize=7)
    chart(fig,'held',height=210);p('Held programs request 710 us periods. All physical MUXes in a participating bank remain at the chosen address. '
        'The four-input versus one-input comparison changes ADC input cycling, while external address remains fixed.',True)

    for clock in (16000000,8000000):
        page(f'First eight conversions after VMID - {clock//1000000} MHz')
        fig,axs=plt.subplots(2,4,figsize=(12,5.2),constrained_layout=True)
        for a in (1,2):
            for m in range(1,5):
                ax=axs[a-1,m-1]
                for delay in ((1,5,50) if clock==16000000 else (1,5)):
                    name=f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_eight'
                    line,=ax.plot(range(1,9),[value(name,a,m,2,r) for r in range(8)],'o-',label=f'Initial wait {delay} us')
                    wait=value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_wait',a,m,2)
                    ax.plot([8.3],[wait],'x',markersize=7,color=line.get_color())
                    later=value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_wait_plus',a,m,2)
                    ax.plot([8.65],[later],'D',fillstyle='none',markersize=6,color=line.get_color())
                ax.set(title=f'Array {"AB"[a-1]} / MUX {m} / ADC IN{m}',xlabel='Reading 1-8; controls at right',ylabel='Baseline (counts)',xlim=(.7,9),xticks=range(1,9))
        handles,labels=axs[0,0].get_legend_handles_labels()
        handles.extend([Line2D([],[],color='black',marker='x',ls='None',label='Wait only'),Line2D([],[],color='black',marker='D',fillstyle='none',ls='None',label='Wait only + 1 us')])
        fig.legend(handles=handles,loc='outside lower center',ncol=len(handles),fontsize=8)
        chart(fig,'recovery_'+str(clock));p('One bank and one ADC input at a time. Reference phase: 100 us wait in a 120 us slot. '
            'Sensor phase: fixed 100 us slot, no selector prime inside acquisition. All eight consecutive responses retained. '
            'Cross at conversion 8 = single response after waiting seven extra nominal word periods without conversions; plus = one additional us. '
            'Markers bracket bursts; exact sample apertures and actual SCLK edges were not probed.',True)

    page('Recovery comparison: conversion activity versus elapsed waiting')
    table(['Array/MUX','Delay us','MHz','First code','Eighth code','Wait-only code','Eighth-first','Eighth-wait','Burst begins after switch, us'],[
        [f'{"AB"[a-1]}/{m}',delay,clock//1000000,
         n(value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_eight',a,m,2,0)),
         n(value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_eight',a,m,2,7)),
         n(value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_wait',a,m,2)),
         n(value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_eight',a,m,2,7)-value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_eight',a,m,2,0)),
         n(value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_eight',a,m,2,7)-value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_wait',a,m,2)),
         n(value(f'recovery_b{a-1}_m{m-1}_{clock}_{delay}_eight',a,m,2,0,field='burst_start_after_switch_us'))]
        for a in (1,2) for m in range(1,5) for clock,delay in ((16000000,1),(16000000,5),(16000000,50),(8000000,1),(8000000,5))])
    p('Counts shown are repetition medians. Differences below a count are sensitive to code thresholds. '
      'A different wait-only endpoint is a candidate conversion-activity effect, but nominal word spacing is not an independently measured aperture.',True)

    page('Address order: does the pattern follow physical address?')
    fig,axs=plt.subplots(2,2,figsize=(12,5.2),constrained_layout=True)
    for i,(a,m) in enumerate(((1,2),(2,1))):
        for j,ref in enumerate((0,1)):
            ax=axs[i,j]
            for order in ('ascending','descending','rotated','gray','permuted'):
                line_routes(ax,f'order_{order}_ref{ref}',a,m,order)
            ax.set(title=f'Array {"AB"[a-1]} / MUX {m} / VMID boundary: {"yes" if ref else "no"}',xlabel='External MUX address',ylabel='Counts');ax.legend(fontsize=7)
    chart(fig,'order');p('All orders use the same six sensor addresses, 100 us sensor slots and one 100 us boundary slot, period 710 us. '
        'Without boundary VMID, the final sensor remains selected during that slot. Gray cycle is 0-2-3-1-5-4-0 (one bit changes each transition). '
        'Changing order changes both electrical predecessors and GPIO transitions; it does not isolate their individual contributions.',True)

    page('Fast address order and inter-cycle pause placement')
    fig,axs=plt.subplots(2,2,figsize=(12,5.2),constrained_layout=True)
    for i,a in enumerate((1,2)):
        for j,m in enumerate((2 if a==1 else 1,4)):
            for name,label in (('fast_full_5','0-6 ascending'),('fast_order_full_descending','6-0 descending'),
                ('fast_order_full_rotated','3-4-5-6-0-1-2'),('fast_core_5','0-5 ascending'),('fast_order_core_gray','0-5 Gray cycle')):
                line_routes(axs[i,j],name,a,m,label)
            axs[i,j].set(title=f'{"AB"[i]} / MUX{m}',xlabel='External MUX address',ylabel='Counts');axs[i,j].legend(fontsize=7)
    chart(fig,'fast_order');p('Requested 16 MHz, 5 us, no configured phase slots or cycle throttle. Full descending retains the hidden VMID visit '
        'but changes its next sensor from address 0 to 5. Full rotation preserves the cyclic analog predecessor order while moving the '
        'USB encoding/enqueue gap from after address 6 to after address 2. Gray versus ascending uses only addresses 0-5. '
        'Timing/transition counts may differ; these are diagnostic rates and comparisons, not a production optimization benchmark.',True)

    page('Bank and ADC-input read order')
    fig,axs=plt.subplots(2,3,figsize=(12,5.2),constrained_layout=True)
    for i,a in enumerate((1,2)):
        for j,delay in enumerate((1,5,50)):
            for bank,order,label in ((0,228,'ADC A first; IN1-IN4'),(1,228,'ADC B first; IN1-IN4'),(0,27,'ADC A first; IN4-IN1')):
                axs[i,j].plot(range(1,5),[center(f'readorder_{delay}_banks3_first{bank}_order{order}',a,m) for m in range(1,5)],'o-',label=label)
            axs[i,j].set(title=f'{"AB"[i]} / {delay} us',xlabel='MUX number (ADC input number)',ylabel='Median baseline (counts)');axs[i,j].legend(fontsize=6)
    chart(fig,'read_order');p('Fixed 100 us slots, six sensor addresses and one boundary VMID visit without ADC activity; period 710 us. '
        'Per-bank burst-start timestamps are in channels.csv. Input reversal changes position within the four-word burst as well as ADC predecessor.',True)

    page('Single-bank versus dual-bank activity')
    table(['Array','MUX','Delay us','Both, A first','Both, B first','Only this bank','Single - matched-first dual'],[
        ['AB'[a-1],m,delay,n(center(f'readorder_{delay}_banks3_first0_order228',a,m)),
         n(center(f'readorder_{delay}_banks3_first1_order228',a,m)),n(center(f'readorder_{delay}_banks{1<<(a-1)}_first0_order228',a,m)),
         n(center(f'readorder_{delay}_banks{1<<(a-1)}_first0_order228',a,m)-center(f'readorder_{delay}_banks3_first{a-1}_order228',a,m))]
        for a in (1,2) for m in range(1,5) for delay in (1,5,50)])
    p('Comparing single-bank to dual-bank with the same bank read first reduces the extra-dwell confound. '
        'The inactive bank is EN-disabled. Coordinated versus single-bank GPIO preparation still differs slightly.',True)

    page('VMID clamp versus discarded conversions, with controlled sensor dwell')
    fig,axs=plt.subplots(2,3,figsize=(12,5.2),constrained_layout=True)
    for i,a in enumerate((1,2)):
        for j,delay in enumerate((1,5,50)):
            for kind,label in (('idle','Disabled interval'),('vmid','VMID; no ADC conversions'),('vmid_adc','VMID; discarded ADC conversions')):
                axs[i,j].plot(range(1,5),[center(f'reset_control_{kind}_{delay}',a,m) for m in range(1,5)],'o-',label=label)
            axs[i,j].set(title=f'{"AB"[i]} / {delay} us',xlabel='MUX number',ylabel='Median baseline (counts)');axs[i,j].legend(fontsize=6)
    chart(fig,'reset_control');p('Sensor connection is held for a 100 us slot and disabled at its end. The following interval is 50 us, '
        'either disabled throughout or connected to VMID until its end. VMID-with-ADC executes four discarded conversions per bank within the same slot. '
        'Cycle period is 920 us. Even the disabled common output remains connected to its bias resistor.',True)

    page('Shorter clamp control: the same comparison at faster cycling')
    fig,axs=plt.subplots(2,2,figsize=(12,5.2),constrained_layout=True)
    for i,a in enumerate((1,2)):
        for j,delay in enumerate((1,5)):
            for kind,label in (('idle','Disabled interval'),('vmid','VMID; no ADC conversions'),('vmid_adc','VMID; discarded ADC conversions')):
                axs[i,j].plot(range(1,5),[center(f'reset_short_{kind}_{delay}',a,m) for m in range(1,5)],'o-',label=label)
            axs[i,j].set(title=f'{"AB"[i]} / {delay} us',xlabel='MUX number',ylabel='Median baseline (counts)');axs[i,j].legend(fontsize=7)
    chart(fig,'reset_short');p('Sensor connection 25 us, then disabled at slot end. Reference/disabled interval 20 us; cycle period 300 us. '
        'Both VMID variants keep the reference connected for the same 20 us. This is a separate controlled schedule, not an optimized production driver. '
        'It does not demonstrate that all production VMID conversions can be removed without retesting at the production dwell/cadence.',True)

    page('Controlled isolation and reconnection')
    fig,axs=plt.subplots(2,4,figsize=(12,5.2),constrained_layout=True)
    for a in (1,2):
        for m in range(1,5):
            ax=axs[a-1,m-1]
            for repeat,label in ((0,'First conversion'),(7,'Eighth conversion')):
                ax.plot((1,10,100,1000),[value(f'leak_b{a-1}_m{m-1}_{gap}',a,m,2,repeat) for gap in (1000,10000,100000,1000000)],'o-',label=label)
            held=value(f'held_a2_mask{1<<(m-1)}',a,m,2)
            ax.axhline(held,color='gray',linestyle=':',label='Held-address baseline')
            ax.set_xscale('log');ax.set_xlim(.8,1300);ax.set(title=f'Array {"AB"[a-1]} / MUX {m}',xlabel='Programmed off-wait (ms)',ylabel='Baseline (counts)');ax.legend(fontsize=6)
    chart(fig,'leakage');p('Address 2 is connected for a 100.1 ms preconditioning slot, then the bank is EN-disabled for the specified interval. '
        'Reconnect at address 2, wait 1 us, and retain eight conversions of one ADC input. Six cycles or more per repetition. '
        'The common-node bias continues during isolation; initial sensor voltage is not directly measured. These curves do not uniquely determine PZT leakage or capacitance.',True)

    page('Retention response and unchanged-address EN control')
    table(['Array/MUX','First code, 1 ms off','First code, 1 s off','Change counts','Change mV','Eighth code, 1 s off','1 s first-code range across reps'],[
        [f'{"AB"[a-1]}/{m}',n(value(f'leak_b{a-1}_m{m-1}_1000',a,m,2)),
         n(value(f'leak_b{a-1}_m{m-1}_1000000',a,m,2)),
         n(value(f'leak_b{a-1}_m{m-1}_1000000',a,m,2)-value(f'leak_b{a-1}_m{m-1}_1000',a,m,2)),
         n((value(f'leak_b{a-1}_m{m-1}_1000000',a,m,2)-value(f'leak_b{a-1}_m{m-1}_1000',a,m,2))*LSB*1000),
         n(value(f'leak_b{a-1}_m{m-1}_1000000',a,m,2,7)),
         n(value(f'leak_b{a-1}_m{m-1}_1000000',a,m,2,field='baseline_rep_range_counts'))] for a in (1,2) for m in range(1,5)])
    p('The address cache stays at 2 throughout the disconnected interval. Reconnection changes EN but does not rewrite address pins. '
        'An early-reading deficit in this experiment therefore does not require an address-bit transition. EN switching, charge redistribution '
        'on reconnection and ADC history remain coupled; this does not measure EN injection charge separately.')
    p('The preconditioning interval is fixed at approximately 100 ms, without a recorded voltage immediately before disconnection. '
        'Consequently, compare these as repeatable reconnection responses, not direct measurements of isolated sensor voltage, a guaranteed '
        'fully charged initial state, or an identified leakage resistance/capacitance. MUX off-state leakage, PCB leakage and the sensor can all contribute.')

    page('Known-voltage control: address 6 and 7 are the same VMID net')
    table(['Delay us','Array/MUX','Held address 6','Held address 7','Toggle: address 6','Toggle: address 7','Toggle SD, addr6 / 7'],[
        [delay,f'{"AB"[a-1]}/{m}',n(value(f'known_held6_{delay}_banks3',a,m,6)),
         n(value(f'known_held7_{delay}_banks3',a,m,7)),n(value(f'known_toggle67_{delay}_banks3',a,m,6)),
         n(value(f'known_toggle67_{delay}_banks3',a,m,7)),
         n(value(f'known_toggle67_{delay}_banks3',a,m,6,field='sd_counts'),3)+' / '+n(value(f'known_toggle67_{delay}_banks3',a,m,7,field='sd_counts'),3)]
        for delay in (1,5,50) for a in (1,2) for m in (1,2,3)])
    p('Only ADC inputs 1-3 are read; each has address 6 physically tied to its address-7 VMID network. '
        'MUX4 still switches sensor 6 versus VMID because address/EN lines are shared, so this is a whole-board low-impedance control rather than a pure isolated GPIO test. '
        'Requested cycle period 710 us; held case reads once/cycle, toggled case twice. Nominal VMID is 2048 counts.',True)

    page('Reference-only bursts: no external switching or sensor transition')
    fig,axs=plt.subplots(2,2,figsize=(12,5.2),constrained_layout=True)
    for i,a in enumerate((1,2)):
        cases=[(f'refburst_banks{1<<(a-1)}_first0_n8','Only this array'),
            ('refburst_banks3_first0_n8','Both arrays; ADC A first'),('refburst_banks3_first1_n8','Both arrays; ADC B first')]
        for name,label in cases:
            axs[i,0].plot(range(1,9),[value(name,a,1,7,r,field='mean_counts') for r in range(8)],'o-',label=label)
            axs[i,1].plot(range(1,9),[value(name,a,1,7,r,field='sd_counts') for r in range(8)],'o-',label=label)
        one=value(f'refburst_banks{1<<(a-1)}_first0_n1',a,1,7,field='mean_counts')
        axs[i,0].axhline(one,color='gray',linestyle=':',label='One conversion / cycle')
        axs[i,0].set(title=f'{"AB"[i]} / IN1 continuously at VMID',xlabel='Conversion in burst',ylabel='Mean code (counts)')
        axs[i,1].set(title=f'{"AB"[i]} / IN1 continuously at VMID',xlabel='Conversion in burst',ylabel='SD (counts)')
        for ax in axs[i]:ax.legend(fontsize=7)
    chart(fig,'reference_burst');p('External MUX address remains 7 throughout acquisition and ADC input remains IN1. No external MUX or ADC-input '
        'switching occurs after preparation. Fixed 120 us reference-wait phase plus 100 us read slot, period 250 us. '
        'Means expose sub-count code occupancy; values are medians of three per-record means. A within-burst shift here cannot be attributed '
        'to a sensor transition. It does not uniquely distinguish ADC behavior, buffer drive, shared VREF/VBIAS transients or digital coupling.',True)

    page('Shared conversion history and the VBIAS / VREF ratio')
    table(['Bank activity','Array','First mean code','Eighth mean code','Change counts','Single-bank one-conversion mean'],[
        [label,'AB'[a-1],n(value(name,a,1,7,0,field='mean_counts'),4),n(value(name,a,1,7,7,field='mean_counts'),4),
         n(value(name,a,1,7,7,field='mean_counts')-value(name,a,1,7,0,field='mean_counts'),4),
         n(value(f'refburst_banks{1<<(a-1)}_first0_n1',a,1,7,field='mean_counts'),4)]
        for name,label,arrays in [('refburst_banks1_first0_n8','A only',(1,)),('refburst_banks2_first0_n8','B only',(2,)),
            ('refburst_banks3_first0_n8','A then B',(1,2)),('refburst_banks3_first1_n8','B then A',(2,1))] for a in arrays])
    p('All inputs in this control are held on a low-impedance reference connection. A change that follows conversion position, including '
        'which ADC bank was read first, is evidence of shared conversion history. It cannot be assigned solely to external MUX switching '
        'or PZT charging. ADC behavior, buffer output drive, reference/supply transients and digital coupling remain possible contributors.')
    p('Near midscale, N approximately equals 4096 x VBIAS / VREF. Small changes obey: '
        'delta-N approximately equals (4096 / 3.3) x (delta-VBIAS - 0.5 x delta-VREF). '
        'For illustration, a +3-count change could correspond to VBIAS/buffer output rising about 2.42 mV with VREF fixed, '
        'or VREF falling about 4.83 mV with VBIAS fixed. These are equivalent-voltage calculations, not rail measurements.')
    p('The ADC124S101 uses its analog supply VA as its reference and draws conversion-dependent current. '
        'REF2033 VREF and VBIAS sharing an internal source does not guarantee cancellation of different fast output-load disturbances. '
        'Simultaneous probing of ADC VA/VREF, VMID and buffer output would distinguish these possibilities. '
        'The report does not identify a defective component or demonstrate a production fix.',True)

    # Long records: keep every spike. PSDs use contiguous sequence blocks only.
    noise=[];correlations=[];quiet_channels=[];quiet_metadata={};spikes=[];timing_correlations=[]
    longdata={}
    display_name={'long_scan':'Scan; no explicit VMID','long_scan_vmid':'Scan; VMID after each address','long_held':'Hold MUX address 2'}
    for name in ('long_scan','long_scan_vmid','long_held'):
        f=raw_root/'captures'/(name+'_r1.json')
        if not f.exists():continue
        m=json.loads(f.read_text());z=np.load(f.with_suffix('.npz'));t,keep=steady(m,z)
        v=z['values'][keep].astype(float)
        et=z['events'][keep].astype(np.int64)
        event_keys=list(dict.fromkeys((r['step'],r['array']) for r in m['labels']))
        quiet=between_bursts(t[keep],v,np.ones(len(v),dtype=bool))
        variance_mask,screen=burst_window_screen(t[keep],v)
        qmask=longest_quiet(quiet['mask']&variance_mask,z['sequence'][keep])
        qt=t[keep][qmask]
        qseconds=float(qt[-1]-qt[0]+np.median(np.diff(t[keep])))
        quiet_metadata[name]=dict(variance_screen=screen,coherent_rejected_fraction=quiet['fine_rejected_fraction'],
            quiet_start_s=float(qt[0]),quiet_end_s=float(qt[-1]),quiet_seconds=qseconds)
        longdata[name]=(m,z,t,keep,qmask)
        for col,route in enumerate(m['labels']):
            qv=v[qmask,col]
            quiet_channels.append(dict(name=name,**route,raw_sd_counts=float(np.std(v[:,col],ddof=1)),
                raw_ptp_counts=float(np.ptp(v[:,col])),quiet_sd_counts=float(np.std(qv,ddof=1)),
                quiet_sd_volts=float(np.std(qv,ddof=1))*LSB,quiet_ptp_counts=float(np.ptp(qv)),
                quiet_ptp_volts=float(np.ptp(qv))*LSB,quiet_start_s=float(qt[0]),quiet_end_s=float(qt[-1]),quiet_seconds=qseconds))
        for arr in (1,2):
            indices=[i for i,r in enumerate(m['labels']) if r['array']==arr]
            pc=max(indices,key=lambda i:np.ptp(v[:,i]));pr=m['labels'][pc]
            spikes.append(dict(name=name,array=arr,mux=pr['mux'],address=pr['address'],column=pc,
                ptp_counts=float(np.ptp(v[:,pc])),sd_counts=float(np.std(v[:,pc],ddof=1))))
            col=max(indices,key=lambda i:np.std(v[:,i]));r=m['labels'][col]
            event_index=event_keys.index((r['step'],r['array']))
            delay_ticks=et[:,event_index,1]-et[:,event_index,0]
            duration_ticks=et[:,event_index,2]-et[:,event_index,1]
            cpu=int(m['status']['cpu_hz'])
            def rho(ticks):return float(np.corrcoef(v[:,col],ticks)[0,1]) if np.ptp(ticks) and np.ptp(v[:,col]) else None
            timing_correlations.append(dict(name=name,array=arr,mux=r['mux'],address=r['address'],
                delay_median_us=float(np.median(delay_ticks))*1e6/cpu,delay_span_ns=float(np.ptp(delay_ticks))*1e9/cpu,
                duration_median_us=float(np.median(duration_ticks))*1e6/cpu,duration_span_ns=float(np.ptp(duration_ticks))*1e9/cpu,
                code_delay_correlation=rho(delay_ticks),code_duration_correlation=rho(duration_ticks)))
            noise.append(dict(name=name,array=arr,mux=r['mux'],address=r['address'],column=col,
                sd_counts=float(np.std(v[:,col],ddof=1)),ptp_counts=float(np.ptp(v[:,col])),
                min_counts=float(v[:,col].min()),max_counts=float(v[:,col].max()),
                quiet_sd_counts=float(np.std(v[qmask,col],ddof=1)),
                quiet_sd_volts=float(np.std(v[qmask,col],ddof=1))*LSB,
                quiet_ptp_counts=float(np.ptp(v[qmask,col])),
                quiet_ptp_volts=float(np.ptp(v[qmask,col]))*LSB,quiet_seconds=qseconds,
                quiet_start_s=float(qt[0]),quiet_end_s=float(qt[-1]),detector_quiet_seconds=quiet['quiet_seconds'],
                rejected_fraction=quiet['fine_rejected_fraction']))
        for address in sorted({r['address'] for r in m['labels']}):
            ii=[i for i,r in enumerate(m['labels']) if r['address']==address and np.std(v[:,i])>.05]
            if len(ii)<2:continue
            a=v[:,ii];raw=np.corrcoef(a,rowvar=False)
            hp=a.copy();seconds=np.floor(t[keep]).astype(int)
            for sec in np.unique(seconds):hp[seconds==sec]-=hp[seconds==sec].mean(axis=0)
            corr=np.corrcoef(hp,rowvar=False)
            for i in range(len(ii)):
                for j in range(i):
                    r1=m['labels'][ii[i]];r2=m['labels'][ii[j]]
                    correlations.append(dict(name=name,address=address,route1=f'{r1["array"]}:{r1["mux"]}:{address}',
                        route2=f'{r2["array"]}:{r2["mux"]}:{address}',raw_correlation=float(raw[i,j]),
                        within_second_correlation=float(corr[i,j])))
    if noise:
        page('Long records: time traces and spikes')
        fig,axs=plt.subplots(3,2,figsize=(12,5.2),constrained_layout=True)
        for i,name in enumerate(('long_scan','long_scan_vmid','long_held')):
            if name not in longdata:continue
            m,z,t,keep,qmask=longdata[name];v=z['values'][keep];tt=t[keep]
            for j,arr in enumerate((1,2)):
                r=next(r for r in noise if r['name']==name and r['array']==arr);vv=v[:,r['column']].astype(float);vv-=np.median(vv)
                bins=(tt*10).astype(int);unique=np.unique(bins)
                low=np.minimum.reduceat(vv,np.searchsorted(bins,unique));high=np.maximum.reduceat(vv,np.searchsorted(bins,unique))
                ax=axs[i,j];ax.fill_between(unique/10,low,high,alpha=.5)
                ax.set(title=f'{display_name[name]} / Array {"AB"[j]} / MUX {r["mux"]} / address {r["address"]}',xlabel='Time since run start (s)',ylabel='Deviation (counts)')
        chart(fig,'long_time');p('The highest-SD route in each array/condition is shown. Each 100 ms envelope retains minimum and maximum samples, '
            'so spikes are not hidden by averaging. Warm-up is excluded. These are 120-second recordings.',True)
        page('Largest excursions: independently selected by peak-to-peak range')
        fig,axs=plt.subplots(3,2,figsize=(12,5.2),constrained_layout=True)
        for i,name in enumerate(('long_scan','long_scan_vmid','long_held')):
            if name not in longdata:continue
            m,z,t,keep,qmask=longdata[name];v=z['values'][keep].astype(float);tt=t[keep]
            for j,arr in enumerate((1,2)):
                r=next(r for r in spikes if r['name']==name and r['array']==arr);vv=v[:,r['column']];base=np.median(vv)
                event=float(tt[np.argmax(np.abs(vv-base))]);lo=max(float(tt[0]),min(event-.0125,float(tt[-1])-.025))
                clip=(tt>=lo)&(tt<=lo+.025)
                ax=axs[i,j];ax.plot((tt[clip]-lo)*1000,vv[clip]-base,'.-',ms=3,lw=.8)
                ax.set(title=f'{display_name[name]} / Array {"AB"[j]} / MUX {r["mux"]} / address {r["address"]} / p-p {r["ptp_counts"]:g} counts',
                    xlabel=f'Time in 25 ms window starting {lo:.3f} s (ms)',ylabel='Deviation (counts)')
        chart(fig,'long_spikes',height=300)
        table(['Condition','Array/MUX/address','Raw p-p counts / mV','Raw SD counts / mV'],[
            [r['name'],f'{"AB"[r["array"]-1]}/{r["mux"]}/{r["address"]}',n(r['ptp_counts'],0)+' / '+n(r['ptp_counts']*LSB*1000,3),
             n(r['sd_counts'],4)+' / '+n(r['sd_counts']*LSB*1000,4)] for r in spikes])
        p('Selection maximizes the full-record peak-to-peak range independently of the SD-based selections. '
            'Every sampled point in the 25 ms window is shown. A one-count excursion may be ordinary ADC code toggling; '
            'a large range alone does not establish whether an event was electrical noise or a real PZT response.',True)
        page('Long records: power spectral density')
        fig,axs=plt.subplots(3,2,figsize=(12,5.2),constrained_layout=True)
        for i,name in enumerate(('long_scan','long_scan_vmid','long_held')):
            if name not in longdata:continue
            m,z,t,keep,qmask=longdata[name];seq=z['sequence'][keep].astype(np.int64);v=z['values'][keep].astype(float)
            edges=np.r_[0,np.flatnonzero(np.diff(seq)!=1)+1,len(seq)]
            nseg=min(2048,int(np.max(np.diff(edges))))
            fs=1e6/float(np.median(np.diff(z['timing'][keep,0].astype(np.int64))))
            for j,arr in enumerate((1,2)):
                r=next(r for r in noise if r['name']==name and r['array']==arr)
                spectra=[];weights=[]
                for lo,hi in zip(edges[:-1],edges[1:]):
                    if hi-lo<nseg:continue
                    f,power=welch(v[lo:hi,r['column']],fs=fs,nperseg=nseg,detrend='constant');spectra.append(power);weights.append(hi-lo)
                if spectra:
                    power=np.average(spectra,axis=0,weights=weights)[1:]
                    if np.any(power>0):axs[i,j].semilogy(f[1:],np.where(power>0,power,np.nan),label='Raw')
                qseq=seq[qmask];qv=v[qmask,r['column']]
                qe=np.r_[0,np.flatnonzero(np.diff(qseq)!=1)+1,len(qseq)]
                qp=[];qw=[]
                for lo,hi in zip(qe[:-1],qe[1:]):
                    if hi-lo<nseg:continue
                    qf,power=welch(qv[lo:hi],fs=fs,nperseg=nseg,detrend='constant');qp.append(power);qw.append(hi-lo)
                if qp:
                    power=np.average(qp,axis=0,weights=qw)[1:]
                    if np.any(power>0):axs[i,j].semilogy(qf[1:],np.where(power>0,power,np.nan),label='Longest quiet',alpha=.8)
                    else:axs[i,j].text(.98,.95,'Quiet interval: constant ADC code',ha='right',va='top',transform=axs[i,j].transAxes,fontsize=7)
                axs[i,j].set(title=f'{display_name[name]} / Array {"AB"[j]} / MUX {r["mux"]} / address {r["address"]}',xlabel='Frequency (Hz)',ylabel='Counts squared / Hz')
                axs[i,j].legend(fontsize=7)
        chart(fig,'long_psd');p('Welch PSD, up to 2048 samples/window, constant detrending. Only contiguous received sequence blocks are used; '
            'USB omissions are not joined or interpolated. Constant-code quiet intervals have zero recorded PSD and are annotated; '
            'they do not establish a zero analog-noise floor. Peak locations alone do not identify the interference source.',True)
        page('Long-record noise and shared correlations')
        table(['Condition','Array/MUX/address','Raw SD counts / mV','Raw p-p counts / mV','Quiet SD counts / mV','Quiet p-p counts','Quiet seconds'],[
            [r['name'],f'{"AB"[r["array"]-1]}/{r["mux"]}/{r["address"]}',
             n(r['sd_counts'],4)+' / '+n(r['sd_counts']*LSB*1000,4),
             n(r['ptp_counts'],0)+' / '+n(r['ptp_counts']*LSB*1000,3),
             n(r['quiet_sd_counts'],4)+' / '+n(r['quiet_sd_volts']*1000,4),
             n(r['quiet_ptp_counts'],0),n(r['quiet_seconds'],2)] for r in noise])
        p('Common-quiet statistics use the prior report\'s separated-state detector: a dominant one-code (60%) or adjacent two-code (80%) '
          'state anchors each route; allow a one-count margin. A separated excursion needs at least 1% tail mass and a peak at least 0.5% of '
          'samples and three times the edge density. At least two routes must depart in the same sweep; that sweep and one neighbor each side '
          'are rejected identically for every route. A sustained-burst screen also rejects any one-second bin whose SD on any route exceeds '
          'max(0.75 count, four times that route\'s median bin SD), padding one bin each side. Then select the longest remaining uninterrupted received interval; do not join different '
          'baseline states. This is duration selection, not selection for minimum SD. Isolated spikes inside that interval remain. '
          'Raw traces and raw statistics retain every event. Exact intervals and all routes\' quiet statistics are in long_quiet_channels.csv.',True)
        strong=sorted(correlations,key=lambda r:abs(r['within_second_correlation']),reverse=True)[:14]
        table(['Condition','Route 1','Route 2','Raw correlation','Within-second correlation'],[
            [r['name'],r['route1'],r['route2'],n(r['raw_correlation'],3),n(r['within_second_correlation'],3)] for r in strong])
        p('Pairs are restricted to the same external-address phase; inputs are acquired sequentially, not perfectly simultaneously. '
            'Only routes with SD above 0.05 count are compared. Within-second correlation subtracts each route\'s mean in each one-second bin. '
            'These are descriptive associations, with multiple comparisons; they do not establish a physical coupling source.',True)
        p('The unreset scan record contains a burst/step event late in the record. The later VMID and held-address records are quieter, '
          'but one record per condition at different times cannot establish whether mode or an uninstrumented external disturbance caused that difference.',True)
    csv_write(out/'long_noise.csv',noise);csv_write(out/'correlations.csv',correlations);csv_write(out/'long_quiet_channels.csv',quiet_channels);csv_write(out/'long_spikes.csv',spikes)
    (out/'long_quiet_metadata.json').write_text(json.dumps(quiet_metadata,indent=2))
    csv_write(out/'timing_correlations.csv',timing_correlations)

    page('Capture integrity and firmware restoration')
    life=x['lifecycle'];s=life.get('final_status',{})
    table(['Check','Result'],[
        ['Expected / complete captures',f'{x["expected_captures"]} / {len(x["captures"])}'],
        ['Acquired / received / discarded cycles',f'{total:,} / {received:,} / {dropped:,}'],
        ['Transfer / data / USB write errors',str([sum(r[k] for r in x['captures']) for k in ('transfer_errors','data_errors','usb_write_errors')])],
        ['Controlled timing overruns',str(sum(r['overruns'] for r in x['captures']))],
        ['Frame integrity','All captured frames decoded with ADG1 schema, physical event labels, monotonic sequence and FNV-1a checksum'],
        ['Frozen production image','Original HEX and settings backed up before diagnostic upload; restore uses that frozen HEX'],
        ['Restored firmware / stopped',s.get('firmware_revision','-')+' / '+s.get('running','-')],
        ['Restored engine / scan order',s.get('spiengine','-')+' / '+s.get('scanorder','-')],
        ['Restoration state',life['state']]])
    p('The diagnostic environment excludes production main.cpp. It retains the production GPIO switching guards and LPSPI driver, '
        'and adds arbitrary external-address programs, input/bank ordering, isolated waits and retained repeated conversions. '
        'Its identity and binary magic differ from production; desktop GUI parsing is unchanged.',True)
    p('Validation: 810 tagged diagnostic configurations with three consecutive-cycle checks each; malformed commands, '
        'isolation, checksum and failure checks. Production host-model regression also passed. All physical channel labels in '
        'quiet recordings still rely on the documented board wiring; no external stimulus was applied to establish analog channel identity.',True)
    table(['Long record / array','MUX/address','Delay median us','Delay range ns','Burst duration range ns','Code/delay correlation','Code/duration correlation'],[
        [r['name']+' / '+'AB'[r['array']-1],f'{r["mux"]}/{r["address"]}',n(r['delay_median_us'],4),n(r['delay_span_ns'],2),
         n(r['duration_span_ns'],2),n(r['code_delay_correlation'],4),n(r['code_duration_correlation'],4)] for r in timing_correlations])
    p('These compare the SD-selected routes against logged per-bank burst timing. A constant marker has undefined correlation (dash). '
        'Markers do not resolve actual pin edges or sample apertures; absence of correlation does not rule out electrical USB/supply coupling.',True)

    page('Device references and limits of causal attribution')
    for title,url in [('ADC124S101 data sheet','https://www.ti.com/lit/ds/symlink/adc124s101.pdf'),
        ('TMUX1108 data sheet','https://www.ti.com/lit/ds/symlink/tmux1108.pdf'),
        ('TMUX1308A data sheet','https://www.ti.com/lit/ds/symlink/tmux1308a.pdf'),
        ('OPA365 data sheet','https://www.ti.com/lit/ds/symlink/opa365.pdf'),
        ('REF2033 family data sheet','https://www.ti.com/lit/pdf/sbos600'),
        ('TI high-impedance MUX settling application note','https://www.ti.com/lit/pdf/SCDA054')]:
        p(f'<a href="{url}" color="#174f73">{title}</a> - {url}')
    p('ADC124S101 uses a three-clock acquisition interval and a typical 30 pF sampling capacitor; its data sheet explicitly does not '
      'require dummy conversions for power-up. More conversions changing a measured board baseline therefore does not establish a digital protocol '
      'requirement to discard a sample. They also change sampling-load activity and supply-current history. The OPA365 separates the external MUX '
      'node from the ADC sampling capacitor; directly multiplying that capacitor by the external bias resistor is inappropriate.')
    p('These software-controlled experiments do not separately measure MUX injection charge, PZT capacitance, pin leakage, reference ripple, '
      'buffer stability or physical GPIO/SPI waveforms. MUX type is tied to array location and associated components. The retained data support '
      'comparisons between operating histories; component-level attribution requires a controlled hardware substitution or electrical probing.')

    page('Program manifest and timing')
    capby={}
    for r in x['captures']:capby.setdefault(r['name'],[]).append(r)
    table(['Program','Family','Reps','Clock MHz','Banks','Steps','Requested period us','Measured median period us'],[
        [name,p['family'],len(capby.get(name,[])),p['clock']//1000000,p['arrays'],len(p['steps']),p['period'],
         n(float(np.median([r['period_median_us'] for r in capby.get(name,[]) if r['period_median_us'] is not None])),2)
         if capby.get(name) else '-'] for name,p in x['programs'].items()],widths=[205,100,35,45,35,35,95,108])
    def footer(canvas,doc):
        canvas.setFont('Helvetica',7);canvas.setFillColor(colors.HexColor('#526575'))
        canvas.drawString(42,24,'TestBoard_ADC124 | PCB diagnostics | 2026-10-15')
        canvas.drawRightString(800,24,str(doc.page))
    pdf.parent.mkdir(parents=True,exist_ok=True)
    # Keep figure/statistic preparation independent of the reader-facing document.
    # The explanation-first composer replaces the original terse story above.
    from pcb_diagnostics_report_text import write_report
    write_report(x,out,pdf,figdir,noise,spikes,correlations,timing_correlations)
    print(pdf,flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,default=OUT);p.add_argument('--pdf',type=Path,default=PDF)
    p.add_argument('--allow-partial',action='store_true');a=p.parse_args();build(a.out,a.pdf,a.allow_partial)
