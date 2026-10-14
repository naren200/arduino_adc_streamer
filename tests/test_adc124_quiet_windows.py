import sys
from pathlib import Path
import numpy as np
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Arduino_Sketches/TestBoard_ADC124/benchmarks'))
from adc124_quiet_windows import between_bursts


def quantized(n=20000):
    return np.tile((2000+np.arange(n)%2)[:,None],(1,4)).astype(float)


def test_quantization_and_coherent_unimodal_gaussian_noise_are_retained():
    t=np.arange(20000)/2000;coarse=np.ones(len(t),bool)
    result=between_bursts(t,quantized(),coarse)
    assert result['mask'].all() and result['fine_excursion_frames']==0
    for sigma in (.6,1.5):
        shared=np.rint(np.random.default_rng(4).normal(2000,sigma,len(t)))
        x=np.tile(shared[:,None],(1,4))
        result=between_bursts(t,x,coarse)
        assert result['mask'].all(), 'Coherent Gaussian tails must not be mistaken for a secondary burst state'


def test_coherent_secondary_state_pulses_reject_whole_sweeps_and_neighbors():
    x=quantized();t=np.arange(len(x))/2000;coarse=np.ones(len(t),bool)
    pulse=(np.arange(len(t))%30>=12)&(np.arange(len(t))%30<15)
    x[pulse,0]+=5;x[pulse,1]-=5
    result=between_bursts(t,x,coarse)
    expected=pulse.copy();expected[1:]|=pulse[:-1];expected[:-1]|=pulse[1:]
    assert np.array_equal(result['mask'],~expected)
    assert x[result['mask']].std(axis=0,ddof=1).max()==pytest.approx(.5,abs=.001)
    assert x.std(axis=0,ddof=1).max()>1.5
    assert any(s.get('detect_lower') for s in result['states'])
    assert any(s.get('detect_upper') for s in result['states'])


def test_isolated_secondary_state_variation_is_not_clipped():
    x=quantized();t=np.arange(len(x))/2000;coarse=np.ones(len(t),bool)
    x[np.arange(len(t))%30<3,0]+=5
    result=between_bursts(t,x,coarse)
    assert result['mask'].all() and x[result['mask'],0].std(ddof=1)>1.5


def test_coarse_burst_rejection_and_warmup_exclusion_remain_in_force():
    x=quantized();t=np.arange(len(x))/2000;coarse=(t>=1)&(t<9)
    coarse[5000:7000]=False
    result=between_bursts(t,x,coarse)
    assert np.array_equal(result['mask'],coarse)
    assert result['quiet_seconds']==pytest.approx(7,abs=.01)
