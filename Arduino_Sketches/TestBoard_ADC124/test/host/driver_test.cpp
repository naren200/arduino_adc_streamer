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
bool capture_in_progress = false;
unsigned conversions = 0;
uint64_t last_enable[2] = {}, last_cs = 0;

void verify_spi_cs_release(uint8_t) {
  assert(IMXRT_LPSPI4_S.tx.empty());
  assert(!(IMXRT_LPSPI4_S.SR.value & LPSPI_SR_MBF));
  assert(!IMXRT_LPSPI4_S.final_clock_pending);
}

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
  // Startup may initialize selectors while parked. Acquisition must never
  // clock a conversion from VMID, including zero-delay visits.
  if (capture_in_progress) assert(address(adc) != kVmidAddress);
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
  capture_in_progress = true;
  assert(capture());
  capture_in_progress = false;
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
  for (int i = 0; i < group_count; ++i) {
    words += groups[i].batch.count;
    assert(groups[i].batch.count <= 12);
  }
  assert(words == unsigned(plan_count * channel_repeat));
  assert(conversions - words_before == words);
  unsigned cs_low = 0, en_writes = 0;
  for (const auto &e : gpio_events) {
    if ((e.pin == 9 || e.pin == 24) && e.value == LOW) ++cs_low;
    if (e.pin == 0 || e.pin == 32) ++en_writes;
  }
  assert(cs_low == (burst ? unsigned(group_count) : words));
  if (engine == SpiEngine::LPSPI) assert(SPI.begins - begins == 1);
  assert(fake_gpio_lookups == lookups && SPI.begins == SPI.ends);
  assert(sampling_sweeps == sweep + 1 && usb_frames_sent == sampling_sweeps);
  if (full) {
    unsigned banks = array_mask == 3 ? 2 : 1;
    assert(en_writes == 2 * 7 * banks * (vmid ? 2 : 1));
    assert(delay_events.size() == unsigned(phase_count) * ((settle_us ? 1 : 0) + (vmid && vmid_settle_us ? 1 : 0)));
    if (vmid) for (const auto &e : delay_events) {
      const bool on_vmid = array_mask == 1 ? e.a == 7 : array_mask == 2 ? e.b == 7 : e.a == 7 && e.b == 7;
      assert(e.us == (on_vmid ? vmid_settle_us : settle_us));
    }
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
  assert(engine == SpiEngine::LPSPI);
  assert(vmid_settle_follows_sensor && vmid_settle_us == settle_us);
  assert(command("muxsettle", "10") && vmid_settle_us == 10);
  assert(command("vmidsettle", "0") && !vmid_settle_follows_sensor && vmid_settle_us == 0);
  assert(command("muxsettle", "5") && vmid_settle_us == 0);
  for (const char *bad : {"", "-1", "1.5", "1001", "default extra", "automatic"}) {
    assert(!command("vmidsettle", bad)); assert(vmid_settle_us == 0 && !vmid_settle_follows_sensor);
  }
  assert(command("vmidsettle", "default") && vmid_settle_follows_sensor && vmid_settle_us == 5);
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
          const unsigned preparation_words = conversions;
          assert(command("run", ""));
          assert(conversions - preparation_words <= (array_mask == 3 ? 2u : 1u));
          for (unsigned sweep = 0; sweep < 3; ++sweep) verifyCapture(order, burst_frames, true, sweep);
          stop(); checkParked(); ++cases;
        }
  assert(cases == 648 && IMXRT_LPSPI4_S.TCR.unstable_reads == 0);
  unsigned lookahead_cases = 0;
  for (const char *clock : {"8000000", "16000000"}) for (const char *order : {"adc", "mux"})
    for (const char *mode : {"blocking", "dma", "lpspi"}) for (const char *v : {"false", "true"})
      for (const char *frame : {"single", "burst"}) {
        assert(command("array", "both") && command("scanorder", order));
        assert(command("spiclock", clock) && command("spiengine", mode));
        assert(command("channelrepeat", "1") && command("vmid", v) && command("spiframe", frame));
        assert(command("run", ""));
        for (uint8_t i = 0; i < group_count; ++i) {
          const auto &g = groups[i];
          assert(g.batch.count == (g.input == 6 ? 1 : 4));
          if (g.input == 5) assert(g.batch.commands[3] == (3 << 11));
          if (g.input == 6) assert(g.batch.commands[0] == 0);
        }
        for (unsigned sweep = 0; sweep < 3; ++sweep) verifyCapture(order, burst_frames, true, sweep);
        stop(); checkParked(); ++lookahead_cases;
      }
  assert(lookahead_cases == 48);
  std::cout << lookahead_cases << " no-prime lookahead clock/engine cases passed\n";
  unsigned vmid_cases = 0;
  for (const char *array : {"1", "2", "both"}) for (const char *order : {"adc", "mux"})
    for (const char *mode : {"blocking", "dma", "lpspi"}) for (const char *frame : {"single", "burst"})
      for (const char *repeat : {"1", "3"}) for (const char *delay : {"0", "1", "5"}) {
        stop(); assert(command("array",array)); assert(command("scanorder",order));
        assert(command("spiengine",mode)); assert(command("spiframe",frame)); assert(command("channelrepeat",repeat));
        assert(command("muxsettle","5")); assert(command("vmid","true")); assert(command("vmidsettle",delay));
        assert(command("run",""));
        for (unsigned sweep=0;sweep<3;++sweep) verifyCapture(order,burst_frames,true,sweep);
        stop(); checkParked(); ++vmid_cases;
      }
  assert(vmid_cases == 216);
  // Equal explicit/inherited delays produce the same simulated sweep timing
  // and GPIO/delay trace, including ADC pipeline samples, across both modes.
  for (const char *order : {"adc", "mux"}) for (const char *mode : {"blocking", "dma", "lpspi"}) {
    assert(command("array","both") && command("scanorder",order) && command("spiengine",mode));
    assert(command("spiframe","burst") && command("channelrepeat","1"));
    assert(command("vmidsettle","default") && command("run",""));
    verifyCapture(order,true,true,0);
    const uint32_t duration = le32(Serial.bytes.size()-4)-le32(Serial.bytes.size()-8);
    const auto inherited_delays = delay_events; stop();
    assert(command("vmidsettle","5") && command("run","")); verifyCapture(order,true,true,0);
    assert(duration == le32(Serial.bytes.size()-4)-le32(Serial.bytes.size()-8));
    assert(delay_events.size() == inherited_delays.size());
    for (size_t i=0;i<delay_events.size();++i) {
      assert(delay_events[i].us == inherited_delays[i].us);
      assert(delay_events[i].a == inherited_delays[i].a && delay_events[i].b == inherited_delays[i].b);
    }
    stop();
  }
  assert(command("vmidsettle","default"));
  std::cout << vmid_cases << " independent VMID settling cases passed\n";
  // Divergent bank histories must combine only the changed pins, not assume
  // both banks have the same cached address. All guards are timed per bank.
  for (uint8_t a = 0; a < 8; ++a) for (uint8_t b = 0; b < 8; ++b)
    for (uint8_t target = 0; target < 8; ++target) {
      adc1.selectAddress(a, 0); adc2.selectAddress(b, 0);
      gpio_events.clear(); port_events.clear(); const uint64_t guard = guard_ns;
      const bool changed = Adc124s101::switchTogether(&adc1, &adc2, target);
      assert(changed == (a != target || b != target));
      assert(address(0) == target && address(1) == target);
      assert(guard_ns - guard == (changed ? 2 * kMuxDisableTimeNs : 0));
      unsigned en_a = 0, en_b = 0;
      for (auto e : gpio_events) { en_a += e.pin == 0; en_b += e.pin == 32; }
      assert(en_a == (a != target ? 2u : 0u) && en_b == (b != target ? 2u : 0u));
      verifyGuards();
    }
  Adc124s101::switchTogether(&adc1, &adc2, 7); port_events.clear();
  assert(Adc124s101::switchTogether(&adc1, &adc2, 0));
  assert(port_events.size() == 7); // four EN writes + three address-port clears
  adc1.disable(); gpio_events.clear(); port_events.clear(); uint64_t guard = guard_ns;
  assert(Adc124s101::switchTogether(&adc1, nullptr, 0));
  assert(port_events.size() == 1 && guard_ns - guard == kMuxDisableTimeNs);
  assert(!Adc124s101::switchTogether(&adc1, &adc1, 0));
  assert(!Adc124s101::switchTogether(nullptr, nullptr, 0));
  assert(!Adc124s101::switchTogether(&adc1, &adc2, 8));
  stop(); checkParked();

  // Repeat=3 batches have 12 useful words. Exercise FIFOs smaller than a batch and
  // asymmetric TX/RX capacities, then inject errors and a wrapped timeout.
  adc1.selectAddress(5, 0);
  SensorRoute fifo_routes[] = {{1,1,5},{1,2,5},{1,3,5},{1,4,5}};
  Adc124s101::PreparedBatch fifo_batch; assert(adc1.prepareBatch(fifo_routes, 4, 3, fifo_batch));
  assert(adc1.selectInput(fifo_batch.first_input, SpiEngine::LPSPI));
  for (uint8_t tx_log : {0, 2, 4}) for (uint8_t rx_log : {0, 2, 4}) {
    IMXRT_LPSPI4_S.PARAM = tx_log | (uint32_t(rx_log) << 8);
    IMXRT_LPSPI4_S.max_tx = IMXRT_LPSPI4_S.max_rx = 0;
    uint16_t samples[4]; assert(adc1.readPrepared(fifo_batch, SpiEngine::LPSPI, samples, true));
    for (int i = 0; i < 4; ++i) assert(samples[i] == 1005 + i * 100);
    assert(IMXRT_LPSPI4_S.max_tx <= (1u << tx_log) && IMXRT_LPSPI4_S.max_rx <= (1u << rx_log));
    if (tx_log == 4 && rx_log == 4) assert(IMXRT_LPSPI4_S.max_tx == 12 && IMXRT_LPSPI4_S.max_rx == 12);
    assert(IMXRT_LPSPI4_S.rx.empty() && SPI.begins == SPI.ends);
  }
  IMXRT_LPSPI4_S.PARAM = (4u << 8) | 4u;
  const auto fifo_cs = spi.registerChipSelect(9); uint16_t fifo_responses[13];
  for (uint32_t error : {LPSPI_SR_TEF, LPSPI_SR_REF}) {
    IMXRT_LPSPI4_S.inject_error = error;
    assert(!spi.transferLpspiBurst(fifo_cs, fifo_batch.commands, fifo_responses, 13, 10000, 1000));
    assert(pin_values[9] == HIGH && IMXRT_LPSPI4_S.rx.empty() && SPI.begins == SPI.ends);
  }
  IMXRT_LPSPI4_S.inject_error = 0; IMXRT_LPSPI4_S.stall = true;
  fake_cycle_offset = UINT32_MAX - 100; const auto yields = fake_yield_calls;
  assert(!spi.transferLpspiBurst(fifo_cs, fifo_batch.commands, fifo_responses, 13, 1000, 100));
  assert(fake_yield_calls > yields && pin_values[9] == HIGH && SPI.begins == SPI.ends);
  IMXRT_LPSPI4_S.stall = false;
  assert(spi.beginLpspiSession()); IMXRT_LPSPI4_S.rx.push_back(0xFFFF);
  assert(!spi.transferLpspiBurst(fifo_cs, fifo_batch.commands, fifo_responses, 13, 1000, 100));
  spi.endLpspiSession(); assert(IMXRT_LPSPI4_S.rx.empty());
  assert(!spi.transferLpspiBurst(fifo_cs, fifo_batch.commands, fifo_responses, 0, 1000, 100));
  stop(); checkParked();
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
  unsigned sparse_vmid_cases = 0;
  for (const char *selection : selections) for (const char *order : {"adc","mux"})
    for (const char *mode : {"blocking","dma","lpspi"}) for (const char *delay : {"0","1","5"}) {
      assert(command("adcchannels",selection)); assert(command("array","both"));
      assert(command("scanorder",order)); assert(command("spiengine",mode)); assert(command("vmid","true"));
      assert(command("vmidsettle",delay)); assert(command("run",""));
      verifyCapture(order,true,false,0); verifyCapture(order,true,false,1);
      assert(delay_events.size() == unsigned(phase_count) * (vmid_settle_us ? 2 : 1));
      stop(); checkParked(); ++sparse_vmid_cases;
    }
  assert(sparse_vmid_cases == 90); assert(command("vmidsettle","default"));
  std::cout << sparse_vmid_cases << " sparse independent VMID settling cases passed\n";
