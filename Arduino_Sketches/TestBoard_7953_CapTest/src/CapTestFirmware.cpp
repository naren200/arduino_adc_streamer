#include "CapTestFirmware.h"

#include <Arduino.h>
#include <SPI.h>

#include "Ads7953Adc.h"
#include "ApiProtocol.h"
#include "BurstEngine.h"
#include "CapTestProtocol.h"
#include "ConfigurableParameters.h"
#include "SpiController.h"
#include "UsbSerialController.h"

#ifndef CAPTEST_GIT_HASH
#define CAPTEST_GIT_HASH "unknown"
#endif

// No EEPROM or flash storage is used anywhere in this firmware, so flashing
// between production and capacitance-test firmware loses nothing.

namespace captest_firmware {
namespace {

using namespace captest;

constexpr uint32_t kNoBootId = 0;

UsbSerialController g_usb;
SpiController g_spi1(SPI, testboard_config::kSpi1MisoPin,
                     testboard_config::kSpi1MosiPin, testboard_config::kSpi1SckPin);
SpiController g_spi2(SPI1, testboard_config::kSpi2MisoPin,
                     testboard_config::kSpi2MosiPin, testboard_config::kSpi2SckPin);
Ads7953Adc g_adc1(g_spi1, testboard_config::kAdc1CsPin);
Ads7953Adc g_adc2(g_spi1, testboard_config::kAdc2CsPin);
Ads7953Adc g_adc3(g_spi2, testboard_config::kAdc3CsPin);
Ads7953Adc g_adc4(g_spi2, testboard_config::kAdc4CsPin);
Ads7953Adc *g_adcs[] = {&g_adc1, &g_adc2, &g_adc3, &g_adc4};

BurstEngine g_engine;
constexpr size_t kPacketCapacity =
    kHeaderBytes + 2u * kMaxVmidWords + 2u * kMaxFrames + 4u * kMaxFrames + 2u;
uint8_t g_packet[kPacketCapacity];
uint32_t g_boot_id = kNoBootId;
uint8_t g_last_adc = 0;  // 1..4, last ADC used (for capabort re-park)

uint16_t crc16(const uint8_t *data, size_t length) {
  // CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, no xorout.
  uint16_t crc = 0xFFFF;
  for (size_t index = 0; index < length; ++index) {
    crc ^= static_cast<uint16_t>(data[index]) << 8;
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc & 0x8000) ? static_cast<uint16_t>((crc << 1) ^ 0x1021)
                           : static_cast<uint16_t>(crc << 1);
    }
  }
  return crc;
}

void put16(uint8_t *dst, uint16_t value) {
  dst[0] = static_cast<uint8_t>(value);
  dst[1] = static_cast<uint8_t>(value >> 8);
}

void put32(uint8_t *dst, uint32_t value) {
  put16(dst, static_cast<uint16_t>(value));
  put16(dst + 2, static_cast<uint16_t>(value >> 16));
}

// The boot id changes on every boot: it mixes the RTC counter, the chip id and
// the cycle counter at the first host command (host timing is not reproducible).
uint32_t bootId() {
  if (g_boot_id == kNoBootId) {
    uint32_t hash = 2166136261u;
    const uint32_t parts[] = {SNVS_HPRTCMR, SNVS_HPRTCLR, HW_OCOTP_CFG0,
                              HW_OCOTP_CFG1, ARM_DWT_CYCCNT, micros()};
    for (const uint32_t part : parts) {
      for (uint8_t shift = 0; shift < 32; shift += 8) {
        hash = (hash ^ ((part >> shift) & 0xFF)) * 16777619u;
      }
    }
    g_boot_id = hash == kNoBootId ? 1u : hash;
  }
  return g_boot_id;
}

Ads7953Adc *adcFor(long adc_number) {
  if (adc_number < 1 || adc_number > testboard_config::kAdcCount) return nullptr;
  return g_adcs[adc_number - 1];
}

