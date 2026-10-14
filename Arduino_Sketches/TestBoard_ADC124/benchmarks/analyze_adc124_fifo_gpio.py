"""Compare matched FIFO/GPIO captures using common quiet windows and raw evidence."""
from pathlib import Path
import argparse
import hashlib
import json
import numpy as np

from analyze_adc124_scan_modes import process, summarize, csv_write, ROOT, bench, operation_counts

QUIET_METHOD = ('50 ms bins; eight array/MUX median channel SD envelopes; threshold '
    'max(0.6, P20+0.15, lower40% median+4*1.4826*MAD); reject any flagged bin and '
    'its neighbors for all 50 channels; no individual sample clipping/filtering/detrending')


def analyze(source, output, partial=False):
    source = source.resolve(); output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    cache_path = output / 'analysis_cache.json'
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    rows = []; channels = []
    for version in ('before', 'after'):
        for build in ('production', 'diagnostic', 'extended_quiet'):
            stage = version + '_' + build; folder = source / stage
            if not (folder / 'session.json').exists():
                if partial or build == 'extended_quiet': continue
                raise ValueError('Missing stage ' + stage)
            session = json.loads((folder / 'session.json').read_text())
            if not partial and (session['state'] != 'complete' or
                    len(session['runs']) != ((60 if build != 'extended_quiet' else 1) * session['repetitions'])):
                raise ValueError('Incomplete matrix ' + stage)
            identity = json.loads((source / (version + '_firmware') / 'identity.json').read_text())
            image_key = next(k for k in identity['source_image_sha256'] if
                k.endswith(('teensy41_profile' if build == 'diagnostic' else 'teensy41') + '\\firmware.hex'))
            for run in session['runs']:
                key = stage + '/' + run['name']
                # Final analysis hashes raw evidence even when metrics are cached.
                if key not in cache or not partial:
                    if hashlib.sha256((folder / run['name'] / 'capture.bin').read_bytes()).hexdigest() != run['sha256']:
                        raise ValueError('Raw evidence hash mismatch')
                if key not in cache:
                    row, ch = process(folder, run, session, output, allow_insufficient_quiet=True)
                    expected_revision = 'adc124-shared-mux-burst-v1' if version == 'before' else 'adc124-fifo-grouped-gpio-v2'
                    if row['firmware_revision'] != expected_revision: raise ValueError('Capture identity mismatch')
                    model = operation_counts(bench.load_manifest(bench.DEFAULT_MANIFEST)['both_full'],
                        row['mode'], vmid=row['vmid'], coordinated=version == 'after')
                    row.update({'estimated_' + k: v for k,v in model.items() if k != 'basis'})
                    row['version'] = version; row['image_sha256'] = identity['source_image_sha256'][image_key]
                    for channel in ch:
                        channel['version'] = version; channel['measurement_s'] = session['measurement_s']
                    cache[key] = dict(row=row, channels=ch, sha256=run['sha256'])
                if cache[key]['sha256'] != run['sha256']: raise ValueError('Cache hash mismatch')
                rows.append(cache[key]['row']); channels.extend(cache[key]['channels'])
    cache_path.write_text(json.dumps(cache, allow_nan=False))
    summary = summarize(rows)
    comparisons = []; channel_comparisons = []
    settings = ('mode', 'engine', 'vmid', 'settle_us')
    for after in summary:
        if after['stage'] != 'after_production': continue
        before = next(r for r in summary if r['stage'] == 'before_production' and
            all(r[k] == after[k] for k in settings))
        comparison = {k: after[k] for k in settings}
        comparison.update(before_period_us=before['mean_period_us'], after_period_us=after['mean_period_us'],
            before_sweeps_s=before['delivered_sweeps_s'], after_sweeps_s=after['delivered_sweeps_s'],
            rate_gain_percent=100 * (before['mean_period_us'] / after['mean_period_us'] - 1),
            period_saved_us=before['mean_period_us'] - after['mean_period_us'],
            before_acquisition_us=before['mean_acquisition_us'], after_acquisition_us=after['mean_acquisition_us'])
        pair = []
        for route in sorted({c['route'] for c in channels if c['stage'] == 'after_production'}):
            groups = []
            for version in ('before', 'after'):
                eligible = [c for c in channels if c['route'] == route and all(c[k] == after[k] for k in settings)]
                extended = [c for c in eligible if c['stage'] == version + '_extended_quiet']
                groups.append(extended or [c for c in eligible if c['stage'] == version + '_production'])
            if not all(groups): continue
            metrics = [dict(sd=float(np.median([c['sd_counts'] for c in group])),
                baseline=float(np.median([c['baseline_counts'] for c in group]))) for group in groups]
            c = {**{k:after[k] for k in settings}, 'route':route, 'array':groups[0][0]['array'],
                'mux':groups[0][0]['mux'], 'bias_ohms':groups[0][0]['bias_ohms'],
                'before_quiet_repetitions':len(groups[0]), 'after_quiet_repetitions':len(groups[1]),
                'before_measurement_s':groups[0][0].get('measurement_s',15 if groups[0][0]['stage'].endswith('extended_quiet') else 2),
                'after_measurement_s':groups[1][0].get('measurement_s',15 if groups[1][0]['stage'].endswith('extended_quiet') else 2),
                'before_sd_counts':metrics[0]['sd'], 'after_sd_counts':metrics[1]['sd'],
                'sd_change_counts':metrics[1]['sd']-metrics[0]['sd'],
                'before_baseline_counts':metrics[0]['baseline'], 'after_baseline_counts':metrics[1]['baseline'],
                'baseline_shift_counts':metrics[1]['baseline']-metrics[0]['baseline']}
            pair.append(c); channel_comparisons.append(c)
        for array in (1,2):
            group = [c for c in pair if c['array'] == array]
            comparison[f'A{array}_before_worst_sd_counts'] = max((c['before_sd_counts'] for c in group), default=None)
            comparison[f'A{array}_after_worst_sd_counts'] = max((c['after_sd_counts'] for c in group), default=None)
        comparison['matched_quiet_channels'] = len(pair)
        comparison['largest_sd_increase_counts'] = max((c['sd_change_counts'] for c in pair), default=None)
        comparison['largest_abs_baseline_shift_counts'] = max((abs(c['baseline_shift_counts']) for c in pair), default=None)
        comparisons.append(comparison)
    csv_write(output / 'capture_metrics.csv', rows); csv_write(output / 'summary.csv', summary)
    csv_write(output / 'comparisons.csv', comparisons); csv_write(output / 'channel_noise_baselines.csv', channels)
    csv_write(output / 'channel_comparisons.csv', channel_comparisons)
    result = dict(raw_root=str(source.relative_to(ROOT)), quiet_method=QUIET_METHOD,
        rows=rows, summary=summary, comparisons=comparisons, channel_comparisons=channel_comparisons,
        state='partial' if partial else 'complete')
    (output / 'analysis.json').write_text(json.dumps(result, indent=2, allow_nan=False))
    print(f'Analyzed {len(rows)} captures, {len(comparisons)} matched production settings', flush=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path); parser.add_argument('output', type=Path)
    parser.add_argument('--partial', action='store_true'); args = parser.parse_args()
    analyze(args.source, args.output, args.partial)
