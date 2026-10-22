"""Full disconnected LPSPI-only lookahead matrix; stop and restore settings."""
from pathlib import Path
from datetime import datetime,timezone
import argparse
from benchmark_adc124_upgrade import configurations,restore_settings
from benchmark_adc124_fifo_gpio import snapshot,upload_image
from benchmark_adc124_scan_modes import board_status,capture_matrix,bench


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    parser.add_argument('--dry-run',action='store_true');args=parser.parse_args()
    configs=[c for c in configurations() if c.spiengine=='lpspi']
    assert len(configs)==28 and len({c.test_id for c in configs})==28
    if args.dry_run:
        for c in configs:print(c.test_id)
        print('28 settings x 3 repeats; 1 s warm-up + 15 s measurement; production only');return
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    old=board_status(root)
    def valid(status):
        return (status.get('firmware_revision')=='adc124-selector-lookahead-v1' and status.get('adc_selector_policy')=='lookahead'
                and status.get('adc_batch_prime_frames')=='0' and status.get('profile_available')=='false')
    if not valid(old):raise RuntimeError('Unexpected production identity; no upload performed')
    images=snapshot(root,'current')
    life=dict(state='starting',created_utc=datetime.now(timezone.utc).isoformat(),before_status=old,
        settings_count=28,repetitions=3,warmup_ms=1000,measurement_ms=15000,
        sensor_setup='Both array connectors disconnected; charger connected; TPS7A2033PDBVR 3.3 V LDO',
        engines=['lpspi'],diagnostic_captures=False)
    def save():bench.write_json(root/'firmware_lifecycle.json',life)
    save()
    try:
        life['state']='production_upload';save()
        with (root/'firmware_uploads.log').open('a') as log:upload_image(images/'teensy41.hex',log)
        loaded=board_status(root)
        if not valid(loaded):raise RuntimeError('Uploaded production identity mismatch')
        life['uploaded_status']=loaded;life['state']='production_captures';save()
        capture_matrix(root,'production',configs,3,1000,15000,'off')
    except BaseException as error:
        life['error']=repr(error);save();raise
    finally:
        life['final_production_status']=restore_settings(root,old)
        if not valid(life['final_production_status']):raise RuntimeError('Final production identity mismatch')
        life['state']='failed_production_restored_verified' if 'error' in life else 'complete_production_restored_verified'
        save();print(life['state'],flush=True)


if __name__=='__main__':main()
