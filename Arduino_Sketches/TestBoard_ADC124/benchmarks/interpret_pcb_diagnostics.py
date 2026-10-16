"""Derive explicit numeric comparisons for the diagnostic report."""
import json
import numpy as np
from analyze_pcb_diagnostics import OUT,LSB,csv_write

def derive(out=OUT):
    x=json.loads((out/'analysis.json').read_text());assert not x['partial']
    c=x['channels'];sp=x['mux_spreads']
    def row(name,a,m,addr=2,r=0):
        rr=[z for z in c if z['name']==name and z['array']==a and z['mux']==m and z['address']==addr and z['repeat']==r]
        assert len(rr)==1,(name,a,m,addr,r,len(rr))
        return rr[0]
    def v(name,a,m,addr=2,r=0,field='baseline_counts'):return row(name,a,m,addr,r)[field]
    def center(name,a,m):return next(r['baseline_center_counts'] for r in sp if r['name']==name and r['array']==a and r['mux']==m)
    recovery=[]
    for first in c:
        if first['family']!='recovery' or not first['name'].endswith('_eight') or first['repeat']!=0:continue
        name=first['name'];a=first['array'];m=first['mux'];last=v(name,a,m,r=7)
        wait=v(name.replace('_eight','_wait'),a,m)
        later=v(name.replace('_eight','_wait_plus'),a,m)
        recovery.append(dict(name=name,array=a,mux=m,first=first['baseline_counts'],eighth=last,wait=wait,wait_plus=later,
            gain_counts=last-first['baseline_counts'],eighth_minus_later_counts=last-later,
            outside_wait_bracket_counts=max(last-max(wait,later),min(wait,later)-last,0)))
    retention=[]
    for a in (1,2):
        for m in range(1,5):
            short=v(f'leak_b{a-1}_m{m-1}_1000',a,m);long=v(f'leak_b{a-1}_m{m-1}_1000000',a,m)
            retention.append(dict(array=a,mux=m,first_1ms=short,first_1s=long,change_counts=long-short,
                change_mv=(long-short)*LSB*1000,rep_range_1s=row(f'leak_b{a-1}_m{m-1}_1000000',a,m)['baseline_rep_range_counts']))
    reference=[]
    for r in c:
        if r['family']=='refburst' and r['repeat']==0 and r['name'].endswith('_n8'):
            last=v(r['name'],r['array'],1,7,7,'mean_counts')
            reference.append(dict(name=r['name'],array=r['array'],arrays=r['arrays'],first_bank=r['first_bank'],first_mean=r['mean_counts'],eighth_mean=last,
                change_counts=last-r['mean_counts']))
    discard=[]
    for family,delays in (('reset_control',(1,5,50)),('reset_short',(1,5))):
        for delay in delays:
            for a in (1,2):
                for m in range(1,5):
                    no=center(f'{family}_vmid_{delay}',a,m);yes=center(f'{family}_vmid_adc_{delay}',a,m)
                    discard.append(dict(family=family,delay=delay,array=a,mux=m,no_adc_center=no,adc_center=yes,change_counts=yes-no))
    readorder=[]
    for delay in (1,5,50):
        for a in (1,2):
            for m in range(1,5):
                aa=center(f'readorder_{delay}_banks3_first0_order228',a,m)
                bb=center(f'readorder_{delay}_banks3_first1_order228',a,m)
                rev=center(f'readorder_{delay}_banks3_first0_order27',a,m)
                readorder.append(dict(delay=delay,array=a,mux=m,A_first=aa,B_first=bb,reversed_inputs=rev,
                    bank_order_change_counts=bb-aa,input_order_change_counts=rev-aa))
    fast=[]
    for name,base in (('fast_order_full_descending','fast_full_5'),('fast_order_full_rotated','fast_full_5'),('fast_order_core_gray','fast_core_5')):
        for r in c:
            if r['name']==name and r['address']<6:
                old=v(base,r['array'],r['mux'],r['address'])
                fast.append(dict(name=name,array=r['array'],mux=r['mux'],address=r['address'],baseline=r['baseline_counts'],
                    ascending_baseline=old,change_counts=r['baseline_counts']-old))
    for name,data in [('recovery_comparisons',recovery),('retention_comparisons',retention),('reference_burst_comparisons',reference),
        ('vmid_adc_comparisons',discard),('readorder_comparisons',readorder),('fast_order_comparisons',fast)]:csv_write(out/(name+'.csv'),data)
    ac=[r['change_counts'] for r in retention if r['array']==1];bc=[r['change_counts'] for r in retention if r['array']==2]
    refmax=max(abs(r['change_counts']) for r in reference)
    gain=[r['gain_counts'] for r in recovery]
    shortmax=max(abs(r['change_counts']) for r in discard if r['family']=='reset_short')
    longmax=max(abs(r['change_counts']) for r in discard if r['family']=='reset_control')
    bankmax=max(abs(r['bank_order_change_counts']) for r in readorder)
    excursions={name:max(r['ptp_counts'] for r in c if r['name']==name) for name in ('long_scan','long_scan_vmid','long_held')}
    ref_second=[v('refburst_banks3_first1_n8',1,1,7,field='mean_counts')-v('refburst_banks3_first0_n8',1,1,7,field='mean_counts'),
        v('refburst_banks3_first0_n8',2,1,7,field='mean_counts')-v('refburst_banks3_first1_n8',2,1,7,field='mean_counts')]
    paragraphs=[
        f'Extending the powered EN-disabled interval from 1 ms to 1 s changes Array A first-reconnection codes by {min(ac):+.1f} to {max(ac):+.1f} counts, '
        f'versus {min(bc):+.1f} to {max(bc):+.1f} counts on B. This is a strong array-dependent retention/reconnection effect; it does not separately identify sensor, MUX or PCB leakage.',
        f'In sensor recovery tests, the eighth conversion differs from the first by {min(gain):.1f} to {max(gain):.1f} counts. '
        f'The reference-only burst changes its mean by at most {refmax:.3f} count with no external or ADC-input switching. '
        f'Reading a bank second raises its first mean code by {min(ref_second):.3f} to {max(ref_second):.3f} counts. '
        'A shared conversion-related analog effect therefore contributes; sensor settling alone cannot explain all burst recovery. '
        'Reference/supply ratio disturbance is a candidate, not a measured rail-voltage diagnosis.',
        f'Adding discarded ADC conversions during an otherwise equal VMID connection changes median MUX baseline centers by at most {longmax:.1f} count with 50 us reference slots '
        f'and {shortmax:.1f} count with 20 us slots. These are controlled diagnostic schedules; no optimized production speed gain was measured. '
        f'Swapping which ADC bank is read first changes centers by up to {bankmax:.1f} counts in the matched-slot tests.',
        f'The long unreset scan contains a valid-frame burst and baseline step. Maximum route peak-to-peak ranges are {excursions["long_scan"]:.0f} counts '
        f'({excursions["long_scan"]*LSB*1000:.1f} mV) without reset, {excursions["long_scan_vmid"]:.0f} counts '
        f'({excursions["long_scan_vmid"]*LSB*1000:.1f} mV) with VMID reset, and {excursions["long_held"]:.0f} counts held. '
        'The reset record still contains an isolated spike, despite lower sustained variance. '
        'Sequential recordings without vibration monitoring cannot establish the source or prove a mode suppresses all events. '
        'Removing address 6 is a diagnostic intervention: it also removes the seventh MUX4 sensor, so it is not a full-50-channel production fix.'
    ]
    result=dict(paragraphs=paragraphs,retention_A_change_range=ac,retention_B_change_range=bc,
        reference_max_mean_change_counts=refmax,recovery_gain_range=[min(gain),max(gain)],
        reference_second_bank_start_changes=ref_second,
        long_record_max_ptp_counts=excursions,
        vmid_ADC_max_center_change_short=shortmax,vmid_ADC_max_center_change_long=longmax,bank_order_max_center_change=bankmax)
    (out/'interpretation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
    return result

if __name__=='__main__':derive()
