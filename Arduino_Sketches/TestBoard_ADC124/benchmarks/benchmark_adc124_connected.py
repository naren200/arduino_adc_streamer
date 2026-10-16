"""Connected PZT startup and matched LPSPI matrix, with explicit setup labels."""
from pathlib import Path
from datetime import datetime,timezone
import argparse,json
from benchmark_adc124_upgrade import configurations,restore_settings
from benchmark_adc124_fifo_gpio import snapshot
from benchmark_adc124_scan_modes import board_status,capture_matrix,bench

SETUP=dict(connectors='both sensor strips attached',sensor_state='PZT strips untouched; table vibration not instrumented',charger='connected',
    reference_volts=3.3,vmid_volts=1.65,ldo='TPS7A2033PDBVR',bias_ohms=[[470000,1000000,470000,1000000],[1000000,470000,1000000,470000]])


def valid(status):
    return (status.get('firmware_revision')=='adc124-selector-lookahead-v1' and status.get('adc_selector_policy')=='lookahead'
        and status.get('adc_batch_prime_frames')=='0' and status.get('profile_available')=='false')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('stage',choices=('startup','matrix'));p.add_argument('output',type=Path)
    p.add_argument('--discharge-description',default='Not yet documented');p.add_argument('--startup-name',default='cold_poweron')
    p.add_argument('--dry-run',action='store_true');a=p.parse_args();root=a.output.resolve()
    configs=[c for c in configurations() if c.spiengine=='lpspi']
    if a.dry_run:
        print('Connected setup:',SETUP)
        print('Startup: 60 s, MUX/LPSPI/16 MHz/5 us/VMID off; no warm-up discarded')
        print('Matrix:',len(configs),'settings x 3, 1 s warm-up + 15 s measurement');return
    life_path=root/'firmware_lifecycle.json'
    if a.stage=='startup':
        root.mkdir(parents=True,exist_ok=life_path.exists())
        if life_path.exists():life=json.loads(life_path.read_text())
        else:
            old=board_status(root)
            if not valid(old):raise RuntimeError('Unexpected production firmware; no capture or upload')
            images=snapshot(root,'current')
            life=dict(state='starting',created_utc=datetime.now(timezone.utc).isoformat(),before_status=old,setup=SETUP,
                startup_requested='Power-on/reset with discharged sensors; first sweep is time zero',settings_count=28,repetitions=3,
                firmware_uploaded=False,source_images=str(images),discharge_description=a.discharge_description)
        old=life['before_status'];life['discharge_description']=a.discharge_description
        bench.write_json(life_path,life)
    else:
        life=json.loads(life_path.read_text());old=life['before_status']
        if life['state']!='startup_complete_settings_restored':raise RuntimeError('Complete startup record required first')
        if not valid(board_status(root)):raise RuntimeError('Production firmware changed')
    def save():bench.write_json(life_path,life)
    try:
        if a.stage=='startup':
            life['state']='startup_capture';save()
            config=bench.BenchmarkConfig('both','mux','lpspi',16000000,1,5,False,'burst',5)
            setup=dict(SETUP,initial_condition=a.discharge_description,time_origin='First recorded sensor sweep after host-triggered run; USB power edge not timestamped')
            capture_matrix(root,a.startup_name,[config],1,0,60000,'off',setup=setup)
            life.setdefault('startup_stages',[]).append(a.startup_name)
        else:
            life['state']='connected_matrix';save()
            capture_matrix(root,'production',configs,3,1000,15000,'off',setup=SETUP)
    except BaseException as error:life['error']=repr(error);save();raise
    finally:
        life['final_production_status']=restore_settings(root,old)
        if not valid(life['final_production_status']):raise RuntimeError('Stopped production restore identity mismatch')
        life['state']='failed_settings_restored' if 'error' in life else ('startup_complete_settings_restored' if a.stage=='startup' else 'complete_production_restored_verified')
        save();print(life['state'],flush=True)


if __name__=='__main__':main()
