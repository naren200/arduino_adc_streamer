"""Connected PZT matrix, matched baseline/noise comparison and cold-start report."""
from pathlib import Path
import argparse,json
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from reportlab.platypus import SimpleDocTemplate,Paragraph,Table,TableStyle,Image,PageBreak,Spacer
from reportlab.lib.pagesizes import A4,landscape
from reportlab.lib.styles import getSampleStyleSheet,ParagraphStyle
from reportlab.lib import colors
from analyze_adc124_upgrade import ROUTES,LSB,key
from analyze_adc124_scan_modes import csv_write

DELAYS=(1,2,5,10,50)
COLORS=('#0072B2','#E69F00','#009E73','#CC79A7','#D55E00')


def build(folder,pdf):
    data=json.loads((folder/'analysis.json').read_text());raw=data['raw_summary'];quiet=data['summary'];channels=data['raw_channels']
    current=[r for r in raw if r['dataset']=='connected']
    assert len(current)==28 and all(r['repetitions']==3 for r in current) and not data['partial']
    assert data['lifecycle']['state']=='complete_production_restored_verified'
    startup=[]
    for name in data['lifecycle']['startup_stages']:
        stage=folder/name
        startup.append((name,json.loads((stage/'charge_analysis.json').read_text())))
    best_name,charge=max(startup,key=lambda item:(item[1]['measured_intervals'],data['lifecycle']['startup_stages'].index(item[0])))
    z=np.load(folder/best_name/'charge_plot_data.npz');ct,cx=z['t'],z['x']
    figdir=folder/'figures';figdir.mkdir(exist_ok=True)
    plt.rcParams.update({'font.size':8,'axes.grid':True,'grid.alpha':.2,'axes.spines.top':False})
    styles=getSampleStyleSheet();styles.add(ParagraphStyle(name='Body',fontSize=9,leading=12,spaceAfter=7))
    styles.add(ParagraphStyle(name='Small',fontSize=7.5,leading=10,spaceAfter=5))
    styles['Heading1'].fontSize=16;styles['Heading1'].leading=20;styles['Heading1'].textColor=colors.HexColor('#184f73')
    story=[];width=758
    def p(text,small=False):story.append(Paragraph(text,styles['Small' if small else 'Body']))
    def h(text):story.append(Paragraph(text,styles['Heading1']))
    def page():story.append(PageBreak())
    def n(value,d=3):return '-' if value is None or not np.isfinite(value) else f'{value:,.{d}f}'
    def table(head,rows,widths=None):
        cell=ParagraphStyle('Cell',fontSize=7.3,leading=9)
        t=Table([[Paragraph(str(c),cell) for c in row] for row in [head]+rows],colWidths=widths or [width/len(head)]*len(head),repeatRows=1)
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#e6eef4')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f4f6f8')]),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),5),('RIGHTPADDING',(0,0),(-1,-1),5),('TOPPADDING',(0,0),(-1,-1),3),('BOTTOMPADDING',(0,0),(-1,-1),3)]))
        story.extend([t,Spacer(1,6)])
    def chart(fig,name,height=330):
        path=figdir/(name+'.png');fig.savefig(path,dpi=180);plt.close(fig);story.append(Image(str(path),width=width,height=height))
    def get(mode,vmid,delay=5,vd=None,clock=16000000,dataset='connected',values=None):
        vd=delay if vd is None else vd
        return next((r for r in (raw if values is None else values) if (r['dataset'],r['mode'],r['vmid'],r['settle_us'],r['vmid_settle_effective_us'],r['requested_spi_clock_hz'])==(dataset,mode,vmid,delay,vd,clock)),None)
    def val(row,name):return row.get(name,np.nan) if row else np.nan
    def points(setting,array,field='baseline_counts'):
        selected={c['route']:c for c in channels if setting and key(c)==key(setting)}
        return np.array([selected.get(str(r),{}).get(field,np.nan) for r in ROUTES if r.array==array])
    def routeaxis(ax,array,ylabel='Baseline (counts)'):
        ax.set_xticks(range(25),[f'M{r.mux}/{r.input}' for r in ROUTES if r.array==array],rotation=90,fontsize=7)
        ax.set(xlabel='MUX / external address (zero-based)',ylabel=ylabel);ax.ticklabel_format(axis='y',useOffset=False)
        ax.secondary_yaxis('right',functions=(lambda x:x*LSB,lambda x:x/LSB)).set_ylabel('Volts')
    def worst(setting):
        return max((c for c in channels if key(c)==key(setting)),key=lambda c:c['sd_counts'])['route']
    connected_rows=[r for r in data['rows'] if r['dataset']=='connected']
    title='TestBoard_ADC124 - connected PZT sensors / LPSPI'
    story.append(Paragraph(title,styles['Title']))
    p('14 October 2026 | Both sensor strips connected and untouched | Charger connected | TPS7A2033PDBVR 3.3 V LDO')
    h('Same firmware, changed sensor load')
    p('Both connected and disconnected matrices use adc124-selector-lookahead-v1: four conversions for a four-channel address group, no discarded selector-prime sample. Both arrays, all 50 routes; manual repeat 1, burst framing, LPSPI only. ADC scan finishes Array A before B. MUX scan switches both banks together, shares the settling delay, then reads ADC A and ADC B on the shared SPI bus.')
    p('28 settings x 3 repetitions, each 1 s warm-up + 15 s measurement. Sensor delays 1/2/5/10/50 us, VMID off/on with equal VMID delays at requested 16 MHz; extras at sensor 5 us: VMID delays 0/1 us with VMID on, and requested 8 MHz versus 16 MHz with VMID off/on. The primary reference is the latest disconnected lookahead dataset, not the older firmware with selector primes.')
    table(['Verified connected captures','Received sweeps','Accounted USB discards','Qualified quiet records','Cold-start attempts'],[[len(connected_rows),f"{sum(r['frames'] for r in connected_rows):,}",sum(r['usb_frames_discarded'] for r in connected_rows),sum(r['quiet_qualified'] for r in connected_rows),len(startup)]])
    max_rate=max(abs(r['rate_change_percent']) for r in data['comparisons'])
    p(f"Delivered rates agree within {max_rate:.3f}% across all 28 matches. At 5 us with VMID off, median per-channel raw SD changes from {get('adc',False,dataset='disconnected')['all_sd_median_counts']:.4f} to {get('adc',False)['all_sd_median_counts']:.4f} counts in ADC scan, and {get('mux',False,dataset='disconnected')['all_sd_median_counts']:.4f} to {get('mux',False)['all_sd_median_counts']:.4f} counts in MUX scan. Digital noise statistics also depend on baseline position within ADC code boundaries.",True)
    table(['5 us / 16 MHz','Disconnected Hz/channel','Connected Hz/channel','Change %','Connected raw worst SD counts / mV','Connected raw worst p-p counts / mV'],[
        [m.upper()+' / VMID '+('on' if v else 'off'),n(val(get(m,v,dataset='disconnected'),'delivered_sweeps_s'),1),n(val(get(m,v),'delivered_sweeps_s'),1),
         n(100*(val(get(m,v),'delivered_sweeps_s')/val(get(m,v,dataset='disconnected'),'delivered_sweeps_s')-1),2),
         n(val(get(m,v),'all_sd_max_counts'))+' / '+n(val(get(m,v),'all_sd_max_volts')*1000),n(val(get(m,v),'all_ptp_max_counts'),1)+' / '+n(val(get(m,v),'all_ptp_max_volts')*1000)] for m in ('adc','mux') for v in (False,True)])
    p('Noise includes unfiltered and common-quiet-window statistics. Baselines use unfiltered medians and line graphs. Nominal 1 count = 3.3/4096 V = 0.805664 mV; rail calibration and table vibration were not measured. The experiment observes sensor-load changes but cannot assign every transient to a vibration or electrical source.',True)
    largest=max(data['channel_comparisons'],key=lambda r:abs(r['baseline_change_counts']))
    p(f"Largest observed baseline shift across all exact matches: {largest['baseline_change_counts']:+.1f} counts ({largest['baseline_change_volts']*1000:+.3f} mV), route {largest['route']}, {largest['mode'].upper()}, sensor/VMID {largest['settle_us']}/{largest['vmid_settle_effective_us']} us, VMID {'on' if largest['vmid'] else 'off'}, {largest['requested_spi_clock_hz']//1000000} MHz.",True)
    p(f"All 50 startup routes are plotted. Complete B/3-to-2B/3 intervals were observed for {charge['measured_intervals']}/50 routes; the remaining crossings were already missed at the first reading. Missing intervals stay unmeasured. The report includes first-rise and 200 ms tail zooms plus full 60 s traces.",True)

    page();h('Sampling rates - connected versus disconnected')
    fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
    for i,m in enumerate(('adc','mux')):
        for j,v in enumerate((False,True)):
            ax=axes[i,j]
            for ds,col,label in [('disconnected','#777777','Disconnected'),('connected','#0072B2','Connected')]:
                ax.plot(DELAYS,[val(get(m,v,d,dataset=ds),'delivered_sweeps_s') for d in DELAYS],'o-',label=label,color=col)
            ax.set(title=m.upper()+' / VMID '+('on' if v else 'off'),xlabel='Sensor = VMID delay (us)',ylabel='Samples/s/channel');ax.legend(fontsize=7)
    chart(fig,'rates');p('Requested 16 MHz. Median of three delivered timestamp-based rates. Each full sweep yields one value for each route; aggregate useful values/s = 50 x this rate. Whole-run acquisition counters and USB discards are retained separately. Apparent delivered-rate differences may include USB pressure.',True)
    page();h('Acquisition duration - checking load-independent scan timing')
    table(['Mode','VMID','Sensor / VMID us','MHz','Disconnected acquisition us','Connected acquisition us','Change us','Disconnected counter Hz','Connected counter Hz'],[
        [r['mode'].upper(),'on' if r['vmid'] else 'off',f"{r['settle_us']}/{r['vmid_settle_effective_us']}",r['requested_spi_clock_hz']//1000000,
         n(r['disconnected_acquisition_us']),n(r['connected_acquisition_us']),n(r['acquisition_change_us']),n(r['disconnected_counter_rate_estimate_hz'],1),n(r['connected_counter_rate_estimate_hz'],1)] for r in data['comparisons']])
    p('Acquisition duration is sweep-end minus sweep-start for received sweeps after warm-up. Counter rates are whole-run estimates from acquired-sweep counts and delivered endpoints, including warm-up; they are not independently timestamped rates. Comparing both distinguishes changed scan duration from delivered-rate changes caused by USB discards.',True)
    page();h('Timing budgets and settling-delay slopes')
    budget=[]
    for m in ('adc','mux'):
        for v in (False,True):
            for clock in (8000000,16000000):
                r=get(m,v,clock=clock);wire=r['estimated_spi_words']*16e6/clock
                settle=r['estimated_settle_budget_us'];guards=r['estimated_cs_allowance_us']+r['estimated_mux_switch_guard_us']
                budget.append(dict(mode=m,vmid=v,clock_hz=clock,settle_us=settle,words=r['estimated_spi_words'],wire_us=wire,guards_us=guards,
                    acquisition_us=r['mean_acquisition_us'],residual_us=r['mean_acquisition_us']-wire-settle-guards,gap_us=r['mean_gap_us']))
    table(['Mode','VMID','MHz','Settle budget us','SPI words','Nominal SPI us','CS + switch guards us','Measured acquisition us','Residual us','Inter-sweep gap us'],[
        [r['mode'].upper(),'on' if r['vmid'] else 'off',r['clock_hz']//1000000,n(r['settle_us'],1),n(r['words'],0),n(r['wire_us'],1),n(r['guards_us'],1),n(r['acquisition_us']),n(r['residual_us']),n(r['gap_us'])] for r in budget])
    p('Sensor = VMID 5 us. Nominal SPI clock budget = 16 clocks/word divided by requested SCLK; actual clock/CS/GPIO edges were not probed. Residual includes GPIO, delay-call and driver/software costs plus model error. Inter-sweep gap includes encoding, enqueue and loop work; it is not a measured USB wire-transmission time.',True)
    fits=[]
    for m in ('adc','mux'):
        for v in (False,True):
            item=dict(mode=m,vmid=v,nominal_slope=(14 if m=='adc' else 7)*(2 if v else 1))
            for ds in ('connected','disconnected'):
                slope,intercept=np.polyfit(DELAYS,[get(m,v,d,dataset=ds)['mean_period_us'] for d in DELAYS],1)
                item[ds+'_slope_us_per_us']=float(slope);item[ds+'_intercept_us']=float(intercept)
            fits.append(item)
    table(['Mode','VMID','Nominal waits / delay','Disconnected slope us/us','Connected slope us/us','Connected intercept us'],[
        [r['mode'].upper(),'on' if r['vmid'] else 'off',r['nominal_slope'],n(r['disconnected_slope_us_per_us']),n(r['connected_slope_us_per_us']),n(r['connected_intercept_us'])] for r in fits])
    p('Fits use five matched sensor=VMID delays at 16 MHz: sweep period = intercept + slope x delay. The intercept extrapolates to an unmeasured zero sensor delay and does not isolate a physical phase. A full four-input address group needs four SPI words per ADC: nominal 4 us/ADC at 16 MHz, 8 us at 8 MHz. A shared-MUX pair needs 8/16 us of SPI clock time plus one shared settling wait; address 6 reads one word per ADC. VMID on adds four discarded words per ADC per address group, giving 106 words/sweep versus 50 off.',True)
    csv_write(folder/'timing_budgets.csv',budget);csv_write(folder/'timing_delay_fits.csv',fits)
    for mode in ('adc','mux'):
        page();h(mode.upper()+' - every setting: rates and noise')
        records=[]
        for r in current:
            if r['mode']!=mode:continue
            old=get(mode,r['vmid'],r['settle_us'],r['vmid_settle_effective_us'],r['requested_spi_clock_hz'],'disconnected')
            q=get(mode,r['vmid'],r['settle_us'],r['vmid_settle_effective_us'],r['requested_spi_clock_hz'],values=quiet)
            records.append(['on' if r['vmid'] else 'off',f"{r['settle_us']}/{r['vmid_settle_effective_us']}",r['requested_spi_clock_hz']//1000000,
                n(old['delivered_sweeps_s'],1),n(r['delivered_sweeps_s'],1),n(100*(r['delivered_sweeps_s']/old['delivered_sweeps_s']-1),2),
                n(r['all_sd_max_counts'])+' / '+n(r['all_sd_max_volts']*1000),n(val(q,'all_sd_max_counts'))+' / '+n(val(q,'all_sd_max_volts')*1000),
                n(r['all_ptp_max_counts'],1)+' / '+n(r['all_ptp_max_volts']*1000),
                n(val(q,'all_ptp_max_counts'),1)+' / '+n(val(q,'all_ptp_max_volts')*1000),worst(r)])
        table(['VMID','Sensor / VMID us','MHz','Disconnected Hz','Connected Hz','Rate change %','Raw SD counts / mV','Quiet SD counts / mV','Raw p-p counts / mV','Quiet p-p counts / mV','Worst SD route'],records)
        p('Worst SD and worst p-p select independent routes. Values are maxima across route medians of three repetition statistics. Quiet metrics are reported only when enough common quiet time remains; full raw metrics preserve every excursion. Every route and repetition is saved in counts and volts.',True)

    for metric,title2 in [('all_sd_max_counts','Worst channel temporal SD'),('all_ptp_max_counts','Worst channel peak-to-peak')]:
        page();h(title2+' - full and quiet windows')
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,m in enumerate(('adc','mux')):
            for j,v in enumerate((False,True)):
                ax=axes[i,j]
                for ds,col,label in [('disconnected','#777777','Disconnected'),('connected','#0072B2','Connected')]:
                    for values,style,suffix in [(raw,'-',' raw'),(quiet,'--',' quiet')]:
                        ax.plot(DELAYS,[val(get(m,v,d,dataset=ds,values=values),metric) for d in DELAYS],'o'+style,color=col,label=label+suffix,ms=3)
                ax.set(title=m.upper()+' / VMID '+('on' if v else 'off'),xlabel='Sensor = VMID delay (us)',ylabel=title2+' (counts)');ax.legend(fontsize=6)
                ax.secondary_yaxis('right',functions=(lambda x:x*LSB*1000,lambda x:x/LSB/1000)).set_ylabel('mV')
        chart(fig,'noise_'+metric);p('Solid lines retain all samples; dashed lines use the same common-mask method for both setups. No individual-channel clipping or detrending. P-p depends on observation length and sample count; each raw measurement is 15 s.',True)

    for m in ('adc','mux'):
        page();h('Connected baselines - every route / '+m.upper())
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,v in enumerate((False,True)):
            for j,array in enumerate((1,2)):
                ax=axes[i,j]
                for d,col in zip(DELAYS,COLORS):ax.plot(range(25),points(get(m,v,d),array),'.-',lw=.8,ms=3,label=str(d)+' us',color=col)
                routeaxis(ax,array);ax.set_title(f"Array {'A' if array==1 else 'B'} / VMID {'on' if v else 'off'}");ax.legend(fontsize=6,ncol=5)
        chart(fig,'baselines_'+m);p('Requested 16 MHz, equal sensor/VMID delays. External addresses 0-6; MUX1-4 feed ADC IN1-4. Lines connect discrete routes. Physical sensor labels and bias resistance are in the route tables.',True)
        page();h('Baseline shifts with sensor strips - '+m.upper())
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,v in enumerate((False,True)):
            for j,array in enumerate((1,2)):
                ax=axes[i,j]
                for d,col in zip(DELAYS,COLORS):ax.plot(range(25),points(get(m,v,d),array)-points(get(m,v,d,dataset='disconnected'),array),'.-',lw=.8,ms=3,label=str(d)+' us',color=col)
                routeaxis(ax,array,'Connected - disconnected (counts)');ax.axhline(0,color='#999999',lw=.7)
                ax.set_title(f"Array {'A' if array==1 else 'B'} / VMID {'on' if v else 'off'}");ax.legend(fontsize=6,ncol=5)
        chart(fig,'baseline_shifts_'+m);p('Same firmware, physical routes and requested settings. Sequential recordings allow environmental drift; these shifts are measured baseline changes rather than calibrated ADC offsets.',True)
    page();h('Baseline spread across routes - every setting')
    table(['Mode','VMID','Sensor / VMID us','MHz','A spread disconnected counts / mV','A spread connected counts / mV','B spread disconnected counts / mV','B spread connected counts / mV'],[
        [r['mode'].upper(),'on' if r['vmid'] else 'off',f"{r['settle_us']}/{r['vmid_settle_effective_us']}",r['requested_spi_clock_hz']//1000000,
         *[n(r[ds+'_A'+str(a)+'_baseline_spread_counts'],1)+' / '+n(r[ds+'_A'+str(a)+'_baseline_spread_counts']*LSB*1000) for a in (1,2) for ds in ('disconnected','connected')]] for r in data['comparisons']])
    p('Spread = max-min of the 25 per-route baseline medians in an array. Population SD across route baselines is also saved. These measure spatial baseline differences, separately from temporal noise SD.',True)
    page();h('Baseline centers of the four ADC inputs - 5 us / 16 MHz')
    records=[]
    for m in ('adc','mux'):
        for v in (False,True):
            for array in (1,2):
                r=next(r for r in data['adc_input_spread'] if key(r)==key(get(m,v)) and r['array']==array)
                old=next(r for r in data['adc_input_spread'] if key(r)==key(get(m,v,dataset='disconnected')) and r['array']==array)
                records.append([m.upper(),'on' if v else 'off','A' if array==1 else 'B',
                    *[n(r[f'IN{i}_counts'],1)+' / '+n(r[f'IN{i}_volts']) for i in (1,2,3,4)],
                    n(old['center_spread_counts'],1)+' / '+n(old['center_spread_volts']*1000),
                    n(r['center_spread_counts'],1)+' / '+n(r['center_spread_volts']*1000)])
    table(['Mode','VMID','Array','IN1 counts / V','IN2 counts / V','IN3 counts / V','IN4 counts / V','Disconnected center spread counts / mV','Connected center spread counts / mV'],records)
    p('An input center is the median of that ADC input\'s route baselines: six external addresses for IN1-3, seven for IN4. Center spread is max-min across the four input centers. Full setting-specific centers and within-input route spreads are in adc_input_baselines.csv and adc_input_spread.csv.',True)
    for mode in ('adc','mux'):
        page();h('Baseline spread between ADC inputs at each address - '+mode.upper())
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for i,v in enumerate((False,True)):
            for j,array in enumerate((1,2)):
                ax=axes[i,j]
                for delay,col in zip(DELAYS,COLORS):
                    setting=get(mode,v,delay)
                    rows=[r for r in data['address_input_spread'] if key(r)==key(setting) and r['array']==array]
                    ax.plot([r['external_address'] for r in rows],[r['spread_counts'] for r in rows],'o-',color=col,label=str(delay)+' us',ms=3,lw=.8)
                ax.set(title=f"Array {'A' if array==1 else 'B'} / VMID {'on' if v else 'off'}",xlabel='External MUX address',ylabel='Max-min across ADC IN1-4 (counts)',xticks=range(6))
                ax.secondary_yaxis('right',functions=(lambda x:x*LSB*1000,lambda x:x/LSB/1000)).set_ylabel('Spread (mV)');ax.legend(fontsize=6,ncol=5)
        chart(fig,'matched_address_spread_'+mode);p('Connected, 16 MHz, equal sensor/VMID delays. Every point compares four input baseline medians at the same external address. Address 6 has only IN4 and provides no four-input comparison. This baseline spread is separate from temporal SD.',True)
    for m in ('adc','mux'):
        page();h('Independent VMID waits and SPI clocks - '+m.upper())
        fig,axes=plt.subplots(2,2,figsize=(12,5.3),constrained_layout=True)
        for j,array in enumerate((1,2)):
            for vd,col in zip((0,1,5),COLORS):axes[0,j].plot(range(25),points(get(m,True,vd=vd),array),'.-',color=col,label=f'VMID {vd} us',lw=.8)
            routeaxis(axes[0,j],array);axes[0,j].set_title(f'Array {array} / VMID on / sensor 5 us / 16 MHz');axes[0,j].legend(fontsize=6)
            for v,style in [(False,'-'),(True,'--')]:
                for clock,col in zip((8000000,16000000),COLORS):axes[1,j].plot(range(25),points(get(m,v,clock=clock),array),'.'+style,color=col,label=f"{clock//1000000} MHz / VMID {'on' if v else 'off'}",lw=.8)
            routeaxis(axes[1,j],array);axes[1,j].set_title(f'Array {array} / sensor = VMID 5 us');axes[1,j].legend(fontsize=6,ncol=2)
        chart(fig,'extras_'+m);p('Connected full-window baseline medians. Explicit zero VMID wait preserves switching guards and discarded VMID conversions. Requested SCLK was not probed; conversion and settling history change with clock.',True)

    page();h('Cold-start capture and initial-condition coverage')
    p('The user unplugged and reconnected USB; sensors discharge through leakage to ground when disconnected from VMID. Firmware parks the MUX banks on VMID until host-triggered acquisition. All startup attempts use MUX/LPSPI, requested 16 MHz, sensor settling 5 us, VMID off, 60 s, with no warm-up samples discarded. No firmware upload or reset was added between physical power-on and capture.')
    p('VMID off omits the between-group VMID address visits. The op-amp bias resistors remain tied to VMID, so selected sensors can charge toward baseline. Mandatory start/stop parking on address 7 also remains active.',True)
    table(['Attempt','Documented initial condition','Measured B/3 to 2B/3 intervals','Received / USB discarded sweeps'],[
        [name,c['session']['setup']['initial_condition'],str(c['measured_intervals'])+' / 50',f"{c['run']['frames']:,} / {c['run']['usb_frames_discarded']}"] for name,c in startup],[135,383,120,120])
    p('Detailed curves below use '+best_name+'. Time zero is the first recorded sweep, not the USB power-on edge. Routes reading at/above a threshold at the first sample are left-censored; their missing times are not extrapolated or set to zero. Power-off duration alone does not establish complete discharge. The two-thirds crossing may be visible even if the one-third crossing is missed.')
    measured=[r['one_third_to_two_thirds_s'] for r in charge['channels'] if r['one_third_to_two_thirds_s'] is not None]
    p('Observed full intervals: '+str(len(measured))+'/50'+(f"; range {min(measured)*1000:.3f}-{max(measured)*1000:.3f} ms." if measured else '. No full intervals were observed.'))
    page();h('Charging measurement method and limits')
    p('B is the median final 10 s baseline of each route. Thresholds are absolute B/3 and 2B/3, not fractions of the change from its initial value. Each crossing must remain reached for 2 ms, with no gap over three median periods in that confirmation interval. Linear interpolation uses adjacent observed sweeps only. Near baseline is the first 2 s interval inside max(1% B, 3 tail SD, 2 counts); the stricter noise-band arrival uses max(3 tail SD, 2 counts). These are operational tolerances, not exact analog equilibrium.',True)
    p('Within a sweep, channels are sampled at different instants; only sweep-start timestamps are available. Crossing times and rapid rises have roughly one-sweep timing resolution; interpolation does not create additional physical precision. The curves are sampled ADC responses and may include analog settling and conversion-history effects. Effective charge timing depends on the address duty cycle; it is not a directly measured continuous-RC time constant.',True)
    p('The firmware does not scan sensor addresses before run; both banks are parked on VMID during configuration and selector initialization. In shared-MUX scanning, ADC A is read before B, so B receives additional analog dwell during A\'s transfer. The high first readings on A changed little between the one-minute and twelve-minute power-off attempts. They do not by themselves prove retained sensor charge. Residual charge and the first switched analog/ADC response cannot be separated without a direct voltage probe or another startup acquisition design.',True)
    initial_rows=[]
    for name,c in startup:
        for array in (1,2):
            ch=[r for r in c['channels'] if r['array']==array]
            initial_rows.append([name,'A' if array==1 else 'B',
                n(min(r['initial_counts'] for r in ch),0)+' - '+n(max(r['initial_counts'] for r in ch),0),
                n(min(r['initial_counts'] for r in ch)*LSB)+' - '+n(max(r['initial_counts'] for r in ch)*LSB),
                sum(r['one_third_to_two_thirds_s'] is not None for r in ch),
                n(max(abs(r['tail_half_drift_counts']) for r in ch),1)])
    table(['Attempt','Array','First readings counts','First readings V','Observed intervals / 25','Max tail median drift counts'],initial_rows,[175,55,135,130,130,133])
    p('Tail drift compares medians of the two five-second halves of the final ten seconds. A stable tail validates the observed final reference for that capture; it does not identify the physical origin of the first switched response.',True)
    for array in (1,2):
        page();h(f"Startup - every route / Array {'A' if array==1 else 'B'}")
        table(['Route MUX/address','Sensor/channel','Bias kOhm','Initial V','Final B V','B/3 time ms','2B/3 time ms','Interval ms','Near B time ms','Noise band time ms'],[
            [f"M{r['mux']}/{r['route'].split(':')[-1]}",r['sensor']+'/'+str(r['sensor_channel']),r['bias_ohms']//1000,n(r['initial_counts']*LSB),n(r['baseline_volts']),
             n(r['t_one_third_s']*1000) if r['t_one_third_s'] is not None else r['one_third_status'].replace('_',' '),
             n(r['t_two_thirds_s']*1000) if r['t_two_thirds_s'] is not None else r['two_thirds_status'].replace('_',' '),
             n(r['one_third_to_two_thirds_s']*1000) if r['one_third_to_two_thirds_s'] is not None else '-',
             n(r['first_two_seconds_within_baseline_s']*1000) if r['first_two_seconds_within_baseline_s'] is not None else '-',
             n(r['first_two_seconds_within_noise_band_s']*1000) if r['first_two_seconds_within_noise_band_s'] is not None else '-'] for r in charge['channels'] if r['array']==array])
        p('Left censored = at/above threshold before the first recorded value. Counts, voltage thresholds, tail SD and tail drift are retained in charge_channels.csv for each startup attempt.',True)
        for sensor in sorted({r.sensor for r in ROUTES if r.array==array}):
            page();h(f"Charging curves - Array {'A' if array==1 else 'B'} / PZT{sensor}")
            indices=[i for i,r in enumerate(ROUTES) if r.array==array and r.sensor==sensor]
            fig,axes=plt.subplots(1,3,figsize=(12,4.2),constrained_layout=True)
            for i,col in zip(indices,COLORS):
                r=ROUTES[i];record=charge['channels'][i];b=record['baseline_counts'];early=ct<=.01
                label=f'ch {r.sensor_channel} (M{r.mux}/{r.input})'
                axes[0].plot(ct[early]*1000,cx[early,i]*LSB,'.-',color=col,label=label,lw=.8,ms=2)
                axes[0].axhline(b*LSB,color=col,lw=.6,ls=':',alpha=.5)
                tail=ct<=.2
                axes[1].plot(ct[tail]*1000,cx[tail,i]-b,color=col,label=label,lw=.7)
                axes[2].plot(ct*1000,cx[:,i]/b,color=col,label=label,lw=.8)
            axes[0].set(xlabel='Time from first sweep (ms)',ylabel='ADC voltage (V)',title='Raw first 10 ms',xlim=(0,10))
            axes[0].secondary_yaxis('right',functions=(lambda x:x/LSB,lambda x:x*LSB)).set_ylabel('Counts')
            axes[1].set(xlabel='Time from first sweep (ms)',ylabel='Deviation from final B (counts)',title='Tail: first 200 ms / +/-6 counts',ylim=(-6,6),xlim=(0,200))
            for level in (-2,0,2):axes[1].axhline(level,color='#888888',lw=.7,ls='--')
            axes[2].set_xscale('symlog',linthresh=.1);axes[2].set(xlabel='Time from first sweep (ms; symlog)',ylabel='Measured value / own final B',title='Full 60 s: normalized to final B')
            for fraction in (1/3,2/3,1):axes[2].axhline(fraction,color='#888888',lw=.7,ls='--')
            for ax in axes:ax.legend(fontsize=5.5)
            chart(fig,f'charge_A{array}_PZT{sensor}',265)
            table(['Channel / route','Initial counts','Final B counts / V','B/3 time ms','2B/3 time ms','Interpolated interval ms','Sample bracket ms'],[
                [str(ROUTES[i].sensor_channel)+' / '+str(ROUTES[i]),n(charge['channels'][i]['initial_counts'],0),n(charge['channels'][i]['baseline_counts'],0)+' / '+n(charge['channels'][i]['baseline_volts']),
                 *[n(charge['channels'][i][field]*1000) if charge['channels'][i][field] is not None else '-' for field in ('t_one_third_s','t_two_thirds_s','one_third_to_two_thirds_s')],
                 n(charge['channels'][i]['interval_lower_s']*1000)+' - '+n(charge['channels'][i]['interval_upper_s']*1000) if charge['channels'][i]['interval_lower_s'] is not None else '-'] for i in indices])
            p('Dots show every early recorded sweep. Long plateau curves are decimated only for display; all measurements use original raw samples. Dashed normalized lines mark B/3, 2B/3 and B. The interval bracket uses the observed samples surrounding each crossing and shows the sampling limit; interpolated digits do not imply equivalent timing accuracy. Missing crossings remain blank.',True)

    noise_path=folder/'selected_noise_current.json'
    page();h('Rare excursions - maximum across individual recordings')
    table(['Mode','VMID','Sensor / VMID us','MHz','Disconnected max p-p counts / mV','Connected max p-p counts / mV','Connected max SD counts / mV','Peak route','Peak repetition'],[
        [r['mode'].upper(),'on' if r['vmid'] else 'off',f"{r['settle_us']}/{r['vmid_settle_effective_us']}",r['requested_spi_clock_hz']//1000000,
         n(r['disconnected_max_ptp_counts'],1)+' / '+n(r['disconnected_max_ptp_volts']*1000),
         n(r['connected_max_ptp_counts'],1)+' / '+n(r['connected_max_ptp_volts']*1000),
         n(r['connected_max_sd_counts'])+' / '+n(r['connected_max_sd_volts']*1000),r['connected_peak_route'],r['connected_peak_repetition']] for r in data['excursion_audit']])
    p('For each setting, these are the largest single-record p-p and SD across every route and all three 15 s repetitions. The peak route/repetition identifies the p-p maximum; the SD maximum can occur elsewhere. These values preserve rare excursions. All-sample traces and peak-centered zooms accompany the spectral figures.',True)
    if noise_path.exists():
        noise=json.loads(noise_path.read_text());page();h('Temporal noise and PSD - all-route pattern screen')
        p(noise['method']);p(f"{len(noise['selected'])} of {noise['checked_route_settings']} route/settings meet the descriptive screen. This screen detects repeatable temporal structure; it is not a calibrated white-noise hypothesis test. Routes below 0.5 count SD can still be correlated.")
        if noise['selected']:
            table(['Delay us','Array/MUX/address','Sensor/channel','Raw SD counts / mV','Pattern repetitions'],[
                [r['settle_us'],r['route'],r['sensor']+'/'+str(r['sensor_channel']),n(r['sd_counts'])+' / '+n(r['sd_volts']*1000),r['pattern_repeats']] for r in noise['selected']])
        below=noise.get('correlated_below_threshold',[])
        p(f"{len(below)} further route/settings meet the correlation screen while their median SD stays at/below 0.5 count. Every one is listed and plotted below; these are the complete cases from the 100-route-setting audit, rather than illustrative examples.")
        if below:
            table(['Delay us','Array/MUX/address','Sensor/channel','Raw SD counts / mV','Median lag-1 ACF','Pattern repetitions'],[
                [r['settle_us'],r['route'],r['sensor']+'/'+str(r['sensor_channel']),n(r['sd_counts'])+' / '+n(r['sd_volts']*1000),n(r['lag1_acf']),r['pattern_repeats']] for r in below])
        for item in noise['figures']:
            page();h(item['title']);story.append(Image(str(folder/item['path']),width=width,height=item['height']));p(item['caption'],True)
    page();h('Setup, methods and verification')
    table(['Array','MUX1 / ADC IN1','MUX2 / ADC IN2','MUX3 / ADC IN3','MUX4 / ADC IN4'],[['A','470 kOhm','1 MOhm','470 kOhm','1 MOhm'],['B','1 MOhm','470 kOhm','1 MOhm','470 kOhm']])
    p('Both sensor strips remain connected and untouched during all steady-state recordings. Vibration was not instrumented. Five mapped PZT groups per array have five route channels each; connector mapping labels are preserved from the manifest. No known-input stimulation was applied to independently verify wiring.')
    p('Each array reads MUX1-4 at external addresses 0-5, and MUX4 alone at address 6: 25 useful routes per array, 50 total. External address 7 is VMID. Sensor channel numbers in the charging tables are connector channel labels; ADC inputs 1-4 correspond to MUX1-4.',True)
    p('Unfiltered sample SD uses ddof=1; p-p is max-min; baseline is the median. Quiet windows use 50 ms bins and median channel SD in eight array/MUX groups. Threshold max(0.6 counts, P20+0.15, lower40% median+4 x 1.4826 x MAD); reject exceeding bins and neighboring bins. The second stage rejects coherent separated secondary states in at least two routes plus adjacent sweeps. It preserves unimodal tails and isolated route variation. Qualified quiet records require at least 1 s and 100 retained frames. Common masks are applied identically to every route; raw statistics expose rejected events.',True)
    p('Per-setting statistics are medians of the three per-route repetition statistics. Worst-route SD and p-p are independent maxima across those medians. Zero digital SD indicates identical recorded codes, not zero analog noise. Bursts, spikes and baseline drift can contribute to unfiltered SD; data alone do not identify their physical cause.',True)
    table(['Dataset','Captures','Quiet qualified','Transfer / data / USB-write errors'],[[ds,len([r for r in data['rows'] if r['dataset']==ds]),sum(r['quiet_qualified'] for r in data['rows'] if r['dataset']==ds),sum(r[k] for r in data['rows'] if r['dataset']==ds for k in ('transfer_errors','data_errors','usb_write_errors'))] for ds in ('disconnected','connected')])
    p('Each raw stream passes SHA-256, independent binary decoders, 12-bit bounds, status/route/settings checks, nonoverlapping device timestamps and transport accounting. Received = sent; acquired = sent + intentional complete-sweep USB discards. Production firmware was not uploaded or changed for this experiment. Original settings restored; final running='+data['lifecycle']['final_production_status']['running']+'. Source and local HEX snapshots are archived; no MCU flash readback was performed.',True)
    p(f"Frozen local source/HEX snapshot hashes match the disconnected reference for all {len(data['local_firmware_snapshot_matches'])} files. Runtime revision and selector/prime policy were also verified. This is not an MCU flash readback.",True)
    page();h('Saved results and reproduction')
    p('Connected raw root: '+data['raw_root']);p('Disconnected reference analysis: '+data['reference_analysis'])
    table(['Files','Contents'],[['analysis.json / connected_analysis_cache.json','Hash-bound connected captures and same-firmware reference results'],['summary.csv / channel_summary.csv / channel_repetitions.csv','Common quiet-window setting, route and repetition measurements'],['unfiltered_*','All-sample rates, baseline, SD, p-p and repetition correlations'],['setup_comparisons.csv / channel_comparisons.csv','All 28 exact-setting comparisons and all 1,400 route differences, counts and volts'],['excursion_audit.csv','Largest single-record SD and p-p for every setting, including rare spikes'],['cold_poweron*/charge_channels.csv','Every startup route, threshold coverage, initial/final levels and warm-up timing'],['selected_noise_audit.csv / noise_psd.csv / noise_psd_coverage.csv','All 100 route/settings screened and plotted PSD data/coverage'],['figures/','Rate/noise curves, every-route baseline line graphs and all sensor charging curves']],[260,498])
    p('Reproduce with benchmark_adc124_connected.py (startup before matrix), analyze_adc124_charge.py (each startup stage), analyze_adc124_connected.py, noise_adc124_connected.py and report_adc124_connected.py in Arduino_Sketches/TestBoard_ADC124/benchmarks. A cold-start rerun requires physical sensor discharge, not just starting acquisition. No commit or push was made.',True)
    def footer(canvas,doc):
        canvas.saveState();canvas.setFont('Helvetica',7);canvas.drawString(42,22,'ADC124 | LPSPI | connected PZT | 2026-10-14');canvas.drawRightString(800,22,str(doc.page));canvas.restoreState()
    pdf.parent.mkdir(parents=True,exist_ok=True)
    SimpleDocTemplate(str(pdf),pagesize=landscape(A4),leftMargin=42,rightMargin=42,topMargin=35,bottomMargin=36,title=title).build(story,onFirstPage=footer,onLaterPages=footer)
    (folder/'README.md').write_text('# TestBoard_ADC124 - connected PZT experiment\n\n'
        'Completed 28-setting LPSPI matrix, three repetitions per setting, compared against the latest same-firmware disconnected captures. '
        'Both arrays, manual repeat 1, burst SPI. Baseline plots are line graphs.\n\n'
        f'[PDF report](../../../../reports/exports/{pdf.name})\n\n'
        'Unfiltered and common quiet-window baseline/noise data are retained in counts and volts. '
        'Cold-start attempts and every route\'s threshold coverage are in the startup subfolders. '
        'A missing one-third crossing is censored, never extrapolated. Time zero is first recorded sweep, not USB power edge.\n\n'
        'Firmware unchanged; original settings restored and acquisition stopped. See analysis.json, setup_comparisons.csv, channel_comparisons.csv, '
        'selected_noise_audit.csv and figures/.\n',encoding='utf-8')
    print('REPORT',pdf,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('folder',type=Path);p.add_argument('pdf',type=Path)
    a=p.parse_args();build(a.folder.resolve(),a.pdf.resolve())
