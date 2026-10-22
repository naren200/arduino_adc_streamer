"""Checks that quiet selection rejects time bursts but preserves ADC quantization."""
import importlib
from pathlib import Path
import numpy as np
import pytest

@pytest.fixture
def analysis(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'Arduino_Sketches/TestBoard_ADC124/benchmarks'))
    return importlib.import_module('report_adc124_settling')

def test_adjacent_code_toggling_is_not_a_burst(analysis):
    rng=np.random.default_rng(62)
    t=np.arange(0,16,.005)
    x=2048+rng.integers(0,2,(len(t),8))
    mask,_,_,thresholds,keep,_=analysis.quiet_mask(t,x,[[0,1],[2,3],[4,5],[6,7]])
    assert keep.all()
    assert np.all(thresholds>=.6)
    assert mask.sum()==np.count_nonzero(t>=1)

def test_common_burst_mask_and_transition_guard(analysis):
    t=np.arange(0,16,.002)
    x=np.full((len(t),8),2048.,dtype=float)
    burst=(t>=5)&(t<5.2)
    x[burst,2:4]+=10*(np.arange(burst.sum())%2)[:,None]
    mask,_,_,_,keep,above=analysis.quiet_mask(t,x,[[0,1],[2,3],[4,5],[6,7]])
    assert above.any()
    assert not mask[(t>=4.95)&(t<5.25)].any()
    assert mask[(t>=6)&(t<7)].all()
    assert keep.sum()<len(keep)

def test_isolated_channel_events_are_not_clipped(analysis):
    t=np.arange(0,16,.002)
    x=np.full((len(t),12),2048.)
    x[(t>=5)&(t<5.2),0]+=20*(np.arange(((t>=5)&(t<5.2)).sum())%2)
    mask,*_=analysis.quiet_mask(t,x,[list(range(6)),list(range(6,12))])
    assert mask[(t>=5)&(t<5.2)].all()
    assert np.ptp(x[mask,0])==20


def test_insufficient_common_quiet_coverage_remains_explicit(analysis):
    t=np.arange(1,3,.002)
    x=np.full((len(t),8),2048.)
    bin_index=np.floor((t-1)/.05+1e-8).astype(int)
    x[bin_index%2==1,0:2]+=20*(np.arange((bin_index%2==1).sum())%2)[:,None]
    groups=[[0,1],[2,3],[4,5],[6,7]]
    with pytest.raises(ValueError,match='Less than 1 second'):
        analysis.quiet_mask(t,x,groups,start=1,end=3)
    mask,_,_,_,keep,above=analysis.quiet_mask(t,x,groups,start=1,end=3,require_coverage=False)
    assert above.any() and keep.sum()*.05<1 and not mask.any()
    assert np.ptp(x[:,0])==20  # diagnostic return does not clip or rewrite samples

def test_wire_reader_handles_wrap_and_rejects_bad_frame(analysis,tmp_path):
    from capture_adc124_settling import FRAME,decode
    data=np.zeros(2,dtype=FRAME)
    data['magic']=0x55aa;data['count']=50;data['samples']=2048
    data['start']=[2**32-6,4];data['end']=[2**32-1,9];data['avg']=1
    path=tmp_path/'capture.bin';path.write_bytes(data.tobytes())
    assert len(decode(path))==2
    data['count'][1]=49;path.write_bytes(data.tobytes())
    with pytest.raises(ValueError,match='header/count'):
        decode(path)

@pytest.mark.parametrize('mode,version',[('mux',2),('channel',2),('adc',3),('mux',3)])
def test_mux_payload_is_mapped_to_physical_channel_labels(analysis,mode,version):
    from capture_adc124_settling import FRAME,bench
    canonical=bench.payload_order(bench.load_manifest(bench.DEFAULT_MANIFEST)['both_full'],'channel',2)
    wire=bench.payload_order(canonical,mode,version)
    data=np.zeros(2,dtype=FRAME)
    data['samples']=[1000*r.array+100*r.mux+r.input for r in wire]
    x=analysis.canonical_samples(data,dict(scanorder=mode,protocol_version=str(version),payload_routes=','.join(map(str,wire))))
    assert x[0].tolist()==[1000*r.array+100*r.mux+r.input for r in canonical]
    assert x[0,0]==1100 and x[0,5]==1105 and x[0,6]==1200
    with pytest.raises(ValueError,match='Payload route order'):
        analysis.canonical_samples(data,dict(scanorder=mode,protocol_version=str(version),payload_routes=','.join(map(str,reversed(wire)))))
