"""Measured scan timing/noise comparison; raw hashes and all-channel quiet masks.

Uses the same 50 ms/eight-group quantization-aware common-window method as the
supplied baseline report. No individual sample is clipped, filtered or detrended.
"""
from pathlib import Path
import argparse
import csv
import hashlib
import json
import sys
import numpy as np

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from Arduino_Sketches.TestBoard_ADC124.benchmarks import testboard_adc124_benchmark as bench
from Arduino_Sketches.TestBoard_ADC124.benchmarks.scan_modes import operation_counts
from capture_adc124_settling import decode
from report_adc124_settling import canonical_samples, quiet_mask

LSB = 3.3 / 4096
MODES = ("adc", "mux")
ENGINES = ("blocking", "dma", "lpspi")
SETTLES = (1, 2, 5, 10, 50)


def csv_write(path, rows):
    if not rows: return
    fields = sorted({k for row in rows for k in row})
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def process(source, run, session, output, allow_insufficient_quiet=False):
    folder = source / run["name"]
    if hashlib.sha256((folder / "capture.bin").read_bytes()).hexdigest() != run["sha256"]:
        raise ValueError("Raw hash mismatch")
    frames = decode(folder / "capture.bin")
    saved = json.loads((folder / "status.json").read_text()); status = saved["after"]
    config = bench.BenchmarkConfig(run["array"], run["scanorder"], run["spiengine"], run["spi_clock_hz"],
                                   run["channelrepeat"], run["mux_settle_us"], run["vmid"], run["spi_frame"],run.get('vmid_settle_us'))
    bench.validate_status(status, config, bench.load_manifest(bench.DEFAULT_MANIFEST)["both_full"])
    count, failures = bench.stream_accounting(saved["before"], status, len(frames))
    if failures or any(int(status[k]) for k in bench.error_counter_keys(status)): raise ValueError(str(failures or "Firmware errors"))
    starts = np.r_[0, np.cumsum(np.diff(frames["start"].astype(np.int64)) % 2**32)]
    periods = np.diff(starts)
    acquisition = (frames["end"].astype(np.int64) - frames["start"].astype(np.int64)) % 2**32
    warmup = session["warmup_s"]
    measured = starts / 1e6 >= warmup
    intervals = measured[:-1] & measured[1:]
    gaps = periods[intervals] - acquisition[:-1][intervals]
    if np.any(gaps < 0): raise ValueError("Overlapping acquisition")
    mean_period = float(periods[intervals].mean())
    rows = dict(stage=session["stage"], name=run["name"], repetition=run["repetition"],
        mode=run["scanorder"], engine=run["spiengine"], vmid=run["vmid"], settle_us=run["mux_settle_us"],
        spi_frame=run["spi_frame"], protocol_version=3, firmware_revision=status["firmware_revision"],
        scan_semantics=status["scan_semantics"], requested_spi_clock_hz=run["spi_clock_hz"],
        vmid_settle_us=config.vmid_settle_us,
        vmid_settle_effective_us=int(status.get('vmid_settle_us',config.mux_settle_us)),
        vmid_settle_mode=status.get('vmid_settle_mode','follow_sensor'),
        frames=len(frames), measured_frames=int(measured.sum()), mean_period_us=mean_period,
        delivered_sweeps_s=1e6 / mean_period, useful_values_s=50e6 / mean_period,
        mean_acquisition_us=float(acquisition[measured].mean()), mean_gap_us=float(gaps.mean()),
        period_p99_us=float(np.quantile(periods[intervals], .99)),
        **{k:count[k] for k in ("sampling_sweeps","usb_frames_sent","usb_frames_discarded","usb_discard_percent")},
        **{k:int(status[k]) for k in bench.error_counter_keys(status)},
        sampling_period_max_us=int(status["sampling_period_max_us"]), sampling_period_over_1ms=int(status["sampling_period_over_1ms"]))
    full_span = float(starts[-1]) + mean_period
    rows["sampling_sweeps_s_estimate_whole_run"] = count["sampling_sweeps"] * 1e6 / full_span
    # A stable complete-frame rate is a measurement. Inferring sampled rate
    # from sent+discarded counters and delivered endpoints is explicitly estimated.
    c = operation_counts(bench.load_manifest(bench.DEFAULT_MANIFEST)["both_full"], config.scanorder,
                         config.channelrepeat, config.vmid, config.spi_frame,
                         coordinated=status.get('mux_switch_guards') == 'coordinated',
                         lookahead=status.get('adc_selector_policy') == 'lookahead')
    rows.update({"estimated_" + k:v for k,v in c.items() if k != "basis"})
    rows["estimated_settle_budget_us"] = (c['sensor_settling_windows'] * config.mux_settle_us +
        c['vmid_settling_windows'] * rows['vmid_settle_effective_us'])
    profile = json.loads((folder / "firmware_profile.jsonl").read_text().splitlines()[-1])["summary"]
    if profile["enabled"]:
        for phase, metric in profile["phases"].items(): rows["profile_" + phase + "_us"] = metric["mean_us"]
        rows["estimated_profile_prepare_residual_us"] = rows["profile_prepare_us"] - rows["estimated_settle_budget_us"]
    channels = []
    if not profile["enabled"]:
        canonical = tuple(sorted(bench.load_manifest(bench.DEFAULT_MANIFEST)["both_full"]))
        x = canonical_samples(frames, status)
        groups = [[i for i,r in enumerate(canonical) if (r.array,r.mux) == (a,m)] for a in (1,2) for m in (1,2,3,4)]
        mask, edges, envelope, thresholds, keep, above = quiet_mask(starts / 1e6, x, groups,
            start=warmup, end=warmup + session["measurement_s"], require_coverage=not allow_insufficient_quiet)
        z = x[mask]
        rows.update(quiet_seconds=float(keep.sum() * .05), quiet_frames=int(mask.sum()),
                    rejected_time_percent=float(100 * (1 - keep.mean())), above_threshold_bins=int(above.sum()))
        bins = [dict(start_s=float(a), end_s=float(b), quiet=bool(k), above_threshold=bool(ab),
                    **{f"group{g+1}_sd_counts":float(v) for g,v in enumerate(env)})
                for a,b,k,ab,env in zip(edges[:-1],edges[1:],keep,above,envelope)]
        csv_write(output / (session["stage"] + "__" + run["name"] + "__quiet_bins.csv"), bins)
        (output / (session["stage"] + "__" + run["name"] + "__quiet_thresholds.json")).write_text(json.dumps(thresholds.tolist()))
        rows['quiet_qualified'] = bool(keep.sum() * .05 >= 1 and mask.sum() >= 100)
        if not rows['quiet_qualified']:
            rows['quiet_failure'] = 'Less than 1 second / 100 frames of common quiet data'
            return rows, channels
        for i, route in enumerate(canonical):
            sd = float(z[:,i].std(ddof=1)); pp = float(np.ptp(z[:,i])); baseline = float(np.median(z[:,i]))
            channels.append(dict(stage=session["stage"], name=run["name"], repetition=run["repetition"],
                mode=config.scanorder, engine=config.spiengine, settle_us=config.mux_settle_us, vmid=config.vmid,
                spi_frame=config.spi_frame, route=str(route), array=route.array, mux=route.mux,
                vmid_settle_us=config.vmid_settle_us,
                vmid_settle_effective_us=rows['vmid_settle_effective_us'],
                sensor=f"PZT{route.sensor}", sensor_channel=route.sensor_channel, bias_ohms=route.bias_ohms,
                quiet_frames=len(z), sd_counts=sd, sd_volts=sd*LSB, ptp_counts=pp, ptp_volts=pp*LSB,
                baseline_counts=baseline, baseline_volts=baseline*LSB))
        for a in (1,2):
            ix=[i for i,r in enumerate(canonical) if r.array == a]
            sd=z[:,ix].std(axis=0,ddof=1); pp=np.ptp(z[:,ix],axis=0); levels=np.median(z[:,ix],axis=0)
            rows.update({f"A{a}_quiet_sd_median_counts":float(np.median(sd)),f"A{a}_quiet_sd_max_counts":float(sd.max()),
                         f"A{a}_quiet_ptp_max_counts":float(pp.max()),f"A{a}_baseline_spread_counts":float(np.ptp(levels)),
                         f"A{a}_baseline_min_volts":float(levels.min()*LSB),f"A{a}_baseline_max_volts":float(levels.max()*LSB)})
    return rows, channels


