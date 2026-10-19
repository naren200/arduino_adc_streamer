"""Diagnostic physical labels, binary integrity and actual firmware pipeline model."""
import importlib.util
from pathlib import Path
import shutil,struct,subprocess,sys
import pytest
from Arduino_Sketches.TestBoard_ADC124.benchmarks import pcb_diagnostics as d

def test_all_programs_are_bounded_and_label_reference_routes():
    ps=d.programs()
    assert len(ps)==256 and sum(p.repetitions for p in ps)==762
    for p in ps:
        p.validate()
        labels,events=p.labels()
        assert sum(e[3] for e in events)==len(labels)
        for x in labels:
            assert x['role']==('vmid' if x['address']==7 or(x['address']==6 and x['mux']<4) else 'sensor')
    full=next(p for p in ps if p.name=='fast_full_5')
    assert len(full.labels()[0])==50
    assert len(next(p for p in ps if p.name=='fast_core_5').labels()[0])==48

def fixture_frame(p):
    _,events=p.labels();raw=bytearray()
    for a,b,c,n in events:
        raw.extend(d.EVENT.pack(a,b,c,n,10,20,30))
        raw.extend(struct.pack('<'+'H'*n,*([2048]*n)))
    h=d.HEADER.pack(b'ADG1',28+len(raw),len(events),0,100,200,0)
    data=h+raw
    return b'#OK\r\n'+data+struct.pack('<I',d.fnv(data))

def test_decoder_requires_exact_frame_counts_and_checksum():
    p=d.Program('t','t',(d.Step(0),d.Step(7,mask=0)),order=27,first_bank=1).validate()
    raw=fixture_frame(p)
    out=d.decode(raw,p)
    assert out['values'].shape==(1,8)
    assert out['events'].shape==(1,2,3)
    assert p.labels()[0][0]['array']==2 and p.labels()[0][0]['mux']==4
    for bad in (raw[:-1],raw[:35]+bytes([raw[35]^1])+raw[36:],b'garbage'+raw):
        with pytest.raises(ValueError):d.decode(bad,p)
    other=d.Program('t','t',(d.Step(1),d.Step(7,mask=0)),order=27,first_bank=1)
    with pytest.raises(ValueError,match='ordering'):d.decode(raw,other)

def test_long_isolation_reader_waits_for_a_final_full_cycle():
    p=next(p for p in d.programs() if p.name=='leak_b0_m0_1000000')
    assert d.completion_wait_s(p)>1.45
    assert d.completion_wait_s(p)>sum(max(s.wait,s.slot) for s in p.steps)/1e6

def test_analysis_preserves_counts_volts_and_burst_timing(tmp_path,monkeypatch):
    import json
    from dataclasses import asdict
    import numpy as np
    monkeypatch.syspath_prepend(str(d.HERE))
    import analyze_pcb_diagnostics as analysis
    root=tmp_path/'raw';cap=root/'captures';cap.mkdir(parents=True)
    p=d.Program('unit','unit',(d.Step(0,mask=1),),arrays=1,warmup_ms=0)
    m=dict(state='complete',program=asdict(p),rep=1,labels=p.labels()[0],frames=2,
        status=dict(cpu_hz='600000000',cycles='2',dropped='0',overruns='0',transfer_errors='0',data_errors='0',usb_write_errors='0'))
    (cap/'unit.json').write_text(json.dumps(m));(root/'lifecycle.json').write_text('{"state":"production_restored_verified"}')
    np.savez_compressed(cap/'unit.npz',values=np.array([[2048],[2050]],dtype=np.uint16),
        timing=np.array([[0,100,0],[1000,1100,0]],dtype=np.uint32),sequence=np.array([0,1],dtype=np.uint32),
        events=np.array([[[600,1200,1800]],[[600,1200,1800]]],dtype=np.uint32))
    result=analysis.analyze(root,tmp_path/'out');c=result['channels'][0]
    assert c['baseline_counts']==2049 and c['ptp_counts']==2
    assert c['sd_counts']==pytest.approx(2**.5)
    assert c['sd_volts']==pytest.approx(2**.5*3.3/4096)
    assert c['burst_start_after_switch_us']==1 and c['burst_duration_us']==1
    assert result['captures'][0]['period_median_us']==1000
    assert result['captures'][0]['acquisition_median_us']==100
    selected=analysis.longest_quiet(np.array([1,1,1,1,0,1,1,1,1,1,1,0],dtype=bool),
        np.array([0,1,2,3,4,5,6,7,9,10,11,12]))
    assert np.flatnonzero(selected).tolist()==[0,1,2,3]

def test_quiet_window_screen_distinguishes_sustained_burst_from_spike(monkeypatch):
    import numpy as np
    monkeypatch.syspath_prepend(str(d.HERE))
    from analyze_pcb_diagnostics import burst_window_screen
    t=np.arange(12000)/1000
    rng=np.random.default_rng(11)
    white=rng.normal(0,1,(len(t),2))
    mask,_=burst_window_screen(t,white)
    assert mask.all()
    v=np.zeros((len(t),2));v[100,0]=20
    v[6000:7000,1]=8*np.sin(np.arange(1000)*.1)
    mask,meta=burst_window_screen(t,v)
    assert mask[100] and not mask[6500]
    assert meta['flagged_bins']==[6]
    assert mask[:5000].all() and mask[8000:].all()

def test_real_diagnostic_firmware_tagged_pipeline(tmp_path):
    project=Path(__file__).resolve().parents[1]/'Arduino_Sketches/TestBoard_ADC124'
    if importlib.util.find_spec('ziglang'):
        compiler=[sys.executable,'-m','ziglang','c++']
        if sys.platform=='win32':compiler+=['-target','x86_64-windows-gnu']
    elif shutil.which('g++'):compiler=[shutil.which('g++')]
    else:pytest.skip('Native C++ compiler required')
    exe=tmp_path/('diag.exe' if sys.platform=='win32' else 'diag')
    cmd=compiler+['-std=c++17','-O1','-UNDEBUG','-D__IMXRT1062__','-DTESTBOARD_DIAGNOSTIC=1',
        '-I',str(project/'test/host'),'-I',str(project/'include'),str(project/'test/host/diagnostic_test.cpp'),
        *[str(project/'src'/f'{n}.cpp') for n in ('Adc124s101','SpiController','UsbSerialController','ApiProtocol')],'-o',str(exe)]
    result=subprocess.run(cmd,capture_output=True,text=True,timeout=180)
    assert result.returncode==0,result.stdout+result.stderr
    result=subprocess.run([str(exe)],capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert '810 diagnostic tagged pipeline/order/ADC/GPIO cases passed' in result.stdout
