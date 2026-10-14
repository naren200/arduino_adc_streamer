"""Render VMID settling parity, throughput and common-window noise comparisons."""
from pathlib import Path
import argparse
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from report_adc124_fifo_gpio import table,number

ROOT=Path(__file__).resolve().parents[1]
COLORS={'blocking':'#0072B2','dma':'#009E73','lpspi':'#D55E00'}


def report(folder):
    folder=folder.resolve(); d=json.loads((folder/'analysis.json').read_text())
    comparisons=d['comparisons']; rows=d['rows']; summary=d['summary']; channels=d['channel_comparisons']
    if len(comparisons)!=24: raise ValueError('Need all 24 new production settings')
    plt.rcParams.update({'font.size':10,'axes.grid':True,'grid.alpha':.25,'figure.dpi':150})
    fig,axes=plt.subplots(1,2,figsize=(12,4.6),constrained_layout=True)
    for ax,mode in zip(axes,('adc','mux')):
        for engine,color in COLORS.items():
            selected=[next(r for r in comparisons if (r['mode'],r['engine'],r['vmid_setting'])==(mode,engine,str(s))) for s in (0,1,5)]
            ax.plot((0,1,5),[r['after_rate_hz'] for r in selected],'-o',color=color,label=engine)
            control=next(r for r in comparisons if (r['mode'],r['engine'],r['vmid_setting'])==(mode,engine,'default'))
            ax.scatter(5,control['after_rate_hz'],marker='D',facecolors='none',edgecolors=color,s=80)
            ax.scatter(5,control['before_rate_hz'],marker='x',color=color,s=60)
        ax.set(xticks=(0,1,5),xlabel='Explicit VMID analog wait (us)',ylabel='Delivered sweeps/s/channel',title=mode.upper()+' scan')
    axes[0].legend(); fig.suptitle('Sensor wait fixed at 5 us; diamonds = inherited default, crosses = previous firmware')
    fig.savefig(folder/'sampling_rates.png'); plt.close(fig)

    fig,ax=plt.subplots(figsize=(9,4.8),constrained_layout=True)
    labels=[]; default=[]; explicit=[]
    for mode in ('adc','mux'):
        for engine in COLORS:
            labels.append(mode.upper()+' / '+engine)
            for setting,values in (('default',default),('5',explicit)):
                values.append(next(r['rate_change_vs_before_percent'] for r in comparisons if
                    (r['mode'],r['engine'],r['vmid_setting'])==(mode,engine,setting)))
    y=list(range(len(labels)))
    ax.barh([v-.18 for v in y],default,height=.35,label='New inherited 5 us')
    ax.barh([v+.18 for v in y],explicit,height=.35,label='New explicit 5 us')
    ax.axvline(0,color='black',linewidth=.8); ax.set(yticks=y,yticklabels=labels,
        xlabel='Measured rate change against fresh previous firmware (%)',title='Equal-delay control')
    ax.legend(); fig.savefig(folder/'parity_control.png'); plt.close(fig)

    fig,axes=plt.subplots(1,2,figsize=(12,4.6),constrained_layout=True)
    for ax,mode in zip(axes,('adc','mux')):
        for engine,color in COLORS.items():
            selected=[next(r for r in comparisons if (r['mode'],r['engine'],r['vmid_setting'])==(mode,engine,str(s))) for s in (0,1,5)]
            ax.plot((0,1,5),[r['after_worst_sd_counts'] for r in selected],'-o',color=color,label=engine)
            control=next(r for r in comparisons if (r['mode'],r['engine'],r['vmid_setting'])==(mode,engine,'default'))
            ax.scatter(5,control['before_worst_sd_counts'],marker='x',color=color,s=60)
        ax.set(xticks=(0,1,5),xlabel='Explicit VMID analog wait (us)',ylabel='Worst sensor-channel SD (counts)',title=mode.upper()+' scan')
    axes[0].legend(); fig.suptitle('Common quiet windows; median SD per route over qualified repetitions, then maximum')
    fig.savefig(folder/'quiet_noise.png'); plt.close(fig)

    raw=ROOT/d['raw_root']; life=json.loads((raw/'after_lifecycle.json').read_text())
    total=sum(r['frames'] for r in rows); acquired=sum(r['sampling_sweeps'] for r in rows)
    discarded=sum(r['usb_frames_discarded'] for r in rows)
    errors={k:sum(r[k] for r in rows) for k in ('transfer_errors','data_errors','usb_write_errors')}
    quiet=[r for r in rows if 'quiet_qualified' in r]; unqualified=[r for r in quiet if not r['quiet_qualified']]
    parity=[r for r in comparisons if r['vmid_setting'] in ('default','5')]
    parity_max=max(abs(r['rate_change_vs_before_percent']) for r in parity)
    sections=['# ADC124 independent VMID settling',
        '2026-10-14 | COM4 | connectors disconnected | charger connected',
        '## Firmware and command behavior',
        'Added `vmidsettle default|0..1000*`. Boot defaults to `default`: changing `muxsettle` also updates VMID delay. Explicit values remain fixed when the sensor delay changes; `vmidsettle default*` restores following. Status reports the effective `vmid_settle_us` and `vmid_settle_mode=follow_sensor|explicit`. Revision is `adc124-vmid-settle-v1`, protocol remains 3. Payload layout, routes, primes and conversions are unchanged.',
        'The override applies to VMID conversions during acquisition and configured VMID parking at run preparation/stop/failure. Both scan modes use it; MUX phases retain one shared VMID wait after both banks finish switching. Zero skips only the extra analog wait: the 200 ns off/on guards, ADC priming and all VMID conversions remain. VMID off suppresses optional VMID conversions as before. Startup begins with the normal inherited 5 us default.',
        'Examples: `muxsettle 5*` then `vmidsettle 0*` gives sensor 5 us / VMID 0 us. `vmidsettle 1*` chooses 1 us. `vmidsettle 5*` explicitly matches 5 us; `vmidsettle default*` follows future sensor changes. The standalone runner adds `--vmid-settle-us default 5 0 1`, distinct test IDs/comparison keys, actual setting metadata and restoration of explicit overrides including zero. Older firmware supports the inherited mode only; explicit requests are rejected before configuration. ADC124 still has no desktop GUI profile.',
        '## Verification',
        'Both production/diagnostic builds passed. 100 focused tests passed: existing 708 digital acquisition cases per build plus 216 full/single-array override cases, 90 sparse override cases, default/explicit-5 parity in both modes/all engines, inheritance/reset/invalid commands, GPIO guards, ADC primes/repeats, feature detection, CLI identity, delay budgets and ARM USB checks. In the tagged digital model, explicit/inherited 5 us produced identical sample ordering, delay sequences and sweep duration.',
        'Fresh previous-firmware baseline: 6 settings x 3 repetitions. New production: ADC/MUX x blocking/DMA/LPSPI x inherited/explicit 5/0/1 us x 3 repetitions = 72 captures. Each production record has 1 s warm-up + 5 s measurement. Diagnostic: 24 settings x 1 repetition, 1 s warm-up + 2 s measurement. All use sensor wait 5 us, VMID on, both full arrays, requested 16 MHz, repeat 1, burst/manual acquisition. Sessions are sequential; environmental drift is not eliminated. The baseline includes the preceding FIFO/GPIO optimizations.',
        f'{len(rows)} captures; {total:,} complete frames received; {acquired:,} acquired; {discarded:,} accounted USB discards ({100*discarded/acquired:.5f}%). Error totals: {errors}. Every sent frame was received. Raw hashes, independent parsers, 12-bit code ranges, physical payload labels and nonoverlapping timestamps were checked. Timing is measured using device sweep-start intervals; actual SCLK/individual edge times were not probed.',
        '## Equal-delay control: sensor = VMID = 5 us',
        f'The effective wait remains 5 us for both default and explicit settings. The largest measured throughput change versus previous firmware is {parity_max:.3f}%. Default and explicit controls use the same acquisition path; small build/layout/timing changes and sequential-session effects remain visible rather than being called bit-for-bit analog equivalence.',
        '![Equal-delay control](parity_control.png)',
        table(['Engine','Mode','Previous sweeps/s','New default','New explicit 5','Default change','Explicit change'],[
            [e,m,number(next(r['before_rate_hz'] for r in parity if r['engine']==e and r['mode']==m)),
                number(next(r['after_rate_hz'] for r in parity if r['engine']==e and r['mode']==m and r['vmid_setting']=='default')),
                number(next(r['after_rate_hz'] for r in parity if r['engine']==e and r['mode']==m and r['vmid_setting']=='5')),
                number(next(r['rate_change_vs_before_percent'] for r in parity if r['engine']==e and r['mode']==m and r['vmid_setting']=='default'),3)+'%',
                number(next(r['rate_change_vs_before_percent'] for r in parity if r['engine']==e and r['mode']==m and r['vmid_setting']=='5'),3)+'%']
            for m in ('adc','mux') for e in COLORS]),
        '## Measured effects of shorter VMID waits','![Sampling rates](sampling_rates.png)',
        table(['Engine','Mode','VMID us','Sweeps/s','Gain vs explicit 5','Measured saved us/sweep','Calculated saved us/sweep'],[
            [r['engine'],r['mode'],r['vmid_setting'],number(r['after_rate_hz']),number(r['rate_gain_vs_5_percent'],2)+'%',
                number(r['measured_period_saved_vs_5_us'],3),number(r['estimated_saved_vs_5_us'],1)]
            for r in comparisons if r['vmid_setting'] in ('0','1','5')]),
        'Calculated delay savings are 14*(5-D) us per ADC sweep and 7*(5-D) us per MUX sweep: 70/35 us at D=0, or 56/28 us at D=1. These are configured-delay arithmetic, not measured speed gains. Measurements also include skipped delay-call overhead, loop/profiling/interrupt effects and sparse USB pressure. SPI word count, CS batch count and switching guards do not decrease. All 134 conversion words and 28 batches still run at repeat 1. Diagnostic prepare-phase means for all settings are saved in summary.csv; validation is nested and profiling overhead is excluded from production gains.',
        '## Quiet sensor-channel acquisition quality',
        '50 ms bins; eight array/MUX median channel sample-SD envelopes; threshold max(0.6 counts, P20+0.15, lower40% median+4*1.4826*MAD). Any flagged bin and its neighboring bins are rejected for every channel together. No individual sample clipping/filtering/detrending. Each qualified record retains >=1 s and >=100 frames. Charger switching/rails were not separately recorded, so this method identifies relative envelope bursts rather than proving their electrical cause.',
        f'{len(quiet)-len(unqualified)}/{len(quiet)} production quiet records qualified. Unqualified records remain explicit in capture_metrics.csv. SD is ddof=1 around each route\'s own mean; repetitions are combined by per-route median, then the worst sensor channel is shown. The discarded VMID responses are validated digitally but are not exported, so their analog values/noise are not directly measured. Counts convert at 3.3/4096 V/count; biases A=470k/1M/470k/1M, B=1M/470k/1M/470k.',
        '![Quiet noise](quiet_noise.png)',
        table(['Engine','Mode','VMID setting','Previous worst SD','New worst SD','Largest route SD increase vs explicit 5','Largest baseline shift vs previous'],[
            [r['engine'],r['mode'],r['vmid_setting'],number(r['before_worst_sd_counts'],3),number(r['after_worst_sd_counts'],3),
                number(r['largest_sd_increase_vs_5_counts'],3),number(r['largest_abs_baseline_shift_vs_before_counts'],1)] for r in comparisons]),
        'Reducing the VMID dwell changes analog history. Functional streaming/parity checks do not qualify analog accuracy with attached sensors. Review channel_comparisons.csv for individual routes, baseline shifts, repeated noise changes and qualification coverage; sustained or isolated variation is retained by this method.',
        '## Identity and final state',
        f'Raw root: `{d["raw_root"]}`. Frozen before/after sources, both HEX files, source/image hashes, run/status/profile logs and raw bytes are retained. Final lifecycle: `{life["state"]}`; production revision `{life["final_production_status"]["firmware_revision"]}`, acquisition stopped, VMID setting inherited. Original both-array ADC/blocking/16 MHz/repeat 1/sensor 5 us/VMID-off configuration was restored.',
        'No commit or push was performed. Previous reports and raw captures remain intact. Reproduce with scripts/benchmark_adc124_vmid_settle.py, scripts/analyze_adc124_vmid_settle.py and scripts/report_adc124_vmid_settle.py. CSVs/analysis.json contain all measured settings, counters, profile phases, estimated budgets and channel metrics.']
    (folder/'README.md').write_text('\n\n'.join(sections)+'\n',encoding='utf-8')
    print('Rendered report and three plots',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('output',type=Path); report(p.parse_args().output)
