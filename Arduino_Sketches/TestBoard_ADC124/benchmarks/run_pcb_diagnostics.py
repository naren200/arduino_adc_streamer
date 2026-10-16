"""Diagnostic upload/capture with unconditional frozen-production restore."""
import argparse,hashlib,json,shutil
from pathlib import Path
from datetime import datetime,timezone
import pcb_diagnostics as d
from benchmark_adc124_fifo_gpio import upload_image,restore

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--smoke',action='store_true')
    parser.add_argument('--only',nargs='+');args=parser.parse_args()
    root=d.RAW;life_path=root/'lifecycle.json';life=json.loads(life_path.read_text())
    backup=root/'production_backup/.pio/build/teensy41/firmware.hex'
    expected=life['production_hashes']['Arduino_Sketches\\TestBoard_ADC124\\.pio\\build\\teensy41\\firmware.hex']
    if hashlib.sha256(backup.read_bytes()).hexdigest()!=expected:raise RuntimeError('Production backup hash mismatch')
    image=d.FW/'.pio/build/teensy41_pcbdiag/firmware.hex'
    frozen=root/'diagnostic_firmware';frozen.mkdir(exist_ok=True)
    for source in list((d.FW/'src').glob('*.cpp'))+list((d.FW/'include').glob('*.h'))+[d.FW/'platformio.ini',image]:
        target=frozen/source.relative_to(d.FW);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    life['diagnostic_sha256']=hashlib.sha256(image.read_bytes()).hexdigest()
    life.setdefault('attempts',[]).append(dict(smoke=args.smoke,only=args.only,utc=datetime.now(timezone.utc).isoformat()))
    attempt=life['attempts'][-1]
    def save():life_path.write_text(json.dumps(life,indent=2))
    life['state']='uploading_diagnostic';save()
    try:
        with (root/'uploads.log').open('a') as log:upload_image(image,log)
        life['state']='capturing_diagnostic';save()
        d.run(root,args.only,args.smoke)
        attempt['result']='captures_complete'
    except BaseException as error:
        attempt['error']=repr(error);life['state']='capture_failed';save();raise
    finally:
        print('Restoring frozen production firmware and original settings',flush=True)
        life['final_status']=restore(root,backup,life['before_status'])
        life['state']='production_restored_verified'
        save();print(life['state'],flush=True)

if __name__=='__main__':main()
