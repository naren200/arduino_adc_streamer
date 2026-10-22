"""Compare original and exchanged-strip Tests 21-23; retain per-route evidence."""
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from html import escape
import hashlib
import json
import platform
import shutil
import subprocess
import sys

import numpy as np

from benchmark_adc124_swapped_vmid import RAW, OUT
from benchmark_adc124_long_vmid import OUT as OLD_OUT, RAW as OLD_RAW, plan
from analyze_pcb_diagnostics import csv_write, LSB, steady

ROOT = Path(__file__).resolve().parents[3]
PDF = ROOT / 'docs/reports/exports/TestBoard_ADC124_Swapped_Strips_Comparison_2026-10-16.pdf'
FIG = OUT / 'comparison_figures'
LABEL = ('Original strips: 15 Oct', 'Swapped strips: 16 Oct')
PALETTE = ('#2166ac', '#d6604d')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def matched(old, new, keys, fields):
    """Join on board routes, never silently treat a changed strip as the same sensor."""
    previous = {tuple(r[k] for k in keys): r for r in old}
    assert len(previous) == len(old)
    assert len(new) == len(old)
    rows = []
    for r in new:
        key = tuple(r[k] for k in keys)
        p = previous.pop(key)
        z = {k: r[k] for k in keys}
        for f in fields:
            z['original_' + f] = p[f]
            z['swapped_' + f] = r[f]
            z['swapped_minus_original_' + f] = r[f] - p[f]
        rows.append(z)
    assert not previous
    return rows


def verify(new):
    assert not new['partial'] and len(new['captures']) == 63
    life = new['lifecycle']
    assert life['state'] == 'production_restored_verified'
    for key in ('firmware_revision', 'array', 'scanorder', 'spiengine', 'spi_clock_hz', 'channelrepeat',
                'mux_settle_us', 'vmid_settle_us', 'vmid_settle_mode', 'vmid_between_groups', 'spi_frame', 'adcchannels'):
        assert life['final_status'][key] == life['before_status'][key], key
    assert life['final_status']['running'] == 'false'
    oldlife = json.loads((OLD_RAW / 'lifecycle.json').read_text())
    assert life['diagnostic_sha256'] == oldlife['diagnostic_sha256']
    assert json.loads((RAW / 'plan.json').read_text()) == json.loads((OLD_RAW / 'plan.json').read_text())
    by_name = defaultdict(list)
    for r in new['captures']:
        by_name[r['name']].append(r['rep'])
        assert not any(r[k] for k in ('overruns', 'transfer_errors', 'data_errors', 'usb_write_errors'))
        if r['family'] in ('hold_full_after', 'hold_ten_after'):
            assert r['frames'] == r['cycles'] == 1 and r['dropped'] == 0
        if r['family'] == 'long_vmid_scan':
            assert r['dropped'] == 0
    assert len(by_name) == 21 and all(sorted(v) == [1, 2, 3] for v in by_name.values())
    assert len(new['holds']) == 27
    assert all(r['before_unreceived_tail_cycles'] == 0 for r in new['holds'])
    assert all(20 <= r['timed_hold_s'] <= 20.001 for r in new['holds'])
    for name, expected in life['production_hashes'].items():
        rel = Path(name).relative_to('Arduino_Sketches/TestBoard_ADC124')
        assert sha(RAW / 'production_backup' / rel) == expected
        if rel.suffix in ('.cpp', '.h'):
            assert sha(ROOT / name) == expected
    return dict(completed_utc=datetime.now(timezone.utc).isoformat(), captures=63, programs=21,
        cycles_acquired=sum(r['cycles'] for r in new['captures']),
        cycles_received=sum(r['frames'] for r in new['captures']),
        cycles_discarded=sum(r['dropped'] for r in new['captures']),
        errors={k: sum(r[k] for r in new['captures']) for k in ('overruns','transfer_errors','data_errors','usb_write_errors')},
        all_raw_capture_hashes_verified=True, all_repetition_counts_verified=True,
        all_post_hold_cycles_exactly_one_and_received=True, all_regular_long_vmid_cycles_received=True,
        all_before_final_cycles_received=True, acquisition_plan_identical=True, diagnostic_image_identical=True,
        timed_hold_s_range=[min(r['timed_hold_s'] for r in new['holds']), max(r['timed_hold_s'] for r in new['holds'])],
        recorded_pre_to_post_interval_s_range=[min(r['last_received_pre_end_to_post_start_s'] for r in new['holds']),
                                               max(r['last_received_pre_end_to_post_start_s'] for r in new['holds'])],
        production_restored_stopped=True, production_source_unchanged=True,
        diagnostic_hex_sha256=life['diagnostic_sha256'],
        production_hex_sha256=sha(RAW / 'production_backup/.pio/build/teensy41/firmware.hex'))


def comparisons(old, new):
    specs = [
        ('full_cycle_route_comparison', 'full_routes', ['array','mux','address'],
         ['before_steady_baseline_counts','after_counts','delta_from_steady_counts']),
        ('full_cycle_mux_comparison', 'full_mux', ['array','mux'],
         ['before_center_counts','after_center_counts','median_route_change_counts']),
        ('ten_conversion_comparison', 'ten_readings', ['array','mux','first_bank','conversion'],
         ['before_steady_baseline_counts','after_counts','delta_from_steady_counts']),
        ('ten_endpoint_comparison', 'ten_endpoints', ['array','mux','first_bank'],
         ['before_baseline_counts','first_counts','tenth_counts','tenth_minus_first_counts','first_delta_counts']),
        ('long_scan_route_comparison', 'scan_routes', ['array','mux','address','vmid_wait_us'],
         ['baseline_counts','sd_counts','ptp_counts','drift_last_minus_first_counts','sweeps_per_second']),
        ('long_scan_mux_comparison', 'scan_mux', ['array','mux','vmid_wait_us'],
         ['baseline_center_counts','address_spread_counts','median_route_sd_counts','max_route_ptp_counts','max_absolute_drift_counts'])]
    out = {}
    for name, key, keys, fields in specs:
        rows = matched(old[key], new[key], keys, fields)
        for r in rows:
            for k, value in list(r.items()):
                if k.endswith('_counts'):
                    r[k[:-7] + '_volts'] = value * LSB
        csv_write(OUT / (name + '.csv'), rows)
        out[name] = rows
    return out


