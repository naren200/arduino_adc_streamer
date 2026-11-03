#include <Arduino.h>

#include "CapTestFirmware.h"

void setup() {
  captest_firmware::setupFirmware();
}

void loop() {
  captest_firmware::loopFirmware();
}
