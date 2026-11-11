#pragma once

#include <Arduino.h>

// Binary burst reply of the capacitance-test firmware. Layout and rationale:
// docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md (little-endian).
namespace captest {

static constexpr const char kFirmwareName[] = "TestBoard_7953_CapTest";
static constexpr const char kFirmwareVersion[] = "1.4.0";
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

// Continuous capture (firmware 1.4.0, capture protocol 1). Chunk layout: see ScanEngine.h.
static constexpr uint8_t kCaptureProtocol = 1;
static constexpr uint8_t kCaptureChunkVersion = 1;
static constexpr uint8_t kChunkHeaderBytes = 36;
static constexpr uint8_t kChunkRecordBytes = 12;
static constexpr uint8_t kMaxScanRoutes = 64;
static constexpr uint8_t kMaxSweepsPerChunk = 8;
// One chunk is one USB frame: it must fit the patched core's TX_SIZE (2048 bytes).
static constexpr uint16_t kMaxChunkBytes = 2048;
static constexpr uint16_t kRingSweeps = 128;
static constexpr uint32_t kMaxScanLagUs = 1000000;
// Interrupts are masked for one ADC block of a sweep only; bound that time.
static constexpr uint32_t kMaxBlockCriticalUs = 5000;
// No chunk accepted by the host for this long: the capture ends (host gone) and parks.
static constexpr uint32_t kCaptureStallMs = 10000;

enum ChunkFlags : uint16_t {
  FLAG_CHUNK_OVERRUN = 1u << 0,    // sweeps were dropped (ring full)
  FLAG_CHUNK_FINAL = 1u << 1,      // last chunk of the session
  FLAG_CHUNK_TAG_ERRORS = 1u << 2,
  FLAG_CHUNK_RESET_BAD = 1u << 3,  // reserved: the host checks the Vmid readings
  FLAG_CHUNK_STOPPED = 1u << 4,    // ended by capstop
  FLAG_CHUNK_ERROR = 1u << 5,      // transfer error: abnormal end
};

enum Flags : uint8_t {
  FLAG_PERIOD_TOO_SHORT = 1u << 0,   // min measured period < 1.0 us
  FLAG_ACQUISITION_SHORT = 1u << 1,  // estimated acquisition < 325 ns
  FLAG_INCOMPLETE = 1u << 2,         // transfer timeout, fewer frames returned
  FLAG_TAG_ERRORS = 1u << 3,         // returned channel tag != commanded channel
  FLAG_CONTINUE_MODE = 1u << 4,      // sample frames used the "continue" command
  FLAG_ALTERNATE = 1u << 5,          // capalt: sensor and Vmid commands alternate
};

}  // namespace captest