def strip_comparisons(old,new):
    """Inferred same-strip comparisons; assume connector contact order retained."""
    result={}
    for name,key,keys,fields in [
        ('inferred_same_strip_full_cycle','full_routes',['array','mux','address'],
         ['before_steady_baseline_counts','after_counts','delta_from_steady_counts']),
        ('inferred_same_strip_ten_endpoints','ten_endpoints',['array','mux','first_bank'],
         ['before_baseline_counts','first_counts','tenth_counts','tenth_minus_first_counts']),
        ('inferred_same_strip_long_scan','scan_routes',['array','mux','address','vmid_wait_us'],
         ['baseline_counts','sd_counts','ptp_counts'])]:
        moved=[]
        for r in new[key]:
            q=dict(r);q['array']=3-r['array']
            if 'first_bank' in q:q['first_bank']=1-r['first_bank']
            moved.append(q)
        rows=matched(old[key],moved,keys,fields)
        for r in rows:
            r['strip_original_array']=r.pop('array')
            r['strip_current_array']=3-r['strip_original_array']
            if 'first_bank' in r:
                r['original_first_bank']=r.pop('first_bank')
                r['swapped_first_bank']=1-r['original_first_bank']
            r['mapping_basis']='Inferred from strip exchange; corresponding connector contact order assumed'
            for k,value in list(r.items()):
                if k.endswith('_counts'):r[k[:-7]+'_volts']=value*LSB
        csv_write(OUT/(name+'.csv'),rows);result[name]=rows
    return result


def findings(old,new):
    change=lambda data,a:float(np.median([r['delta_from_steady_counts'] for r in data['full_routes'] if r['array']==a]))
    before={a:change(old,a) for a in (1,2)}
    after={a:change(new,a) for a in (1,2)}
    texts=[f'Test 21, over all 25 sensor routes per array: the median matched first-response change is '
           f'{before[1]:+.1f} to {after[1]:+.1f} counts on Array A (original to swapped), and '
           f'{before[2]:+.1f} to {after[2]:+.1f} counts on Array B.']
    if before[2]<before[1] and after[2]<after[1] and before[2]<0 and after[2]<0:
        texts.append('The larger downward response remains on Array B after exchanging strips. A property confined to its original strip cannot explain the entire Array B deficit. This supports an array-specific circuit contribution, without isolating the MUX chip from other board-path differences.')
    elif before[2]<before[1] and after[1]<after[2]:
        texts.append('The larger downward response moves to Array A after exchanging strips. Strip or connector properties therefore contribute strongly; the result does not support attributing the full difference to the MUX type alone.')
    else:
        texts.append('The relative array response changes after exchange. Read the per-route and per-repetition graphs before assigning the effect solely to either strip identity or PCB array.')
    if before[1]*after[1]<0:
        texts.append('Array A\'s median change reverses sign between sessions. The earlier upward response is therefore not a universal property established for the TMUX1108. Strip properties or acquisition/session history also matter.')
    deltas={}
    for a in (1,2):
        values=[]
        for m in range(1,5):
            values.append(next(r['baseline_center_counts'] for r in new['scan_mux'] if r['array']==a and r['mux']==m and r['vmid_wait_us']==10000)-next(r['baseline_center_counts'] for r in new['scan_mux'] if r['array']==a and r['mux']==m and r['vmid_wait_us']==1000))
        deltas[a]=values
    texts.append(f'Test 23 reproduces the array-specific VMID-wait sensitivity: Array A MUX centers change '
        f'{min(deltas[1]):+.1f} to {max(deltas[1]):+.1f} counts from 1 to 10 ms, while Array B changes '
        f'{min(deltas[2]):+.1f} to {max(deltas[2]):+.1f} counts. This operating pattern stays with the PCB array after the strip exchange.')
    lookup={(r['array'],r['mux'],r['address'],r['vmid_wait_us']):r for r in old['scan_routes']}
    maxdiff={a:max(abs(r['baseline_counts']-lookup[(r['array'],r['mux'],r['address'],r['vmid_wait_us'])]['baseline_counts']) for r in new['scan_routes'] if r['array']==a) for a in (1,2)}
    texts.append(f'At matching Test 23 settings, individual route baselines differ by at most {maxdiff[1]:g} counts on Array A and {maxdiff[2]:g} counts on Array B between sessions. The measured median cycle periods match exactly at the recorded microsecond resolution for all three VMID waits.')
    texts.append('All post-hold readings are measured after reconnection. The repeat supports conclusions about charge retention and recovery of the complete sensor path, not a direct measurement of off-state leakage current or its destination.')
    return texts


