# MG24 PCB1.7 Modular Sketch

Main sketch:

- `MG24_Dual_MUX_SPI_Slave1.7_DRDY_Modular.ino`

Board config:

- `BoardConfig.h` defines ADC MUX pins, SPI slave pins, DRDY, and SPIDRV setup.

Libraries:

- `libraries/Mg24SharedProtocol.*`
- `libraries/Mg24AdcMux.*`
- `libraries/Mg24CommandEngine.*`
- `libraries/Mg24SpiSlaveTransport.*`

This sketch follows the same modular MG24 split as `PCB1.5_with_Libraries`, with
the PCB1.7 MUX settle timing from `PCB1.7_SPI`:

- `kMuxSettleUs = 20`

## Transport Behavior

- 20-byte command frames
- 4-byte ACK responses
- Binary streaming block format `[AA 55][count][payload][trailer]`
- DRDY asserted when a response is armed

Flash this sketch with the matching Teensy sketch in
`../Teensy/Teensy_SPI_Master_Array_PZT_PZR1.7_DRDY_Modular.ino`.
