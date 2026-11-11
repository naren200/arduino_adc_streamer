# TestBoard_ADC124 capacitance-test firmware and wire protocol

Implemented 2026-10-23. Code: [`Arduino_Sketches/TestBoard_ADC124_CapTest/`](../../../Arduino_Sketches/TestBoard_ADC124_CapTest/) (firmware). PC application: [`CapTest/`](../../../CapTest/) (shared with the other CapTest boards; this board's driver is `captest/boards/testboard_adc124.py`). Sibling design: [`TESTBOARD_7953_CAPTEST.md`](TESTBOARD_7953_CAPTEST.md). Plan: [`plan-testboard-adc124-capacitance-test.prompt.md`](../../plans/firmware/plan-testboard-adc124-capacitance-test.prompt.md).

## Purpose

Measure, per PZT sensor channel of TestBoard_ADC124 (two arrays x 25 channels), the total capacitance `C_total` at the channel node (PZT + trace + mux pin + the shared mux-output "D" node of that mux) and the ratio `C_D / C_total`, from the charge-up curve after a sensor is connected to a parked D node. The charge-up time constant is `tau = R_bias x C_total`.

**Difference to TestBoard_7953.** There the ADC's own command word switches the mux, so the connection instant has to be inferred from ADC frames. Here the mux is switched by Teensy GPIO (EN and A0..A2), so the firmware *controls and time-stamps* the connection instant, and the ADC only has to read the node continuously. A connection at address X connects input X of **all four muxes of the array** at once; one ADC input is converted per frame, so one burst can read one to four muxes at the same address.

## Separation from production firmware

The capacitance-test firmware is a **separate PlatformIO project** (`env teensy41_captest`, pinned `platform = teensy@6.0.0`). It compiles four production files straight from `Arduino_Sketches/TestBoard_ADC124/` (`SpiController.cpp`, `Adc124s101.cpp`, `ApiProtocol.cpp`, `UsbSerialController.cpp`, plus the production headers via `-I`) and runs the unmodified production USB-core pre-script. Nothing under `Arduino_Sketches/TestBoard_ADC124/` is edited and the production `teensy41_profile` / `teensy41_pcbdiag` environments are not part of the test project. The production classes are used through their public interface only:

* `Adc124s101::begin()`, `park()` and `switchAddress()` (the production break-before-make: disable, 200 ns guard, grouped-port address write, enable, 200 ns guard) and its state tracking stay valid, because the in-burst switch *is* `switchAddress()`; only the LPSPI frame loop is new (`CapBurstEngine`), using `SpiController::beginLpspiSession()` / `endLpspiSession()` and the LPSPI4 FIFO directly.
* Production's own SPI transfer functions are not used.

Acceptance (verified at the end of this work, see "Verification status"): `git diff --stat -- Arduino_Sketches/TestBoard_ADC124` is empty and the production `firmware.hex` SHA-256 is `10c89bbbd5fb6643e6f747c605003718ff50ce222a02a0a85e452631901d507b` before and after.

The Teensy runs one image at a time. Neither firmware writes EEPROM or flash storage (production settings are runtime commands and desktop-GUI profiles), so switching loses nothing. The shared script `CapTest/scripts/flash_firmware.*` only builds and uploads an image.

Start-up equals production: CS lines high, pin modes and the SPI bus as in `main.cpp setup()`, `Adc124s101::begin()` for both banks (EN inactive, then every bank enabled on address 7 = VMID). No other mux command is sent until a command arrives.

## Measurement sequence (firmware, per `capburst`)

One bank is active; the other bank stays parked on VMID (EN active, address 7) with its CS high, as at start-up. The active bank's ADC is the only one clocked.

1. **Park.** `park()` (address 7, VMID) and `park_us` microseconds of waiting (default 200). Every D node of the bank sits at VMID.
2. **Frames, continuous CS.** CS goes low once; 16-clock frames follow back to back through the LPSPI TX FIFO (kept full, bounded by the 16-word RX FIFO, as the production burst). The command clocked in during submitted word `s` selects the input converted in word `s+1` (one-frame selector latency). Submitted word 0 is the **lead frame**: its response converts whatever was selected before and is discarded. Returned frame `k` is the response to submitted word `k+1` and converts the input `inputs[k mod m]` (`m` = number of selected inputs, ascending IN1..IN4).
3. **Pre-connect baseline.** The first `pre_frames` returned frames (rounded up to a multiple of `m`; `pre_samples` in the command, default 8) are taken while the bank is still parked on VMID: the pre-connect reading `V_D` of each converted mux.
4. **Connection.** As soon as the loop has popped returned frame `pre_frames - 1`, it runs `switchAddress(address)` with interrupts disabled for that ~0.6 us, and records the cycle counter before (`switch_begin`) and after (`switch_end`). The disable edge is at `switch_begin` (plus a few cycles); the enable edge is at `switch_end - guard_ns` (guard = 200 ns, in the header). Everything is in the same `ARM_DWT_CYCCNT` time base as the frame stamps.
5. **Sampling.** The loop keeps popping frames until `pre_frames + post_frames` have been returned (`post_frames` = `samples` of the command rounded up to a multiple of `m`).
6. **Release.** CS is raised after receive-complete and idle, the LPSPI session is closed, and the bank is re-parked on address 7 immediately (also after an error). The host then waits `discharge_ms` with the bank left parked: the sensors that were connected float, and their charge leaks away only through the sensor's own leakage (measure it with the discharge check).
7. Statistics, flags and the packet are produced after the critical part. Nothing is written to USB during the burst.

No `yield()`, no `Serial` call (apart from polling `Serial.available()` once per 32 frames to notice an abort), no allocation and no function dispatch per frame; interrupts stay enabled except for the switch.

### Time stamps (read this before analysing)

With CS held low across back-to-back frames there is **no per-frame CS edge**, so a "CS-fall stamp per frame" does not exist. Each returned frame carries `ARM_DWT_CYCCNT` read when the loop **popped its result** from the RX FIFO (`P_k`). `P_k` is the true end of frame `k` plus the polling latency, which is a small non-negative number (the loop iteration is about 100 ns). The frame period is set by the SPI clock and the LPSPI inter-frame gap, so `P_k` is a regular comb with polling jitter on top. The PC reconstructs frame times from a robust line fit `P_k = a + T_f k` and the assumed constants (documented in the driver):

```text
frame start   S_k = a + T_f k - pop_latency - 16 / f_spi
sample instant    = S_k + hold_clocks / f_spi            (hold_clocks = 3, assumption)
disable edge      = switch_begin
enable edge       = switch_end - guard_ns
connection t_c    = enable edge + mux turn-on delay      (25 ns, assumption)
```

Measured on the board (600 MHz CPU, 200 bursts, array 1 address 2):

| Item | Value |
| --- | --- |
| frame period, 16 MHz clock | **1037.1-1037.7 ns** mean per burst (622.3 cycles) = 1000 ns + 37 ns inter-frame gap; per-frame min 960 ns, max 1120 ns (polling jitter) |
| frame period, 8 MHz clock | 2070.8 ns (2000 ns + 71 ns) |
| first frame (start of the TDR write to its pop) | 1336.7 ns |
| switch duration `switch_end - switch_begin` | **611.7 ns, no jitter** (standard deviation 0.0 ns over 200 bursts; the cycle counter resolves 1.67 ns). Open window (disable to enable) about 410 ns |
| switch begins after the last pre-connect pop | 53.3 ns, no jitter |

So the in-burst GPIO switch is deterministic with the FIFO engine (the criterion in the plan was jitter below 100 ns), and the fallback "switch before the burst" was not needed. The `capvmid` command is built on the same engine without a switch.

Interrupts are enabled during the burst (as in production). The 16-word TX FIFO is 16 us of slack, so a USB or timer interrupt does not starve the SPI; it only delays one pop stamp (`FLAG_STAMP_GAP` when a period exceeds 3x the mean). Because the switch itself is interrupt-free, the connection instant is never disturbed.

### Command validation

`#NOT_OK` is returned (and no packet is sent) for: array outside 1..2; address 7 (VMID) or above 6; an input mask that is 0 or above 15; any converted ADC input whose mux has no sensor at that address (`validSensorRoute()`: address 6 only on MUX4, so mask 8 only); `samples` below 16 or `pre_frames + post_frames` above 4096; `pre_samples` outside 1..64; `park_us > 10000`; an SPI clock outside 8..16 MHz; a firmware in the latched `faulted` state. A burst cannot be interrupted except at a frame boundary: a byte arriving on USB during the burst ends it after the current frame (`FLAG_ABORTED`, the partial data is still returned), the board is re-parked and the command is then processed. A transfer error or timeout latches the `faulted` state until the board is reset, exactly like production (`capstatus` reports `state=faulted`).

## Commands

ASCII, terminated by `*`, answered by `#OK ...` / `#NOT_OK ...` at 460800 baud as in production. Lines starting with `# ` carry data.

| Command | Reply |
| --- | --- |
| `capstatus*` | `# capstatus firmware=TestBoard_ADC124_CapTest board=TestBoard_ADC124 version=1.0.0 protocol=1 git=<hash> build=<date_time> boot_id=0x<hex> uptime_ms=<n> cpu_hz=<n> spi_clock_hz=<n> max_samples=4096 state=idle\|faulted bursts=<n> transfer_errors=<n> hold_clocks=3 guard_ns=200 min_spi_hz=8000000 max_spi_hz=16000000`, then `#OK`. `status*` is an alias (production's `status*` is a different, longer list; use `mcu*` to tell the firmwares apart). |
| `mcu*` | `# TestBoard_ADC124_CapTest` (production answers `# TestBoard_ADC124`, the diagnostic profile `ADC124_PCB_DIAGNOSTIC`). |
| `capvmid <array> <input>* [n]` | `<input>` = ADC input 1..4 (IN1..IN4) or 0 for all four; `n` readings per input (default 16, max 64) with the bank parked on VMID. `# capvmid array=<a> mask=<m> n=<n> mean_code=<x> min=<c> max=<c> in1=<x> in2=<x> in3=<x> in4=<x> data_errors=<e>` (`-` for an input that was not read), then `#OK`. |
| `capburst <array> <address> <input_mask> <samples\|auto> <park_us> <pre_samples> [<spiclock_hz>]*` | `#OK capburst ...` followed immediately by one binary packet. `input_mask`: bit 0 = IN1 ... bit 3 = IN4 (MUX1..4 -> IN1..4, an assumption); `samples` = frames after the connection; `auto` covers 6 tau of a guessed 100 pF behind the largest bias resistor of the converted inputs, capped at 2048 frames. An incomplete or aborted burst is acknowledged `#NOT_OK` but still sends its packet (flags say why). |
| `capabort*` | parks both banks on VMID, `#OK`. `stop*` is an alias. |
| `spiclock <hz>*` | 8000000..16000000 Hz (the ADC124S101 AC specification range), as in production. |
| `help*` | list of commands. |

`boot_id` is a hash of the SNVS RTC counter, the chip id and the cycle counter at the first host command; a changed boot id (or smaller uptime) tells the PC the board was reset, so every sensor is "fresh" again.

## Burst packet (protocol version 1, little-endian)

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 2 | magic `CB` (0x43 0x42) |
| 2 | 1 | protocol version (1) |
| 3 | 1 | header length (64) |
| 4 | 1 | array 1..2 |
| 5 | 1 | address 0..6 (7 = parked read, no connection) |
| 6 | 1 | input mask (bit 0 = IN1 ... bit 3 = IN4) |
| 7 | 1 | number of converted inputs `m` |
| 8 | 2 | `n_frames` returned (pre + post when complete) |
| 10 | 2 | `pre_frames` (frames before the connection) |
| 12 | 1 | flags: bit0 incomplete, bit1 aborted, bit2 no switch (parked read), bit3 mean period below 16 clocks, bit4 switch slower than 1 us, bit5 data errors, bit6 stamp gap |
| 13 | 1 | `hold_clocks` the firmware assumes (3; informational, the PC applies its own setting) |
| 14 | 2 | `guard_ns` (200) |
| 16 | 4 | `cpu_hz` (`F_CPU_ACTUAL`) |
| 20 | 4 | `spi_clock_hz` |
| 24 | 4 | `boot_id` |
| 28 | 4 | `park_us` requested |
| 32 | 4 | `start_cycles`: cycle counter as the first command word is queued (CS already low) |
| 36 | 4 | `lead_cycles`: pop of the discarded lead frame |
| 40 | 4 | `switch_begin_cycles` (0 when there was no switch) |
| 44 | 4 | `switch_end_cycles` |
| 48 | 4 | min frame period, cycles (between consecutive pops) |
| 52 | 4 | sum of the `n_frames - 1` frame periods, cycles (mean = sum / (n_frames - 1)) |
| 56 | 4 | max frame period, cycles |
| 60 | 2 | transfer errors in this burst |
| 62 | 2 | data errors (responses with non-zero upper 4 bits) |
| 64 | 2 x n_frames | response word of every returned frame, in frame order (12-bit code, 4 leading zeros) |
| ... | 4 x n_frames | `ARM_DWT_CYCCNT` at the pop of every returned frame (wraps at 2^32; subtract and mask) |
| end-2 | 2 | CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no xor-out; check value of `123456789` is 0x29B1) over all preceding bytes |

Total length = 64 + 6 x n_frames + 2. Maximum 24,642 bytes (4096 frames). Frame `k` converts the input `k mod m`-th set bit of the mask. The ADC returns no channel tag, so a selector mismatch cannot be detected from the data alone (the pre-connect samples are all VMID, whatever the input).

Firmware memory (`pio run`, 2026-10-23): flash code 28,016 B + data 7,448 B (production 34,480 + 7,448); RAM1 variables 106,048 B (production 59,840 B: +46 KB for the 4096-frame result arrays and packet buffer, 6 x 4096 + 6 x 4096 + 66), RAM2 12,480 B. The production address-change cache (about 48 KB) is part of the reused `Adc124s101` and included in both numbers.

### Example (measured on the board, array 1 address 0, IN1, `capburst 1 0 1 16 200 2`; 174 bytes)

Full hex: [`example_packet_1_0_n16.hex`](../../testing/results/testboard-adc124/capacitance-test/example_packet_1_0_n16.hex).

```text
header (64 bytes):
43 42 01 40 01 00 01 01 12 00 02 00 00 03 c8 00 00 46 c3 23 00 24 f4 00
90 70 21 9b c8 00 00 00 e5 34 02 14 07 38 02 14 bc 3c 02 14 2b 3e 02 14
60 02 00 00 5c 29 00 00 a0 02 00 00 00 00 00 00
```

Decoded: protocol 1, header 64, array 1, address 0, mask 0x01, m 1, n_frames 18 (2 pre + 16 post), flags 0, hold_clocks 3, guard_ns 200 (`c8 00`), cpu_hz 600,000,000 (`00 46 c3 23`), spi_clock_hz 16,000,000 (`00 24 f4 00`), boot_id 0x9B217090, park_us 200, `start_cycles` 0x140234E5, `lead_cycles` 0x14023807, `switch_begin` 0x14023CBC, `switch_end` 0x14023E2B (= 367 cycles = 611.7 ns later), period min/sum/max 608 / 10,588 / 672 cycles, errors 0/0. The response words start `00 08 01 08 0c 08 15 08` = 2048, 2049, 2060, 2069 ...: two pre-connect samples at VMID, then the connection and the charge-up. A byte-exact copy of this frame is part of the PC-side tests.

## Assumptions and unknowns (document, do not forget)

| Item | Assumption / status |
| --- | --- |
| MUX1..4 -> ADC IN1..IN4 | Assumed PCB wiring (`kMuxToAdcInput`). Verify with one sensor touched or a function generator on a single channel. |
| Hold instant | ADC124S101 holds about 3 SCLK falling edges after the frame start (`hold_clocks`, setting). Not checked against CS/SCLK edges on a scope. In continuous-CS mode the frame start is the previous frame's 16th clock. |
| Pop latency | 50 ns assumed (`pop_latency_ns`, setting): the polling loop notices a finished frame 0-100 ns late; the PC reconstructs frame times from a line fit through the pops. A constant error here shifts the whole curve by the same amount (a 100 ns error is 0.2 % of tau = 47 us). |
| Mux turn-on delay | 25 ns after the enable edge (setting); TMUX1308A/TMUX1108 datasheets give tens of ns typical. |
| Charge injection | EN and address switching puts a small pedestal on the D node (QSPICE: about 1.606 V instead of 1.65 V). It moves the step ratio, not tau. Ignored. |
| Sensor start voltage `V_s0` | 0 V (setting). On this board a sensor can float at an arbitrary level (the first test burst on array 1 address 0, IN1, stepped **up** by about +96 counts, i.e. the sensor was above VMID), so the ratio is only trustworthy for sensors that have been discharged; see the discharge check. |
| Sensor discharge between bursts | Only through the sensor's own leakage while the D node is parked on VMID. Needs measuring (discharge check test, 10..1000 ms). |
| `R_bias` | `kBiasResistorOhms` in `ConfigurableParameters.h` (array 1: 470 k, 1 M, 470 k, 1 M; array 2: 1 M, 470 k, 1 M, 470 k) and `benchmarks/testboard_adc124_routes.json`. Tolerance maps 1:1 to `C_total`. Confirm with a meter. |
| `VREF` | 3.3 V (`VA`); LSB 0.806 mV. |
| Other bank during a burst | Left parked on VMID with CS high (not disabled). The plan text says "disabled"; no electrical difference for the measured bank. |

## Verification status

Tested on the board (COM4, Teensy 4.1, 2026-10-23): flash from production, `mcu*` / `capstatus*`, `capvmid` (all eight inputs read 2048..2051 counts, 1.650-1.653 V), single-input and four-input bursts on both arrays, 8 and 16 MHz, 4096-frame maximum burst, rejected commands (array 3, address 6 with mask 15, 7 MHz clock, mask 0, bad pre/samples), abort during a burst (`FLAG_ABORTED`, partial packet followed by the next reply), 200-burst switch-jitter run (table above). Not verifiable without extra equipment: the real hold instant, the real mux edge, the MUX-to-ADC-input wiring and charge injection; see the verification checklist in `docs/guides/TESTBOARD_ADC124_CAPTEST_VERIFICATION_CHECKLIST.md`.
