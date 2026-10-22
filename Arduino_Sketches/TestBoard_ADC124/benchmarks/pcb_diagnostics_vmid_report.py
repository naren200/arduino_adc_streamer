"""Reader explanations for the three added long-VMID experiments."""
from pathlib import Path
import numpy as np

def append_vmid_tests(e,folder,guide,results,page,p,label,table,conclude,n):
    folder=Path(folder);figs=folder/'figures';lsb=3.3/4096
    holds=e['holds'];intervals=[r['last_received_pre_end_to_post_start_s'] for r in holds]
    gap=f'{min(intervals):.3f}-{max(intervals):.3f} s'
    repeat_settings=[('SPI / arrays','LPSPI; requested 16 MHz; both arrays. External MUX address changes coordinated between arrays.'),
        ('Before recording','3.5 s continuous full scan: exclude its first 1 s, analyze the last 2.5 s; also retain its last received cycle.'),
        ('After recording','One cycle only after the timed hold; retain the first response, with no discarded sensor readings. Three repetitions per condition.')]
    guide(21,'One full cycle after a 20-second timed VMID hold',
        'Test how the first complete sensor scan after prolonged VMID parking differs from the immediately preceding continuous-scan baseline.',[
        'Scan all 50 sensors continuously with software VMID off. Read external MUX addresses 0-5 with ADC IN1-IN4, then address 6 with ADC IN4 only. Keep the final received full cycle as the before snapshot.',
        'Stop and select external MUX address 7 on every MUX. Prepare the next ADC selector before starting the timed hold. Keep the analog VMID connection for 20 seconds without ADC conversions.',
        'Switch to address 0 and acquire exactly one full address-0-to-6 cycle, with no explicit VMID visit between addresses. Stop and park again.'],
        [('Sensor wait / order','5 µs after each external address selection. ADC A IN1-IN4 first, then ADC B IN1-IN4; address 6 reads only IN4.'),
         ('Hold timing','Timed no-conversion interval: 20.000 s. Total time from last received pre-cycle end to post-cycle start: '+gap+'. Host setup/selector preparation precede the timed interval.')]+repeat_settings,
        'For each physical sensor: the before-window baseline, its last received pre-cycle code, and its first post-hold-cycle code. The after value is a transient first reading, not a new steady baseline.',
        'Each panel is one array and MUX number / ADC input. X = external MUX address. Blue = before-window baseline; orange dashed = last pre-cycle; red = first post-cycle. '
        'Curves are medians of three repetitions; red shading spans the minimum to maximum post code across those three repetitions, not noise SD.',recordings=False)
    a=[r['median_route_change_counts'] for r in e['full_mux'] if r['array']==1]
    b=[r['median_route_change_counts'] for r in e['full_mux'] if r['array']==2]
    results(21,'First full cycle after the long hold',figs/'hold_full_cycle.png',[
        f'Across external MUX addresses 0-5, MUX median changes are {min(a):+.1f} to {max(a):+.1f} counts on Array A, versus {min(b):+.1f} to {max(b):+.1f} on Array B. '
        'The result is not a uniform lowering on both arrays. The per-address graph shows which individual inputs depart from these summaries.',
        'Parking on VMID also disconnects every sensor from its buffer/bias path for a long time. The effect therefore combines sensor off-time, retention/leakage and reconnection history with any reference-related effect. '
        'It does not isolate the 150 pF reference capacitor or establish charge accumulation between sensor addresses.'])
    page('21. Full-cycle changes - numerical comparison')
    p('Only addresses 0-5 enter each MUX summary so all MUXes compare six sensors. MUX 4’s seventh sensor at address 6 is retained in the graph and route CSV. '
      'The change column is the median of matched sensor-route changes, which need not equal the difference of the two center columns.')
    table(['Array / MUX','Before center counts / V','First-post center counts / V','Median route change counts / mV','Route-change range counts'],[
        [f'{"AB"[r["array"]-1]} / {r["mux"]}',n(r['before_center_counts'])+' / '+n(r['before_center_counts']*lsb,4),
         n(r['after_center_counts'])+' / '+n(r['after_center_counts']*lsb,4),n(r['median_route_change_counts'])+' / '+n(r['median_route_change_counts']*lsb*1000,2),
         n(r['min_route_change_counts'])+' to '+n(r['max_route_change_counts'])] for r in e['full_mux']])
    p('A full post-hold cycle reads different sensors in succession. Its points are not the recharge trajectory of one sensor. '
      'Repeated-measurement variability is shown by the red band; it is not valid to calculate a noise SD from the 50 different sensor codes.')

    guide(22,'Ten conversions of one fixed ADC input after the long hold',
        'Measure the first ten responses on one sensor path after VMID parking, and check the influence of which ADC is read first.',[
        'Before each hold, record the same short continuous full scan as Test 21. Select external MUX address 2 as the target for the post-hold measurement.',
        'Choose one MUX number m. Set the ADC selector to INm before the timed hold, with the external MUXes already parked at address 7. Then wait 20 seconds with no conversion clocks.',
        'Switch both external MUX groups to address 2, wait 5 µs, and read INm ten consecutive times on ADC A, followed by ten on ADC B. Do not change external address or ADC input within either burst.',
        'Repeat for MUX numbers 1, 2, 3 and 4. Repeat all four with ADC B’s ten reads first, then ADC A’s. Thus one address-2 sensor per MUX is tested on both arrays.'],
        [('Timing / selection','One post-hold external MUX address: 2. Sensor wait 5 µs. Ten retained readings per ADC, 20 codes total per post recording.'),
         ('Read-order control','Before-scan ADC order matches its post-burst order. Changing order also changes prior full-scan read history slightly. The ADC read second has extra elapsed time and preceding conversions.')]+repeat_settings,
        'The code at each conversion position 1-10, the before-window baseline for that same sensor, and the first-to-tenth change. These ten readings describe recovery, not stationary noise.',
        'X = consecutive conversion position of the same ADC input. Blue circles = ADC A first; orange squares = ADC B first. Matching dashed lines are before-window baselines. '
        'Solid curves are median codes across three repeats; shading is the min-to-max repetition range, not SD or error bars.',recordings=False)
    ga=[r['tenth_minus_first_counts'] for r in e['ten_endpoints'] if r['array']==1]
    gb=[r['tenth_minus_first_counts'] for r in e['ten_endpoints'] if r['array']==2]
    results(22,'Ten-conversion responses',figs/'hold_ten_conversions.png',[
        f'The median tenth-minus-first change spans {min(ga):+.1f} to {max(ga):+.1f} counts on Array A and {min(gb):+.1f} to {max(gb):+.1f} on Array B. '
        'The before-baseline dashed lines show whether ten readings return the sensor path to its prior scanning level.',
        'Order differences combine additional settling time before the second ADC with shared conversion activity. They do not identify the reference as the only cause. '
        'The earlier continuously connected VMID control (Test 14) demonstrates a smaller conversion-history effect even without a sensor transition.'])
    for array in (1,2):
        page(f'22. Array {"AB"[array-1]} - before baseline and burst endpoints')
        p('All targets are external MUX address 2. “First ADC” identifies the order of the two ten-reading bursts, not MUX number. '
          'The before baseline is from that same repetition’s continuous scan; reported values aggregate three repetitions.')
        table(['MUX / ADC input','First ADC','Before baseline counts / V','First code counts / V','Tenth code counts / V','Tenth - first counts / mV'],[
            [f'MUX {r["mux"]} / IN{r["mux"]}','AB'[r['first_bank']],n(r['before_baseline_counts'])+' / '+n(r['before_baseline_counts']*lsb,4),
             n(r['first_counts'])+' / '+n(r['first_counts']*lsb,4),n(r['tenth_counts'])+' / '+n(r['tenth_counts']*lsb,4),
             n(r['tenth_minus_first_counts'])+' / '+n(r['tenth_minus_first_counts']*lsb*1000,2)] for r in e['ten_endpoints'] if r['array']==array])
        p('A difference across ten conversion positions includes the transient response and conversion activity. Calling its SD a noise level would conflate recovery with noise. '
          'Every individual conversion and every repetition is retained in ten_conversion_repetitions.csv.')

    guide(23,'Regular full cycles with 1, 5 and 10 ms VMID settling',
        'Test whether long VMID intervals between external MUX addresses change baseline, recorded temporal variation and full-cycle sampling rate.',[
        'Run a continuous full two-array scan. At each external MUX address 0-5, wait 5 µs and read all four ADC inputs on ADC A then ADC B; at address 6 read only IN4.',
        'After every address, select external MUX address 7 on both arrays together. Wait the configured VMID delay, then read/discard IN1-IN4 on each ADC.',
        'Repeat with VMID settling set separately to 1 ms, 5 ms and 10 ms. Keep sensor settling fixed at 5 µs and retain all 50 sensor readings per cycle.'],
        [('SPI / ordering','LPSPI; requested 16 MHz; both arrays; coordinated external-address switching; ADC A first; IN1-IN4; one sensor conversion per route.'),
         ('Intervals per cycle','Seven sensor-address waits of 5 µs and seven shared VMID waits. VMID settles once per address for both arrays, not once per ADC input.'),
         ('Recordings','Three 12 s recordings per VMID delay. Exclude the first 2 s; analyze the following 10 s. No fixed phase slots or cycle throttle.')],
        'Per-route median code, raw temporal SD and peak-to-peak in the analyzed window; spatial baseline spread across addresses; measured start-to-start cycle period. '
        'Drift is the last-two-second median minus the first-two-second median within that 10 s window. The initial exclusion does not establish equilibrium, and SD may include residual drift or isolated events.',
        'The address graphs compare the same physical sensor baselines for each VMID delay. The center/noise graph uses external addresses 0-5 for equal six-sensor MUX summaries. '
        'The earlier 5 µs VMID curve is context from a separate recording session and a shorter analysis window, not a simultaneous matched-duration control.',recordings=False)
    changes={a:[next(r['baseline_center_counts'] for r in e['scan_mux'] if r['array']==a and r['mux']==m and r['vmid_wait_us']==10000)-
        next(r['baseline_center_counts'] for r in e['scan_mux'] if r['array']==a and r['mux']==m and r['vmid_wait_us']==1000) for m in range(1,5)] for a in (1,2)}
    results(23,'Per-sensor baselines with long VMID settling',figs/'long_vmid_all_baselines.png',[
        f'Changing VMID wait from 1 to 10 ms changes MUX baseline centers by {min(changes[1]):+.1f} to {max(changes[1]):+.1f} counts on Array A, '
        f'and {min(changes[2]):+.1f} to {max(changes[2]):+.1f} counts on Array B. Longer dwell changes both reference-connection history and sensor disconnected time; these effects are not separated.',
        'MUX 4’s address-6 sensor remains included. Address 6 on MUXes 1-3 is VMID, so those reference responses are not substituted for sensor data.'],
        'Colour identifies VMID wait 1, 5 or 10 ms. Grey dashed = earlier full scan with VMID wait 5 µs. X = external MUX address; Y = median sensor code. Lines connect different physical sensors.')
    results(23,'MUX baseline centers and recorded temporal SD',figs/'long_vmid_centers_noise.png',[
        'These are medians over a defined 10-second window, not a proof that every sensor reached a final equilibrium. '
        'Raw SD retains all excursions and any drift; it should not be called an isolated white-noise floor.',
        'Longer VMID delay lowers the per-sensor sampling rate and observed bandwidth. A lower SD at a slower rate would not by itself demonstrate a quieter analog circuit.'],
        'Each colour = MUX number. X = VMID settling delay in ms. Left = median baseline across six external addresses 0-5. Right = median of the six individual routes’ temporal SDs, not SD across their baseline values.')
    maxdrift=max(r['max_absolute_drift_counts'] for r in e['scan_mux'])
    results(23,'Time traces: check for drift before calling variation noise',figs/'long_vmid_time_examples.png',[
        f'Across addresses 0-5 and all repetitions, the largest absolute first-to-last two-second median change is {maxdrift:g} counts. '
        'That check separates a slow level change from a single SD summary; it is not a proof that every remaining fluctuation is white noise.',
        'The examples show external MUX address 2 in recording 1 only. All routes and all three recordings contribute to the summary tables.'],
        'Each dot is a sampled ADC code; colour = VMID wait 1, 5 or 10 ms. X = time within the analyzed 10-second window after the 2-second exclusion. '
        'The three curves come from separate recordings, not simultaneous measurements. Lines connect samples for visibility.')
    page('23. Sampling rate and configured wait contribution')
    timing=[]
    for wait in (1000,5000,10000):
        rr=[r for r in e['captures'] if r['name']==f'long_vmid_{wait}']
        period=float(np.median([r['period_median_us'] for r in rr]));hz=1e6/period
        routes=[r for r in e['scan_routes'] if r['vmid_wait_us']==wait]
        timing.append((wait,period,hz))
        assert routes
    table(['VMID wait','Shared VMID wait / cycle µs','Sensor wait / cycle µs','Measured cycle µs','Cycles/s = samples/s per sensor','Retained sensor codes/s'],[
        [f'{wait//1000} ms',7*wait,35,n(period,2),n(hz,3),n(50*hz,1)] for wait,period,hz in timing])
    slope,intercept=np.polyfit([r[0] for r in timing],[r[1] for r in timing],1)
    p(f'Fit to these three measured periods: <b>cycle time approximately {slope:.5f} × VMID_wait_us + {intercept:.2f} µs</b>. '
      'The expected wait contribution is seven times the VMID setting because there are seven address groups per full cycle. '
      'The fitted remainder includes the 35 µs configured sensor waits, switching guards, SPI conversions, driver work and diagnostic transport overhead; it is not a direct measurement of each component.')
    p('Each cycle retains 50 sensor codes and performs another 56 discarded VMID conversions, for 106 conversions total. '
      'Cycle rate is therefore the sampling rate of each sensor, while 50 × cycle rate is the combined retained sensor-code rate. '
      'These are diagnostic-profile rates, not production-driver optimization measurements.')
    page('23. Baseline and variation summaries - addresses 0-5')
    table(['Array / MUX','VMID wait ms','Baseline center counts / V','Spatial spread counts / mV','Median route SD counts / mV','Worst range counts / mV','Largest absolute drift counts','Samples / route / record'],[
        [f'{"AB"[r["array"]-1]} / {r["mux"]}',r['vmid_wait_us']//1000,n(r['baseline_center_counts'])+' / '+n(r['baseline_center_counts']*lsb,4),
         n(r['address_spread_counts'])+' / '+n(r['address_spread_counts']*lsb*1000,2),n(r['median_route_sd_counts'],4)+' / '+n(r['median_route_sd_counts']*lsb*1000,4),
         n(r['max_route_ptp_counts'],0)+' / '+n(r['max_route_ptp_counts']*lsb*1000,3),n(r['max_absolute_drift_counts']),n(r['samples_per_record_median'],0)] for r in e['scan_mux']],compact=True)
    p('Temporal SD/range are computed for each sensor route within each recording, then aggregated over repetitions. '
      '“Median route SD” summarizes six routes; “spatial spread” compares their baseline values. Worst range and largest absolute drift select the largest value across all six routes and all three recordings. '
      'All 50 routes, including MUX 4 address 6, have counts and volts in long_scan_routes.csv and long_scan_repetitions.csv.')
    p('On Array B, the 1 Mohm paths (MUXes 1 and 3) fall by about 280 counts between 1 and 10 ms; the 470 kohm paths (MUXes 2 and 4) fall by about 144 counts. '
      'This pattern is consistent with an influence of bias strength and disconnected time, but does not prove that resistor value alone causes the differences.',True)

    page('VMID extension - acquisition evidence and limitations')
    caps=e['captures'];life=e['lifecycle']
    table(['Check / evidence','Result'],[
        ['Completed recordings','63: 27 continuous before scans, 27 single post-hold captures and 9 long-VMID regular scans. Smoke test excluded.'],
        ['Hold check',f'27 timed holds; {min(r["timed_hold_s"] for r in holds):.6f} to {max(r["timed_hold_s"] for r in holds):.6f} s. Total recorded pre-to-post gaps: '+gap+'.'],
        ['Acquired / received / discarded cycles',f'{sum(r["cycles"] for r in caps):,} / {sum(r["frames"] for r in caps):,} / {sum(r["dropped"] for r in caps):,}'],
        ['Transfer / ADC data / USB-write errors / timing overruns',' / '.join(str(sum(r[k] for r in caps)) for k in ('transfer_errors','data_errors','usb_write_errors','overruns'))],
        ['Single-frame verification','Every post-hold recording has exactly one complete transmitted cycle, sequence zero; no dropped post-hold cycles.'],
        ['Firmware isolation','pcbdiag-v2 adds single-capture/long-hold control and ten repeated responses. Hold occurs outside per-cycle CPU-tick timing; selectors are prepared before the hold. Production protocol unchanged.'],
        ['Validation','84 focused regression tests passed, including the actual C++ diagnostic model with a simulated 20 s hold and ten-response bursts. Production and diagnostic builds passed.'],
        ['Restoration',life['state']+'; '+life['final_status']['firmware_revision']+'; acquisition stopped'],
        ['Data folder','docs/testing/datasets/testboard-adc124/vmid_long_hold_2026-10-15'],
        ['Results folder','docs/testing/results/testboard-adc124/vmid_long_hold_2026-10-15']],[190,568])
    p('The analyzed before-window defines a reproducible scan history, but the 20-second reference interval is not an isolated charge-injection test: '
      'sensors are disconnected throughout parking. Host setup adds parked time before the exact timed hold. '
      'No sensor was intentionally pressed; mechanical disturbances were not instrumented. The arrays’ different MUX types remain confounded with their complete analog paths and PCB locations.')
