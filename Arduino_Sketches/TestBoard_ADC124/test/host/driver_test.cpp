// Execute the production application with tagged ADC/CS/GPIO/register models.
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
bool bad_bits = false;
unsigned conversions = 0;
uint64_t last_enable[2] = {}, last_cs = 0;

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
  assert(bus == &IMXRT_LPSPI4_S);
  int adc = -1;
  for (int i = 0; i < 2; ++i) if (pin_values[kCsPins[i]] == LOW) { assert(adc == -1); adc = i; }
  assert(adc != -1 && pin_values[kMuxEnablePins[adc]] == kMuxEnableActiveHigh[adc]);
  // Fresh selector latency persists across CS edges, ADC switches and bursts.
  assert(!(command_word & ~0x1800));
  uint16_t value = (adc + 1) * 1000 + selected_input[adc] * 100 + address(adc);
  selected_input[adc] = (command_word >> 11) & 3;
  ++conversions; fake_us += 2;
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

void verifyGuards() {
  uint64_t off[2] = {}, on[2] = {};
  for (const auto &e : gpio_events) {
    for (int bank = 0; bank < 2; ++bank) {
      if (e.pin == kMuxEnablePins[bank]) {
        if (e.value == kMuxEnableActiveHigh[bank]) on[bank] = e.ns;
        else off[bank] = e.ns;
      }
      for (auto pin : kMuxAddressPins[bank]) if (e.pin == pin) {
        assert((bank ? e.enable_b : e.enable_a) != kMuxEnableActiveHigh[bank]);
        if (off[bank]) assert(e.ns - off[bank] >= kMuxDisableTimeNs);
      }
      if (e.pin == kCsPins[bank] && e.value == LOW && on[bank])
        assert(e.ns - on[bank] >= kMuxDisableTimeNs);
    }
  }
}

void verifyCapture(const char *order, bool burst, bool full, unsigned sweep) {
  Serial.bytes.clear(); gpio_events.clear(); delay_events.clear();
  unsigned words_before = conversions, begins = SPI.begins, lookups = fake_gpio_lookups;
  assert(capture());
  assert(Serial.bytes.size() == 14 + plan_count * 2 && le16(2) == plan_count);
  std::vector<SensorRoute> expected;
  for (int i = 0; i < route_count; ++i) if (selected(routes[i].array)) expected.push_back(routes[i]);
  std::sort(expected.begin(), expected.end(), [&](SensorRoute a, SensorRoute b) {
    return std::string(order) == "adc" ? std::tie(a.array, a.input, a.mux) < std::tie(b.array, b.input, b.mux)
                                      : std::tie(a.input, a.array, a.mux) < std::tie(b.input, b.array, b.mux);
  });
  for (size_t i = 0; i < expected.size(); ++i) {
    const auto &r = expected[i];
    assert(le16(4 + i * 2) == r.array * 1000 + (r.mux - 1) * 100 + r.input);
  }
  unsigned words = 0;
  for (int i = 0; i < group_count; ++i) words += groups[i].batch.count + (vmid ? 5 : 0);
  assert(conversions - words_before == words);
  unsigned cs_low = 0, en_writes = 0;
  for (const auto &e : gpio_events) {
    if ((e.pin == 9 || e.pin == 24) && e.value == LOW) ++cs_low;
    if (e.pin == 0 || e.pin == 32) ++en_writes;
  }
  assert(cs_low == (burst ? unsigned(group_count * (vmid ? 2 : 1)) : words));
  if (engine == SpiEngine::LPSPI) assert(SPI.begins - begins == 1);
  assert(fake_gpio_lookups == lookups && SPI.begins == SPI.ends);
  assert(sampling_sweeps == sweep + 1 && usb_frames_sent == sampling_sweeps);
  if (full) {
    unsigned banks = array_mask == 3 ? 2 : 1;
    assert(en_writes == 2 * 7 * banks * (vmid ? 2 : 1));
    assert(delay_events.size() == (settle_us ? unsigned(phase_count * (vmid ? 2 : 1)) : 0));
  }
  if (full && std::string(order) == "mux" && array_mask == 3) {
    for (const auto &e : delay_events) {
      bool has_both = false;
      for (int p = 0; p < phase_count; ++p) if (phases[p].count == 2 && phases[p].input == e.a) has_both = true;
      if (has_both || e.a == 7) assert(e.a == e.b);
    }
  }
  verifyGuards();
}