def summarize(rows):
    groups = {}
    for row in rows:
        key=(row["stage"],row["mode"],row["engine"],row["vmid"],row["settle_us"],row["spi_frame"],row.get('vmid_settle_us'))
        groups.setdefault(key,[]).append(row)
    output=[]
    for key, group in groups.items():
        result=dict(zip(("stage","mode","engine","vmid","settle_us","spi_frame","vmid_settle_us"),key));result["repetitions"]=len(group)
        for field,value in group[0].items():
            if isinstance(value,(int,float)) and not isinstance(value,bool) and field not in ("repetition","settle_us","protocol_version"):
                numbers=[r[field] for r in group if field in r]
                result[field]=float(np.median(numbers));result[field+"_min"]=float(min(numbers));result[field+"_max"]=float(max(numbers))
        output.append(result)
    return output


def analyze(root, output, partial=False):
    output.mkdir(parents=True,exist_ok=True)
    cache=json.loads((output/"analysis_cache.json").read_text()) if (output/"analysis_cache.json").exists() else {}
    rows=[]; channels=[]; pending=[]
    for stage in ("production","single_controls","diagnostic"):
        path=root/stage/"session.json"
        if not path.exists(): continue
        session=json.loads(path.read_text())
        if not partial and session["state"] != "complete":raise ValueError(f"Incomplete {stage}")
        for run in session["runs"]:
            key=stage+"/"+run["name"]
            if not partial and hashlib.sha256((root/stage/run['name']/"capture.bin").read_bytes()).hexdigest() != run['sha256']:
                raise ValueError("Final evidence hash mismatch")
            if key not in cache:
                try:
                    row,ch=process(root/stage,run,session,output);cache[key]=dict(row=row,channels=ch,sha256=run["sha256"])
                except ValueError as error:
                    if partial: pending.append(dict(run=key,error=str(error)));continue
                    raise
            if cache[key]["sha256"] != run["sha256"]:raise ValueError("Cache hash mismatch")
            rows.append(cache[key]["row"]);channels.extend(cache[key]["channels"])
    (output/"analysis_cache.json").write_text(json.dumps(cache,allow_nan=False))
    summaries=summarize(rows)
    baseline=json.loads((ROOT/"docs/testing/results/testboard-adc124/mux_timing_2026-10-13/timing_analysis.json").read_text())
    comparisons=[]
    for r in summaries:
        if r["stage"] != "production":continue
        old=next(v for v in baseline["summary"] if (v["engine"],v["vmid"],v["settle_us"])==(r["engine"],r["vmid"],r["settle_us"]))
        partner=next((v for v in summaries if (v["stage"],v["mode"],v["engine"],v["vmid"],v["settle_us"])
                     ==("production","adc",r["engine"],r["vmid"],r["settle_us"])),None)
        comparisons.append(dict(mode=r["mode"],engine=r["engine"],vmid=r["vmid"],settle_us=r["settle_us"],
            old_v2_period_us=old["mean_period_us"],new_period_us=r["mean_period_us"],
            speed_gain_vs_reference_percent=100*(old["mean_period_us"]/r["mean_period_us"]-1),
            speed_gain_vs_new_adc_percent=100*(partner["mean_period_us"]/r["mean_period_us"]-1) if partner else None))
    csv_write(output/"capture_metrics.csv",rows);csv_write(output/"summary.csv",summaries)
    csv_write(output/"channel_noise_baselines.csv",channels);csv_write(output/"comparisons.csv",comparisons)
    result=dict(rows=rows,summary=summaries,comparisons=comparisons,pending=pending,
        quiet_method="50 ms bins; eight ADC/MUX group median channel sample SDs; threshold max(0.6, P20+0.15, lower40% median+4*1.4826*MAD); any-group rejection plus neighboring bins; one common time mask for all 50 channels; no sample clipping/filtering/detrending",
        baseline_timing_source="docs/testing/results/testboard-adc124/mux_timing_2026-10-13/timing_analysis.json",
        raw_root=str(root.relative_to(ROOT)))
    (output/"analysis.json").write_text(json.dumps(result,indent=2,allow_nan=False))
    print(f"Analyzed {len(rows)} captures, {len(summaries)} settings; pending {len(pending)}",flush=True)
    return result


if __name__ == "__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("source",type=Path);p.add_argument("output",type=Path);p.add_argument("--partial",action="store_true")
    a=p.parse_args();analyze(a.source,a.output,a.partial)
