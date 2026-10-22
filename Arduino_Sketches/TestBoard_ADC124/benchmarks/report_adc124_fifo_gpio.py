"""Render matched ADC124 FIFO/GPIO timing and acquisition-quality evidence."""
from pathlib import Path
import argparse
import json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[3]
COLORS = {'blocking':'#0072B2','dma':'#009E73','lpspi':'#D55E00'}
SETTLES = (1,2,5,10,50)


def table(headers, rows):
    return '\n'.join(['| '+' | '.join(headers)+' |','| '+' | '.join(['---']*len(headers))+' |']+
        ['| '+' | '.join(map(str,row))+' |' for row in rows])


def number(value, digits=1): return 'unqualified' if value is None else f'{value:.{digits}f}'


def report(folder):
    folder = folder.resolve(); data = json.loads((folder/'analysis.json').read_text())
    comparisons = data['comparisons']; summaries = data['summary']; rows = data['rows']
    channels = data['channel_comparisons']
    if len(comparisons) != 60: raise ValueError('Need all 60 matched settings')
    raw = ROOT/data['raw_root']
    plt.rcParams.update({'font.size':10, 'axes.grid':True, 'grid.alpha':.25, 'figure.dpi':150})
    fig, axes = plt.subplots(2,2,figsize=(12,8),constrained_layout=True)
    for i,mode in enumerate(('adc','mux')):
        for j,vmid in enumerate((False,True)):
            ax=axes[i,j]
            for engine,color in COLORS.items():
                selected=[next(r for r in comparisons if (r['mode'],r['engine'],r['vmid'],r['settle_us'])==
                    (mode,engine,vmid,s)) for s in SETTLES]
                for version,style in (('before','--'),('after','-')):
                    ax.plot(SETTLES,[r[version+'_sweeps_s'] for r in selected],style,color=color,
                        marker='o' if version=='after' else None,label=engine+' '+version)
            ax.set(xscale='log',xticks=SETTLES,xticklabels=SETTLES,xlabel='Settling delay (us)',
                ylabel='Delivered sweeps/s/channel',title=f'{mode.upper()} mode - VMID {"on" if vmid else "off"}')
            if i==0 and j==0: ax.legend(fontsize=8,ncol=2)
    fig.suptitle('Production: matched before/after medians, requested 16 MHz, repeat 1')
    fig.savefig(folder/'production_rates.png'); plt.close(fig)

    fig,axes=plt.subplots(1,2,figsize=(12,4.5),constrained_layout=True)
    for ax,vmid in zip(axes,(False,True)):
        for mode,style in (('adc','--'),('mux','-')):
            for engine,color in COLORS.items():
                selected=[next(r for r in comparisons if (r['mode'],r['engine'],r['vmid'],r['settle_us'])==
                    (mode,engine,vmid,s)) for s in SETTLES]
                ax.plot(SETTLES,[r['rate_gain_percent'] for r in selected],style,color=color,marker='o',
                    label=f'{engine} {mode.upper()}')
        ax.axhline(0,color='black',linewidth=.7)
        ax.set(xscale='log',xticks=SETTLES,xticklabels=SETTLES,xlabel='Settling delay (us)',
            ylabel='Rate gain (%)',title='VMID '+('on' if vmid else 'off'))
    axes[0].legend(fontsize=8,ncol=2); fig.suptitle('Measured throughput gains against fresh baseline')
    fig.savefig(folder/'rate_gains.png'); plt.close(fig)

    fig,axes=plt.subplots(2,2,figsize=(12,7),constrained_layout=True)
    for i,array in enumerate((1,2)):
        for j,vmid in enumerate((False,True)):
            ax=axes[i,j]
            for mode,color in (('adc','#D55E00'),('mux','#0072B2')):
                for version,style in (('before','--'),('after','-')):
                    values=[]
                    for s in SETTLES:
                        group=[c for c in channels if (c['mode'],c['engine'],c['vmid'],c['settle_us'],c['array'])==
                            (mode,'lpspi',vmid,s,array)]
                        values.append(max((c[version+'_sd_counts'] for c in group),default=np.nan))
                    ax.plot(SETTLES,values,style,color=color,marker='o',label=mode.upper()+' '+version)
            ax.set(xscale='log',xticks=SETTLES,xticklabels=SETTLES,xlabel='Settling delay (us)',
                ylabel='Worst channel SD (counts)',title=f'Array {"A" if array==1 else "B"} - VMID {"on" if vmid else "off"}')
            if i==0 and j==0: ax.legend(fontsize=8,ncol=2)
    fig.suptitle('LPSPI quiet noise: median SD per route across qualified repetitions, then array maximum')
    fig.savefig(folder/'quiet_noise.png'); plt.close(fig)

    diagnostic=[r for r in summaries if r['stage'].endswith('_diagnostic') and r['settle_us']==5]
    if len(diagnostic)==24:
        fig,axes=plt.subplots(1,2,figsize=(12,5),constrained_layout=True)
        for ax,mode in zip(axes,('adc','mux')):
            x=0; labels=[]
            for engine in COLORS:
                for vmid in (False,True):
                    for version in ('before','after'):
                        r=next(r for r in diagnostic if (r['stage'],r['mode'],r['engine'],r['vmid'])==
                            (version+'_diagnostic',mode,engine,vmid))
                        prepare=r['profile_prepare_us']; transfer=r['profile_transfer_us']; acquisition=r['profile_acquisition_us']
                        phases=(prepare,transfer,max(0,acquisition-prepare-transfer),max(0,r['mean_period_us']-acquisition))
                        bottom=0
                        for value,color,label in zip(phases,('#56B4E9','#E69F00','#009E73','#999999'),
                                ('Prepare','Transfer','Acquisition remainder','Loop remainder')):
                            ax.bar(x,value,bottom=bottom,color=color,label=label if x==0 else None); bottom+=value
                        label_engine = {'blocking':'BLK','dma':'DMA','lpspi':'LPSPI'}[engine]
                        labels.append(f'{label_engine}\n{"on" if vmid else "off"}\n{version}'); x+=1
            ax.set(xticks=range(x),xticklabels=labels,ylabel='Diagnostic mean time (us)',title=mode.upper()+' mode')
            ax.tick_params(axis='x',labelsize=7)
        axes[0].legend(fontsize=8); fig.suptitle('5 us diagnostic breakdown; probes included, nested validation omitted')
        fig.savefig(folder/'diagnostic_breakdown.png'); plt.close(fig)

    frames=sum(r['frames'] for r in rows); sent=sum(r['usb_frames_sent'] for r in rows)
    discarded=sum(r['usb_frames_discarded'] for r in rows); acquired=sum(r['sampling_sweeps'] for r in rows)
    errors={k:sum(r[k] for r in rows) for k in ('transfer_errors','data_errors','usb_write_errors')}
    production=[r for r in rows if r['stage'].endswith('_production')]
    quiet=[r for r in rows if 'quiet_qualified' in r]
    unqualified=[r for r in quiet if not r['quiet_qualified']]
    texts=['# ADC124 FIFO batching and coordinated GPIO switching',
        '2026-10-14 | COM4 | connectors disconnected | charger connected',
        'The validated scan-mode firmware was merged into `main` through [PR #51](https://github.com/sheramir/arduino_adc_streamer/pull/51) after the required `pytest` check passed. This report covers the subsequent FIFO/GPIO optimizations.',
        '## Implementation and compatibility',
        'LPSPI now queues prepared 16-bit commands under one GPIO CS and drains responses in order. PARAM supplies TX/RX depths; actual TXCOUNT includes command entries; outstanding responses never exceed RX capacity. FIFO error flags and an unsigned cycle deadline bound the operation. All responses, empty TX, TCF and idle are required before CS release. Cancellation flushes hardware and releases CS. Pipeline primes, repeats and payload labels are unchanged. [NXP RT1060 reference manual, chapter 48](https://www.pjrc.com/teensy/IMXRT1060RM_rev3.pdf).',
        'Address changes use precomputed SET/CLEAR register sequences, including different cached addresses on the two banks. GPIO6 carries A0; GPIO9 carries A1/A2/B0; GPIO8 carries B1/B2. Only changed bits are written, with no read/modify/write of unrelated output bits. Single-bank phases use a direct cached path. Shared phases disable all changing banks, wait 200 ns after the last disable, update addresses, enable with A-high/B-low polarity, then wait 200 ns after the last enable. One full analog settling wait follows. VMID phases use the same coordination. Unchanged banks remain untouched; startup/stop/failure parking remains.',
        'Protocol remains 3: `adc` finishes A then B, and `mux` shares switching/settling before sequential A/B transfers. Wire layout, ACKs, routes and historical decoding are unchanged. New status reports revision `adc124-fifo-grouped-gpio-v2`, FIFO batching, grouped ports and coordinated guards. There is no ADC124 desktop profile to migrate.',
        'The cached transition tables use 48,096 bytes on ARM. The production build reports 59,744 bytes of RAM1 variables and 431,776 bytes free for local variables. This spends memory to remove mask assembly from acquisition.',
        '## Validation and benchmark method',
        'Both production/diagnostic builds passed. 80 focused tests passed, including 708 acquisition cases per build, 512 paired address histories per build, asymmetric 1/4/16-word FIFOs, 13-word repeated batches, final-clock completion, FIFO faults, wrapped timeouts, pipeline mapping, sparse routes, guard timing, EN polarity and USB behavior. Native digital tests and ARM USB emulation supplement hardware captures; they do not prove PCB analog routing or electrical edge timing.',
        'Fresh before/after production matrices each contain 60 settings x 3 repetitions. Diagnostic matrices each contain 60 settings x 1 repetition. Every standard run uses 1 s warm-up + 2 s measurement, both arrays, requested 16 MHz, manual acquisition, repeat 1, bursts, ADC/MUX modes, blocking/DMA/LPSPI, 1/2/5/10/50 us settling and VMID off/on. The same configuration rotation is used for both versions. Before and after are sequential sessions, so thermal/background drift is not eliminated.',
        f'{len(rows)} analyzed captures: **{frames:,} received frames**, {acquired:,} acquired, {discarded:,} accounted discards ({100*discarded/acquired:.5f}%). Sent frames: {sent:,}; received equals sent: {frames==sent}. Error totals: {errors}. Raw SHA-256 checks, independent parsers, valid 12-bit codes, exact payload routes and nonoverlapping monotonic sweep intervals passed. Exploratory smoke captures are excluded from these comparisons.',
        '## Measured production performance',
        'Tables use medians of three device timestamp measurements. Rates count delivered complete sweeps; useful per-channel sample rate equals sweeps/s. Counter-derived whole-run sampling rates are separately labeled estimates in the CSV. Accounted discards can lower delivered rate. Actual SCLK and individual conversion/CS edges were not measured.',
        '![Production rates](production_rates.png)','![Rate gains](rate_gains.png)',
        table(['Engine','Mode','VMID','Before sweeps/s','After sweeps/s','Gain','Saved us/sweep'],[
            [r['engine'],r['mode'],'on' if r['vmid'] else 'off',number(r['before_sweeps_s']),number(r['after_sweeps_s']),
                number(r['rate_gain_percent'])+'%',number(r['period_saved_us'],3)] for r in comparisons if r['settle_us']==5]),
        'Full 60-setting comparison:',
        table(['Settle us','VMID','Blocking ADC gain','Blocking MUX gain','DMA ADC gain','DMA MUX gain','LPSPI ADC gain','LPSPI MUX gain'],[
            [s,'on' if v else 'off']+[number(next(r['rate_gain_percent'] for r in comparisons if
                (r['settle_us'],r['vmid'],r['engine'],r['mode'])==(s,v,e,m)))+'%' for e in COLORS for m in ('adc','mux')]
            for s in SETTLES for v in (False,True)]),
        '## Measured diagnostic phases',
        'Profiling includes probes, interrupts and warm-up. Prepare includes sensor/VMID switching, guards and analog settling in both versions. Validation is nested in transfer; acquisition overlaps these phases. Do not sum nested scopes or use diagnostic throughput as production performance.',
        '![Diagnostic breakdown](diagnostic_breakdown.png)',
        table(['Engine','Mode','VMID','Before prepare us','After prepare us','Before transfer us','After transfer us','Before encode/enqueue us','After encode/enqueue us'],[
            [e,m,'on' if v else 'off']+[number(next(r[field] for r in diagnostic if
                (r['stage'],r['engine'],r['mode'],r['vmid'])==(version+'_diagnostic',e,m,v)),3)
                for field,version in (('profile_prepare_us','before'),('profile_prepare_us','after'),
                    ('profile_transfer_us','before'),('profile_transfer_us','after'))]+[
                number(next(r['profile_encode_us']+r['profile_usb_write_us'] for r in diagnostic if
                    (r['stage'],r['engine'],r['mode'],r['vmid'])==(version+'_diagnostic',e,m,v)),3)
                for version in ('before','after')] for e in COLORS for m in ('adc','mux') for v in (False,True)] if len(diagnostic)==24 else []),
        '## Calculations distinguished from measurements',
        'SPI words, batches and CS allowances are unchanged by this optimization: 64 words / 14 batches / 2.8 us configured CS allowance with VMID off; 134 / 28 / 5.6 us with VMID on. This round does not claim new CS-edge allowance savings. FIFO gains arise from CPU/queue scheduling rather than reducing analog settling.',
        'Full two-array bank transitions stay 14/28 (VMID off/on). ADC mode retains nominal 5.6/11.2 us of switching guards. Coordinated MUX phases reduce the configured guard budget from 5.6 to 2.8 us off and 11.2 to 5.6 us on. These are firmware allowance calculations, not measured propagation times. Analog waits remain 14/28 in ADC and 7/14 in MUX. Steady VMID-off address writes fall from 24 individual-bit writes to 22 cached register writes in ADC and 21 in MUX; EN writes are unchanged. Prepare-phase differences include these effects and CPU overhead, not an isolated GPIO measurement.',
        '## Quiet-period acquisition quality', data['quiet_method']+'.',
        f'{len(quiet)-len(unqualified)}/{len(quiet)} analyzed quiet records qualified with >=1 s and >=100 retained frames. Unqualified short records: {len(unqualified)}. A failed quiet criterion never becomes a noise PASS. ADC/blocking/2 us/VMID-off receives matched 15 s supplementary captures, with the same 1 s warm-up and common-window method. Extended qualified records take precedence over short records for that setting.',
        'Charger switching/rails were not independently recorded. This method detects relative group-envelope bursts; it does not establish their electrical cause or remove sustained/isolated channel variation. No individual sample is clipped, filtered or detrended. SD uses ddof=1; median SD per route across qualified repetitions is then compared. Peak-to-peak is saved but not treated as duration-independent. Counts convert at 3.3/4096 V/count. Biases A: 470k/1M/470k/1M; B: 1M/470k/1M/470k.',
        '![Quiet noise](quiet_noise.png)',
        table(['Engine','Mode','VMID','Before worst SD counts','After worst SD counts','Largest route SD increase','Largest baseline shift counts'],[
            [r['engine'],r['mode'],'on' if r['vmid'] else 'off',
                number(max(r['A1_before_worst_sd_counts'],r['A2_before_worst_sd_counts']),3),
                number(max(r['A1_after_worst_sd_counts'],r['A2_after_worst_sd_counts']),3),
                number(r['largest_sd_increase_counts'],3),number(r['largest_abs_baseline_shift_counts'],1)]
            for r in comparisons if r['settle_us']==5 and r['matched_quiet_channels']==50]),
        'The following settings retain >1 count worst channel SD or a >0.5 count route SD increase. These are descriptive screening thresholds, not sensor acceptance limits:',
        table(['Engine','Mode','Settle us','VMID','Before worst SD','After worst SD','Largest SD increase'],[
            [r['engine'],r['mode'],r['settle_us'],'on' if r['vmid'] else 'off',
                number(max(r['A1_before_worst_sd_counts'],r['A2_before_worst_sd_counts']),3),
                number(max(r['A1_after_worst_sd_counts'],r['A2_after_worst_sd_counts']),3),number(r['largest_sd_increase_counts'],3)]
            for r in comparisons if r['matched_quiet_channels']==50 and
                (max(r['A1_after_worst_sd_counts'],r['A2_after_worst_sd_counts'])>1 or r['largest_sd_increase_counts']>.5)]),
        'Faster SPI scheduling changes the analog read history, especially at short settling delays. A throughput gain cannot establish equal acquisition quality. Channel-specific SD, baseline shifts and qualification gaps remain visible in `channel_comparisons.csv`; verify attached sensors and known input voltages before analog acceptance.',
        '## Identity, final state and remaining work',
        f'Raw root: `{data["raw_root"]}`. Frozen before/after production and diagnostic HEX files, source/image hashes, Git base identity, run/status logs and raw bytes are retained there. Production was restored with acquisition stopped; final status is in `after_lifecycle.json`. The earlier scan report is [here](../scan_modes_2026-10-13/README.md).',
        'Remaining checks: verify final SCLK/CS/EN edges with a logic analyzer, stimulate known sensor inputs, and compare analog settling with sensors attached. FIFO polling can be tuned further only with measured benefit; cached transition tables could trade some memory back for arithmetic if needed. Encoding/enqueue is unchanged and the counters account for sparse USB pressure.',
        'Reproduction: build and load the corresponding production revision, then run `Arduino_Sketches/TestBoard_ADC124/benchmarks/benchmark_adc124_fifo_gpio.py before|after RAW_ROOT`, `Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_fifo_gpio.py RAW_ROOT RESULT_ROOT`, and `Arduino_Sketches/TestBoard_ADC124/benchmarks/report_adc124_fifo_gpio.py RESULT_ROOT`. Run the benchmark with `--extended-quiet` after each completed main matrix for the matched 15 s quiet captures. Raw folders must be new; captures are not overwritten. Supplementary long records retain their duration in channel comparisons. `capture_metrics.csv`, `summary.csv`, `comparisons.csv`, `channel_noise_baselines.csv`, `channel_comparisons.csv`, quiet-bin CSVs and thresholds contain detailed evidence.']
    (folder/'README.md').write_text('\n\n'.join(texts)+'\n',encoding='utf-8')
    print('Rendered report and four plots',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('output',type=Path)
    report(parser.parse_args().output)