#if TESTBOARD_PROFILE
  assert(command("profile", "on"));
#endif
  assert(command("run", "")); Serial.write_delay_us = 120000;
  assert(capture()); Serial.write_capacity = 0; assert(capture());
  Serial.connected = false; assert(capture()); Serial.connected = true; Serial.write_capacity = 2048; assert(capture());
  assert(sampling_sweeps == 4 && usb_frames_sent == 2 && usb_frames_discarded == 2);
  stop(); checkParked(); Serial.text.clear(); status();
  assert(Serial.text.find("# vmid_scan_policy=wait_only") != std::string::npos);
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
  // A broken selector plan must stop before any clocks, never insert a prime
  // or emit a mislabeled sample. Restart remains blocked after this fault.
  SensorRoute mismatch_routes[] = {{1, 2, 0}};
  Adc124s101::PreparedBatch mismatch;
  assert(adc1.prepareBatch(mismatch_routes, 1, 1, mismatch));
  assert(adc1.selectInput(0, SpiEngine::BLOCKING));
  const unsigned before_mismatch = conversions;
  assert(!adc1.readPrepared(mismatch, SpiEngine::BLOCKING, &discarded, true));
  assert(conversions == before_mismatch && adc1.transferErrors() == 1);
  assert(!command("run", "")); checkParked();
  std::cout << cases << " ADC124 digital acquisition cases and USB/failure checks passed\n";
  std::cout << "VMID wait-only acquisition: no conversions at address 7\n";
}
