"""Report the intended grouped-MUX production matrix and same-order profiles."""
from pathlib import Path
import argparse
import json
import numpy as np
from report_adc124_timing import load_runs, summarize, csv_write, ENGINES, SETTLES
from adc124_timing_model import operation_counts, fit_delay

def analyze(primary, diagnostic, output):
    session, runs=load_runs(primary)
    psession, profiles=load_runs(diagnostic)
    if len(runs)!=90 or len(profiles)!=30:
        raise ValueError('Need 90 production and 30 grouped-MUX diagnostic captures')
    if any(r['order']!='mux' or r['profile'] for r in runs):
        raise ValueError('Production matrix must use mux order with profiling off')
    if any(r['order']!='mux' or not r['profile'] for r in profiles):
        raise ValueError('Diagnostic matrix must use mux order with profiling on')
    summary=summarize(runs);psummary=summarize(profiles)
    if len(summary)!=30 or any(r['repetitions']!=3 for r in summary):
        raise ValueError('Need three production repetitions at every setting')
    fits=[];units=[]
    for e in ENGINES:
        for v in (False,True):
            rows=[r for r in summary if r['engine']==e and r['vmid']==v]
            fits.append(dict(engine=e,vmid=v,expected_slope=operation_counts('mux',v)['address_changes'],
                **fit_delay([r['settle_us'] for r in rows],[r['mean_period_us'] for r in rows])))
        off=[r for r in profiles if r['engine']==e and not r['vmid']]
        units.append(dict(engine=e,
            aggregate_transfer_us_per_word=float(np.median([r['profile_transfer_us']/64 for r in off])),
            prepare_residual_us_per_change=float(np.median([r['profile_prepare_fixed_us']/16 for r in off]))))
    output.mkdir(parents=True,exist_ok=True)
    for name,rows in [('production_capture_metrics',runs),('production_summary',summary),
                      ('diagnostic_capture_metrics',profiles),('diagnostic_summary',psummary),
                      ('delay_fits',fits),('aggregate_unit_estimates',units)]:
        csv_write(output/(name+'.csv'),rows)
    lifecycle=json.loads((output/'firmware_lifecycle.json').read_text())
    if lifecycle['state']!='complete_production_restored_verified':
        raise ValueError('Production restoration must be verified before final reporting')
    result=dict(session=session,profile_session=psession,runs=runs,profiles=profiles,
                summary=summary,profile_summary=psummary,fits=fits,units=units,lifecycle=lifecycle)
    (output/'timing_analysis.json').write_text(json.dumps(result,indent=2))
    return result

