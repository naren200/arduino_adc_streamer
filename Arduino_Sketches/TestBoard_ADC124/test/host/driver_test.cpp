// Run the production ADC driver, SPI controller, USB wrapper and application.
// Digital simulation only: electrical timing/analog settling require a board.
#include "../../src/main.cpp"
#ifdef NDEBUG
#error Runtime checks require assertions
#endif
#include <iostream>
#include <tuple>

FakeSerial Serial;
uint32_t fake_us = 0;
int pin_values[64] = {};
IMXRT_LPSPI_t IMXRT_LPSPI4_S, IMXRT_LPSPI3_S;
SPIClass SPI(&IMXRT_LPSPI4_S), SPI1(&IMXRT_LPSPI3_S);
uint8_t selected_input[2] = {3, 2};
int previous_adc = -1;
bool bad_bits = false;
unsigned conversions = 0;

extern "C" int usb_serial_try_write_frame(const void *buffer, uint32_t size) {
  ++Serial.live_write_calls;
  if (!buffer || !size || size > 2048 || Serial.write_limit < size) return -1;
  if (!Serial.connected || Serial.write_capacity < static_cast<int>(size)) return 0;
  const auto *bytes = static_cast<const uint8_t *>(buffer);
  Serial.bytes.insert(Serial.bytes.end(), bytes, bytes + size);
  return size;
}

uint8_t address(int array) {
  const auto &pins = kMuxAddressPins[array];
  return pin_values[pins[0]] | (pin_values[pins[1]] << 1) | (pin_values[pins[2]] << 2);
}

uint16_t emulate_conversion(IMXRT_LPSPI_t *bus, uint16_t command_word) {
  assert(bus == &IMXRT_LPSPI4_S);  // Never use a second SPI bus.
  int adc = -1;
  for (int i = 0; i < 2; ++i) if (pin_values[kCsPins[i]] == LOW) {
    assert(adc == -1); adc = i;
  }
  assert(adc != -1);
  assert(pin_values[kMuxEnablePins[adc]] == kMuxEnableActiveHigh[adc]);
  if (previous_adc >= 0 && previous_adc != adc) assert(address(previous_adc) == 7);
  previous_adc = adc;
  assert(!(command_word & ~0x1800));
  uint16_t value = (adc + 1) * 1000 + selected_input[adc] * 100 + address(adc);
  selected_input[adc] = (command_word >> 11) & 3;
  ++conversions;
  fake_us += 2;
  return bad_bits ? value | 0x8000 : value;
}

uint16_t le16(size_t at) { return Serial.bytes[at] | (Serial.bytes[at + 1] << 8); }
uint32_t le32(size_t at) { return le16(at) | (uint32_t(le16(at + 2)) << 16); }

void checkParked() {
  assert(pin_values[9] == HIGH && pin_values[24] == HIGH);
  assert(address(0) == 7 && address(1) == 7);
  assert(pin_values[0] == HIGH && pin_values[32] == LOW);
  assert(SPI.begins == SPI.ends);
}