bool setSpiClock(uint32_t hz) {
  if (hz > kMaxSpiClockHz) return false;
  return g_spi1.setClockHz(hz) && g_spi2.setClockHz(hz);
}

// Whitespace tokenizer for the argument string (no allocation beyond String).
struct Tokens {
  String item[10];
  uint8_t count = 0;
};

Tokens tokenize(const String &arguments) {
  Tokens tokens;
  int start = 0;
  const int length = arguments.length();
  while (start < length && tokens.count < 10) {
    while (start < length && arguments.charAt(start) == ' ') ++start;
    if (start >= length) break;
    int end = start;
    while (end < length && arguments.charAt(end) != ' ') ++end;
    tokens.item[tokens.count++] = arguments.substring(start, end);
    start = end;
  }
  return tokens;
}

bool parseUnsigned(const String &text, uint32_t &value) {
  if (!text.length()) return false;
  for (unsigned index = 0; index < text.length(); ++index) {
    if (!isdigit(text.charAt(index))) return false;
  }
  const unsigned long parsed = strtoul(text.c_str(), nullptr, 10);
  if (parsed > 0xFFFFFFFEul) return false;
  value = static_cast<uint32_t>(parsed);
  return true;
}

void printStatus() {
  String build = String(__DATE__) + "_" + String(__TIME__);
  build.replace(' ', '_');
  Serial.print(F("# capstatus firmware="));
  Serial.print(kFirmwareName);
  Serial.print(F(" version="));
  Serial.print(kFirmwareVersion);
  Serial.print(F(" protocol="));
  Serial.print(kProtocolVersion);
  Serial.print(F(" git="));
  Serial.print(F(CAPTEST_GIT_HASH));
  Serial.print(F(" build="));
  Serial.print(build);
  Serial.print(F(" boot_id=0x"));
  Serial.print(bootId(), HEX);
  Serial.print(F(" uptime_ms="));
  Serial.print(millis());
  Serial.print(F(" cpu_hz="));
  Serial.print(F_CPU_ACTUAL);
  Serial.print(F(" spi_clock_hz="));
  Serial.print(g_spi1.clockHz());
  Serial.print(F(" max_samples="));
  Serial.print(kMaxSamples);
  Serial.print(F(" min_period_ns="));
  Serial.println(kMinFramePeriodNs);
}

void printHelp() {
  Serial.println(F("# Commands are terminated by '*'."));
  Serial.println(F("# capstatus, capvmid <adc> [n], capabort, spiclock <hz <= 20000000>"));
  Serial.println(F("# capburst <adc 1-4> <channel> <samples|auto> <park_us> <gap_ns> [<first_clock_delay_ns> [<hold_mode 0|1>]]"));
  Serial.println(F("# capalt <adc 1-4> <channel> <frames> <park_us> <gap_ns> <sensor_run 1-8> <vmid_run 1-8> [<first_clock_delay_ns> [<gap_long_ns>]]"));
  Serial.println(F("# Binary burst reply follows '#OK' as documented in TESTBOARD_7953_CAPTEST.md"));
}

