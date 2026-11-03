#pragma once

#include <Arduino.h>

// Binary burst reply of the capacitance-test firmware. Layout and rationale:
// docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md (little-endian).
namespace captest {

static constexpr const char kFirmwareName[] = "TestBoard_7953_CapTest";
static constexpr const char kFirmwareVersion[] = "1.3.0";
static constexpr uint8_t kProtocolVersion = 1;

static constexpr uint8_t kPacketMagic1 = 'C';
static constexpr uint8_t kPacketMagic2 = 'B';
static constexpr uint8_t kHeaderBytes = 56;
static constexpr uint8_t kMaxVmidWords = 16;
static constexpr uint16_t kMaxSamples = 2048;
// First two frames of a burst carry the pre-connection results (tag 15).
static constexpr uint16_t kLeadFrames = 2;
static constexpr uint16_t kMaxFrames = kMaxSamples + kLeadFrames;
static constexpr uint16_t kDefaultAutoCap = 1024;

// Frame period and acquisition limits (ADS7953 SLAS605C: tacq >= 325 ns).
static constexpr uint32_t kMinFramePeriodNs = 1000;
static constexpr uint32_t kMinAcquisitionNs = 325;
static constexpr uint32_t kMaxSpiClockHz = 20000000UL;
// Interrupts are masked for the whole burst; bound that time.
static constexpr uint32_t kMaxCriticalUs = 20000;
static constexpr uint32_t kMaxParkUs = 10000;
static constexpr uint32_t kMaxGapNs = 100000;
static constexpr uint32_t kMaxFirstClockDelayNs = 1000;
static constexpr uint8_t kReleaseFrames = 3;
// capalt: longest run of sensor or Vmid commands in one cycle.
static constexpr uint8_t kMaxRunFrames = 8;

enum Flags : uint8_t {
  FLAG_PERIOD_TOO_SHORT = 1u << 0,   // min measured period < 1.0 us
  FLAG_ACQUISITION_SHORT = 1u << 1,  // estimated acquisition < 325 ns
  FLAG_INCOMPLETE = 1u << 2,         // transfer timeout, fewer frames returned
  FLAG_TAG_ERRORS = 1u << 3,         // returned channel tag != commanded channel
  FLAG_CONTINUE_MODE = 1u << 4,      // sample frames used the "continue" command
  FLAG_ALTERNATE = 1u << 5,          // capalt: sensor and Vmid commands alternate
};

}  // namespace captest
