import sys
from pathlib import Path
import numpy as np
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Arduino_Sketches/TestBoard_ADC124/benchmarks'))
from analyze_adc124_charge import measure_charge, sustained_crossing


def test_absolute_thresholds_and_interpolation_with_nonzero_start():
    t=np.arange(0,5.001,.01);z=np.minimum(100+200*t,1000)
    r=measure_charge(t,z,baseline=1000)
    assert r['t_one_third_s']==pytest.approx((1000/3-100)/200)
    assert r['t_two_thirds_s']==pytest.approx((2000/3-100)/200)
    assert r['one_third_to_two_thirds_s']==pytest.approx(1000/3/200)


def test_exponential_charge_interval_matches_tau_log_two():
    t=np.arange(0,.1,.0001);tau=.003
    z=2000*(1-np.exp(-t/tau))
    r=measure_charge(t,z,baseline=2000)
    assert r['one_third_to_two_thirds_s']==pytest.approx(tau*np.log(2),abs=1e-6)
    assert r['interval_lower_s']<=tau*np.log(2)<=r['interval_upper_s']
    assert r['interval_upper_s']-r['interval_lower_s']<=.0002+1e-12


def test_noise_band_arrival_is_distinct_from_one_percent_arrival():
    t=np.arange(0,12,.01);z=1000-200*np.exp(-t/.1)
    r=measure_charge(t,z,baseline=1000)
    assert r['first_two_seconds_within_baseline_s']==pytest.approx(.30)
    assert r['first_two_seconds_within_noise_band_s']==pytest.approx(.47)


def test_initial_charge_is_censored_not_extrapolated_to_zero():
    t=np.arange(0,5.001,.01);z=np.minimum(500+200*t,1000)
    r=measure_charge(t,z,baseline=1000)
    assert r['one_third_status']=='left_censored'
    assert r['t_two_thirds_s']==pytest.approx((2000/3-500)/200)
    assert r['one_third_to_two_thirds_s'] is None


def test_short_impulse_does_not_define_charging_crossing():
    t=np.arange(0,1,.001);z=np.zeros(len(t));z[100]=1000;z[500:]=1000
    crossing,state=sustained_crossing(t,z,500,.002)
    assert state=='observed' and crossing==pytest.approx(.4995)


def test_unreached_threshold_and_timestamp_gap_are_not_invented():
    t=np.arange(0,1,.001)
    assert sustained_crossing(t,np.ones(len(t))*10,100)==(None,'not_reached')
    t[500:]+=1;z=np.r_[np.zeros(500),np.ones(500)*1000]
    assert sustained_crossing(t,z,500)==(None,'not_reached')


def test_later_transport_gap_does_not_erase_confirmed_early_crossing():
    t=np.arange(0,1,.001);t[800:]+=1
    z=np.r_[np.zeros(100),np.ones(900)*1000]
    assert sustained_crossing(t,z,500)[0]==pytest.approx(.0995)
