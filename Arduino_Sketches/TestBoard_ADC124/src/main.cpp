#include <Arduino.h>
#include <SPI.h>
#include "Adc124s101.h"
#include "ApiProtocol.h"
#include "UsbSerialController.h"
#include "AcquisitionProfiler.h"

using namespace testboard_config;

namespace {
UsbSerialController usb;
SpiController spi(SPI, kMisoPin, kMosiPin, kSckPin);
Adc124s101 adc1(spi, 0), adc2(spi, 1);
Adc124s101 *adcs[] = {&adc1, &adc2};
SensorRoute routes[kMaxRoutes], plan[kMaxRoutes];
uint8_t route_count = 0, plan_count = 0;
uint8_t array_mask = 3, channel_repeat = 1;
enum class ScanOrder { ADC, MUX };
ScanOrder scan_order = ScanOrder::ADC;
bool burst_frames = true;
SpiEngine engine = SpiEngine::BLOCKING;
uint32_t settle_us = kDefaultMuxSettleUs;
bool vmid = false, running = false;
uint32_t run_started_ms = 0, duration_ms = 0, sweep_count = 0;
struct PreparedGroup {
  uint8_t array, input, destination;
  Adc124s101::PreparedBatch batch;
};
PreparedGroup groups[kMaxRoutes];
uint8_t group_count = 0;
struct PreparedPhase { uint8_t first, count, input; };
PreparedPhase phases[kMaxRoutes];
uint8_t phase_count = 0;
uint16_t samples[kMaxRoutes];
uint8_t wire_block[4 + kMaxRoutes * 2 + api_protocol::kTrailerBytes];
uint32_t sampling_sweeps = 0, usb_frames_sent = 0, usb_frames_discarded = 0;
uint32_t sampling_previous_start = 0, sampling_period_max_us = 0, sampling_period_over_1ms = 0;
uint32_t session_start_errors = 0;
#if TESTBOARD_PROFILE
AcquisitionProfiler profile_;
#endif

class LpspiSession {
 public:
  bool begin() { active_ = spi.beginLpspiSession(); return active_; }
  void close() { if (active_) { spi.endLpspiSession(); active_ = false; } }
  ~LpspiSession() { close(); }
 private:
  bool active_ = false;
};

bool parseUint(const String &text, uint32_t minimum, uint32_t maximum,
               uint32_t &value) {
  if (!text.length()) return false;
  uint32_t parsed = 0;
  for (unsigned int i = 0; i < text.length(); ++i) {
    char c = text.charAt(i);
    if (c < '0' || c > '9') return false;
    uint8_t digit = c - '0';
    if (parsed > maximum / 10 ||
        (parsed == maximum / 10 && digit > maximum % 10)) return false;
    parsed = parsed * 10 + digit;
  }
  if (parsed < minimum) return false;
  value = parsed;
  return true;
}

void parkAll() { for (auto adc : adcs) adc->park(settle_us); }
void stop() { running = false; spi.endLpspiSession(); parkAll(); }
bool selected(uint8_t array) { return array_mask & (1 << (array - 1)); }

uint16_t orderKey(const SensorRoute &r) {
  if (scan_order == ScanOrder::MUX)
    return r.input * 8 + (r.array - 1) * 4 + r.mux - 1;
  return r.array * 32 + r.input * 4 + r.mux - 1;
}

void buildPlan() {
  plan_count = 0;
  for (uint8_t i = 0; i < route_count; ++i)
    if (selected(routes[i].array)) plan[plan_count++] = routes[i];
  for (uint8_t i = 1; i < plan_count; ++i) {
    SensorRoute r = plan[i];
    uint8_t j = i;
    while (j && orderKey(plan[j - 1]) > orderKey(r)) {
      plan[j] = plan[j - 1]; --j;
    }
    plan[j] = r;
  }
}

bool prepareAcquisition() {
  for (auto adc : adcs) adc->refreshTiming();
  group_count = 0;
  phase_count = 0;
  for (uint8_t i = 0; i < plan_count;) {
    const SensorRoute &r = plan[i];
    uint8_t end = i + 1;
    while (end < plan_count && plan[end].array == r.array && plan[end].input == r.input) ++end;
    PreparedGroup &group = groups[group_count++];
    group.array = r.array; group.input = r.input; group.destination = i;
    if (!adcs[r.array - 1]->prepareBatch(plan + i, end - i, channel_repeat, group.batch)) return false;
    if (scan_order == ScanOrder::MUX && phase_count && phases[phase_count - 1].input == r.input)
      ++phases[phase_count - 1].count;
    else phases[phase_count++] = {uint8_t(group_count - 1), 1, r.input};
    i = end;
  }
  return group_count != 0;
}

bool setRoutes(const String &args) {
  SensorRoute candidate[kMaxRoutes];
  uint8_t count = 0;
  int start = 0;
  while (start < static_cast<int>(args.length())) {
    int end = args.indexOf(',', start);
    if (end < 0) end = args.length();
    String token = args.substring(start, end);
    token.trim();
    int first = token.indexOf(':'), second = token.indexOf(':', first + 1);
    if (first < 1 || second < first + 2 || count >= kMaxRoutes) return false;
    uint32_t array, mux, input;
    if (!parseUint(token.substring(0, first), 1, 2, array) ||
        !parseUint(token.substring(first + 1, second), 1, 4, mux) ||
        !parseUint(token.substring(second + 1), 0, 7, input)) return false;
    SensorRoute r{static_cast<uint8_t>(array), static_cast<uint8_t>(mux),
                  static_cast<uint8_t>(input)};
    if (!validSensorRoute(r)) return false;
    for (uint8_t i = 0; i < count; ++i)
      if (candidate[i].array == r.array && candidate[i].mux == r.mux &&
          candidate[i].input == r.input) return false;
    candidate[count++] = r;
    start = end + 1;
    if (start == static_cast<int>(args.length())) return false;
  }
  if (!count) return false;
  memcpy(routes, candidate, sizeof(SensorRoute) * count);
  route_count = count;
  buildPlan();
  return true;
}

const char *orderName() {
  return scan_order == ScanOrder::ADC ? "adc" : "mux";
}
const char *engineName() {
  if (engine == SpiEngine::DMA) return "dma";
  if (engine == SpiEngine::LPSPI) return "lpspi";
  return "blocking";
}
void printRoute(const SensorRoute &r) {
  Serial.print(r.array); Serial.print(':');
  Serial.print(r.mux); Serial.print(':'); Serial.print(r.input);
}
void status() {
  Serial.println("# board=TestBoard_ADC124");
  Serial.println("# protocol_version=3");
  Serial.println("# firmware_revision=adc124-shared-mux-burst-v1");
  Serial.print("# running="); Serial.println(running ? "true" : "false");
  Serial.println("# mode=PZT");
  Serial.print("# array="); Serial.println(array_mask == 3 ? "both" : array_mask == 1 ? "1" : "2");
  Serial.print("# scanorder="); Serial.println(orderName());
  Serial.print("# scan_semantics="); Serial.println(scan_order == ScanOrder::ADC ? "array_sequential_address_grouped" : "shared_address_settle_adc_sequential");
  Serial.print("# spi_frame="); Serial.println(burst_frames ? "burst" : "single");
  Serial.print("# spiengine="); Serial.println(engineName());
  Serial.print("# spi_clock_hz="); Serial.println(spi.clockHz());
  Serial.print("# channelrepeat="); Serial.println(channel_repeat);
  Serial.print("# mux_settle_us="); Serial.println(settle_us);
  Serial.print("# vmid_between_groups="); Serial.println(vmid ? "true" : "false");
  Serial.println("# vmid_address=7");
  Serial.println("# vmid_parking=mandatory");
  Serial.println("# vmid_parking_scope=start_stop_failure");
  Serial.println("# adc_pipeline_frames=1");
  Serial.println("# vref=3.3");
  Serial.println("# vmid_volts=1.65");
  Serial.println("# active_spi_buses=1");
  Serial.println("# mux_enable_active_high=true,false");
  Serial.print("# adcchannels=");
  for (uint8_t i = 0; i < route_count; ++i) {
    if (i) Serial.print(',');
    printRoute(routes[i]);
  }
  Serial.println();
  Serial.print("# payload_routes=");
  for (uint8_t i = 0; i < plan_count; ++i) {
    if (i) Serial.print(',');
    printRoute(plan[i]);
  }
  Serial.println();
  Serial.print("# route_count="); Serial.println(plan_count);
  Serial.print("# sweep_count="); Serial.println(sweep_count);
  Serial.print("# transfer_errors="); Serial.println(adc1.transferErrors() + adc2.transferErrors() + session_start_errors);
  Serial.print("# data_errors="); Serial.println(adc1.dataErrors() + adc2.dataErrors());
  Serial.print("# usb_write_errors="); Serial.println(usb.writeErrors());
  Serial.println("# usb_stream_policy=discard_if_busy");
  Serial.print("# sampling_sweeps="); Serial.println(sampling_sweeps);
  Serial.print("# usb_frames_sent="); Serial.println(usb_frames_sent);
  Serial.print("# usb_frames_discarded="); Serial.println(usb_frames_discarded);
  Serial.print("# sampling_period_max_us="); Serial.println(sampling_period_max_us);
  Serial.print("# sampling_period_over_1ms="); Serial.println(sampling_period_over_1ms);
#if TESTBOARD_PROFILE
  profile_.printStatus(running);
#else
  Serial.println("# profile_available=false");
  Serial.println("# profile_enabled=false");
#endif
}

bool command(const String &name, const String &args) {
  uint32_t value;
  if (name == "stop") { stop(); return args.length() == 0; }
  if (name == "mcu") { Serial.println("# TestBoard_ADC124"); return !args.length(); }
  if (name == "status") { status(); return !args.length(); }
  if (name == "help") {
    Serial.println("# array 1|2|both; adcchannels array:mux:input,...; scanorder adc|mux (protocol 3 semantics)");
    Serial.println("# spiframe burst|single; adc=finish array A then B; mux=switch both then shared wait");
    Serial.println("# spiengine blocking|dma|lpspi; spiclock 8000000..16000000; channelrepeat 1..3");
    Serial.println("# muxsettle 0..1000 (us); vmid true|false|7; ref 3.3 (fixed); mode PZT");
    Serial.println("# run [1..3600000 ms]; stop; status; mcu; help; commands end with '*'");
    Serial.println("# profile on|off (diagnostic build, stopped only); USB policy: discard_if_busy");
    return !args.length();
  }
  if (name == "mode") return args == "PZT" || args == "pzt";
  if (name == "profile") {
#if TESTBOARD_PROFILE
    return !running && profile_.setEnabled(args);
#else
    return false;
#endif
  }
  if (name == "ref") return args == "3.3";
  if (name == "adcchannels") return setRoutes(args);
  if (name == "array") {
    if (args == "1") array_mask = 1;
    else if (args == "2") array_mask = 2;
    else if (args == "both") array_mask = 3;
    else return false;
    buildPlan(); return true;
  }
  if (name == "scanorder") {
    if (args == "adc") scan_order = ScanOrder::ADC;
    else if (args == "mux") scan_order = ScanOrder::MUX;
    else return false;
    buildPlan(); return true;
  }
  if (name == "spiframe") {
    if (args == "burst") burst_frames = true;
    else if (args == "single") burst_frames = false;
    else return false;
    return true;
  }
  if (name == "spiengine") {
    if (args == "blocking") engine = SpiEngine::BLOCKING;
    else if (args == "dma") engine = SpiEngine::DMA;
    else if (args == "lpspi") engine = SpiEngine::LPSPI;
    else return false;
    return true;
  }
  if (name == "spiclock") return parseUint(args, kMinSpiClockHz, kMaxSpiClockHz, value) && spi.setClockHz(value);
  if (name == "channelrepeat") {
    if (!parseUint(args, 1, 3, value)) return false;
    channel_repeat = value; return true;
  }
  if (name == "muxsettle") {
    if (!parseUint(args, 0, kMaxMuxSettleUs, value)) return false;
    settle_us = value; return true;
  }
  if (name == "vmid" || name == "ground") {
    if (args == "true" || args == "7") vmid = true;
    else if (args == "false") vmid = false;
    else return false;
    return true;
  }
  if (name == "run") {
    if (!plan_count || adc1.transferErrors() || adc2.transferErrors() || session_start_errors || usb.writeErrors()) return false;
    value = 0;
    if (args.length() && !parseUint(args, 1, 3600000, value)) return false;
#if TESTBOARD_PROFILE
    profile_.reset();
    const uint32_t preparation_started = profile_.stamp();
#endif
    if (!prepareAcquisition()) return false;
    parkAll();
    for (uint8_t i = 0; i < 2; ++i) if (!selected(i + 1)) adcs[i]->disable();
    sampling_sweeps = usb_frames_sent = usb_frames_discarded = 0;
    sampling_period_max_us = sampling_period_over_1ms = 0;
#if TESTBOARD_PROFILE
    profile_.runPrepared(preparation_started);
#endif
    duration_ms = value; run_started_ms = millis(); running = true;
    return true;
  }
  return false;
}

bool capture() {
#if TESTBOARD_PROFILE
  AcquisitionProfiler::Sweep profile_sweep(profile_);
#endif
  const uint32_t started = micros();
#if TESTBOARD_PROFILE
  profile_.acquisitionBegin(started);
#endif
  LpspiSession session;
  {
    TB_PROFILE_SCOPE(session_profile, Session);
    if (engine == SpiEngine::LPSPI && !session.begin()) { ++session_start_errors; return false; }
  }
  for (uint8_t p = 0; p < phase_count; ++p) {
    const PreparedPhase &phase = phases[p];
    // CS gates DOUT on the shared SPI bus. The unity buffers isolate each
    // analog bank: changing ADC ownership does not require external parking.
    {
      TB_PROFILE_SCOPE(mux_profile, Prepare);
      bool changed = false;
      for (uint8_t i = phase.first; i < phase.first + phase.count; ++i)
        changed |= adcs[groups[i].array - 1]->switchAddress(phase.input);
      // Starts AFTER all active banks' turn-on allowances, once per phase.
      if (changed && settle_us) delayMicroseconds(settle_us);
    }
    {
      TB_PROFILE_SCOPE(transfer_profile, Transfer);
      for (uint8_t i = phase.first; i < phase.first + phase.count; ++i) {
        const PreparedGroup &group = groups[i];
        if (!adcs[group.array - 1]->readPrepared(group.batch, engine, samples + group.destination, burst_frames)) return false;
      }
    }
    if (vmid) {
      {
        TB_PROFILE_SCOPE(vmid_switch_profile, Prepare);
        bool changed = false;
        for (uint8_t i = phase.first; i < phase.first + phase.count; ++i)
          changed |= adcs[groups[i].array - 1]->switchAddress(kVmidAddress);
        if (changed && settle_us) delayMicroseconds(settle_us);
      }
      {
        TB_PROFILE_SCOPE(vmid_transfer_profile, Transfer);
        for (uint8_t i = phase.first; i < phase.first + phase.count; ++i)
          if (!adcs[groups[i].array - 1]->readVmid(engine, burst_frames)) return false;
      }
    }
  }
  {
    TB_PROFILE_SCOPE(session_profile, Session);
    session.close();  // Restore ordinary SPI state before any USB work.
  }
  const uint32_t ended = micros();
  if (sampling_sweeps) {
    const uint32_t period = started - sampling_previous_start;
    if (period > sampling_period_max_us) sampling_period_max_us = period;
    if (period > 1000) ++sampling_period_over_1ms;
  }
  sampling_previous_start = started;
  ++sampling_sweeps;
  ++sweep_count;
#if TESTBOARD_PROFILE
  profile_.acquisitionEnd(ended);
#endif
  uint32_t size;
  {
    TB_PROFILE_SCOPE(encode_profile, Encode);
    const uint32_t average = (ended - started + plan_count / 2) / plan_count;
    size = api_protocol::encodeBinaryBlock(wire_block, sizeof(wire_block), samples,
        plan_count, average > 65535 ? 65535 : average, started, ended);
    if (!size) return false;
  }
#if TESTBOARD_PROFILE
  if (profile_.enabled()) {
    TB_PROFILE_SCOPE(capacity_profile, Capacity);
    profile_.noteCapacity(Serial.availableForWrite(), size);
  }
#endif
  UsbSerialController::FrameWrite result;
  {
    TB_PROFILE_SCOPE(write_profile, UsbWrite);
    result = usb.tryWriteBinaryBlock(wire_block, size);
  }
  const bool written = result == UsbSerialController::FrameWrite::Sent;
  const bool discarded = result == UsbSerialController::FrameWrite::Discarded;
  if (written) ++usb_frames_sent;
  if (discarded) ++usb_frames_discarded;
#if TESTBOARD_PROFILE
  profile_sweep.written(written, discarded);
#endif
  return written || discarded;
}
}  // namespace

