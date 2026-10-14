"""Count acquisition operations and fit rate-vs-settling timing models."""
import numpy as np

def operation_counts(order, vmid):
    if order not in ('channel', 'mux'):
        raise ValueError('Timing model supports full two-array channel/mux orders')
    groups = 50 if order == 'channel' else 14
    address_changes = 2 * groups if vmid else groups + 2
    words = 50 + groups + (5 * groups if vmid else 0)
    return dict(sensor_values=50, address_groups=groups,
                sensor_address_changes=groups, mandatory_park_changes=0 if vmid else 2,
                vmid_park_changes=groups if vmid else 0,
                address_changes=address_changes, gpio_writes=address_changes*5,
                prime_words=groups, sensor_words=50, vmid_words=5*groups if vmid else 0,
                words=words, spi_clocks=16*words, frame_bytes=114,
                switch_allowance_us=.4*address_changes,
                cs_allowance_us=.2*words, wire_clock_us=words*1.)

def fit_delay(settles, periods):
    """Least squares descriptive slope/intercept, not isolated phase timing."""
    x=np.asarray(settles,dtype=float);y=np.asarray(periods,dtype=float)
    if len(np.unique(x))<2:
        raise ValueError('Need multiple settling delays')
    slope,intercept=np.polyfit(x,y,1)
    residual=y-(slope*x+intercept)
    return dict(slope_us_per_us=float(slope),intercept_us=float(intercept),
                residual_rms_us=float(np.sqrt(np.mean(residual**2))),
                residual_max_us=float(np.max(np.abs(residual))))