def build_report(data,output,pdf):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Table,TableStyle,Image,PageBreak
    from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.enums import TA_CENTER
    summary=data['summary'];profiles=data['profile_summary']
    def get(s,v,e='lpspi',profile=False):
        return next(r for r in (profiles if profile else summary) if (r['settle_us'],r['vmid'],r['engine'])==(s,v,e))
    plt.rcParams.update({'font.size':9,'axes.spines.top':False,'axes.spines.right':False,'axes.grid':True,'grid.alpha':.15})
    palette=['#658c49','#b66525','#2278b4']
    def save(fig,name):
        fig.tight_layout();path=output/(name+'.png');fig.savefig(path,dpi=180);plt.close(fig);return path
    fig,axes=plt.subplots(1,2,figsize=(10,3.5))
    for v,ax in zip((False,True),axes):
        for e,c in zip(ENGINES,palette):
            rows=[get(s,v,e) for s in SETTLES]
            y=np.array([r['rate_hz_channel'] for r in rows]);low=np.array([r['rate_hz_channel_min'] for r in rows]);high=np.array([r['rate_hz_channel_max'] for r in rows])
            ax.errorbar(SETTLES,y,yerr=[y-low,high-y],fmt='o-',capsize=3,color=c,label=e)
        ax.set_xscale('log');ax.set_xticks(SETTLES,labels=list(map(str,SETTLES)));ax.set_xlabel('Settling delay (us)');ax.set_ylabel('Samples/s/channel');ax.set_title('Grouped MUX / VMID '+('on' if v else 'off'));ax.legend()
    rateplot=save(fig,'mux_engine_rates')
    fig,axes=plt.subplots(1,2,figsize=(10,3.8))
    for v,ax in zip((False,True),axes):
        bottom=np.zeros(5)
        for key,label,c in zip(('settle_budget_us','wire_budget_us','explicit_allowances_us','remaining_acquisition_us','mean_gap_us'),('Requested settling','SPI clocking','Explicit MUX/CS allowances','Remaining acquisition','Outside acquisition'),['#467fbd','#95b5ce','#ce8d39','#719a6f','#8c738d']):
            y=np.array([get(s,v)[key] for s in SETTLES]);ax.bar(range(5),y,bottom=bottom,color=c,label=label);bottom+=y
        ax.set_xticks(range(5),labels=list(map(str,SETTLES)));ax.set_xlabel('Settling delay (us)');ax.set_ylabel('Sweep period (us)');ax.set_title('LPSPI / VMID '+('on' if v else 'off'))
    axes[1].legend(fontsize=7)
    budgetplot=save(fig,'mux_timing_budget')
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name='MuxTitle',fontName='Helvetica-Bold',fontSize=22,leading=26,alignment=TA_CENTER,textColor=colors.HexColor('#184f73'),spaceAfter=16))
    styles.add(ParagraphStyle(name='MuxBody',fontName='Helvetica',fontSize=9,leading=12,spaceAfter=8,textColor=colors.HexColor('#203747')))
    styles.add(ParagraphStyle(name='MuxSmall',fontName='Helvetica',fontSize=7.5,leading=10,spaceAfter=6,textColor=colors.HexColor('#203747')))
    styles['Heading1'].textColor=colors.HexColor('#184f73')
    story=[]
    def p(t,small=False):story.append(Paragraph(t,styles['MuxSmall' if small else 'MuxBody']))
    def h(t):story.append(Paragraph(t,styles['Heading1']))
    def page():story.append(PageBreak())
    def image(path,height):story.append(Image(str(path),width=490,height=height))
    def table(headers,rows,widths,size=8,pad=4):
        t=Table([[Paragraph(str(x),styles['MuxSmall']) for x in headers]]+rows,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6eef3')),('FONTNAME',(0,1),(-1,-1),'Helvetica'),('FONTSIZE',(0,1),(-1,-1),size),('LEADING',(0,1),(-1,-1),size+2),('VALIGN',(0,0),(-1,-1),'MIDDLE'),('TOPPADDING',(0,0),(-1,-1),pad),('BOTTOMPADDING',(0,0),(-1,-1),pad),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f3f5f7')])]))
        story.append(t);story.append(Spacer(1,9))
    story.append(Paragraph('TestBoard_ADC124<br/>Grouped MUX timing and SPI engines',styles['MuxTitle']))
    p('13 October 2026 | scanorder mux throughout | Production rates | Manual / repeat 1')
    h('The intended address-grouped scan')
    p('<b>Every production and diagnostic capture uses scanorder mux.</b> Switch and settle once at each external address; read ADC IN1-IN4 for addresses 0-5, then IN4 alone at address 6. Complete array A, park it, complete array B and park it. There are 25 useful values per array, 50 per full sweep. This report replaces the earlier channel-order primary timing comparison for the requested scan plan.')
    p('Both connectors disconnected; charger connected. Bias resistors A: 470 kOhm / 1 MOhm / 470 kOhm / 1 MOhm; B: 1 MOhm / 470 kOhm / 1 MOhm / 470 kOhm. Nominal reference 3.3 V, VMID 1.65 V. Requested SPI clock 16 MHz on one shared bus; the two ADCs acquire sequentially.')
    h('Production sampling rate: three repetitions')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=[get(s,v,e) for e in ENGINES]
            rows.append([s,'on' if v else 'off',*[f'{x["rate_hz_channel"]:,.1f}' for x in r],f'{100*(r[2]["rate_hz_channel"]/r[0]["rate_hz_channel"]-1):.1f}%',f'{100*(r[2]["rate_hz_channel"]/r[1]["rate_hz_channel"]-1):.1f}%'])
    table(['Settle us','VMID','Blocking Hz/ch','DMA Hz/ch','LPSPI Hz/ch','LPSPI vs blocking','LPSPI vs DMA'],rows,[43,38,83,80,82,82,82])
    image(rateplot,170)
    p('90 fresh production captures: 30 settings x 3 repetitions, each 1 s warm-up + 15 s measured. Configuration order rotates between repetitions. Rates use device sweep-start timestamps after warm-up. Table entries are repetition medians; error bars show the min-max range. Useful total values/s = 50 x Hz/channel.')
    page();h('Operations per address, per ADC and per sweep')
    table(['Address per array','Groups / array','Useful values / group','Words / group off','Words / group on'],[['0-5','6','4','5','10'],['6 / IN4 alone','1','1','2','7']],[133,78,91,93,95])
    table(['Full two-array sweep','VMID off','VMID on'],[['Sensor address groups','14','14'],['Actual sensor switches','14','14'],['Additional VMID park switches','0','14'],['Additional mandatory park changes','2','0 (calls return already parked)'],['Total physical address changes','16','28'],['Selector-prime sensor words','14','14'],['Retained sensor words','50','50'],['Discarded VMID words','0','70'],['Total 16-bit ADC words','64','134']],[232,102,156])
    p('A sensor group includes one selector prime plus its 4 (or 1) retained conversions. VMID-on adds five discarded words per group: one selector prime plus all four ADC inputs. End/handoff parking still runs; with insertion enabled those calls do not produce another address change or settling wait.')
    p('Each real external-address change performs EN off, a 200 ns allowance, three address GPIO writes, EN on, a 200 ns allowance and the configured extra analog settling. Each ADC word has 100 ns CS setup and 100 ns CS-high allowance in the current driver. GPIO/API overhead and interrupts add to these explicit waits.')
    p('At nominal 16 MHz, one 16-clock ADC frame takes 1.000 us: 3 clock cycles of track/acquisition (0.1875 us), followed by 13 conversion/output cycles (0.8125 us). Conversion and output shifting occupy the same frame, so they are not added again as independent costs. The DIN selector applies to the next conversion. <link href="https://www.ti.com/lit/ds/symlink/adc124s101.pdf">TI ADC124S101 datasheet, Operation / Using the ADC</link>.',True)
    p('The pinned SPI library selects a 240 MHz peripheral root and divider 15 for the 16 MHz request. Calculations use this nominal frequency; electrical GPIO propagation and the SCLK waveform were not independently scoped.',True)
    page();h('LPSPI production timing budget')
    p('Period = acquisition + outside-acquisition gap. Acquisition is split into requested settling, nominal SPI clocking, explicit MUX/CS allowances and a remaining cost. The remaining cost contains GPIO/register work, driver loops, delay-call overhead and interrupts; it is not a uniquely measured phase.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v)
            rows.append([s,'on' if v else 'off',*[f'{r[k]:.2f}' for k in ('settle_budget_us','wire_budget_us','explicit_allowances_us','remaining_acquisition_us','mean_gap_us','mean_period_us')]])
    table(['Settle us','VMID','Settle us','SPI clock us','Explicit waits us','Remaining acquisition us','Gap us','Period us'],rows,[40,35,65,65,67,85,57,76],7.5)
    image(budgetplot,194)
    p('Off: settling = 16 x delay; clocking = 64 us; explicit allowances = 16 x 0.4 + 64 x 0.2 = 19.2 us. On: settling = 28 x delay; clocking = 134 us; explicit allowances = 28 x 0.4 + 134 x 0.2 = 38.0 us. The residual closes the measured budget. Rows are medians of repetition means; independently summarized rounded components may differ slightly from their displayed sum.')
    page();h('Rate-versus-settling fits and engine overhead')
    p('Fit full sweep period T = fixed intercept + slope x settling delay. A slope near 16 off or 28 on validates the number of actual waits. The intercept is an extrapolated combined fixed cost, including transfer/GPIO/loop/USB work; it is not just ADC conversion time.')
    rows=[]
    for f in data['fits']:
        rows.append([f['engine'],'on' if f['vmid'] else 'off',f'{f["slope_us_per_us"]:.4f}',f['expected_slope'],f'{f["intercept_us"]:.3f}',f'{f["residual_max_us"]:.3f}'])
    table(['Engine','VMID','Measured slope us/us','Expected waits','Intercept us','Max fit residual us'],rows,[75,43,104,84,92,92])
    h('Per-channel amortized production cost')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v)
            rows.append([s,'on' if v else 'off',f'{r["mean_acquisition_us"]/50:.4f}',f'{r["mean_period_us"]/50:.4f}',f'{r["values_per_s"]:,.0f}'])
    table(['Settle us','VMID','Acquisition / useful value us','Full period / useful value us','50-channel useful values/s'],rows,[59,54,125,125,127])
    p('These are shared-work costs divided by 50, not the ADC conversion duration or individual sample apertures. Four ADC inputs at one external address are read sequentially. Exact per-channel timestamps are not in the frame; each value shares the sweep start/end interval.')
    page();h('Measured diagnostic phases')
    p('30 fresh grouped-MUX diagnostic captures: one repetition per setting, 1 s warm-up + 2 s measurement. Existing profile firmware records CPU cycles at 600 MHz. Means include warm-up, interrupts and probe overhead; production rate values are kept separate. Table shows 5 us settling.')
    rows=[]
    for e in ENGINES:
        for v in (False,True):
            r=get(5,v,e,True)
            rows.append([e,'on' if v else 'off',*[f'{r[k]:.3f}' for k in ('profile_prepare_us','profile_session_us','profile_transfer_us','profile_validate_us','profile_encode_us','profile_usb_write_us','profile_bookkeeping_us')]])
    table(['Engine','VMID','Prepare us','Session us','Transfer us','Validation us','Encode us','USB enqueue us','Profiler book-keeping us'],rows,[62,32,60,48,57,55,47,58,71],7.2)
    p('<b>Nested scopes:</b> validation is inside transfer; prepare/session/transfer are inside acquisition. Transfer with VMID on includes the additional VMID parking and settling. Do not sum every phase column. Profiler bookkeeping is extra diagnostic work absent from the ordinary production build.')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v);pr=get(s,v,profile=True)
            rows.append([s,'on' if v else 'off',f'{r["mean_period_us"]:.3f}',f'{pr["mean_period_us"]:.3f}',f'{100*(pr["mean_period_us"]/r["mean_period_us"]-1):.2f}%'])
    table(['Settle us','VMID','Production period us','Diagnostic period us','Probe/build slowdown'],rows,[58,49,121,121,141],8,3)
    p('The slowdown is a whole-build comparison. Adjacent cycle-read calibration cannot remove every probe and bookkeeping operation, so individual phases are not presented as corrected production timings.')
    page();h('Per-address scan-time estimates')
    p('For each engine, off transfer / 64 gives an aggregate cost per conversion word, including its share of the 14 read-batch calls and profiling overhead. Prepare residual / 16 subtracts requested settling and includes the 0.4 us switch allowances plus GPIO/delay/probe work. With only one topology these measurements cannot independently separate word and batch-call overhead.')
    rows=[]
    for u in data['units']:
        rows.append([u['engine'],f'{u["aggregate_transfer_us_per_word"]:.4f}',f'{u["prepare_residual_us_per_change"]:.4f}'])
    table(['Engine','Aggregate transfer / word us','Prepare residual / actual switch us'],rows,[96,197,197])
    p('The following 5 us LPSPI estimates use uniform aggregate word cost. Scan includes all priming/retained/VMID words; switch/settle includes the group\'s sensor address change and any VMID park. Shared mandatory parking, session setup, outer-loop and USB costs are excluded. A particular group\'s driver overhead may differ from its allocated average.')
    u=next(r for r in data['units'] if r['engine']=='lpspi');rows=[]
    for label,values,offwords,onwords in [('Addresses 0-5',4,5,10),('Address 6 / IN4',1,2,7)]:
        for v,words in ((False,offwords),(True,onwords)):
            switches=2 if v else 1;change=switches*u['prepare_residual_us_per_change'];settle=switches*5
            scan=words*u['aggregate_transfer_us_per_word'];service=change+settle+scan
            rows.append([label,'on' if v else 'off',values,words,f'{change:.3f}',f'{settle:.1f}',f'{scan:.3f}',f'{service:.3f}',f'{service/values:.3f}'])
    table(['Group per ADC','VMID','Values','Words','Switch us','Settle us','Scan us','Service us','Service / value us'],rows,[104,37,39,39,51,47,51,56,66],7.5)
    p('Each ADC has six four-value groups and one single-value group. VMID-off adds one mandatory park per array; the two-array sweep has two. VMID-on parking is already included at every group. Per-channel service is amortized, not simultaneous: IN1-IN4 conversion results are acquired in sequence.')
    page();h('USB work and optimization priorities')
    rows=[]
    for s in SETTLES:
        for v in (False,True):
            r=get(s,v);pr=get(s,v,profile=True)
            rows.append([s,'on' if v else 'off',f'{r["mean_gap_us"]:.3f}',f'{pr["profile_encode_us"]:.3f}',f'{pr["profile_usb_write_us"]:.3f}',f'{r["USB_bytes_s"]/1000:.1f}',f'{100*r["mean_gap_us"]/r["mean_period_us"]:.2f}%'])
    table(['Settle us','VMID','Production gap us','Profile encode us','Profile enqueue us','USB kB/s','Gap share'],rows,[42,38,86,82,87,80,75],7.5)
    p('A frame contains 114 bytes: 4-byte header, 100-byte values, 10-byte timing trailer. USB CDC is not limited by the configured 460800 UART baud value. The core partial-buffer flush timer is nominally 75 us and asynchronous; it is not an added sampling-loop wait. Enqueue measures a queue copy, not physical USB transmission or host receipt. External transport latency and negotiated USB speed were not measured.')
    h('What can still improve the intended grouped scan')
    p('<b>Engine:</b> use the measured engine ranking above. LPSPI keeps one SPI transaction per sweep; blocking sets up/ends a transaction per word, while current DMA starts/completes an asynchronous two-byte transfer per word. The grouped scan is already active throughout these results, so address grouping is not presented as an additional future optimization.')
    p('<b>Burst within an address:</b> keeping CS low for the prepared 5/2-word sensor batches and 5-word VMID batches could reduce per-word CS/API/DMA overhead. Pure explicit CS allowance savings would be (64-14) x 0.2 = 10.0 us/sweep off and (134-28) x 0.2 = 21.2 us/sweep on. Software savings are unmeasured. TI supports multiple back-to-back 16-clock conversions with CS held low; a new driver would still require timing/state/error and analog validation. <link href="https://www.ti.com/lit/ds/symlink/adc124s101.pdf">TI serial-interface specification</link>.')
    p('<b>GPIO and settling:</b> fast/prepared GPIO writes may reduce the fixed switch cost, preserving EN polarity and disable/enable allowances. Lower settling has a larger gain at long delays but must be selected from analog baseline/response evidence. Quiet open-connector data do not validate stimulated ghosting or attached sensors.')
    p('<b>USB:</b> the measured outside gap bounds how much period can be saved by encoding/enqueue/main-loop changes; USB interrupts also occur inside acquisition. Accounted busy discards quantify transport pressure. Prioritize USB work only if it materially limits delivery, rather than treating the asynchronous flush timer as a sampling delay.')
    page();h('Integrity and evidence')
    allruns=data['runs']+data['profiles'];total=sum(r['frames'] for r in allruns);acquired=sum(r['acquired'] for r in allruns);drops=sum(r['discarded'] for r in allruns)
    p(f'<b>120 fresh grouped-MUX captures:</b> 90 production and 30 diagnostic. Received {total:,} full sweeps ({50*total:,} channel values including warm-up); acquired {acquired:,}, deliberately discarded {drops:,} whole sweeps. Every transmitted sweep was received; acquired equals sent plus discarded. Firmware error counters remained zero.')
    p('Every capture preserved raw bytes, command/status logs and SHA-256 hash. Firmware configuration and exact payload_routes were validated before run. A structured reader and existing incremental parser agreed on every value/timestamp/count, including reads split through frame boundaries. Codes stayed within 0-4095; wrapped timestamps were unwrapped, monotonic and non-overlapping. Hashes were rechecked for this report.')
    p('Ordinary production firmware was used for throughput. The existing diagnostic build was loaded for phase measurement with one CLI uploader and direct USB reboot, then production restored. Final status confirms profiling unavailable, running false and the original full-array/MUX-order/blocking/16 MHz/repeat-1/5 us/VMID-off configuration. No firmware acquisition source or protocol changes were made.')
    table(['Quantity','Evidence / limit'],[['Sweep period and delivered rate','Production device sweep-start intervals after warm-up'],['Acquisition and outside gap','Correctly paired production start/end intervals'],['Switch/word/group counts','Firmware plan and exact grouped payload routes'],['Settling and explicit wait budgets','Event counts x configured waits'],['Prepare/transfer/session/encode/enqueue','Cycle-counter diagnostic scopes; probes included'],['Per-address service estimates','Uniform allocation from aggregate word/switch costs'],['Exact electrical MUX/SCLK edge timing','Not independently measured with a logic analyzer'],['Individual channel apertures and USB host latency','Not transmitted/instrumented in these captures']],[208,282],8,3)
    p('CSV evidence: 90 production capture rows, 30 production settings, 30 diagnostic rows/settings, 6 period fits and 3 aggregate-unit rows. Timing_analysis.json includes original/final configuration and firmware image/source hashes. The companion grouped-MUX settling/VMID report contains quiet masks and full per-channel noise/baseline values in counts and volts.',True)
    pdf.parent.mkdir(parents=True,exist_ok=True)
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Helvetica',7);canvas.setFillColor(colors.HexColor('#607c90'))
        canvas.drawString(42,25,'TestBoard_ADC124 | 13 October 2026 | scanorder mux | blocking / DMA / LPSPI')
        canvas.drawRightString(A4[0]-42,25,str(doc.page));canvas.restoreState()
    SimpleDocTemplate(str(pdf),pagesize=A4,leftMargin=42,rightMargin=42,topMargin=38,bottomMargin=42,title='TestBoard_ADC124 grouped-MUX timing and engines').build(story,onFirstPage=footer,onLaterPages=footer)
    print('PDF',pdf,flush=True)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('primary',type=Path);p.add_argument('diagnostic',type=Path);p.add_argument('output',type=Path);p.add_argument('--pdf',type=Path)
    args=p.parse_args();result=analyze(args.primary,args.diagnostic,args.output)
    if args.pdf:build_report(result,args.output,args.pdf)

if __name__=='__main__':main()
