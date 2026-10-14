import pytest
from Arduino_Sketches.TestBoard_ADC124.benchmarks import testboard_adc124_benchmark as b
from test_testboard_adc124_benchmark import status_for


@pytest.mark.parametrize('text,value',[('default',None),('0',0),('1',1),('5',5),('1000',1000)])
def test_cli_vmid_settling_accepts_default_and_bounded_integers(text,value):
    assert b.parse_args(['--dry-run','--vmid-settle-us',text]).vmid_settle_us == [value]


@pytest.mark.parametrize('text',['-1','1001','1.5','auto','nan',''])
def test_cli_rejects_invalid_vmid_settling(text):
    with pytest.raises(SystemExit): b.parse_args(['--vmid-settle-us',text])


def test_matrix_and_engine_keys_keep_override_zero_distinct_from_default():
    args=b.parse_args(['--arrays','both','--scan-order','mux','--spi-engine','lpspi',
        '--mux-settle-us','5','--vmid','on','--vmid-settle-us','default','5','0','1'])
    configs=b.build_matrix(args,b.load_manifest(b.DEFAULT_MANIFEST)['both_full'])
    assert [c.vmid_settle_us for c in configs] == [None,5,0,1]
    assert len({c.test_id for c in configs}) == len({c.comparison_key for c in configs}) == 4
    assert '__vmidsettle' not in configs[0].test_id and configs[2].test_id.endswith('__vmidsettle0')


class Protocol:
    def __init__(self,initial,final): self.initial,self.final,self.commands,self.status_reads=initial,final,[],0
    def command(self,command):
        self.commands.append(command)
        if command=='status':
            status=self.initial if self.status_reads==0 else self.final; self.status_reads+=1
            return [f'# {k}={v}' for k,v in status.items()]+['#OK']
        return ['#OK']


@pytest.mark.parametrize('delay',[None,0,1,5])
def test_configure_overwrites_stale_override_and_validates_effective_delay(delay):
    routes=(b.Route(1,1,0),)
    config=b.BenchmarkConfig('1','adc','blocking',16000000,1,5,True,'burst',delay)
    final=status_for(config,routes); initial={**final,'vmid_settle_us':'12','vmid_settle_mode':'explicit'}
    protocol=Protocol(initial,final); assert b.configure(protocol,config,routes)==final
    expected='vmidsettle default' if delay is None else f'vmidsettle {delay}'
    assert expected in protocol.commands
    assert protocol.commands.index('muxsettle 5') < protocol.commands.index(expected)
    bad={**final,'vmid_settle_us':'9'}
    with pytest.raises(b.BenchmarkError,match='vmid_settle_us'): b.validate_status(bad,config,routes)


def test_older_protocol3_firmware_accepts_inheritance_but_rejects_explicit_override():
    routes=(b.Route(1,1,0),)
    config=b.BenchmarkConfig('1','adc','blocking',16000000,1,5,True)
    status=status_for(config,routes)
    status.pop('vmid_settle_us'); status.pop('vmid_settle_mode')
    protocol=Protocol(status,status); b.configure(protocol,config,routes)
    assert not any(c.startswith('vmidsettle') for c in protocol.commands)
    explicit=b.BenchmarkConfig('1','adc','blocking',16000000,1,5,True,'burst',0)
    with pytest.raises(b.BenchmarkError,match='does not support'): b.configure(Protocol(status,status),explicit,routes)
    with pytest.raises(b.BenchmarkError,match='does not support'): b.validate_status(status,explicit,routes)


def test_restore_preserves_explicit_zero_and_inheritance_mode():
    assert b.vmid_settle_from_status({'vmid_settle_us':'0','vmid_settle_mode':'explicit'}) == 0
    assert b.vmid_settle_from_status({'vmid_settle_us':'5','vmid_settle_mode':'follow_sensor'}) is None
    assert b.vmid_settle_from_status({'mux_settle_us':'5'}) is None


@pytest.mark.parametrize('mode,windows,guard',[('adc',14,11.2),('mux',7,5.6)])
def test_vmid_delay_budget_changes_without_reducing_conversion_or_guard_counts(mode,windows,guard):
    from Arduino_Sketches.TestBoard_ADC124.benchmarks.scan_modes import operation_counts
    c=operation_counts(b.load_manifest(b.DEFAULT_MANIFEST)['both_full'],mode,vmid=True,coordinated=True)
    assert c['sensor_settling_windows']==c['vmid_settling_windows']==windows
    assert c['spi_words']==134 and c['cs_frames']==28
    assert c['mux_switch_guard_us']==pytest.approx(guard)
    assert [windows*5+windows*vmid_delay for vmid_delay in (5,1,0)] == [windows*10,windows*6,windows*5]