bool handleVmid(const String &arguments) {
  const Tokens tokens = tokenize(arguments);
  uint32_t adc_number = 0, count = 16;
  if (tokens.count < 1 || tokens.count > 2 || !parseUnsigned(tokens.item[0], adc_number)) {
    return false;
  }
  if (tokens.count == 2 && (!parseUnsigned(tokens.item[1], count) || count < 1 || count > 64)) {
    return false;
  }
  Ads7953Adc *adc = adcFor(adc_number);
  if (adc == nullptr) return false;
  // Two pipeline frames, then `count` Vmid readings.
  uint16_t words[66];
  if (!g_engine.park(*adc, static_cast<uint16_t>(count + 2), words)) return false;
  g_last_adc = static_cast<uint8_t>(adc_number);
  uint32_t sum = 0, minimum = 0xFFFF, maximum = 0, tag_errors = 0;
  for (uint32_t index = 2; index < count + 2; ++index) {
    const uint16_t code = adc->returnedSample(words[index]);
    sum += code;
    if (code < minimum) minimum = code;
    if (code > maximum) maximum = code;
    if (adc->returnedChannel(words[index]) != testboard_config::kDefaultVmidChannel) {
      ++tag_errors;
    }
  }
  Serial.print(F("# capvmid adc="));
  Serial.print(adc_number);
  Serial.print(F(" n="));
  Serial.print(count);
  Serial.print(F(" mean_code="));
  Serial.print(static_cast<float>(sum) / count, 3);
  Serial.print(F(" min="));
  Serial.print(minimum);
  Serial.print(F(" max="));
  Serial.print(maximum);
  Serial.print(F(" tag_errors="));
  Serial.println(tag_errors);
  return true;
}

bool handleAbort() {
  // A burst cannot be interrupted (it runs with interrupts masked and finishes
  // in a few ms), so abort only re-parks the converters on Vmid.
  bool ok = true;
  for (Ads7953Adc *adc : g_adcs) ok = g_engine.park(*adc, 4, nullptr) && ok;
  return ok;
}

void sendPacket(const BurstRequest &request, const BurstResult &result) {
  uint8_t *p = g_packet;
  memset(p, 0, kHeaderBytes);
  p[0] = kPacketMagic1;
  p[1] = kPacketMagic2;
  p[2] = kProtocolVersion;
  p[3] = kHeaderBytes;
  p[4] = request.adc_number;
  p[5] = request.channel;
  put16(p + 6, request.n_samples);
  put16(p + 8, result.n_frames);
  p[10] = result.n_vmid;
  p[11] = result.flags;
  put32(p + 12, result.cpu_hz);
  put32(p + 16, result.spi_clock_hz);
  put32(p + 20, request.gap_ns);
  put32(p + 24, request.first_clock_delay_ns);
  put32(p + 28, bootId());
  put32(p + 32, request.park_us);
  put32(p + 36, result.park_frames);
  put32(p + 40, result.period_min_cycles);
  put32(p + 44, result.period_sum_cycles);
  put32(p + 48, result.period_max_cycles);
  put16(p + 52, result.timeout_errors);
  put16(p + 54, result.tag_errors);
  size_t offset = kHeaderBytes;
  for (uint8_t index = 0; index < result.n_vmid; ++index, offset += 2) {
    put16(p + offset, g_engine.vmidWords()[index]);
  }
  for (uint16_t index = 0; index < result.n_frames; ++index, offset += 2) {
    put16(p + offset, g_engine.words()[index]);
  }
  for (uint16_t index = 0; index < result.n_frames; ++index, offset += 4) {
    put32(p + offset, g_engine.timestamps()[index]);
  }
  put16(p + offset, crc16(p, offset));
  offset += 2;
  // Written after the burst only; Serial.write has the patched core's bounded
  // wait and returns short when the host has gone away.
  g_usb.writeBinaryBlock(p, offset);
}

bool runBurst(Ads7953Adc &adc, const BurstRequest &request, const String &arguments);