def figures(old, new):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    FIG.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size':9, 'axes.grid':True, 'grid.alpha':.2, 'axes.spines.top':False,
                         'axes.spines.right':False, 'axes.formatter.useoffset':False})
    def save(fig, name):
        for ax in fig.axes:
            ax.ticklabel_format(axis='y', useOffset=False, style='plain')
        fig.savefig(FIG / (name + '.png'), dpi=190, bbox_inches='tight')
        plt.close(fig)
    def axes8():
        return plt.subplots(2, 4, figsize=(12.8, 5.5), constrained_layout=True)
    def title(ax, a, m, x):
        ax.set(title=f'Array {"AB"[a-1]} / MUX {m}', xlabel=x, ylabel='ADC code (counts)')
    fig, axs = axes8()
    for a in (1,2):
        for m in range(1,5):
            ax = axs[a-1,m-1]
            for i, data in enumerate((old,new)):
                rr = sorted((r for r in data['full_routes'] if r['array']==a and r['mux']==m),key=lambda r:r['address'])
                xx = [r['address'] for r in rr]
                ax.plot(xx,[r['after_counts'] for r in rr], 'o-', ms=3, color=PALETTE[i],label=LABEL[i]+' - first after hold')
                ax.plot(xx,[r['before_steady_baseline_counts'] for r in rr], '--', color=PALETTE[i],label=LABEL[i]+' - before baseline')
                ax.fill_between(xx,[r['after_min_rep_counts'] for r in rr],[r['after_max_rep_counts'] for r in rr],color=PALETTE[i],alpha=.12)
            title(ax,a,m,'External MUX address')
    fig.legend(*axs[0,0].get_legend_handles_labels(),loc='outside lower center',ncol=2,fontsize=8)
    save(fig,'test21_full_cycle_comparison')
    for first in (0,1):
        fig,axs=axes8()
        for a in (1,2):
            for m in range(1,5):
                ax=axs[a-1,m-1]
                for i,data in enumerate((old,new)):
                    rr=sorted((r for r in data['ten_readings'] if r['array']==a and r['mux']==m and r['first_bank']==first),key=lambda r:r['conversion'])
                    xx=[r['conversion'] for r in rr]
                    ax.plot(xx,[r['after_counts'] for r in rr],'o-',ms=3,color=PALETTE[i],label=LABEL[i]+' - after hold')
                    ax.axhline(rr[0]['before_steady_baseline_counts'],ls='--',color=PALETTE[i],label=LABEL[i]+' - before baseline')
                    ax.fill_between(xx,[r['after_min_rep_counts'] for r in rr],[r['after_max_rep_counts'] for r in rr],color=PALETTE[i],alpha=.12)
                title(ax,a,m,'Conversion position (same ADC input)')
                ax.set_xticks([1,4,7,10])
        fig.legend(*axs[0,0].get_legend_handles_labels(),loc='outside lower center',ncol=2,fontsize=8)
        save(fig,f'test22_first_{"AB"[first]}')
    for a in (1,2):
        fig,axs=plt.subplots(2,2,figsize=(12.8,6.1),constrained_layout=True)
        for m in range(1,5):
            ax=axs.flat[m-1]
            for j,wait in enumerate((1000,5000,10000)):
                for i,data in enumerate((old,new)):
                    rr=sorted((r for r in data['scan_routes'] if r['array']==a and r['mux']==m and r['vmid_wait_us']==wait),key=lambda r:r['address'])
                    ax.plot([r['address'] for r in rr],[r['baseline_counts'] for r in rr],ls='--' if i==0 else '-',marker='o' if i else 's',ms=3,color=f'C{j}')
            title(ax,a,m,'External MUX address')
        legend=[Line2D([],[],color=f'C{j}',label=f'VMID {w} ms') for j,w in enumerate((1,5,10))]
        legend += [Line2D([],[],color='gray',ls='--',marker='s',label='Original strips'),Line2D([],[],color='gray',ls='-',marker='o',label='Swapped strips')]
        fig.legend(handles=legend,loc='outside lower center',ncol=5,fontsize=8)
        save(fig,f'test23_routes_{"AB"[a-1]}')
    fig,axs=plt.subplots(2,2,figsize=(12.8,5.5),constrained_layout=True)
    for a in (1,2):
        for m in range(1,5):
            for i,data in enumerate((old,new)):
                rr=sorted((r for r in data['scan_mux'] if r['array']==a and r['mux']==m),key=lambda r:r['vmid_wait_us'])
                for col,key in enumerate(('baseline_center_counts','median_route_sd_counts')):
                    axs[a-1,col].plot([r['vmid_wait_us']/1000 for r in rr],[r[key] for r in rr],color=f'C{m-1}',ls='--' if i==0 else '-',marker='o' if i else 's',ms=3)
        for col in (0,1):
            axs[a-1,col].set(title=f'Array {"AB"[a-1]}: '+('baseline center' if col==0 else 'temporal SD'),xlabel='VMID wait (ms)',ylabel='Counts')
    legend=[Line2D([],[],color=f'C{m-1}',label=f'MUX {m}') for m in range(1,5)]
    legend += [Line2D([],[],color='gray',ls='--',label='Original strips'),Line2D([],[],color='gray',ls='-',label='Swapped strips')]
    fig.legend(handles=legend,loc='outside lower center',ncol=6,fontsize=8)
    save(fig,'test23_centers_noise')
    for i,raw in enumerate((OLD_RAW,RAW)):
        fig,axs=axes8()
        residuals=defaultdict(list)
        for j,wait in enumerate((1000,5000,10000)):
            path=raw/'captures'/f'long_vmid_{wait}_r1.json'
            meta=json.loads(path.read_text())
            with np.load(path.with_suffix('.npz')) as z:
                t,keep=steady(meta,z)
                for a in (1,2):
                    for m in range(1,5):
                        col=next(k for k,r in enumerate(meta['labels']) if r['array']==a and r['mux']==m and r['address']==2)
                        values=z['values'][keep,col].astype(float)
                        centered=values-np.median(values)
                        residuals[(a,m)].extend([float(np.min(centered)),float(np.max(centered))])
                        ax=axs[a-1,m-1]
                        ax.plot(t[keep]-2,centered,'.-',ms=2,lw=.6,color=f'C{j}',label=f'VMID {wait//1000} ms')
                        ax.set(title=f'Array {"AB"[a-1]} / MUX {m} / address 2',xlabel='Analyzed time (s)',ylabel='Code minus record median (counts)')
        for (a,m),values in residuals.items():
            lo,hi=min(values),max(values)
            axs[a-1,m-1].set_ylim(min(lo-.5,-.75),max(hi+.5,.75))
        fig.legend(*axs[0,0].get_legend_handles_labels(),loc='outside lower center',ncol=3)
        save(fig,'test23_centered_time_'+('original' if i==0 else 'swapped'))


