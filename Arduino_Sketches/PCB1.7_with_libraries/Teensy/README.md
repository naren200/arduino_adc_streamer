# Teensy PCB1.7 Library-Backed Sketch

Main sketch:

- `Teensy_SPI_Master_Array_PZT_PZR1.7_DRDY_Modular.ino`

Board config:

- `BoardConfig.h` records the PCB1.7 pinout and 555 defaults.

Firmware libraries:

- `libraries/Pcb17Firmware.h`
- `libraries/Pcb17Firmware.cpp`
- `libraries/SharedProtocol.h`
- `libraries/SharedProtocol.cpp`
- `libraries/SerialLineParser.h`
- `libraries/SerialLineParser.cpp`
- `libraries/SpiMasterLink.h`
- `libraries/SpiMasterLink.cpp`
- `libraries/PztController.h`
- `libraries/PztController.cpp`
- `libraries/PztRsController.h`
- `libraries/PztRsController.cpp`
- `libraries/PzrController.h`
- `libraries/PzrController.cpp`

`Pcb17Firmware.cpp` is intentionally thin. It owns serial command dispatch,
mode switching, setup, and loop orchestration. The hardware-specific behavior is
split into sketch-local libraries:

- `SharedProtocol` owns shared ACKs, binary block framing, and value parsing.
- `SerialLineParser` owns `*`-terminated serial command collection.
- `SpiMasterLink` owns the Teensy-to-MG24 SPI/DRDY transport.
- `PztController` owns MG24/PZT configuration and streaming.
- `PztRsController` owns `PZT_RS` sensor routing, RS refresh, held values, and
  combined block repacking.
- `PzrController` owns the 555 timer MUX, resistance calculation, and PZR
  binary/ASCII streaming.

This keeps the PCB1.7 DRDY streaming path and combined `PZT_RS` behavior intact
while isolating replaceable hardware details such as the ADC SPI protocol.

## Supported Serial Modes

- `mode PZT*`
- `mode PZR*`
- `mode PZT_RS*`

`PZT_RS` adds:

- `pztmuxes mux1,mux2...*`
- `rschannels rs1,rs2...*`
- Seven output values per selected sensor:
  `[PZT_CH1,PZT_CH2,PZT_CH3,PZT_CH4,PZT_CH5,RS1_hold,RS2_hold]`

The RS hold values use `PZT_RS_WIRE_UNITS_PER_OHM = 100`, so host-side code
divides those two values by `100.0` to recover ohms.

## Wiring Notes

- MG24 DRDY: Teensy pin `0`
- SPI CS: Teensy pin `10`
- PZR 555 MUX enable: Teensy pin `7`
- RS 555 MUX enable: Teensy pin `8`
- Default 555 source: `RS/555_A`