bool handleBurst(const String &arguments) {
  const Tokens tokens = tokenize(arguments);
  if (tokens.count < 5 || tokens.count > 7) return false;
  uint32_t adc_number = 0, channel = 0, park_us = 0, gap_ns = 0, fcd_ns = 25, hold_mode = 0;
  if (!parseUnsigned(tokens.item[0], adc_number) ||
      !parseUnsigned(tokens.item[1], channel) ||
      !parseUnsigned(tokens.item[3], park_us) ||
      !parseUnsigned(tokens.item[4], gap_ns)) {
    return false;
  }
  if (tokens.count >= 6 && !parseUnsigned(tokens.item[5], fcd_ns)) return false;
  if (tokens.count == 7 && (!parseUnsigned(tokens.item[6], hold_mode) || hold_mode > 1)) return false;
  if (channel > 15 || !channelRouted(adc_number, channel)) return false;
  Ads7953Adc *adc = adcFor(adc_number);
  if (adc == nullptr) return false;
  if (park_us > kMaxParkUs || gap_ns > kMaxGapNs || fcd_ns > kMaxFirstClockDelayNs) {
    return false;
  }

  uint32_t n_samples = 0;
  if (tokens.item[2] == "auto") {
    n_samples = autoSampleCount(adc_number, adc->spiController().clockHz(), gap_ns);
  } else if (!parseUnsigned(tokens.item[2], n_samples)) {
    return false;
  }
  if (n_samples > kMaxSamples) return false;  // before the 16-bit narrowing

  BurstRequest request;
  request.adc_number = static_cast<uint8_t>(adc_number);
  request.channel = static_cast<uint8_t>(channel);
  request.n_samples = static_cast<uint16_t>(n_samples);
  request.park_us = park_us;
  request.gap_ns = gap_ns;
  request.first_clock_delay_ns = fcd_ns;
  request.continue_mode = hold_mode == 1;
  return runBurst(*adc, request, arguments);
}

// capalt <adc> <channel> <frames> <park_us> <gap_ns> <sensor_run> <vmid_run> [<fcd_ns> [<gap_long_ns>]]
// Same burst, but the commands repeat sensor_run x channel, vmid_run x Vmid
// after the park, like production with Vmid between channels. gap_long_ns
// (1.3.0, 0 = off) stretches the connected frames of every second cycle.
bool handleAlternate(const String &arguments) {
  const Tokens tokens = tokenize(arguments);
  if (tokens.count < 7 || tokens.count > 9) return false;
  uint32_t adc_number = 0, channel = 0, n_samples = 0, park_us = 0, gap_ns = 0;
  uint32_t sensor_run = 0, vmid_run = 0, fcd_ns = 25, gap_long_ns = 0;
  if (!parseUnsigned(tokens.item[0], adc_number) ||
      !parseUnsigned(tokens.item[1], channel) ||
      !parseUnsigned(tokens.item[2], n_samples) ||
      !parseUnsigned(tokens.item[3], park_us) ||
      !parseUnsigned(tokens.item[4], gap_ns) ||
      !parseUnsigned(tokens.item[5], sensor_run) ||
      !parseUnsigned(tokens.item[6], vmid_run)) {
    return false;
  }
  if (tokens.count >= 8 && !parseUnsigned(tokens.item[7], fcd_ns)) return false;
  if (tokens.count == 9 && !parseUnsigned(tokens.item[8], gap_long_ns)) return false;
  if (sensor_run < 1 || sensor_run > kMaxRunFrames || vmid_run < 1 || vmid_run > kMaxRunFrames) {
    return false;
  }
  if (channel > 15 || !channelRouted(adc_number, channel)) return false;
  Ads7953Adc *adc = adcFor(adc_number);
  if (adc == nullptr) return false;
  // The long gap must not be shorter: the period limits are checked on gap_ns.
  if (gap_long_ns != 0 && gap_long_ns < gap_ns) return false;
  if (park_us > kMaxParkUs || gap_ns > kMaxGapNs || gap_long_ns > kMaxGapNs ||
      fcd_ns > kMaxFirstClockDelayNs) {
    return false;
  }

  BurstRequest request;
  request.adc_number = static_cast<uint8_t>(adc_number);
  request.channel = static_cast<uint8_t>(channel);
  if (n_samples > kMaxSamples) return false;  // before the 16-bit narrowing
  request.n_samples = static_cast<uint16_t>(n_samples);
  request.park_us = park_us;
  request.gap_ns = gap_ns;
  request.first_clock_delay_ns = fcd_ns;
  request.continue_mode = false;
  request.sensor_run = static_cast<uint8_t>(sensor_run);
  request.vmid_run = static_cast<uint8_t>(vmid_run);
  request.gap_long_ns = gap_long_ns;
  return runBurst(*adc, request, arguments);
}