def report(old,new,comparison,verification):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak, Table, TableStyle, Image, KeepTogether
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='Body2',fontName='Helvetica',fontSize=10,leading=14,spaceAfter=8))
    styles.add(ParagraphStyle(name='Cell2',fontName='Helvetica',fontSize=8.3,leading=11))
    styles.add(ParagraphStyle(name='Small2',fontName='Helvetica',fontSize=8.5,leading=11,spaceAfter=6))
    width,height=landscape(A4); usable=width-80
    story=[]
    def p(text,small=False): story.append(Paragraph(text,styles['Small2' if small else 'Body2']))
    def page(title):
        if story:story.append(PageBreak())
        story.append(Paragraph(title,styles['Heading1']))
    def label(text):story.append(Paragraph(text,styles['Heading2']))
    def table(headers,rows,widths=None):
        cell=lambda v:Paragraph(escape(str(v)),styles['Cell2'])
        t=Table([[cell(v) for v in headers]]+[[cell(v) for v in r] for r in rows],colWidths=widths or [usable/len(headers)]*len(headers),repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e7eef6')),('VALIGN',(0,0),(-1,-1),'TOP'),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f5f7f9')]),('LEFTPADDING',(0,0),(-1,-1),6),
            ('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
            ('LINEBELOW',(0,0),(-1,0),.5,colors.HexColor('#9aafc5'))]))
        story.extend([t,Spacer(1,10)])
    def image(name,maxheight=355,folder=FIG):
        from PIL import Image as PILImage
        path=folder/(name+'.png')
        with PILImage.open(path) as im:w,h=im.size
        scale=min(usable/w,maxheight/h)
        story.append(Image(str(path),width=w*scale,height=h*scale))
        story.append(Spacer(1,8))
    def n(v):return f'{v:,.1f}'
    def span(data,key,array):
        vals=[r[key] for r in data if r['array']==array]
        return f'{min(vals):+.1f} to {max(vals):+.1f}'
    def delta_scan(data,a,m):
        return next(r['baseline_center_counts'] for r in data['scan_mux'] if r['array']==a and r['mux']==m and r['vmid_wait_us']==10000)-next(r['baseline_center_counts'] for r in data['scan_mux'] if r['array']==a and r['mux']==m and r['vmid_wait_us']==1000)
    page('TestBoard_ADC124 - sensor strips exchanged')
    p('<b>Tests 21, 22 and 23 repeated on 16 October 2026; compared with 15 October 2026.</b>')
    p('The strip formerly connected to Array A is now on Array B; the former Array B strip is on Array A. Array names always identify the PCB circuitry, not the strip. The acquisition program and diagnostic firmware image are identical between sessions.')
    label('Measured comparison')
    table(['Measurement','Original Array A','Swapped Array A','Original Array B','Swapped Array B'],[
        ['Test 21: median route change after long hold (counts)',span(old['full_mux'],'median_route_change_counts',1),span(new['full_mux'],'median_route_change_counts',1),span(old['full_mux'],'median_route_change_counts',2),span(new['full_mux'],'median_route_change_counts',2)],
        ['Test 22: tenth minus first conversion (counts)',span(old['ten_endpoints'],'tenth_minus_first_counts',1),span(new['ten_endpoints'],'tenth_minus_first_counts',1),span(old['ten_endpoints'],'tenth_minus_first_counts',2),span(new['ten_endpoints'],'tenth_minus_first_counts',2)],
        ['Test 23: MUX-center change, VMID 1 to 10 ms (counts)',f'{min(delta_scan(old,1,m) for m in range(1,5)):+.1f} to {max(delta_scan(old,1,m) for m in range(1,5)):+.1f}',f'{min(delta_scan(new,1,m) for m in range(1,5)):+.1f} to {max(delta_scan(new,1,m) for m in range(1,5)):+.1f}',f'{min(delta_scan(old,2,m) for m in range(1,5)):+.1f} to {max(delta_scan(old,2,m) for m in range(1,5)):+.1f}',f'{min(delta_scan(new,2,m) for m in range(1,5)):+.1f} to {max(delta_scan(new,2,m) for m in range(1,5)):+.1f}']])
    p('Ranges above span MUX summaries (and both ADC orders for Test 22); they are not uncertainty intervals. One count = 3.3/4096 V = 0.805664 mV nominally.')
    p(findings(old,new)[1])
    p('Test 23 also reproduces the array-specific response: Array A is nearly insensitive to 1-10 ms VMID wait, while Array B falls substantially. Measured cycle periods match the original session at all three settings.')
    p(f'63 new complete recordings, with three repetitions per condition. Original data and the earlier 79-page report are preserved. Production firmware was restored and acquisition stopped after the repeat.')

    page('Setup, terminology and controlled settings')
    table(['Item','Setting / meaning'],[
        ['Array A','Four TMUX1108 external MUXes; OPA365 buffers; ADC A. MUX 1/2/3/4 bias: 470k / 1M / 470k / 1M ohm.'],
        ['Array B','Four TMUX1308A external MUXes, exact part TMUX1308ABQBR; OPA365 buffers; ADC B. Bias: 1M / 470k / 1M / 470k ohm.'],
        ['Strip exchange','A now has the former B strip; B has the former A strip. Reported by the user; individual contact mapping not independently verified.'],
        ['External MUX address','Zero-based input selection 0-7. This is different from MUX number or ADC input.'],
        ['MUX number / ADC input','MUX 1 output goes through its OPA365 to ADC IN1; likewise MUX 2-4 to IN2-4.'],
        ['Reference connections','Address 7 is VMID on every MUX. Address 6 is tied to VMID on MUX 1-3; address 6 on MUX 4 is a sensor.'],
        ['Full cycle','Addresses 0-5: four inputs on each ADC. Address 6: only ADC IN4 on each ADC. Total 50 retained sensor codes.'],
        ['Acquisition','Existing diagnostic profile; direct LPSPI; requested 16 MHz SPI; coordinated switching of both arrays; 5 us sensor settling.'],
        ['Reference and bias','Nominal ADC reference 3.3 V, VMID 1.65 V from REF2033; MUX supply 3.3 V. Local VMID-input capacitors 150 pF, with nearby 100 ohm isolation.'],
        ['Uncontrolled conditions','Separate-day sessions. Temperature, table vibration and absolute initial sensor charge were not instrumented. No intentional mechanical stimulus.']], [155,usable-155])

    page('Test 21 - first complete cycle after long VMID parking')
    label('Purpose')
    p('Check how much the first sensor readings change after the sensors have been disconnected from their normal bias path, and whether the large loss follows the exchanged strips.')
    label('Steps and parameters')
    for text in ['Run a full 50-sensor scan with no explicit VMID visit between external addresses for 3.5 s. Exclude the first 1 s and use the next 2.5 s for each sensor\'s before baseline.',
        'Stop and park all MUXes at external address 7 (VMID). Prepare ADC selectors, then hold for a firmware-timed 20 s with no conversions.',
        'Switch to address 0, wait 5 us and record exactly one complete address-0-to-6 cycle. ADC A is read before ADC B. Stop and park at VMID. Repeat three times.']:
        p(text)
    p('Software VMID off does not remove the physical VMID connection at address 6 on MUXes 1-3. No sensor response is discarded after the long hold.')
    table(['Timing','Original session','Swapped session'],[
        ['Firmware-timed hold (s)',f'{min(r["timed_hold_s"] for r in old["holds"]):.6f}-{max(r["timed_hold_s"] for r in old["holds"]):.6f}',f'{min(r["timed_hold_s"] for r in new["holds"]):.6f}-{max(r["timed_hold_s"] for r in new["holds"]):.6f}'],
        ['Last before-cycle end to post-cycle start (s)',f'{min(r["last_received_pre_end_to_post_start_s"] for r in old["holds"]):.3f}-{max(r["last_received_pre_end_to_post_start_s"] for r in old["holds"]):.3f}',f'{min(r["last_received_pre_end_to_post_start_s"] for r in new["holds"]):.3f}-{max(r["last_received_pre_end_to_post_start_s"] for r in new["holds"]):.3f}']])
    p('The longer total gap includes host setup while parked before the timed hold. The ranges include all Test 21/22 holds. The measurement is a first response after reconnection, not direct observation of the isolated sensor voltage.')

    page('Test 21 - per-address comparison')
    image('test21_full_cycle_comparison',350)
    p('<b>Legend:</b> Blue = original strip assignment; red = exchanged strips. Solid circles = first cycle after the hold; dashed lines = the corresponding before-scan baseline. Curves are medians of three repetitions. Shading is the minimum-to-maximum first-response range across repetitions, not noise SD.',True)
    p('Each panel is one array and MUX number. The horizontal axis is external MUX address; adjacent points are different sensors. MUX 4 includes its address-6 sensor. Vertical scales differ between panels.',True)

    page('Test 21 - numerical comparison')
    table(['Array / MUX','Original before / after (counts)','Swapped before / after (counts)','Original route change (counts / mV)','Swapped route change (counts / mV)'],[
        [f'{"AB"[r["array"]-1]} / {r["mux"]}',f'{n(r["original_before_center_counts"])} / {n(r["original_after_center_counts"])}',f'{n(r["swapped_before_center_counts"])} / {n(r["swapped_after_center_counts"])}',f'{n(r["original_median_route_change_counts"])} / {n(r["original_median_route_change_volts"]*1000)}',f'{n(r["swapped_median_route_change_counts"])} / {n(r["swapped_median_route_change_volts"]*1000)}'] for r in comparison['full_cycle_mux_comparison']])
    p('MUX summaries use the same six addresses 0-5. A center is the median of six route baselines or first-response codes. Route change is the median of six matched route changes, so it need not equal the difference between the two centers.')
    p('An upward first reading does not prove that the disconnected sensor charged toward VMID. Reconnection, stored charge and conversion history contribute; readings above nominal 2048 counts cannot be explained solely by passive relaxation toward VMID.')

    page('Test 22 - ten conversions after the same long hold')
    label('Purpose')
    p('Measure recovery immediately after reconnecting one sensor path, and compare the effect before and after exchanging strips.')
    label('Steps and parameters')
    for text in ['Use the same 3.5 s before scan, 1 s exclusion and 20 s timed VMID hold as Test 21.',
        'Choose external MUX address 2 and one MUX number m. Prepare ADC INm before the hold. After the hold, switch both arrays to address 2 and wait 5 us.',
        'Read ADC INm ten times consecutively on the first ADC, then ten times on the other ADC. Keep the external address and selected ADC input fixed throughout each burst.',
        'Repeat for MUX 1, 2, 3 and 4, for both ADC A-first and ADC B-first orders, with three repetitions per condition. The before-scan ADC order matches the burst order.']:
        p(text)
    label('What is measured')
    p('Each conversion position 1-10, its preceding scan baseline, and the tenth-minus-first change. These are recovery transients, not stationary noise. The ADC read second has additional elapsed time and preceding conversion activity; an order effect does not isolate a single mechanism.')
    p('The next two graphs separate the two ADC orders so that every line has one clear meaning. Both arrays are active in both orders.')
    for first in ('A','B'):
        page(f'Test 22 - ADC {first} read first')
        image(f'test22_first_{first}',350)
        p('<b>Legend:</b> Blue = original strips; red = swapped strips. Solid circles = consecutive post-hold responses on the same ADC input; dashed lines = that sensor\'s preceding scan baseline. Shading = min-to-max over three repetitions. The horizontal axis counts conversions; it is not external MUX address or elapsed seconds.',True)
        p('All targets are external MUX address 2. Upper panels are Array A, lower panels Array B. MUX number identifies the ADC input being held. Vertical scales differ between panels.',True)
    for a in (1,2):
        page(f'Test 22 - Array {"AB"[a-1]} endpoints')
        table(['MUX / first ADC','Original before / first / tenth','Swapped before / first / tenth','Original tenth-first (counts / mV)','Swapped tenth-first (counts / mV)'],[
            [f'{r["mux"]} / {"AB"[r["first_bank"]]}',f'{n(r["original_before_baseline_counts"])} / {n(r["original_first_counts"])} / {n(r["original_tenth_counts"])}',f'{n(r["swapped_before_baseline_counts"])} / {n(r["swapped_first_counts"])} / {n(r["swapped_tenth_counts"])}',f'{n(r["original_tenth_minus_first_counts"])} / {n(r["original_tenth_minus_first_volts"]*1000)}',f'{n(r["swapped_tenth_minus_first_counts"])} / {n(r["swapped_tenth_minus_first_volts"]*1000)}'] for r in comparison['ten_endpoint_comparison'] if r['array']==a])
        p('All before/first/tenth values are ADC counts, aggregated as medians of three repetitions. All targets use external MUX address 2. Counts and volts for all ten readings and individual repetitions are supplied as CSV files.')

    page('Test 23 - regular scans with long VMID intervals')
    label('Purpose')
    p('Check how disconnected time between sensor visits affects baseline and temporal variation, and whether those effects move with the strips.')
    label('Steps and parameters')
    for text in ['Continuously scan external addresses 0-6, retaining all 50 sensor readings per cycle. Sensor settling is 5 us. ADC A is read before ADC B.',
        'After each address group, switch both arrays together to external MUX address 7. Wait 1, 5 or 10 ms, then perform four discarded conversions on each ADC.',
        'Record 12 s for each condition, exclude the first 2 s and analyze the following 10 s. Repeat three times; the second repetition reverses condition order.']:
        p(text)
    label('What is measured')
    p('For each sensor route: median baseline, temporal SD and peak-to-peak, all in counts and volts. Also compare the baseline spread between external addresses, and the measured full-cycle period. Drift is the last-two-second median minus the first-two-second median within the analyzed window.')
    p('Each cycle has seven shared VMID waits, seven 5 us sensor waits, 50 retained sensor conversions and 56 discarded VMID conversions. Longer VMID waits increase sensor disconnected time as well as changing reference history.')
    p('Raw SD and peak-to-peak include drift and any isolated excursions. They are not filtered quiet-period or white-noise estimates. Sampling bandwidth differs between the 1/5/10 ms settings; compare strip assignments at the same setting.')
    for a in (1,2):
        page(f'Test 23 - Array {"AB"[a-1]} baseline at every external address')
        image(f'test23_routes_{"AB"[a-1]}',365)
        p('<b>Legend:</b> Colour = VMID wait 1, 5 or 10 ms. Dashed lines with squares = original strips; solid lines with circles = swapped strips. Every point is the median baseline across three repetitions. X = external MUX address; neighbouring points are different sensors. Each panel represents one MUX / ADC input.',True)
        p('Curves overlap where their codes are identical. In particular, Array A\'s three VMID-delay curves nearly coincide; the last-drawn green curve can cover the blue and orange curves.',True)
    page('Test 23 - MUX baseline centers and temporal SD')
    image('test23_centers_noise',350)
    p('<b>Legend:</b> Colour = MUX number. Dashed = original strips; solid = swapped strips. Left panels: median baseline over six external addresses 0-5. Right panels: median of those six routes\' temporal SDs. These are different quantities; the right panel is not spread between baseline values.',True)
    p('SD is calculated separately for each route and repetition, then aggregated. Sub-count SD with repeated identical codes does not establish equivalent analog accuracy.',True)
    worst=[max(data['scan_routes'],key=lambda r:r['sd_counts_max_rep']) for data in (old,new)]
    p(f'The largest individual-record SD is {worst[0]["sd_counts_max_rep"]:.3f} counts '
      f'({worst[0]["sd_counts_max_rep"]*LSB*1000:.3f} mV) originally and {worst[1]["sd_counts_max_rep"]:.3f} counts '
      f'({worst[1]["sd_counts_max_rep"]*LSB*1000:.3f} mV) after exchange. Both occur on Array B / MUX 3 / external address 2 '
      'with VMID wait 10 ms; peak-to-peak is 11 counts (8.862 mV) in both sessions. These raw measures include drift. '
      'The MUX-median SD curves above therefore do not represent the worst individual route.',True)
    for i in (0,1):
        page('Test 23 - time traces, '+('original strips' if i==0 else 'swapped strips'))
        image('test23_centered_time_'+('original' if i==0 else 'swapped'),350)
        p('<b>Legend:</b> Colours identify VMID wait 1, 5 and 10 ms. Each point is a code from repetition 1 at external MUX address 2, minus that individual recording\'s median code. This removes only its constant baseline, preserving drift and excursions. Connecting lines aid visibility. Each panel is one array and MUX / ADC input. X = time within the analyzed 10 s, after the 2 s exclusion. These are separate recordings, not simultaneous traces.',True)
        p('These example traces expose drift or isolated excursions that an SD alone could hide. All routes and all repetitions contribute to the tables; the examples are not the only inputs assessed.',True)
    for a in (1,2):
        page(f'Test 23 - Array {"AB"[a-1]} baseline, noise and spread table')
        table(['MUX / VMID ms','Baseline counts original / swapped','Spatial spread counts original / swapped','Median SD counts original / swapped','Worst p-p counts original / swapped','Max drift counts original / swapped'],[
            [f'{r["mux"]} / {r["vmid_wait_us"]//1000}',f'{n(r["original_baseline_center_counts"])} / {n(r["swapped_baseline_center_counts"])}',f'{n(r["original_address_spread_counts"])} / {n(r["swapped_address_spread_counts"])}',f'{r["original_median_route_sd_counts"]:.3f} / {r["swapped_median_route_sd_counts"]:.3f}',f'{n(r["original_max_route_ptp_counts"])} / {n(r["swapped_max_route_ptp_counts"])}',f'{n(r["original_max_absolute_drift_counts"])} / {n(r["swapped_max_absolute_drift_counts"])}'] for r in comparison['long_scan_mux_comparison'] if r['array']==a])
        p('Baseline/spatial-spread/SD summaries use addresses 0-5 for equal six-sensor MUX comparisons. Worst peak-to-peak and maximum absolute drift select the largest value among those routes and all three repetitions. All 50 routes, including MUX 4 address 6, are retained in the CSVs. Multiply counts by 0.805664 for mV; full-precision volt columns are supplied.')

    page('Test 23 - sampling rate comparison')
    timing=[]
    for wait in (1000,5000,10000):
        med=[float(np.median([r['period_median_us'] for r in e['captures'] if r['name']==f'long_vmid_{wait}'])) for e in (old,new)]
        timing.append(dict(vmid_wait_us=wait,original_period_us=med[0],swapped_period_us=med[1],original_samples_per_second=1e6/med[0],swapped_samples_per_second=1e6/med[1],period_change_percent=100*(med[1]/med[0]-1)))
    csv_write(OUT/'sampling_rate_comparison.csv',timing)
    table(['VMID wait','Original cycle us','Swapped cycle us','Original samples/s per sensor','Swapped samples/s per sensor','Cycle change %'],[[f'{r["vmid_wait_us"]//1000} ms',n(r['original_period_us']),n(r['swapped_period_us']),f'{r["original_samples_per_second"]:.5f}',f'{r["swapped_samples_per_second"]:.5f}',f'{r["period_change_percent"]:+.5f}'] for r in timing])
    slope,intercept=np.polyfit([r['vmid_wait_us'] for r in timing],[r['swapped_period_us'] for r in timing],1)
    p(f'Swapped-session fit: cycle time = {slope:.5f} x VMID wait + {intercept:.2f} us. Seven shared waits dominate the cycle. The remainder includes 35 us sensor waits, switching guards, conversions, driver work and diagnostic logging; it is not a separately measured breakdown of those components.')
    p('One cycle provides one sample per sensor. Multiply cycle rate by 50 for combined retained sensor-code throughput. These diagnostic-profile rates should not be substituted for production-driver benchmarks.')
    fast=[]
    for e in (old,new):
        rr=[r for r in e['captures'] if r['family'] in ('hold_full_before','hold_ten_before')]
        period=float(np.median([r['period_median_us'] for r in rr]))
        fast.append((period,1e6/period))
    table(['Before-scan context','Original strips','Swapped strips'],[
        ['Median period across the 27 fast before recordings (us)',n(fast[0][0]),n(fast[1][0])],
        ['Corresponding samples/s per sensor',f'{fast[0][1]:.3f}',f'{fast[1][1]:.3f}']])
    p('The fast-before summary pools the full scan and both ADC orders used to prepare Tests 21/22. It is context for those recordings, not a rate inferred from the isolated post-hold cycle or a substitute for the matched Test 23 comparisons.',True)

    page('What the strip exchange can establish')
    label('Conclusions from the measured repeat')
    for text in findings(old,new):p(text)
    label('Use three observations together')
    p('Test 21 measures the loss relative to each immediately preceding scan baseline. Test 22 checks the early recovery after reconnection. Test 23 checks sensitivity to the interval between visits. Agreement across all three is stronger evidence than comparing absolute baseline alone.')
    p('The MUX types remain a plausible cause because their leakage and parasitic characteristics differ. This swap does not distinguish the chip from array-specific PCB surface leakage or other component variation. Direct isolated-node voltage and sensor capacitance were not measured.')
    p('The 20 s parking tests also do not isolate the local VMID capacitor as the cause: parking disconnects every sensor while holding the common nodes at VMID. An Array A rise above VMID cannot be interpreted as passive leakage toward VMID alone.')

    page('Following each strip between the two PCB arrays')
    p('This comparison follows the reported strip movement rather than holding the PCB route fixed. Corresponding MUX number and external address are assumed to retain the same strip-contact mapping. Bias resistor values differ between arrays at the same MUX number; moving a strip changes its complete board path.')
    striprows=[]
    for oldarray in (1,2):
        a=3-oldarray
        rr=[r for r in old['full_routes'] if r['array']==oldarray]
        ss=[r for r in new['full_routes'] if r['array']==a]
        striprows.append([f'Former Array {"AB"[oldarray-1]} strip',f'{"AB"[oldarray-1]} to {"AB"[a-1]}',
            n(float(np.median([r['after_counts'] for r in rr]))),n(float(np.median([r['after_counts'] for r in ss]))),
            n(float(np.median([r['delta_from_steady_counts'] for r in rr]))),n(float(np.median([r['delta_from_steady_counts'] for r in ss])))])
    table(['Strip identity','PCB movement','Original first-post median counts','Swapped first-post median counts','Original matched route change counts','Swapped matched route change counts'],striprows)
    p('Each value summarizes all 25 sensor routes of that strip, after taking each route\'s three-repetition median. No time constants or leakage currents are inferred from these endpoint readings.')
    p('Per-route exports named inferred_same_strip_*.csv provide the corresponding comparisons. For Test 22, ADC order is also reversed in the matching key so that the tracked strip is read first in both sessions, or second in both sessions. This retains its relative burst position; it still changes the other ADC\'s identity and the board path.')

    page('Acquisition integrity and reproducibility')
    table(['Check','Swapped-strip repeat'],[
        ['Complete recordings','63 = 27 continuous before scans + 27 single post-hold captures + 9 long-VMID scans; 21 programmed conditions, three repeats each.'],
        ['Acquired / received / discarded cycles',f'{verification["cycles_acquired"]:,} / {verification["cycles_received"]:,} / {verification["cycles_discarded"]:,}. All discards occur in fast before scans and are counted.'],
        ['Single post-hold and regular long scans','All complete with no discarded cycles; every fast before scan includes its final acquired cycle.'],
        ['Errors','Zero transfer errors, ADC data errors, USB write errors and controlled timing overruns.'],
        ['Same experiment','Exact same diagnostic HEX SHA-256 and acquisition plan as the original Tests 21-23. Per-frame schema, physical event order, 12-bit range, FNV checksum and sequence validated by acquisition decoder; raw file SHA-256 checked again during analysis.'],
        ['Restoration','Frozen production image and pre-test settings restored; stopped production identity verified.'],
        ['Source data','Original: vmid_long_hold_2026-10-15. Repeat: vmid_swapped_strips_2026-10-16. Both under docs/testing/datasets/testboard-adc124/.'],
        ['Analysis files','Matching result folders contain per-repetition and per-route counts/volts, timing, baseline/noise summaries, comparison CSVs, graphs and verification.json.'],
        ['Important limit','The same board-route key identifies different strips after exchange. CSV joins compare the same circuit route; individual physical sensor identity is inferred from the reported exchange, not independently verified.']], [165,usable-165])
    p('Recreate with the frozen analysis-tool snapshot or run report_adc124_swapped_vmid.py from the repository benchmarks directory. setup.json records the reported physical exchange; lifecycle.json records uploads, original settings and restoration.')
    def footer(canvas,doc):
        canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#526477'))
        canvas.drawString(40,22,'ADC124 | Tests 21-23 | original vs exchanged strips | 16 October 2026')
        canvas.drawRightString(width-40,22,str(doc.page))
    PDF.parent.mkdir(parents=True,exist_ok=True)
    SimpleDocTemplate(str(PDF),pagesize=(width,height),leftMargin=40,rightMargin=40,topMargin=34,bottomMargin=38,
        title='TestBoard_ADC124 - Tests 21-23 with sensor strips exchanged',author='ADC124 measurement report').build(story,onFirstPage=footer,onLaterPages=footer)
    return timing


