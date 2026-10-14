import importlib
from pathlib import Path
import pytest

@pytest.fixture
def model(monkeypatch):
    root=Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root/'scripts'))
    monkeypatch.syspath_prepend(str(root/'Arduino_Sketches/TestBoard_ADC124/benchmarks'))
    return importlib.import_module('adc124_timing_model')

@pytest.mark.parametrize('order,vmid,groups,switches,words',[
    ('channel',False,50,52,100),('channel',True,50,100,350),
    ('mux',False,14,16,64),('mux',True,14,28,134)])
def test_full_sweep_counts_match_prepared_firmware_order(model,order,vmid,groups,switches,words):
    # Actual route grouping, including both one-channel input-6 groups.
    import testboard_adc124_benchmark as b
    routes=b.payload_order(b.load_manifest(b.DEFAULT_MANIFEST)['both_full'],order,2)
    grouped=[]
    for route in routes:
        if not grouped or (grouped[-1][0].array,grouped[-1][0].input)!=(route.array,route.input):
            grouped.append([])
        grouped[-1].append(route)
    assert len(grouped)==groups
    assert sum(1+len(g)+(5 if vmid else 0) for g in grouped)==words
    # Replay GPIO parking in steady state, distinguishing calls from real changes.
    addresses={1:7,2:7};previous=None;actual=0
    def change(a,address):
        nonlocal actual
        if addresses[a]!=address:actual+=1;addresses[a]=address
    for g in grouped:
        a=g[0].array
        if previous is not None and previous!=a:change(previous,7)
        change(a,g[0].input)
        if vmid:change(a,7)
        previous=a
    change(previous,7)
    c=model.operation_counts(order,vmid)
    assert actual==switches==c['address_changes']
    assert c['words']==words
    assert c['frame_bytes']==4+50*2+10

def test_fit_recovers_settling_event_count(model):
    fit=model.fit_delay([1,2,5,10,50],[205+52*s for s in [1,2,5,10,50]])
    assert fit['slope_us_per_us']==pytest.approx(52)
    assert fit['intercept_us']==pytest.approx(205)
    assert fit['residual_rms_us']<1e-9

def test_word_and_batch_fit_uses_two_topologies(model):
    from report_adc124_timing import unit_models
    rows=[]
    for engine in ('blocking','dma','lpspi'):
        for order in ('channel','mux'):
            c=model.operation_counts(order,False)
            for settle in (1,2,5,10,50):
                rows.append(dict(engine=engine,order=order,vmid=False,words=c['words'],
                    profile_transfer_us=1.7*c['words']+.12*c['address_groups'],
                    profile_prepare_fixed_us=.8*c['address_changes'],address_changes=c['address_changes']))
    for result in unit_models(rows):
        assert result['word_us']==pytest.approx(1.7)
        assert result['batch_overhead_us']==pytest.approx(.12)
        assert result['switch_residual_us']==pytest.approx(.8)
        assert result['transfer_fit_rms_us']<1e-9
