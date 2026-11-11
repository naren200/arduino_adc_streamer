#pragma once

#include <Arduino.h>

// Binary burst reply of the TestBoard_ADC124 capacitance-test firmware. Layout
// and rationale: docs/architecture/firmware/TESTBOARD_ADC124_CAPTEST.md
// (little-endian).
namespace captest {

static constexpr const char kFirmwareName[] = "TestBoard_ADC124_CapTest";
static constexpr const char kBoardName[] = "TestBoard_ADC124";
static constexpr const char kFirmwareVersion[] = "1.0.0";
static constexpr uint8_t kProtocolVersion = 1;

static constexpr uint8_t kPacketMagic1 = 'C';
static constexpr uint8_t kPacketMagic2 = 'B';
static constexpr uint8_t kHeaderBytes = 64;

// Returned frames per burst (pre-connect + post-connect). RAM: 6 bytes per
// frame for the results plus the same again for the packet buffer.
static constexpr uint16_t kMaxFrames = 4096;
static constexpr uint16_t kMaxPreFrames = 64;
static constexpr uint16_t kMinPostFrames = 16;
static constexpr uint16_t kDefaultAutoCap = 2048;
static constexpr uint16_t kMaxVmidReads = 64;

static constexpr uint32_t kMaxParkUs = 10000;
// Assumed ADC124S101 hold instant: this many SCLK periods after the frame
// start. Informational only; the PC application owns the value it analyses with.
static constexpr uint8_t kHoldClocksAssumed = 3;
// A GPIO break-before-make switch normally takes about 2 x kMuxDisableTimeNs
// plus the register writes; anything above this limit is flagged.
static constexpr uint32_t kSwitchLimitNs = 1000;
static constexpr uint8_t kFrameClocks = 16;

enum Flags : uint8_t {
  FLAG_INCOMPLETE = 1u << 0,    // transfer error or timeout, fewer frames returned
  FLAG_ABORTED = 1u << 1,       // a host command arrived and ended the burst early
  FLAG_NO_SWITCH = 1u << 2,     // address 7 requested: parked read, no connection
  FLAG_PERIOD_SHORT = 1u << 3,  // mean frame period below 16 SCLK periods
  FLAG_SWITCH_SLOW = 1u << 4,   // GPIO switch longer than kSwitchLimitNs
  FLAG_DATA_ERRORS = 1u << 5,   // a response had non-zero upper 4 bits
  FLAG_STAMP_GAP = 1u << 6,     // a frame period above 3 x the mean (stall)
};

}  // namespace captest
