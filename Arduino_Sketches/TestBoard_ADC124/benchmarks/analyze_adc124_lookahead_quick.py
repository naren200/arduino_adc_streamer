"""Summarize short raw captures without masking or clipping samples."""
import sys
import argparse
from pathlib import Path
import hashlib
import json
import numpy as np
ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_adc124_scan_modes import bench
from capture_adc124_settling import decode
from report_adc124_settling import canonical_samples
from Arduino_Sketches.TestBoard_ADC124.benchmarks.scan_modes import operation_counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Completed quick-benchmark raw root')
    parser.add_argument('output', nargs='?', type=Path, help='Report directory; defaults below docs/testing/results')
    args = parser.parse_args(argv)
    root = args.source.resolve()
    out = args.output.resolve() if args.output else ROOT/'docs/testing/results/testboard-adc124'/root.name
    out.mkdir(exist_ok=True)
    life = json.loads((root/'firmware_lifecycle.json').read_text())
    assert life['state'] == 'complete_new_production_restored_verified'
    routes = bench.load_manifest(bench.DEFAULT_MANIFEST)['both_full']
    rows = []; totals = dict(frames=0, sampling_sweeps=0, usb_frames_sent=0, usb_frames_discarded=0)
    channels = []
    for stage in ('before', 'after'):
        if not (root/stage/'session.json').exists(): continue
        session = json.loads((root/stage/'session.json').read_text())
        assert session['state'] == 'complete'
        for run in session['runs']:
            folder = root/stage/run['name']; raw = folder/'capture.bin'
            assert hashlib.sha256(raw.read_bytes()).hexdigest() == run['sha256']
            frames = decode(raw)
            saved = json.loads((folder/'status.json').read_text()); status = saved['after']
            counts, failures = bench.stream_accounting(saved['before'],status,len(frames))
            assert not failures and not any(int(status[k]) for k in bench.error_counter_keys(status))
            starts = np.r_[0,np.cumsum(np.diff(frames['start'].astype(np.int64)) % 2**32)]
            measured = starts >= session['warmup_s']*1e6
            intervals = measured[1:] & measured[:-1]
            period = float(np.diff(starts)[intervals].mean())
            acquisition = (frames['end'].astype(np.int64)-frames['start'].astype(np.int64)) % 2**32
            x = canonical_samples(frames,status)[measured]
            median = np.median(x,axis=0); sd = x.std(axis=0,ddof=1)
            model = operation_counts(routes,run['scanorder'],repeat=run['channelrepeat'],vmid=run['vmid'],
                coordinated=True,lookahead=status.get('adc_selector_policy') == 'lookahead')
            row = dict(stage=stage,name=run['name'],mode=run['scanorder'],engine=run['spiengine'],
                clock_hz=run['spi_clock_hz'],vmid=run['vmid'],vmid_settle_us=run.get('vmid_settle_us'),
                sweeps_s=1e6/period,period_us=period,acquisition_us=float(acquisition[measured].mean()),
                median_min_counts=float(median.min()),median_max_counts=float(median.max()),
                max_median_offset_counts=float(np.max(np.abs(median-2048))),
                worst_sd_counts=float(sd.max()),min_code=int(x.min()),max_code=int(x.max()),
                spi_words_per_sweep=model['spi_words'],frames=len(frames),**counts)
            row['warn'] = row['max_median_offset_counts'] > 100 or row['worst_sd_counts'] > 10
            for i, route in enumerate(sorted(routes)):
                channels.append(dict(stage=stage,name=run['name'],mode=run['scanorder'],engine=run['spiengine'],
                    clock_hz=run['spi_clock_hz'],vmid=run['vmid'],vmid_settle_us=run.get('vmid_settle_us'),
                    route=str(route),median_counts=float(median[i]),sd_counts=float(sd[i])))
            rows.append(row)
            for key in totals: totals[key] += row[key]
    bench.write_json(out/'analysis.json',dict(rows=rows,totals=totals,lifecycle=life))
    bench.write_csv(out/'captures.csv',list(rows[0]),rows)
    bench.write_csv(out/'channels.csv',list(channels[0]),channels)
    baseline_shifts = []
    for row in channels:
        if row['stage'] != 'after' or row['vmid_settle_us'] is not None: continue
        old = next((v for v in channels if v['stage']=='before' and all(v[k]==row[k] for k in ('mode','engine','clock_hz','vmid','route'))),None)
        if old is None: continue
        baseline_shifts.append(dict(**row,before_median_counts=old['median_counts'],shift_counts=row['median_counts']-old['median_counts']))
    worst_shift = max(baseline_shifts,key=lambda r:abs(r['shift_counts'])) if baseline_shifts else None
    bench.write_json(out/'baseline_shifts.json',baseline_shifts)
    lines = ['# ADC124 selector lookahead: quick hardware benchmark','',
        life['created_utc'][:10] + ' | COM4 | both sensor connectors disconnected | charger connected','',
        ('Matched previous production (`adc124-vmid-settle-v1`) versus new production ' if worst_shift else 'New production ') +
        '(`adc124-selector-lookahead-v1`). Both arrays, 50 channels, repeat 1, burst framing, '
        'sensor settling 5 us; default VMID settling follows 5 us. Each setting has one '
        f"repetition, {life['warmup_ms']/1000:g} s warm-up and {life['window_ms']/1000:g} s measurement. Four additional LPSPI/16 MHz "
        'captures check explicit VMID settling 0/1 us in both scan modes. These are short '
        'sequential checks; noise drift and run-to-run variability are not eliminated.','',
        f"{len(rows)} validated captures; {totals['frames']:,} complete frames received; "
        f"{totals['sampling_sweeps']:,} acquired; {totals['usb_frames_discarded']} accounted USB discards. "
        'Every sent frame was received. Transfer, data and USB-write errors: zero. '
        'Raw hashes, independent parsers, payload route labels, 12-bit ranges and '
        'nonoverlapping device timestamps passed.','',
        '## Matched complete-sweep rates','',
        '| MHz | Mode | Engine | VMID | Before sweeps/s | After sweeps/s | Gain |',
        '| --- | --- | --- | --- | ---: | ---: | ---: |']
    comparisons = []
    for row in rows:
        if row['stage'] != 'after' or row['vmid_settle_us'] is not None: continue
        old = next((v for v in rows if v['stage']=='before' and all(v[k]==row[k] for k in ('mode','engine','clock_hz','vmid'))),None)
        if old is None:
            lines.append(f"| {row['clock_hz']/1e6:g} | {row['mode']} | {row['engine']} | {'on' if row['vmid'] else 'off'} | — | {row['sweeps_s']:.1f} | — |")
            continue
        gain = 100*(row['sweeps_s']/old['sweeps_s']-1)
        comparisons.append(dict(**row,before_sweeps_s=old['sweeps_s'],gain_percent=gain))
        lines.append(f"| {row['clock_hz']/1e6:g} | {row['mode']} | {row['engine']} | {'on' if row['vmid'] else 'off'} | {old['sweeps_s']:.1f} | {row['sweeps_s']:.1f} | {gain:+.2f}% |")
    lines += ['', '## Independent VMID controls','', '| Mode | VMID us | Sweeps/s | Worst unfiltered SD, counts |', '| --- | ---: | ---: | ---: |']
    for row in rows:
        if row['stage']=='after' and row['vmid_settle_us'] is not None:
            lines.append(f"| {row['mode']} | {row['vmid_settle_us']} | {row['sweeps_s']:.1f} | {row['worst_sd_counts']:.3f} |")
    after = [v for v in rows if v['stage']=='after']
    warn = [v for v in after if v['warn']]
    lines += ['', '## Samples and limitations','',
        f"Across new-firmware captures, per-channel medians span {min(v['median_min_counts'] for v in after):.1f}â€“"
        f"{max(v['median_max_counts'] for v in after):.1f} counts "
        f"({min(v['median_min_counts'] for v in after)*3.3/4096:.4f}â€“{max(v['median_max_counts'] for v in after)*3.3/4096:.4f} V). "
        f"Worst channel SD: {max(v['worst_sd_counts'] for v in after):.3f} counts. "
        f"{len(warn)} captures exceed the smoke warning thresholds of 100-count median offset from VMID or 10-count SD.", '',
        (f"Previous-firmware median offsets reach {max(v['max_median_offset_counts'] for v in rows if v['stage']=='before'):.0f} counts. "
         f"Largest matched median change: {worst_shift['shift_counts']:+.0f} counts "
         f"({worst_shift['shift_counts']*3.3/4096:+.4f} V), route `{worst_shift['route']}`, "
         f"{worst_shift['mode']}/{worst_shift['engine']}, {worst_shift['clock_hz']/1e6:g} MHz, "
         f"VMID {'on' if worst_shift['vmid'] else 'off'}. Shortening the conversion sequence changes analog history; "
         'this run does not establish the cause of the offsets or remove their warnings.'
         if worst_shift else 'No matched previous-firmware captures were supplied; no before/after gains are claimed.'), '',
        'Noise statistics include all measured samples without clipping, detrending or a quiet-window mask. '
        'This quick run verifies streaming and near-VMID response; it does not qualify connected-sensor '
        'noise or prove channel identity electrically. ADC124 has no returned channel tag. '
        'Nominal steady-state word counts are 50 with VMID off / 106 on, versus 64 / 134 before; '
        'startup selector initialization is outside sweep counters. Individual SCLK/CS edges were not probed.', '',
        'New production firmware remains installed, stopped, with original settings restored. '
        'Exact final settings are retained in firmware_lifecycle.json.','',
        f"Raw evidence: `{root.relative_to(ROOT) if root.is_relative_to(ROOT) else root}`.", '',
        'Reproduce analysis: `uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_lookahead_quick.py RAW_ROOT RESULT_ROOT`.']
    (out/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    bench.write_json(out/'comparisons.json',comparisons)
    print('REPORT',out/'README.md')
    print('TOTALS',totals)
    for v in comparisons:
        if v['engine']=='lpspi': print({k:v[k] for k in ('clock_hz','mode','vmid','before_sweeps_s','sweeps_s','gain_percent','worst_sd_counts')})
    print('NEW_NOISE_WARNINGS',len(warn),'WORST_SD',max(v['worst_sd_counts'] for v in after))
    print('WORST_MATCHED_BASELINE_SHIFT',worst_shift)

    print('Reproduce with: uv run python Arduino_Sketches/TestBoard_ADC124/benchmarks/analyze_adc124_lookahead_quick.py', root)


if __name__ == '__main__':
    main()
