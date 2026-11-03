#pragma once

#include <Arduino.h>

#include "Adc124s101.h"
#include "CapTestProtocol.h"

namespace captest {

struct BurstRequest {
  uint8_t array;         // 1..2
  uint8_t address;       // 0..6 connect; testboard_config::kVmidAddress = parked read
  uint8_t input_mask;    // bit i = ADC input IN(i+1); converted round-robin
  uint16_t pre_frames;   // returned frames taken while still parked on VMID
  uint16_t post_frames;  // returned frames after the connection
  uint32_t park_us;
};

struct BurstResult {
  uint16_t n_frames = 0;  // returned frames (pre + post when complete)
  uint8_t flags = 0;
  uint32_t cpu_hz = 0;
  uint32_t spi_clock_hz = 0;
  uint32_t start_cycles = 0;         // cycle counter as the first command word is queued
  uint32_t lead_cycles = 0;          // pop of the discarded lead frame
  uint32_t switch_begin_cycles = 0;  // before switchAddress (disable edge)
  uint32_t switch_end_cycles = 0;    // after switchAddress returns
  uint32_t period_min_cycles = 0;
  uint32_t period_sum_cycles = 0;
  uint32_t period_max_cycles = 0;
  uint16_t transfer_errors = 0;
  uint16_t data_errors = 0;
};

// One burst on one bank: park on VMID, run continuous-CS 16-clock frames on the
// LPSPI FIFO, break-before-make switch to `address` after pre_frames results,
// keep sampling, re-park on VMID. No USB I/O and no allocation while running;
// interrupts stay enabled except for the ~0.5 us GPIO switch itself.
class CapBurstEngine {
 public:
  bool run(Adc124s101 &bank, SpiController &spi, const BurstRequest &request,
           BurstResult &result);
  bool faulted() const { return faulted_; }
  uint32_t transferErrors() const { return transfer_errors_; }

  const uint16_t *words() const { return words_; }
  const uint32_t *stamps() const { return stamps_; }

 private:
  uint16_t words_[kMaxFrames];
  uint32_t stamps_[kMaxFrames];
  bool faulted_ = false;
  uint32_t transfer_errors_ = 0;
};

}  // namespace captest