void setup() {
  usb.begin(kUsbSerialBaud);
  for (uint8_t cs : kCsPins) spi.registerChipSelect(cs);
  spi.begin();
  for (auto adc : adcs) {
    adc->begin();
#if TESTBOARD_PROFILE
    adc->setProfiler(profile_);
#endif
  }
  for (uint8_t array = 1; array <= 2; ++array)
    for (uint8_t mux = 1; mux <= 4; ++mux)
      for (uint8_t input = 0; input <= (mux == 4 ? 6 : 5); ++input)
        routes[route_count++] = {array, mux, input};
  buildPlan();
  Serial.println("# TestBoard_ADC124");
}

void loop() {
  String line;
  while (usb.readCommand(line)) {
    line.trim();
    if (!line.length()) continue;
    String name, args;
    api_protocol::splitCommand(line, name, args);
    const bool was_running = running;
    // Commands stop the stream at a complete-frame boundary before text.
    if (running) stop();
    if (name == "profile" && was_running) { usb.writeAck(false); continue; }
    bool success = command(name, args);
    if (!(name == "run" && success)) usb.writeAck(success);
  }
  if (running) {
    if (duration_ms && static_cast<uint32_t>(millis() - run_started_ms) >= duration_ms) stop();
    else if (!capture()) stop();
  }
  yield();
}
