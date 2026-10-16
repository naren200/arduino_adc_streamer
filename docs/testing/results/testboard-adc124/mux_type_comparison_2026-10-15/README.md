# Array A / B MUX interpretation

Descriptive reanalysis only; no new hardware captures. Source: sensors_connected_2026-10-14/analysis.json.

Baseline panel: median of 12 route baselines per resistor class, restricted to external addresses 0-5 for equal weighting. This matches resistor values but not physical ADC input, buffer, or sensor identity. Nominal VMID = 2048 counts. Noise panel: median and maximum across all 25 per-route median-of-three temporal SDs per array; raw samples, no quiet mask.

The two MUX types are inseparable from array, ADC and board location in this dataset. Scan order, capacitance, bias resistance, buffering and ADC acquisition remain coupled.

Hardware clarification from user: both MUX banks run from the 3.3 V LDO. Each MUX output is buffered by an OPA365 before its ADC input. Thus the ADC sampling capacitance must not be directly included in a simple bias-resistor/MUX-node RC estimate.

Primary references used for interpretation:
- https://www.ti.com/lit/ds/symlink/tmux1108.pdf
- https://www.ti.com/lit/ds/symlink/tmux1308a.pdf
- https://www.ti.com/lit/ds/symlink/opa365.pdf
- https://www.ti.com/lit/pdf/SCDA054

The OPA365 specifies 0.2 pA typical / 10 pA maximum input bias at 25 C; even 10 pA through 1 Mohm is only 10 uV. Its 200 uV maximum input offset at 25 C is also much smaller than the tens of mV array baseline differences. These static specifications do not rule out dynamic ADC-drive or layout effects.

Additional user hardware information: REF2033 supplies ADC VREF = 3.3 V and VMID = 1.65 V. There is a 100 nF capacitor near the REF2033 VMID pad. The REF2033 is on the other side of the PCB from the MUX circuitry. Separately, each MUX VMID input has a 150 pF capacitor to ground (corrected from the user's initial 100 pF value), physically very close to that MUX input pad, and a nearby 100 ohm isolation resistor. These are distinct capacitors at different locations.

Correction: the user rejected the earlier drawing. Do not retain the assumed ordering REF2033 -> 150 pF node -> 100 ohm -> MUX as a confirmed electrical topology. Physical proximity and the phrase "after the capacitor" do not establish which side of the resistor the capacitor connects to; exact net connectivity requires clarification or a schematic. Treat the 150 pF as local MUX-input decoupling and the 100 nF as reference-output decoupling when discussing placement, without inventing resistor/capacitor net order.

The local 150 pF can supply/absorb fast switching charge near the MUX rather than relying only on the remote reference capacitor. It is not a permanent 150 pF shunt at the common MUX output during sensor selections. A hypothetical 1 pC charge deposited solely on 150 pF changes its voltage by 6.67 mV before recharge; this is an illustration, not a prediction of measured ADC noise. Actual settling involves reference and trace impedance, resistor/capacitor connectivity, MUX switching, other capacitances, and buffer/ADC behavior. REF2033 source: https://www.ti.com/lit/pdf/sbos600

Important later clarification: in BOTH arrays, address-6 pins of MUX1-3 are directly connected to their address-7 VMID pins, including the local charge-absorbing capacitor network. Address 6 is a sensor only on MUX4. Thus software VMID off does NOT mean absence of all reference selections: MUX1-3 select VMID at address 6 once per sweep, despite no useful ADC reading of those paths there. MUX4 does not receive that periodic reset. Address 0 on MUX1-3 is consequently the first sensor selection after a VMID visit in either software VMID mode. This corrects the earlier suggestion that unused address-6 electrical states were unknown.

Cross-check from raw_channels in the connected analysis: MUX scan, LPSPI, 16 MHz, sensor/VMID settling 5 us, baseline counts at address 0 (off / on): A1 2012/2014, A2 1994/1995, A3 2002/2003; B1 1963/1961, B2 2000/1999, B3 1974/1972. All six differences are <=2 counts. A4 is 2046/2007 and B4 is 1993/2004, with different address-6 histories. The address-0 agreement supports a repeatable first-post-VMID operating condition; subsequent upward evolution on A and downward evolution of B's 1 Mohm paths supports a scan-history contribution. It does not isolate charge-injection polarity from other settling or ADC history effects. The ADC-mode address-0 check also agrees within 3 counts for all six MUX1-3 paths.
