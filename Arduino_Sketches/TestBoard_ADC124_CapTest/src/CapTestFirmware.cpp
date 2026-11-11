#include "CapTestFirmware.h"

#include <Arduino.h>
#include <SPI.h>

#include "Adc124s101.h"
#include "ApiProtocol.h"
#include "CapBurstEngine.h"
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
using testboard_config::kBiasResistorOhms;
using testboard_config::kMuxToAdcInput;

constexpr uint32_t kNoBootId = 0;
constexpr size_t kPacketCapacity = kHeaderBytes + 6u * kMaxFrames + 2u;

UsbSerialController g_usb;
SpiController g_spi(SPI, testboard_config::kMisoPin, testboard_config::kMosiPin,
                    testboard_config::kSckPin);
Adc124s101 g_bank1(g_spi, 0), g_bank2(g_spi, 1);
Adc124s101 *g_banks[] = {&g_bank1, &g_bank2};

CapBurstEngine g_engine;
uint8_t g_packet[kPacketCapacity];
uint32_t g_boot_id = kNoBootId;
uint32_t g_bursts = 0;

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

// Whitespace tokenizer for the argument string.
struct Tokens {
  String item[8];
  uint8_t count = 0;
};

Tokens tokenize(const String &arguments) {
  Tokens tokens;
  int start = 0;
  const int length = arguments.length();
  while (start < length && tokens.count < 8) {
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

uint8_t popcount4(uint8_t mask) {
  uint8_t count = 0;
  for (uint8_t bit = 0; bit < 4; ++bit) count += (mask >> bit) & 1u;
  return count;
}

// MUX1..4 -> ADC IN1..IN4 is an assumed PCB connection (kMuxToAdcInput).
// Returns 1..4, or 0 when no mux feeds that ADC input.
uint8_t muxForAdcInput(uint8_t adc_input) {
  for (uint8_t mux = 0; mux < 4; ++mux)
    if (kMuxToAdcInput[mux] == adc_input) return mux + 1;
  return 0;
}

// Every converted input must see a populated sensor at `address`.
bool validConnection(uint8_t array, uint8_t address, uint8_t input_mask) {
  if (array < 1 || array > 2 || input_mask == 0 || input_mask > 15) return false;
  for (uint8_t input = 0; input < 4; ++input) {
    if (!(input_mask & (1u << input))) continue;
    const uint8_t mux = muxForAdcInput(input);
    if (!mux || !validSensorRoute({array, mux, address})) return false;
  }
  return true;
}

bool setSpiClock(uint32_t hz) { return g_spi.setClockHz(hz); }

// Cover 6 tau of a guessed 100 pF behind the largest bias resistor of the
// converted inputs, using the nominal 16-clock frame period.
uint16_t autoPostFrames(uint8_t array, uint8_t input_mask, uint32_t spi_clock_hz,
                        uint16_t pre_frames) {
  uint32_t resistor = 0;
  for (uint8_t input = 0; input < 4; ++input) {
    if (!(input_mask & (1u << input))) continue;
    const uint8_t mux = muxForAdcInput(input);
    if (mux && kBiasResistorOhms[array - 1][mux - 1] > resistor)
      resistor = kBiasResistorOhms[array - 1][mux - 1];
  }
  const uint64_t six_tau_ns = 6ULL * resistor / 10u;  // R x 100 pF = R x 0.1 ns
  const uint64_t frame_ns = 16000000000ULL / spi_clock_hz;
  uint64_t count = six_tau_ns / frame_ns + 1;
  const uint16_t cap = kMaxFrames - pre_frames < kDefaultAutoCap ? kMaxFrames - pre_frames
                                                                  : kDefaultAutoCap;
  if (count > cap) count = cap;
  if (count < kMinPostFrames) count = kMinPostFrames;
  return static_cast<uint16_t>(count);
}

void parkAll() {
  for (Adc124s101 *bank : g_banks) bank->park(0);
}

void printStatus() {
  String build = String(__DATE__) + "_" + String(__TIME__);
  build.replace(' ', '_');
  Serial.print(F("# capstatus firmware="));
  Serial.print(kFirmwareName);
  Serial.print(F(" board="));
  Serial.print(kBoardName);
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
  Serial.print(g_spi.clockHz());
  Serial.print(F(" max_samples="));
  Serial.print(kMaxFrames);
  Serial.print(F(" state="));
  Serial.print(g_engine.faulted() ? F("faulted") : F("idle"));
  Serial.print(F(" bursts="));
  Serial.print(g_bursts);
  Serial.print(F(" transfer_errors="));
  Serial.print(g_engine.transferErrors());
  Serial.print(F(" hold_clocks="));
  Serial.print(kHoldClocksAssumed);
  Serial.print(F(" guard_ns="));
  Serial.print(testboard_config::kMuxDisableTimeNs);
  Serial.print(F(" min_spi_hz="));
  Serial.print(testboard_config::kMinSpiClockHz);
  Serial.print(F(" max_spi_hz="));
  Serial.println(testboard_config::kMaxSpiClockHz);
}

void printHelp() {
  Serial.println(F("# Commands are terminated by '*'."));
  Serial.println(F("# capstatus, capabort, mcu, spiclock <8000000..16000000>"));
  Serial.println(F("# capvmid <array 1-2> <adc input 1-4|0=all> [n per input, default 16]"));
  Serial.println(F("# capburst <array 1-2> <address 0-6> <input_mask 1-15> <samples|auto> <park_us> <pre_samples> [<spiclock_hz>]"));
  Serial.println(F("# Binary burst reply follows '#OK' as documented in TESTBOARD_ADC124_CAPTEST.md"));
}

// Parked read of every converted input; also exercises the burst engine.
bool handleVmid(const String &arguments) {
  const Tokens tokens = tokenize(arguments);
  uint32_t array = 0, input = 0, per_input = 16;
  if (tokens.count < 2 || tokens.count > 3 || !parseUnsigned(tokens.item[0], array) ||
      !parseUnsigned(tokens.item[1], input) || array < 1 || array > 2 || input > 4) {
    return false;
  }
  if (tokens.count == 3 &&
      (!parseUnsigned(tokens.item[2], per_input) || per_input < 1 || per_input > kMaxVmidReads)) {
    return false;
  }
  BurstRequest request;
  request.array = static_cast<uint8_t>(array);
  request.address = testboard_config::kVmidAddress;
  request.input_mask = input == 0 ? 0x0F : static_cast<uint8_t>(1u << (input - 1));
  const uint8_t inputs = popcount4(request.input_mask);
  request.pre_frames = static_cast<uint16_t>(per_input * inputs);
  request.post_frames = 0;
  request.park_us = 0;
  BurstResult result;
  if (!g_engine.run(*g_banks[array - 1], g_spi, request, result) ||
      result.n_frames != request.pre_frames) {
    return false;
  }
  uint32_t sum[4] = {}, count[4] = {}, minimum = 0xFFFF, maximum = 0;
  uint8_t slot = 0;
  for (uint16_t frame = 0; frame < result.n_frames; ++frame) {
    while (!(request.input_mask & (1u << slot))) slot = (slot + 1) & 3;
    const uint16_t code = g_engine.words()[frame] & 0x0FFF;
    sum[slot] += code;
    ++count[slot];
    if (code < minimum) minimum = code;
    if (code > maximum) maximum = code;
    slot = (slot + 1) & 3;
  }
  uint32_t total = sum[0] + sum[1] + sum[2] + sum[3];
  Serial.print(F("# capvmid array="));
  Serial.print(array);
  Serial.print(F(" mask="));
  Serial.print(request.input_mask);
  Serial.print(F(" n="));
  Serial.print(per_input);
  Serial.print(F(" mean_code="));
  Serial.print(static_cast<float>(total) / result.n_frames, 3);
  Serial.print(F(" min="));
  Serial.print(minimum);
  Serial.print(F(" max="));
  Serial.print(maximum);
  for (uint8_t i = 0; i < 4; ++i) {
    Serial.print(F(" in"));
    Serial.print(i + 1);
    Serial.print('=');
    if (count[i]) Serial.print(static_cast<float>(sum[i]) / count[i], 3);
    else Serial.print('-');
  }
  Serial.print(F(" data_errors="));
  Serial.println(result.data_errors);
  return true;
}

bool handleAbort() {
  // Bursts are bounded (a few ms) and re-park on their own; abort re-parks both
  // banks on VMID and is the host's "board is safe" command.
  parkAll();
  return true;
}

void sendPacket(const BurstRequest &request, const BurstResult &result) {
  uint8_t *p = g_packet;
  memset(p, 0, kHeaderBytes);
  p[0] = kPacketMagic1;
  p[1] = kPacketMagic2;
  p[2] = kProtocolVersion;
  p[3] = kHeaderBytes;
  p[4] = request.array;
  p[5] = request.address;
  p[6] = request.input_mask;
  p[7] = popcount4(request.input_mask);
  put16(p + 8, result.n_frames);
  put16(p + 10, request.pre_frames);
  p[12] = result.flags;
  p[13] = kHoldClocksAssumed;
  put16(p + 14, static_cast<uint16_t>(testboard_config::kMuxDisableTimeNs));
  put32(p + 16, result.cpu_hz);
  put32(p + 20, result.spi_clock_hz);
  put32(p + 24, bootId());
  put32(p + 28, request.park_us);
  put32(p + 32, result.start_cycles);
  put32(p + 36, result.lead_cycles);
  put32(p + 40, result.switch_begin_cycles);
  put32(p + 44, result.switch_end_cycles);
  put32(p + 48, result.period_min_cycles);
  put32(p + 52, result.period_sum_cycles);
  put32(p + 56, result.period_max_cycles);
  put16(p + 60, result.transfer_errors);
  put16(p + 62, result.data_errors);
  size_t offset = kHeaderBytes;
  for (uint16_t index = 0; index < result.n_frames; ++index, offset += 2) {
    put16(p + offset, g_engine.words()[index]);
  }
  for (uint16_t index = 0; index < result.n_frames; ++index, offset += 4) {
    put32(p + offset, g_engine.stamps()[index]);
  }
  put16(p + offset, crc16(p, offset));
  offset += 2;
  // Written after the burst only; Serial.write has the patched core's bounded
  // wait and returns short when the host has gone away.
  g_usb.writeBinaryBlock(p, offset);
}

// capburst <array> <address> <input_mask> <samples|auto> <park_us> <pre_samples> [<spiclock_hz>]
bool handleBurst(const String &arguments) {
  const Tokens tokens = tokenize(arguments);
  if (tokens.count < 6 || tokens.count > 7) return false;
  uint32_t array = 0, address = 0, mask = 0, park_us = 0, pre = 0, clock_hz = 0;
  if (!parseUnsigned(tokens.item[0], array) || !parseUnsigned(tokens.item[1], address) ||
      !parseUnsigned(tokens.item[2], mask) || !parseUnsigned(tokens.item[4], park_us) ||
      !parseUnsigned(tokens.item[5], pre)) {
    return false;
  }
  if (tokens.count == 7) {
    if (!parseUnsigned(tokens.item[6], clock_hz)) return false;
  }
  if (array < 1 || array > 2 || address > 6 || mask < 1 || mask > 15) return false;
  if (!validConnection(static_cast<uint8_t>(array), static_cast<uint8_t>(address),
                       static_cast<uint8_t>(mask))) {
    return false;
  }
  if (park_us > kMaxParkUs || pre < 1 || pre > kMaxPreFrames) return false;
  if (g_engine.faulted()) return false;
  if (clock_hz && !setSpiClock(clock_hz)) return false;

  const uint8_t inputs = popcount4(static_cast<uint8_t>(mask));
  // Whole round-robin cycles, so every converted input gets the same count.
  const uint16_t pre_frames = static_cast<uint16_t>((pre + inputs - 1) / inputs * inputs);
  uint32_t post = 0;
  if (tokens.item[3] == "auto") {
    post = autoPostFrames(static_cast<uint8_t>(array), static_cast<uint8_t>(mask),
                          g_spi.clockHz(), pre_frames);
  } else if (!parseUnsigned(tokens.item[3], post)) {
    return false;
  }
  post = (post + inputs - 1) / inputs * inputs;
  if (post < kMinPostFrames || pre_frames + post > kMaxFrames) return false;

  BurstRequest request;
  request.array = static_cast<uint8_t>(array);
  request.address = static_cast<uint8_t>(address);
  request.input_mask = static_cast<uint8_t>(mask);
  request.pre_frames = pre_frames;
  request.post_frames = static_cast<uint16_t>(post);
  request.park_us = park_us;

  BurstResult result;
  const bool complete = g_engine.run(*g_banks[array - 1], g_spi, request, result);
  ++g_bursts;
  // Acknowledge first, then the packet; an incomplete or aborted burst still
  // returns its partial data (flags say why) but is acknowledged as #NOT_OK.
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
  } else if (command == "capabort" || command == "stop") {
    g_usb.writeAck(handleAbort(), arguments);
  } else if (command == "spiclock") {
    uint32_t hz = 0;
    g_usb.writeAck(parseUnsigned(arguments, hz) && setSpiClock(hz), arguments);
  } else if (command == "mcu") {
    Serial.print(F("# "));
    Serial.println(kFirmwareName);
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
  // Same start-up sequence as the production firmware: CS lines high, bus
  // initialised, every bank enabled on VMID (address 7).
  g_usb.begin(testboard_config::kUsbSerialBaud);
  for (uint8_t cs : testboard_config::kCsPins) g_spi.registerChipSelect(cs);
  g_spi.begin();
  for (Adc124s101 *bank : g_banks) bank->begin();
  Serial.print(F("# "));
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
