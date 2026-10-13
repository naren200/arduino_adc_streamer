# COM4 ADC124 quick hardware check

Date: 2026-10-13. Teensy 4.1 on COM4, current protocol-2 production firmware.
Sensor strips were not connected. MUX drain outputs connected to unity buffers
with the board bias resistors to Vmid. Configured reference: 3.3 V; nominal
Vmid: 1.65 V. Each capture used 200 ms warm-up plus 500 ms measurement, with
one repetition. The complete numeric summary is in
[`quick_summary_2026-10-13.json`](quick_summary_2026-10-13.json).

The board responded across both arrays, all three SPI engines, all three scan
orders, and requested clocks of 8/16 MHz. At 500 us settling, all 50 channel
means were 1.649974–1.650832 V. Fast settings showed stable low offsets and
some accounted USB-busy discards. This is initial communication/Vmid readiness
evidence; sensor response, stimulated channel identity, a speed comparison with
the earlier firmware, and longer stability testing remain for later sessions.

25 short captures: {'WARN': 8, 'PASS': 17}.
Received 58,049 complete frames including warm-up; analyzed 1,405,300 sensor samples.
No malformed frames, missing transmitted frames, ADC transfer/data errors, or USB write errors.
Maximum measured channel standard deviation: 0.503 ADC counts.

## Captures

| Session | Configuration | Result | Channel mean range (V) | Measured frames | USB discards (%) |
| --- | --- | --- | --- | ---: | ---: |
| smoke | array1__mux__blocking__spi16000000__repeat1__settle5__vmidoff | WARN | 1.5926–1.6524 | 3958 | 19.01 |
| smoke | array1__mux__dma__spi16000000__repeat1__settle5__vmidoff | PASS | 1.5970–1.6532 | 3253 | 0.00 |
| smoke | array1__mux__lpspi__spi16000000__repeat1__settle5__vmidoff | WARN | 1.5920–1.6524 | 4804 | 7.39 |
| smoke | array2__mux__blocking__spi16000000__repeat1__settle5__vmidoff | WARN | 1.5298–1.6234 | 5218 | 0.00 |
| smoke | array2__mux__dma__spi16000000__repeat1__settle5__vmidoff | PASS | 1.5710–1.6419 | 3253 | 0.00 |
| smoke | array2__mux__lpspi__spi16000000__repeat1__settle5__vmidoff | WARN | 1.5259–1.6210 | 5300 | 0.83 |
| smoke | arrayboth__mux__blocking__spi16000000__repeat1__settle5__vmidoff | WARN | 1.5286–1.6478 | 2476 | 0.00 |
| smoke | arrayboth__mux__dma__spi16000000__repeat1__settle5__vmidoff | PASS | 1.5701–1.6516 | 1574 | 0.00 |
| smoke | arrayboth__mux__lpspi__spi16000000__repeat1__settle5__vmidoff | WARN | 1.5244–1.6476 | 2543 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat1__settle5__vmidoff | WARN | 1.5244–1.6476 | 2545 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat1__settle5__vmidon | WARN | 1.5614–1.6242 | 1324 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat3__settle5__vmidoff | PASS | 1.5735–1.6524 | 1411 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat3__settle5__vmidon | PASS | 1.5839–1.6460 | 935 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat1__settle50__vmidoff | PASS | 1.6217–1.6524 | 545 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat1__settle50__vmidon | PASS | 1.6194–1.6508 | 305 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat3__settle50__vmidoff | PASS | 1.6247–1.6532 | 465 | 0.00 |
| settling | arrayboth__mux__lpspi__spi16000000__repeat3__settle50__vmidon | PASS | 1.6226–1.6516 | 278 | 0.00 |
| scan_orders | arrayboth__channel__blocking__spi8000000__repeat3__settle50__vmidon | PASS | 1.6218–1.6508 | 80 | 0.00 |
| scan_orders | arrayboth__channel__dma__spi8000000__repeat3__settle50__vmidon | PASS | 1.6242–1.6508 | 70 | 0.00 |
| scan_orders | arrayboth__channel__lpspi__spi8000000__repeat3__settle50__vmidon | PASS | 1.6218–1.6508 | 80 | 0.00 |
| scan_orders | arrayboth__interleaved__blocking__spi8000000__repeat3__settle50__vmidon | PASS | 1.6218–1.6508 | 79 | 0.00 |
| scan_orders | arrayboth__interleaved__dma__spi8000000__repeat3__settle50__vmidon | PASS | 1.6242–1.6508 | 70 | 0.00 |
| scan_orders | arrayboth__interleaved__lpspi__spi8000000__repeat3__settle50__vmidon | PASS | 1.6218–1.6508 | 80 | 0.00 |
| long_settling | arrayboth__mux__lpspi__spi16000000__repeat1__settle100__vmidoff | PASS | 1.6363–1.6516 | 291 | 0.00 |
| long_settling | arrayboth__mux__lpspi__spi16000000__repeat1__settle500__vmidoff | PASS | 1.6500–1.6508 | 62 | 0.00 |

Expected nominal Vmid is 1.65 V using the configured fixed 3.3 V reference. Sensor strips were disconnected.
Fast 5 us/repeat-1 captures show a stable low offset, especially array 2 MUX1/MUX3. Longer settling and repeats reduce the observed offset. These data alone do not identify its electrical cause.
USB-busy omissions are whole sweeps explicitly reported by the firmware, distinct from missing transmitted frames. CSV logging can contribute to USB saturation.
Raw data, sample CSVs, channel statistics, command/status logs and Excel workbooks are in each session directory.

Board left stopped, with its original full-array/blocking/16 MHz/repeat-1/5 us/Vmid-off configuration restored.
