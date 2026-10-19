"""Load existing diagnostics, capture mux-only profiles, restore production."""
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys
import time
from capture_adc124_settling import bench,serial
from serial.tools import list_ports

ROOT=Path(__file__).resolve().parents[3]
FW=ROOT/'Arduino_Sketches/TestBoard_ADC124'
LOADER=Path('C:/Users/shera/.platformio/packages/tool-teensy/teensy_loader_cli.exe')

def wait_port():
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if any(p.vid==0x16c0 and p.pid==0x0483 and p.device=='COM4' for p in list_ports.comports()):
            return
        time.sleep(.25)
    raise RuntimeError('Teensy USB serial did not re-enumerate')

def upload(env, log):
    image=FW/'.pio/build'/env/'firmware.hex'
    with subprocess.Popen([str(LOADER),'--mcu=TEENSY41','-w','-v',str(image)],
            stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW) as process:
        try:
            with serial.Serial('COM4',134,timeout=.05,write_timeout=3):time.sleep(.2)
            if process.wait(timeout=30)!=0:raise RuntimeError('Firmware upload failed')
        except BaseException:
            process.terminate();process.wait(timeout=5);raise
    wait_port()

def status(output):
    with (output/'firmware_commands.log').open('a') as log,serial.Serial('COM4',460800,timeout=.02,write_timeout=3) as port:
        protocol=bench.SerialProtocol(port,log);protocol.stop_and_drain()
        if '# TestBoard_ADC124' not in protocol.command('mcu'):raise RuntimeError('Wrong board')
        return bench.parse_status(protocol.command('status'))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('capture',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    original=status(args.output)
    if original['profile_available']!='false':raise RuntimeError('Expected ordinary production build')
    files=[FW/'.pio/build'/e/'firmware.hex' for e in ('teensy41','teensy41_profile')]
    files+=list((FW/'src').glob('*.cpp'))+list((FW/'include').glob('*.h'))
    record=dict(state='before_diagnostic_upload',before_status=original,
        sha256={str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    def save():(args.output/'firmware_lifecycle.json').write_text(json.dumps(record,indent=2))
    save()
    with (args.output/'firmware_uploads.log').open('w') as uploads:
        try:
            print('Uploading existing diagnostic firmware with single CLI uploader',flush=True)
            upload('teensy41_profile',uploads)
            record['diagnostic_status']=status(args.output)
            if record['diagnostic_status']['profile_available']!='true':raise RuntimeError('Diagnostic build not identified')
            record['state']='diagnostic_loaded_verified';save()
            subprocess.run([sys.executable,str(ROOT/'Arduino_Sketches/TestBoard_ADC124/benchmarks/capture_adc124_settling.py'),str(args.capture),
                '--orders','mux','--engines','blocking','dma','lpspi','--repetitions','1',
                '--window-ms','2000','--profile','on'],check=True)
        except BaseException as error:
            record['error']=repr(error);save();raise
        finally:
            print('Restoring ordinary production firmware',flush=True)
            upload('teensy41',uploads)
            with (args.output/'firmware_commands.log').open('a') as log,serial.Serial('COM4',460800,timeout=.02,write_timeout=3) as port:
                p=bench.SerialProtocol(port,log);p.stop_and_drain()
                old=original['protocol_version'] in ('1','2')
                config=bench.BenchmarkConfig(original['array'],'adc' if old else original['scanorder'],original['spiengine'],
                    int(original['spi_clock_hz']),int(original['channelrepeat']),int(original['mux_settle_us']),original['vmid_between_groups']=='true',
                    'single' if old else original.get('spi_frame','burst'),bench.vmid_settle_from_status(original))
                routes=tuple(bench.Route.parse(r) for r in original['adcchannels'].split(','))
                final=bench.configure(p,config,routes)
                if final['profile_available']!='false' or final['running']!='false':raise RuntimeError('Production restoration not verified')
                record['final_production_status']=final
                record['state']='complete_production_restored_verified';save()
            print('Production restored, original settings verified, stopped',flush=True)

if __name__=='__main__':main()
