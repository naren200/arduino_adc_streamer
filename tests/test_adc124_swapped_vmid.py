"""Ensure the strip-exchange comparison joins by circuit route and condition."""
import importlib
import pytest
from Arduino_Sketches.TestBoard_ADC124.benchmarks import pcb_diagnostics as d


def test_numeric_comparisons_do_not_require_optional_report_packages():
    import subprocess
    import sys
    code = '''
import importlib.abc
import sys
class NoReportPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('matplotlib', 'reportlab'):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoReportPackages())
sys.path.insert(0, sys.argv[1])
import report_adc124_swapped_vmid as report
rows = report.matched([dict(array=1, value=10)], [dict(array=1, value=12)], ['array'], ['value'])
assert rows[0]['swapped_minus_original_value'] == 2
'''
    result = subprocess.run([sys.executable, '-c', code, str(d.HERE)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


def module(monkeypatch):
    monkeypatch.syspath_prepend(str(d.HERE))
    return importlib.import_module('report_adc124_swapped_vmid')


def test_comparison_joins_route_and_read_order_without_positional_pairing(monkeypatch):
    m = module(monkeypatch)
    old = [dict(array=1,mux=2,first_bank=0,value=100),dict(array=2,mux=2,first_bank=0,value=10),
           dict(array=2,mux=2,first_bank=1,value=20)]
    new = [dict(array=2,mux=2,first_bank=1,value=30),dict(array=1,mux=2,first_bank=0,value=101),
           dict(array=2,mux=2,first_bank=0,value=12)]
    rows = m.matched(old,new,['array','mux','first_bank'],['value'])
    assert [(r['array'],r['first_bank'],r['swapped_minus_original_value']) for r in rows] == [(2,1,10),(1,0,1),(2,0,2)]


def test_comparison_rejects_missing_or_duplicate_routes(monkeypatch):
    m = module(monkeypatch)
    old = [dict(array=1,value=100),dict(array=2,value=10)]
    with pytest.raises(AssertionError):m.matched(old,old[:1],['array'],['value'])
    with pytest.raises(AssertionError):m.matched([old[0],old[0]],old,['array'],['value'])
    with pytest.raises(KeyError):m.matched(old,[old[0],old[0]],['array'],['value'])


def test_tracking_strip_reverses_array_and_relative_adc_order(monkeypatch,tmp_path):
    m=module(monkeypatch);monkeypatch.setattr(m,'OUT',tmp_path)
    old=dict(full_routes=[],ten_endpoints=[],scan_routes=[])
    new=dict(full_routes=[],ten_endpoints=[],scan_routes=[])
    for a in (1,2):
        old['full_routes'].append(dict(array=a,mux=1,address=2,before_steady_baseline_counts=2048,after_counts=a*100,delta_from_steady_counts=a*100-2048))
        old['ten_endpoints'].append(dict(array=a,mux=1,first_bank=a-1,before_baseline_counts=2048,first_counts=a*100,tenth_counts=a*100+5,tenth_minus_first_counts=5))
        old['scan_routes'].append(dict(array=a,mux=1,address=2,vmid_wait_us=1000,baseline_counts=a*100,sd_counts=.5,ptp_counts=2))
    for key,rows in old.items():
        for r in rows:
            q=dict(r);q['array']=3-r['array']
            if 'first_bank' in q:q['first_bank']=1-r['first_bank']
            new[key].append(q)
    result=m.strip_comparisons(old,new)
    for rr in result.values():
        assert len(rr)==2
        for r in rr:
            assert r['strip_current_array']==3-r['strip_original_array']
            if 'original_first_bank' in r:
                assert r['swapped_first_bank']==1-r['original_first_bank']
            assert all(v==0 for k,v in r.items() if k.startswith('swapped_minus_original_'))
