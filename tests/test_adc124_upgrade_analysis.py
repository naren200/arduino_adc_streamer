"""Scientific summary checks for clock/delay separation and repetition aggregation."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Arduino_Sketches/TestBoard_ADC124/benchmarks'))
from analyze_adc124_upgrade import aggregate, LSB


def evidence(clock,vmid_delay):
    rows=[];channels=[]
    for rep,shift in enumerate((0,100,1),1):
        base=dict(dataset='current',stage='production',mode='mux',engine='lpspi',vmid=True,settle_us=5,
            vmid_settle_effective_us=vmid_delay,requested_spi_clock_hz=clock,repetition=rep)
        rows.append(dict(base,firmware_revision='test',quiet_qualified=True,delivered_sweeps_s=clock/1000+shift))
        for a in (1,2):
            for i in (0,1):
                count=2000+10*a+4*i+shift;sd=.2+.1*i;pp=2+i
                channels.append(dict(base,route=f'{a}:1:{i}',array=a,mux=1,sensor='PZT1',sensor_channel=i+1,bias_ohms=470000,
                    baseline_counts=count,baseline_volts=count*LSB,sd_counts=sd,sd_volts=sd*LSB,
                    ptp_counts=pp,ptp_volts=pp*LSB))
    return rows,channels


def test_frequency_and_explicit_zero_are_distinct_statistical_populations():
    rows=[];channels=[]
    for clock,delay in ((8000000,5),(16000000,5),(16000000,0)):
        r,c=evidence(clock,delay);rows.extend(r);channels.extend(c)
    summary,per_channel=aggregate(rows,channels)
    assert len(summary)==3 and len(per_channel)==12
    fast=next(r for r in summary if r['requested_spi_clock_hz']==16000000 and r['vmid_settle_effective_us']==5)
    slow=next(r for r in summary if r['requested_spi_clock_hz']==8000000)
    assert fast['delivered_sweeps_s']==16001
    assert slow['delivered_sweeps_s']==8001
    assert all(r['qualified_repetitions']==3 for r in summary)


def test_route_medians_resist_one_drifted_repeat_and_spread_is_not_temporal_noise():
    rows,channels=evidence(16000000,5);summary,per_channel=aggregate(rows,channels);r=summary[0]
    first=next(c for c in per_channel if c['route']=='1:1:0')
    assert first['baseline_counts']==2011
    assert first['baseline_counts_min_rep']==2010 and first['baseline_counts_max_rep']==2110
    assert first['baseline_volts']==pytest.approx(2011*LSB)
    assert r['A1_baseline_spread_counts']==4
    assert r['A1_baseline_population_sd_counts']==2
    assert r['A1_sd_max_counts']==pytest.approx(.3)
    assert r['A1_sd_max_volts']==pytest.approx(.3*LSB)
    assert r['all_baseline_spread_counts']==14


def test_unqualified_record_is_counted_without_fabricating_channel_noise():
    rows,channels=evidence(16000000,5)
    rows[-1]['quiet_qualified']=False
    channels=[c for c in channels if c['repetition']!=3]
    summary,per_channel=aggregate(rows,channels)
    assert summary[0]['repetitions']==3 and summary[0]['qualified_repetitions']==2
    assert all(c['qualified_repetitions']==2 for c in per_channel)
