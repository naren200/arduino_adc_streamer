"""Render measured scan comparison as portable Markdown and scientific plots."""
from pathlib import Path
import argparse
import csv
import json
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from capture_adc124_settling import decode, bench
from report_adc124_settling import canonical_samples

ENGINES=("blocking","dma","lpspi");SETTLES=(1,2,5,10,50);MODES=("adc","mux")
COLORS={"blocking":"#1971c2","dma":"#d9480f","lpspi":"#2b8a3e"}
LSB=3.3/4096


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("source",type=Path);p.add_argument("analysis",type=Path);a=p.parse_args()
    root=a.source;out=a.analysis;data=json.loads((out/"analysis.json").read_text());summary=data["summary"]
    lifecycle=json.loads((root/"firmware_lifecycle.json").read_text())
    if lifecycle["state"] != "complete_production_restored_verified":raise RuntimeError("Production restoration not verified")
    rows=data["rows"]
    if len(rows)!=252 or data["pending"]:raise RuntimeError("Need complete validated matrix")
    def get(stage,mode,engine,vmid,settle):
        return next(r for r in summary if (r["stage"],r["mode"],r["engine"],r["vmid"],r["settle_us"])==(stage,mode,engine,vmid,settle))
    plt.rcParams.update({"font.size":10,"axes.spines.top":False,"axes.spines.right":False,"axes.grid":True,"grid.alpha":.2})
    fig,axes=plt.subplots(1,2,figsize=(12,4.5),constrained_layout=True)
    for ax,vmid in zip(axes,(False,True)):
        for engine in ENGINES:
            for mode in MODES:
                values=[get("production",mode,engine,vmid,s)["delivered_sweeps_s"]/1000 for s in SETTLES]
                ax.plot(SETTLES,values,color=COLORS[engine],linestyle="-" if mode=="mux" else "--",marker="o" if mode=="mux" else "s",label=f"{engine} / {mode}")
        ax.set_xscale("log");ax.set_xticks(SETTLES,SETTLES);ax.set_xlabel("Configured settling delay (us)")
        ax.set_ylabel("Measured sweeps/s (thousands)");ax.set_title("VMID "+("on" if vmid else "off"));ax.legend(fontsize=8)
    fig.suptitle("Production measurements: full arrays, repeat 1, requested 16 MHz, CS bursts")
    fig.savefig(out/"production_rates.png",dpi=180);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    for ax,vmid in zip(axes,(False,True)):
        for engine in ENGINES:
            gains=[100*(get("production","adc",engine,vmid,s)["mean_period_us"]/get("production","mux",engine,vmid,s)["mean_period_us"]-1) for s in SETTLES]
            ax.plot(SETTLES,gains,color=COLORS[engine],marker="o",label=engine)
        ax.set_xscale("log");ax.set_xticks(SETTLES,SETTLES);ax.set_xlabel("Settling delay (us)")
        ax.set_ylabel("MUX rate gain over new ADC mode (%)");ax.set_title("VMID "+("on" if vmid else "off"));ax.legend()
    fig.savefig(out/"shared_wait_gains.png",dpi=180);plt.close(fig)
    channels=list(csv.DictReader((out/"channel_noise_baselines.csv").open()))
    old=json.loads((ROOT/"docs/testing/results/testboard-adc124/mux_settling_2026-10-13/analysis.json").read_text())
    def channel_summary(mode,vmid,settle,array):
        groups={}
        for r in channels:
            if (r["stage"],r["mode"],r["engine"],r["vmid"],int(r["settle_us"]),int(r["array"])) == ("production",mode,"lpspi",str(vmid),settle,array):
                groups.setdefault(r["route"],[]).append(float(r["sd_counts"]))
        values=[np.median(v) for v in groups.values()]
        return float(np.median(values)),float(max(values))
    def old_noise(vmid,settle,array):
        values=[r["std_counts"] for r in old["channels"] if (r["vmid"],r["settle_us"],r["array"])==(vmid,settle,array)]
        return float(np.median(values)),float(max(values))
    fig,axes=plt.subplots(2,2,figsize=(12,7),constrained_layout=True)
    for row,array in enumerate((1,2)):
        for col,vmid in enumerate((False,True)):
            ax=axes[row,col]
            for label,mode,color in (("Reference sequential v2",None,"#777777"),("ADC v3","adc","#1971c2"),("Shared MUX v3","mux","#d9480f")):
                values=[(old_noise(vmid,s,array) if mode is None else channel_summary(mode,vmid,s,array))[1] for s in SETTLES]
                ax.plot(SETTLES,values,color=color,marker="o",label=label)
            ax.set_xscale("log");ax.set_xticks(SETTLES,SETTLES);ax.set_xlabel("Settling delay (us)")
            ax.set_ylabel("Worst channel quiet SD (counts)");ax.set_title(f"Array {'AB'[array-1]}, VMID {'on' if vmid else 'off'}");ax.legend(fontsize=8)
    fig.suptitle("LPSPI: maximum after per-channel median of 3 repetitions; common all-channel quiet mask")
    fig.savefig(out/"quiet_noise_comparison.png",dpi=180);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,3.5),constrained_layout=True)
    canonical=tuple(sorted(bench.load_manifest(bench.DEFAULT_MANIFEST)["both_full"]));idx=canonical.index(bench.Route(2,2,1))
    for ax,mode in zip(axes,MODES):
        r=next(r for r in rows if (r["stage"],r["mode"],r["engine"],r["vmid"],r["settle_us"],r["repetition"])==("production",mode,"lpspi",False,1,1))
        folder=root/"production"/r["name"];frames=decode(folder/"capture.bin");status=json.loads((folder/"status.json").read_text())["after"]
        starts=np.r_[0,np.cumsum(np.diff(frames["start"].astype(np.int64))%2**32)]/1e6
        x=canonical_samples(frames,status)[:,idx];use=(starts>=1)&(starts<1.1)
        ax.plot((starts[use]-1)*1000,x[use],lw=.6,color="#1971c2" if mode=="adc" else "#d9480f")
        ax.set_title(f"{mode.upper()} mode / B MUX2 input 1");ax.set_xlabel("Time after warm-up (ms)");ax.set_ylabel("Raw ADC code")
    fig.suptitle("1 us, repeat 1, VMID off, LPSPI: every sample in first 100 ms; no clipping")
    fig.savefig(out/"short_delay_channel_variation.png",dpi=180);plt.close(fig)

    def table(headers,records):
        return ["| "+" | ".join(headers)+" |","| "+" | ".join("---" for _ in headers)+" |"] + ["| "+" | ".join(map(str,r))+" |" for r in records]
    total=sum(r["frames"] for r in rows);sampled=sum(r["sampling_sweeps"] for r in rows);drops=sum(r["usb_frames_discarded"] for r in rows)
    text=["# TestBoard ADC124: sequential ADC and shared MUX scans", "", "2026-10-13 | COM4 | protocol 3 | connectors disconnected | charger connected", "",
      "Implemented ADC mode (array A address groups, then B) and MUX mode (switch both participating banks, one shared settling wait, then ADC A/B). Continuous-CS batches are supported by all three engines. Production firmware was restored and acquisition left stopped.", "",
      f"**Evidence:** 106 focused tests passed; 708 digital cases per production/diagnostic build; both ARM builds passed. Hardware matrix: 180 production captures (60 settings x 3 repetitions), 12 single-word controls and 60 diagnostic captures. {total:,} complete frames received; {sampled:,} acquired; {drops:,} explicitly discarded ({100*drops/sampled:.5f}%). Every sent frame was received. Transfer/data/USB-write errors were zero. Raw hashes and independent parser checks passed.", "",
      "## Scope and exact semantics", "",
      "Protocol 1/2 `scanorder mux` was array-sequential address grouping; it is now `scanorder adc`. Protocol 3 `scanorder mux` means shared switching/waiting. Old `channel`/`interleaved` aliases are rejected, and historical captures are decoded using their stored version and physical payload routes. Wire width remains 50 uint16 values per sweep with the same trailer. No individual conversion timestamps are exported.", "",
      "ADC mode sorts (array,address,MUX); MUX mode sorts (address,array,MUX). Full routes read four inputs per ADC at addresses 0-5, IN4 only at address 6. Partial and single-array plans switch/read only needed banks and populated inputs. Cached state skips repeated addresses and EN transitions. Shared waits start after both banks' turn-on allowances. VMID phases coordinate participating banks at address 7, then discard a selector prime plus four ADC inputs per bank.", "",
      "Ownership parking was inherited from ADS7953 and is not required for SPI isolation here: CS deassertion tri-states ADC DOUT, and analog inputs have their own unity buffers. There is no extra parking on ADC-input/CS changes or sweep end. Startup, run preparation, stop and failure still park both banks safely. Unselected banks are disabled during runs. ADC/MUX analog histories differ; speed and baseline comparisons must not assume electrical equivalence.", "",
      "The desktop registry explicitly lists ADC124 as unsupported: no ADC124 GUI controls or persisted desktop scan setting exist to migrate. The ADS7953 GUI/profile retains its own names. CLI/session metadata now records protocol version, scan semantics, SPI frame mode, physical payload labels and settings. Saved old captures remain unchanged.", "",
      "## Datasheet and implementation", "",
      "TI SNAS283D pp.16-17/19 specifies serial frames containing multiples of 16 clocks and continuous conversions while CS remains low. Commands select the next conversion. A fixed-address batch contains a discarded prime plus prepared channel/repeat commands; only the final repeat is stored. CS rises only after the complete batch and is high before another ADC can drive MISO. [Primary TI datasheet](https://www.ti.com/lit/ds/symlink/adc124s101.pdf?ts=1781720494475).", "",
      "Blocking uses one transaction/CS per batch; DMA one persistent aligned 2N-byte launch; LPSPI one sweep transaction and per-word bounded start/receive/TCF checks under continuous CS. Timeout/error cleanup releases CS, restores SPI and parks. EN polarities remain A active-high / B active-low, with 200 ns off and on guards. GPIO set/clear masks are cached and unchanged address bits skipped. Analog settling was not reduced to offset CPU overhead.", "",
      "## Production rate measurements", "", "All new production repetitions: 1 s warm-up + 2 s measurement, requested 16 MHz, manual prepared acquisition, repeat 1, both arrays, CS bursts. Rates below are medians of three repetitions, from device sweep-start intervals. Sweeps/s equals useful samples/s/channel. Sampling rate inferred from counters/endpoints is separately labeled an estimate. Actual pin SCLK was not measured.", "", "![Production rates](production_rates.png)", ""]
    records=[]
    for s in SETTLES:
      for v in (False,True):
        records.append([s,"on" if v else "off",*[f"{get('production',m,e,v,s)['delivered_sweeps_s']:.1f}" for e in ENGINES for m in MODES]])
    text+=table(["Settle us","VMID","Blocking ADC","Blocking MUX","DMA ADC","DMA MUX","LPSPI ADC","LPSPI MUX"],records)
    text += ["", "![Shared wait gains](shared_wait_gains.png)", "", "Compared with the supplied sequential-mode reference, the optimized ADC mode includes burst/driver changes and removes unnecessary parking. MUX mode adds shared waits. The reference has three 5 s production repetitions; the new matrix has three 2 s repetitions, so session conditions/window lengths are a comparison limitation. Reference data use old `mux` names and remain unchanged.", ""]
    rec=[]
    for mode in MODES:
      for v in (False,True):
        for s in (1,5,50):
          r=next(c for c in data['comparisons'] if (c['mode'],c['engine'],c['vmid'],c['settle_us'])==(mode,'lpspi',v,s))
          rec.append([mode,s,'on' if v else 'off',f"{r['old_v2_period_us']:.3f}",f"{r['new_period_us']:.3f}",f"{r['speed_gain_vs_reference_percent']:.1f}%",f"{r['speed_gain_vs_new_adc_percent']:.1f}%"])
    text += table(["Mode","Settle us","VMID","Reference period us","New period us","Rate gain vs reference","Rate gain vs new ADC"],rec)
    text += ["", "## Burst-only control", "", "The following compares the same new plan/GPIO/parking behavior with per-word CS versus bursts at 5 us. Controls used one 2 s measurement; optimized rows are medians of three. These are measured gains; they are not pure CS-delay arithmetic. Blocking bursts were fastest in this matrix, despite LPSPI's sweep transaction.", ""]
    rec=[]
    for e in ENGINES:
      for m in MODES:
        for v in (False,True):
          single=get('single_controls',m,e,v,5);burst=get('production',m,e,v,5)
          rec.append([e,m,'on' if v else 'off',f"{single['mean_period_us']:.3f}",f"{burst['mean_period_us']:.3f}",f"{100*(single['mean_period_us']/burst['mean_period_us']-1):.1f}%"])
    text += table(["Engine","Mode","VMID","Single period us","Burst period us","Measured rate gain"],rec)
    text += ["", "## Measured diagnostic breakdown", "", "Diagnostic: one repetition per setting, 1 s warm-up + 2 s measurement, profiling on. Values below are whole-run mean cycle-counter phases at 5 us. Probes/storage and interrupts are included. Validation is nested inside transfer and acquisition includes session/prepare/transfer; do not sum overlapping phases. New prepare includes VMID settling, whereas the old reference included it in transfer. Only production determines rate gains.", ""]
    rec=[]
    for e in ENGINES:
      for m in MODES:
        for v in (False,True):
          r=get('diagnostic',m,e,v,5)
          rec.append([e,m,'on' if v else 'off',*[f"{r['profile_'+k+'_us']:.3f}" for k in ('prepare','session','transfer','validate','encode','usb_write','acquisition','bookkeeping')]])
    text += table(["Engine","Mode","VMID","Prepare us","Session us","Transfer us","Validate us (nested)","Encode us","Enqueue us","Acquisition us","Bookkeeping us"],rec)
    text += ["", "## Calculations, separate from measurements", "",
      "Both modes use 64 words / 14 sensor batches with VMID off; 134 words / 28 sensor+VMID batches with VMID on. At configured 0.1 us setup + 0.1 us high per frame, burst CS allowance is 2.8 us off / 5.6 us on. Removing per-word allowances saves `(64-14)*0.2 = 10.0 us` off and `(134-28)*0.2 = 21.2 us` on. The earlier 50 us estimate was for a different channel-by-channel plan and does not apply. These values are firmware arithmetic, not measured CS edge durations.", "",
      "Steady full-array ADC mode has 14/28 settling waits and MUX mode 7/14 (off/on). Both have 14/28 actual bank address changes. Guard budget is 5.6/11.2 us at 0.4 us/change. Shared-wait savings are `7*D` off or `14*D` on; sparse/repeated addresses may skip waits. Nominal word-clock budget is 64/134 us at the requested 16 MHz, not a measured SCLK or an additive track-plus-conversion-plus-shift budget (these overlap). Prepare-minus-requested-waits is a residual model estimate containing guards, GPIO, control and probes, not isolated GPIO propagation.", "",
      "## Quiet-period acquisition quality", "",data['quiet_method']+". Rejection applies one mask to every channel in a capture and excludes one neighboring 50 ms bin each side of a flagged burst. Quantization-aware floor is 0.6 counts. At least 1 s and 100 frames must remain. All 192 production/control captures met this requirement. No bins crossed the method's group-envelope thresholds, so all post-warm-up windows were retained. Charger switching/rails were not independently recorded; this does not establish absence of sub-LSB interference or isolated channel events.", "",
      "![Quiet noise comparison](quiet_noise_comparison.png)", "",
      "SD is sample SD (ddof=1) around each channel's own mean. The plot takes median across repetitions per physical route, then the maximum across the array; it matches the reference statistic. Reference records are 15 s and new records 2 s. Peak-to-peak/sample counts are saved but not treated as duration-independent noise measures. Codes converted using 3.3/4096 V per count. Biases A: 470k/1M/470k/1M; B: 1M/470k/1M/470k.", "",
      "**Quality limitation:** ADC/LPSPI at 1 us, VMID off, showed persistent B MUX2 input 1 variation near 6 counts SD across all repetitions. It was retained because it is not a rejected common burst window. A speed improvement alone cannot qualify this setting. Shared MUX mode, VMID and longer delays change connection/read history; noise and baseline must be assessed with actual sensors before acceptance. Electrical cause is unproven.", "",
      "![Short-delay raw channel variation](short_delay_channel_variation.png)", ""]
    rec=[]
    for s in SETTLES:
      for v in (False,True):
        for arr in (1,2):
          rec.append([s,'on' if v else 'off','AB'[arr-1],*[f"{values[1]:.3f}" for values in (old_noise(v,s,arr),channel_summary('adc',v,s,arr),channel_summary('mux',v,s,arr))]])
    text += table(["Settle us","VMID","Array","Reference worst SD counts","ADC worst SD counts","MUX worst SD counts"],rec)
    text += ["", "## Remaining optimization and validation", "",
      "1. Benchmark with attached sensors and known input stimulation. Short ADC-mode delays show a reproducible channel-specific quality issue; no electrical diagnosis or ghosting acceptance is claimed.",
      "2. LPSPI bursts still start/poll/drain each word and require fresh completion. A bounded FIFO-queued implementation could reduce this overhead, but needs dedicated FIFO/error/clock-end tests and on-board comparison. DMA has reduced launch count but remains launch/completion limited. Blocking burst is the measured best engine in this matrix.",
      "3. Cached GPIO masks and changed-bit writes are implemented. Grouped port writes are a possible further experiment when pins share a hardware port; preserve EN guards and analog settling. Measure an isolated GPIO/delay-call control before attributing residual prepare cost to it.",
      "4. Encoding/enqueue remain around sub-microsecond scopes. USB discards were sparse and fully accounted, so no transport rewrite was prioritized. Verify logic-analyzer CS/SCLK edges and ADC input identity; these were not individually measured here.", "",
      "## Evidence and reproduction", "",
      f"Raw sessions: `{root.relative_to(ROOT)}`. Each capture keeps full raw bytes, command/status logs, exact payload routes, hashes and profile evidence. `firmware_lifecycle.json` records pre-test production images, new image/source hashes, diagnostic loading, and final production revision/configuration. Final flags: profile unavailable, running false, both arrays, ADC mode, blocking, 16 MHz, repeat 1, 5 us, VMID off, CS bursts. Original protocol-2 `mux` preference was explicitly migrated to protocol-3 ADC semantics.", "",
      "References: [sequential timing/engine PDF](../../../../reports/exports/TestBoard_ADC124_MUX_Timing_Engines_2026-10-13.pdf) and [sequential settling/VMID PDF](../../../../reports/exports/TestBoard_ADC124_MUX_Settling_VMID_2026-10-13.pdf). Their raw reports/datasets remain intact. The new script uses the associated timing JSON and common-window method.", "",
      "Reproduce: `scripts/benchmark_adc124_scan_modes.py` (hardware with production/diagnostic restoration), `scripts/analyze_adc124_scan_modes.py` (raw hash/frame/timing/noise evidence), `scripts/report_adc124_scan_modes.py` (this report/plots). `summary.csv`, `capture_metrics.csv`, `comparisons.csv`, `channel_noise_baselines.csv`, per-capture quiet-bin/threshold files and `analysis.json` supply all numeric values. No commit or push was performed."]
    (out/"README.md").write_text("\n".join(text)+"\n",encoding="utf-8")
    print(out/"README.md")


if __name__ == "__main__":main()