// Shared limits, the burst itself and the reply of capburst and capalt.
bool runBurst(Ads7953Adc &adc, const BurstRequest &request, const String &arguments) {
  if (request.n_samples < 8 || request.n_samples > kMaxSamples) return false;

  // Frame period and acquisition limits (nominal; measured values are reported).
  const uint32_t clock_hz = adc.spiController().clockHz();
  const uint32_t period_ns = nominalFramePeriodNs(clock_hz, request.gap_ns);
  if (period_ns < kMinFramePeriodNs) return false;
  const uint32_t clock_period_ns = 1000000000UL / clock_hz;
  if (period_ns < request.first_clock_delay_ns + 13u * clock_period_ns + kMinAcquisitionNs) {
    return false;
  }
  // Bounded as if every frame used the longer gap.
  const uint32_t longest_ns =
      request.gap_long_ns > request.gap_ns ? nominalFramePeriodNs(clock_hz, request.gap_long_ns) : period_ns;
  const uint64_t critical_us =
      static_cast<uint64_t>(request.park_us) +
      static_cast<uint64_t>(request.n_samples + 12u) * longest_ns / 1000u;
  if (critical_us > kMaxCriticalUs) return false;

  BurstResult result;
  g_last_adc = request.adc_number;
  const bool complete = g_engine.run(adc, request, result);
  // Acknowledge first, then the packet; an incomplete burst still returns its
  // partial data with FLAG_INCOMPLETE but is acknowledged as #NOT_OK.
  g_usb.writeAck(complete, arguments);
  sendPacket(request, result);
  return true;  // ack already written
}

void handleLine(const String &raw_line) {
  String line = raw_line;
  line.trim();
  if (!line.length()) return;
  String command, arguments;
  api_protocol::splitCommand(line, command, arguments);
  bootId();  // latch at the first host command

  if (command == "capstatus" || command == "status") {
    printStatus();
    g_usb.writeAck(true, arguments);
  } else if (command == "capvmid") {
    g_usb.writeAck(handleVmid(arguments), arguments);
  } else if (command == "capburst") {
    if (!handleBurst(arguments)) g_usb.writeAck(false, arguments);
  } else if (command == "capalt") {
    if (!handleAlternate(arguments)) g_usb.writeAck(false, arguments);
  } else if (command == "capabort" || command == "stop") {
    g_usb.writeAck(handleAbort(), arguments);
  } else if (command == "spiclock") {
    uint32_t hz = 0;
    g_usb.writeAck(parseUnsigned(arguments, hz) && setSpiClock(hz), arguments);
  } else if (command == "mcu") {
    Serial.print(F("# "));
    Serial.println(testboard_config::kMcuName);
    g_usb.writeAck(true, arguments);
  } else if (command == "help") {
    printHelp();
    g_usb.writeAck(true, arguments);
  } else {
    g_usb.writeAck(false, arguments);
  }
}

}  // namespace

void setupFirmware() {
  // Same start-up sequence as the production firmware: CS lines high, both
  // buses initialised, every ADC primed and parked on Vmid.
  g_usb.begin(testboard_config::kUsbSerialBaud);
  g_spi1.begin();
  g_spi2.begin();
  for (Ads7953Adc *adc : g_adcs) adc->begin();
  Serial.print(F("# "));
  Serial.println(testboard_config::kMcuName);
  Serial.print(F("# firmware="));
  Serial.println(kFirmwareName);
}

void loopFirmware() {
  String line;
  while (g_usb.readCommand(line)) {
    handleLine(line);
  }
  yield();
}

}  // namespace captest_firmware
