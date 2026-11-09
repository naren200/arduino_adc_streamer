#pragma once

#include <Arduino.h>

#include "Ads7953Adc.h"
#include "CapTestProtocol.h"

// Continuous multi-channel capture (capture protocol 1, documented in
// docs/architecture/firmware/TESTBOARD_7953_CAPTEST.md).
//
// One *sweep* visits every configured route once, ADC block by ADC block, in the
// production-like manual sequence: sensor command, one Vmid command per route, then two
// flush Vmid commands so the converter is parked on Vmid when its block ends. Interrupts
// are masked only for one ADC block (tens of microseconds), never across a hold. Sweeps
// go into a RAM ring; the main loop sends whole chunks with the non-blocking USB frame
// write. A full ring drops the new sweep and says so (the sweep number still advances and
// the next chunk reports the dropped total): nothing is lost silently.
namespace captest {

struct ScanSetup {
  uint32_t gap_ns = 200;
  uint32_t first_clock_delay_ns = 25;
  uint32_t lag_us = 0;  // idle time after every sweep
  uint8_t n_routes = 0;
  uint8_t adc[kMaxScanRoutes] = {};      // 1..4, grouped (one block per ADC)
  uint8_t channel[kMaxScanRoutes] = {};
};

struct ScanRecord {  // on-wire record (little-endian, 12 bytes)
  uint32_t ts_conn;  // cycle counter at frame k+1 (the sensor connects)
  uint32_t ts_rel;   // cycle counter at frame k+2 (the Vmid command releases; sample time)
  uint16_t sensor;   // response of frame k+2 (tag = channel)
  uint16_t vmid;     // response of frame k+3 (tag = 15): the reset reading
};
static_assert(sizeof(ScanRecord) == kChunkRecordBytes, "record layout");

enum class ScanState : uint8_t { Idle, Running, Draining, Done };

class ScanEngine {
 public:
  // Limits shared by the command parser: false when the setup cannot be run.
  static bool valid(const ScanSetup &setup, uint32_t spi_clock_hz);
  static uint8_t blockCount(const ScanSetup &setup);
  static uint32_t framesPerSweep(const ScanSetup &setup);

  void begin(const ScanSetup &setup, uint32_t session, uint32_t boot_id, uint32_t max_sweeps);

  ScanState state() const { return state_; }
  bool active() const { return state_ == ScanState::Running || state_ == ScanState::Draining; }
  bool running() const { return state_ == ScanState::Running; }
  uint32_t lagUs() const { return setup_.lag_us; }

  // One sweep into the ring (does nothing unless Running). False: a transfer failed and the
  // capture is draining with the error flag.
  bool sweep(Ads7953Adc *const *adcs);

  // Ask for the end of the capture: remaining sweeps are sent, then the final chunk.
  void requestStop();
  // Give up at once (host gone, abort): no further chunks.
  void abort();
  void transferError() { error_ = true; }

  // Next chunk to send (pointer valid until chunkSent). False when nothing is ready.
  bool nextChunk(const uint8_t *&data, uint16_t &length);
  void chunkSent();

  uint32_t sweeps() const { return sweep_no_; }
  uint32_t dropped() const { return dropped_; }
  uint32_t chunks() const { return seq_; }
  uint32_t session() const { return session_; }
  uint32_t lastSendMs() const { return last_send_ms_; }

 private:
  bool runSweep(Ads7953Adc *const *adcs, ScanRecord *records);
  void startDraining(bool stopped_by_host);

  ScanSetup setup_;
  ScanState state_ = ScanState::Idle;
  uint32_t session_ = 0, boot_id_ = 0, max_sweeps_ = 0;
  uint32_t sweep_no_ = 0, dropped_ = 0, dropped_reported_ = 0, seq_ = 0;
  uint32_t head_ = 0, tail_ = 0;
  uint32_t last_send_ms_ = 0;
  uint8_t per_chunk_ = 1;
  uint8_t pending_sweeps_ = 0;  // sweeps in the chunk being sent
  bool tag_error_ = false, error_ = false, stopped_ = false, final_pending_ = false;
  bool chunk_ready_ = false;
  uint16_t chunk_length_ = 0;
  uint32_t sweep_stamp_[kRingSweeps];
  ScanRecord ring_[kRingSweeps][kMaxScanRoutes];
  uint8_t chunk_[kMaxChunkBytes];
};

}  // namespace captest