int main() {
  setup();
  checkParked();
  assert(command("spiclock", "8000000") && spi.clockHz() == 8000000);
  assert(!command("spiclock", "20000000") && spi.clockHz() == 8000000);
  assert(command("spiclock", "16000000") && spi.clockHz() == 16000000);
  IMXRT_LPSPI4_S.TCR.unstable_read = true;
  IMXRT_LPSPI4_S.completion_reads = 3;
  unsigned cases = 0;
  const char *orders[] = {"mux", "channel", "interleaved"};
  const char *engines[] = {"blocking", "dma", "lpspi"};
  const char *arrays[] = {"1", "2", "both"};
  const char *delays[] = {"0", "2", "5"};
  const char *repeats[] = {"1", "2", "3"};
  for (const char *array : arrays) for (const char *order : orders)
    for (const char *mode : engines) for (const char *delay : delays)
      for (const char *repeat : repeats) for (const char *vmid_arg : {"false", "true"}) {
        assert(command("stop", ""));
        assert(command("array", array)); assert(command("scanorder", order));
        assert(command("spiengine", mode)); assert(command("muxsettle", delay));
        assert(command("channelrepeat", repeat)); assert(command("vmid", vmid_arg));
        assert(command("run", ""));
        Serial.bytes.clear();
        PreparedGroup saved[kMaxRoutes]; memcpy(saved, groups, sizeof(groups));
        const unsigned begins = SPI.begins, lookups = fake_gpio_lookups;
        const uint8_t expected_count = array_mask == 3 ? 50 : 25;
        for (int sweep = 0; sweep < 3; ++sweep) {
          const unsigned prior = conversions;
          assert(capture());
          const size_t base = sweep * (14 + expected_count * 2);
          assert(Serial.bytes[base] == 0xAA && Serial.bytes[base + 1] == 0x55);
          assert(le16(base + 2) == expected_count);
          // Independent expected canonical order, including every PZT7 route.
          std::vector<SensorRoute> expected;
          for (uint8_t a = 1; a <= 2; ++a) if (selected(a))
            for (uint8_t mux = 1; mux <= 4; ++mux)
              for (uint8_t input = 0; input <= (mux == 4 ? 6 : 5); ++input)
                expected.push_back({a, mux, input});
          std::sort(expected.begin(), expected.end(), [&](SensorRoute a, SensorRoute b) {
            if (std::string(order) == "channel") return std::tie(a.array, a.mux, a.input) < std::tie(b.array, b.mux, b.input);
            if (std::string(order) == "interleaved") return std::tie(a.input, a.mux, a.array) < std::tie(b.input, b.mux, b.array);
            return std::tie(a.array, a.input, a.mux) < std::tie(b.array, b.input, b.mux);
          });
          for (size_t i = 0; i < expected.size(); ++i) {
            const auto &route = expected[i];
            assert(le16(base + 4 + i * 2) == route.array * 1000 + (route.mux - 1) * 100 + route.input);
          }
          unsigned expected_conversions = 0;
          for (int i = 0; i < group_count; ++i) expected_conversions += groups[i].batch.count + (vmid ? 5 : 0);
          assert(conversions - prior == expected_conversions);
          assert(le32(base + 10 + expected_count * 2) - le32(base + 6 + expected_count * 2) > 0);
          assert(SPI.begins == SPI.ends);
          assert(sampling_sweeps == uint32_t(sweep + 1) && usb_frames_sent == sampling_sweeps);
          assert(!usb_frames_discarded && !usb.writeErrors());
          assert(memcmp(saved, groups, sizeof(groups)) == 0);
        }
        if (engine == SpiEngine::LPSPI) assert(SPI.begins - begins == 3);
        assert(fake_gpio_lookups == lookups);
        assert(command("stop", "")); checkParked();
        ++cases;
      }
  assert(cases == 486);
  assert(IMXRT_LPSPI4_S.TCR.unstable_reads == 0);
  assert(command("adcchannels", "2:4:6,1:3:0,2:1:5,1:1:0"));
  assert(!command("adcchannels", "1:1:6") && route_count == 4);
  assert(!command("adcchannels", "1:4:7") && route_count == 4);
  assert(!command("adcchannels", "1:1:0,1:1:0") && route_count == 4);
  assert(command("array", "both"));
  assert(command("scanorder", "mux"));
  assert(command("spiengine", "lpspi"));
#if TESTBOARD_PROFILE
  assert(command("profile", "on"));
#else
  assert(!command("profile", "on"));
#endif
  assert(command("run", ""));
  Serial.bytes.clear(); Serial.write_delay_us = 120000;
  assert(capture());
  const size_t accepted = Serial.bytes.size();
  Serial.write_capacity = 0;
  assert(capture()); assert(Serial.bytes.size() == accepted);
  Serial.connected = false;
  assert(capture()); assert(Serial.bytes.size() == accepted);
  Serial.connected = true; Serial.write_capacity = 2048;
  assert(capture());
  assert(sampling_sweeps == 4 && usb_frames_sent == 2 && usb_frames_discarded == 2);
  assert(sampling_period_max_us < 1000 && sampling_period_over_1ms == 0);
  assert(!usb.writeErrors());
  assert(command("stop", "")); checkParked();
  Serial.text.clear(); status();
  std::cout << "PROFILE_STATUS_BEGIN\n" << Serial.text << "PROFILE_STATUS_END\n";

  // Overflow never executes a valid-looking suffix as a command.
  String line;
  Serial.input = std::string(600, 'x') + "run*status*";
  assert(usb.readCommand(line) && line == "invalid_overlength_command");
  assert(usb.readCommand(line) && line == "status");
  assert(command("run", "1")); fake_us += 2000; loop();
  assert(!running); checkParked();
  // Command-driven profile change during a run is rejected at a frame boundary.
#if TESTBOARD_PROFILE
  assert(command("run", ""));
  Serial.input = "profile off*";
  loop(); assert(!running && profile_.enabled()); checkParked();
#endif
  assert(command("run", ""));
  const size_t before_failure_bytes = Serial.bytes.size();
  bad_bits = true; loop(); bad_bits = false;
  assert(Serial.bytes.size() == before_failure_bytes);
  assert(!running && adc1.dataErrors() + adc2.dataErrors() == 1); checkParked();
  // USB write errors stop sampling and cannot be silently retried.
  assert(command("run", "")); Serial.write_limit = 1; loop();
  assert(!running && usb.writeErrors() == 1); checkParked();
  assert(!command("run", "")); Serial.write_limit = size_t(-1);
  // Direct-driver timeout unwinds its session and latches the ADC fault.
  assert(spi.beginLpspiSession());
  adc2.selectAddress(0, 0);
  IMXRT_LPSPI4_S.stall = true;
  fake_cycle_offset = UINT32_MAX - 100;
  uint16_t discarded;
  assert(!adc2.transfer(0, SpiEngine::LPSPI, discarded));
  IMXRT_LPSPI4_S.stall = false;
  spi.endLpspiSession(); stop();
  assert(adc2.transferErrors() == 1); checkParked();
  assert(!adc2.transfer(0, SpiEngine::BLOCKING, discarded));
  assert(fake_yield_calls > 0);
  std::cout << cases << " ADC124 digital acquisition cases and USB/failure checks passed\n";
}