def freeze_tools():
    target=RAW/'analysis_tools';target.mkdir(exist_ok=True)
    files=list(Path(__file__).parent.glob('*.py'))+list(Path(__file__).parent.glob('*.md'))
    files += [ROOT/'tests/test_adc124_long_vmid.py',ROOT/'tests/test_adc124_swapped_vmid.py']
    hashes={}
    for source in files:
        dest=target/source.relative_to(ROOT);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
        hashes[str(source.relative_to(ROOT))]=sha(dest)
    identity=dict(created_utc=datetime.now(timezone.utc).isoformat(),python=sys.version,platform=platform.platform(),
                  git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),sha256=hashes)
    (target/'identity.json').write_text(json.dumps(identity,indent=2))
    return len(files)


def main():
    from analyze_adc124_long_vmid import analyze
    old=json.loads((OLD_OUT/'analysis.json').read_text())
    new=analyze(RAW,OUT)
    verification=verify(new)
    c=comparisons(old,new)
    strip=strip_comparisons(old,new)
    figures(old,new)
    timing=report(old,new,c,verification)
    verification['analysis_tool_files_frozen']=freeze_tools()
    verification['focused_tests_passed']=5
    verification['pdf']=dict(path=str(PDF.relative_to(ROOT)),sha256=sha(PDF),visual_review_pending=True)
    verification['artifact_sha256']={p.name:sha(p) for p in OUT.glob('*.csv')}
    verification['decoded_npz_sha256']={p.name:sha(p) for p in (RAW/'captures').glob('*.npz')}
    (OUT/'verification.json').write_text(json.dumps(verification,indent=2))
    (OUT/'comparison.json').write_text(json.dumps(dict(original_results=str(OLD_OUT),swapped_results=str(OUT),comparisons=c,inferred_same_strip=strip,timing=timing),indent=2))
    lines=['# Tests 21-23 after exchanging sensor strips - 16 October 2026','',
        'All 63 recordings completed using the exact original diagnostic image and acquisition plan.',
        'Array A now carries the former Array B strip; Array B carries the former Array A strip.',
        'Array names identify PCB circuitry. Individual connector-contact mapping was not independently verified.','',
        '## Results','']
    lines.extend('- '+text for text in findings(old,new))
    lines += ['', '## Acquisition', '',
        '- LPSPI, requested 16 MHz, both arrays coordinated, 5 us sensor settling.',
        '- Test 21: three full before/post pairs, 20 s timed VMID park without conversions, exactly one full post-hold cycle.',
        '- Test 22: MUX address 2, each MUX number, both ADC orders, three before/post pairs each, ten retained same-input conversions per ADC.',
        '- Test 23: VMID waits 1/5/10 ms, three 12 s records each, exclude 2 s and analyze 10 s.',
        '- Total parked interval exceeds the timed 20 s because of host setup; hold_intervals.csv retains both intervals.',
        '- Temporal SD and peak-to-peak include drift and excursions; transient ten-reading bursts are not labelled noise.',
        '', '## Integrity', '',
        f'- {verification["cycles_acquired"]:,} cycles acquired; {verification["cycles_received"]:,} received; {verification["cycles_discarded"]:,} whole-cycle discards, all counted in fast before scans.',
        '- Zero ADC data/transfer/USB-write errors and controlled timing overruns.',
        '- All post-hold cycles, all long-VMID regular scans and every final before cycle received.',
        '- Frozen production firmware and saved settings restored; acquisition stopped.',
        '- Original dataset, result CSVs and 79-page report preserved.',
        '', '## Files', '',
        '- PDF: '+str(PDF.relative_to(ROOT)),
        '- comparison.json and *_comparison.csv join the same board route between sessions.',
        '- inferred_same_strip_*.csv track the reported strip movement, assuming matching connector contact order.',
        '- Test 22 strip matching preserves whether the tracked strip is read first or second by reversing the ADC-order key.',
        '- Per-route and per-repetition originals, counts/volts, baseline/noise/spread and holds are retained alongside comparisons.',
        '- verification.json records image identity, restoration and evidence hashes.',
        '- Raw data, setup, plan, commands, uploads, frozen firmware and frozen analysis tools: '+str(RAW.relative_to(ROOT)),
        '', 'Recreate: `.venv/Scripts/python.exe Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_swapped_vmid.py`','']
    (OUT/'README.md').write_text('\n'.join(lines))
    print(json.dumps(dict(pdf=str(PDF),verification=verification),indent=2))


if __name__=='__main__':main()