int main() {
  setup(); checkParked();
  assert(command("spiclock", "8000000") && spi.clockHz() == 8000000);
  assert(!command("spiclock", "20000000")); assert(command("spiclock", "16000000"));
  assert(!command("scanorder", "channel") && !command("scanorder", "interleaved"));
  IMXRT_LPSPI4_S.TCR.unstable_read = true;
  IMXRT_LPSPI4_S.completion_reads = 3;
  unsigned cases = 0;
  for (const char *array : {"1", "2", "both"}) for (const char *order : {"adc", "mux"})
    for (const char *mode : {"blocking", "dma", "lpspi"}) for (const char *delay : {"0", "2", "5"})
      for (const char *repeat : {"1", "2", "3"}) for (const char *vmid_arg : {"false", "true"})
        for (const char *frame : {"single", "burst"}) {
          stop();
          assert(command("array", array)); assert(command("scanorder", order));
          assert(command("spiengine", mode)); assert(command("muxsettle", delay));
          assert(command("channelrepeat", repeat)); assert(command("vmid", vmid_arg)); assert(command("spiframe", frame));
          assert(command("run", ""));
          for (unsigned sweep = 0; sweep < 3; ++sweep) verifyCapture(order, burst_frames, true, sweep);
          stop(); checkParked(); ++cases;
        }
  assert(cases == 648 && IMXRT_LPSPI4_S.TCR.unstable_reads == 0);
  const char *selections[] = {"2:4:6,1:3:0,2:1:0,1:1:0", "1:1:0,2:4:6", "1:4:6", "2:2:5", "1:1:0,2:1:0"};
  for (const char *selection : selections) for (const char *order : {"adc", "mux"})
    for (const char *mode : {"blocking", "dma", "lpspi"}) for (const char *v : {"false", "true"}) {
      assert(command("adcchannels", selection)); assert(command("array", "both"));
      assert(command("scanorder", order)); assert(command("spiengine", mode));
      assert(command("vmid", v)); assert(command("muxsettle", "5")); assert(command("channelrepeat", "1"));
      assert(command("spiframe", "burst")); assert(command("run", ""));
      verifyCapture(order, true, false, 0); verifyCapture(order, true, false, 1);
      if (!vmid && std::string(selection) == "1:1:0,2:1:0") assert(delay_events.empty());
      stop(); checkParked(); ++cases;
    }
  assert(!command("adcchannels", "1:1:6")); assert(!command("adcchannels", "1:4:7"));
  assert(!command("adcchannels", "1:1:0,1:1:0"));
#if TESTBOARD_PROFILE
  assert(command("profile", "on"));
#endif
  assert(command("run", "")); Serial.write_delay_us = 120000;
  assert(capture()); Serial.write_capacity = 0; assert(capture());
  Serial.connected = false; assert(capture()); Serial.connected = true; Serial.write_capacity = 2048; assert(capture());
  assert(sampling_sweeps == 4 && usb_frames_sent == 2 && usb_frames_discarded == 2);
  stop(); checkParked(); Serial.text.clear(); status();
  std::cout << "PROFILE_STATUS_BEGIN\n" << Serial.text << "PROFILE_STATUS_END\n";
  String line; Serial.input = std::string(600, 'x') + "run*status*";
  assert(usb.readCommand(line) && line == "invalid_overlength_command"); assert(usb.readCommand(line) && line == "status");
  assert(command("run", "1")); fake_us += 2000; loop(); assert(!running); checkParked();
  assert(command("run", "")); const size_t prior_bytes = Serial.bytes.size();
  bad_bits = true; loop(); bad_bits = false; assert(!running && Serial.bytes.size() == prior_bytes); checkParked();
  assert(command("run", "")); Serial.write_limit = 1; loop(); assert(!running && usb.writeErrors() == 1); checkParked();
  Serial.write_limit = size_t(-1);
  assert(spi.beginLpspiSession()); adc2.selectAddress(0, 0); IMXRT_LPSPI4_S.stall = true;
  fake_cycle_offset = UINT32_MAX - 100; uint16_t discarded;
  assert(!adc2.transfer(0, SpiEngine::LPSPI, discarded)); IMXRT_LPSPI4_S.stall = false;
  spi.endLpspiSession(); stop(); assert(adc2.transferErrors() == 1); checkParked();
  assert(!adc2.transfer(0, SpiEngine::BLOCKING, discarded));
  std::cout << cases << " ADC124 digital acquisition cases and USB/failure checks passed\n";
}
