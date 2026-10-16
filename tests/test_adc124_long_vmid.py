"""Plan and timeout/schema coverage for the isolated long-VMID extension."""
import importlib
import pytest
from Arduino_Sketches.TestBoard_ADC124.benchmarks import pcb_diagnostics as d

def extension(monkeypatch):
    monkeypatch.syspath_prepend(str(d.HERE))
    return importlib.import_module('benchmark_adc124_long_vmid')

def test_long_vmid_plan_preserves_full_routes_and_ten_read_positions(monkeypatch):
    m=extension(monkeypatch);pairs,scans=m.plan()
    assert len(pairs)==9 and (2*len(pairs)+len(scans))*3==63
    for pair in pairs:
        assert len(pair['before'].labels()[0])==50
        after=pair['after'];labels=after.labels()[0]
        if pair['key']=='full':assert len(labels)==50
        else:
            assert len(labels)==20 and {r['array'] for r in labels}=={1,2}
            assert {r['address'] for r in labels}=={2}
            assert [r['repeat'] for r in labels[:10]]==list(range(10))
            assert labels[0]['array']==after.first_bank+1
    for p in scans:
        assert len(p.labels()[0])==50 and len(p.steps)==14
        assert all(s.wait==5 for s in p.steps[::2])
        assert all(s.address==7 and s.mask==15 and s.flags==0 for s in p.steps[1::2])
    assert [p.steps[1].wait for p in scans]==[1000,5000,10000]

def test_ten_conversion_profile_has_bounded_bursts():
    p=d.Program('t','t',(d.Step(2,repeat=10,mask=4),))
    p.validate();assert len(p.labels()[0])==20
    with pytest.raises(AssertionError):d.Program('t','t',(d.Step(2,repeat=10,mask=3),)).validate()
    with pytest.raises(AssertionError):d.Program('t','t',(d.Step(2,repeat=11,mask=1),)).validate()
