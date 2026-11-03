#pragma once

#include <Arduino.h>

#include "Ads7953Adc.h"
#include "CapTestProtocol.h"

namespace captest {

struct BurstRequest {
  uint8_t adc_number;  // 1..4
  uint8_t channel;
  uint16_t n_samples;
  uint32_t park_us;
  uint32_t gap_ns;
  uint32_t first_clock_delay_ns;
  bool continue_mode;  // after the connection, send "continue" instead of re-commanding the channel
  // capalt: repeat `sensor_run` channel commands then `vmid_run` Vmid commands
  // (production's Vmid-between-channels sequence). vmid_run 0 = ordinary burst.
  uint8_t sensor_run = 0;
  uint8_t vmid_run = 0;
  // capalt: when non-zero, odd cycles keep the sensor connected with this gap,
  // so one burst holds two connected times (C_total and C_share from one burst).
  uint32_t gap_long_ns = 0;
};

// Command frame `frame` (0 = first after the park) selects the sensor.
bool commandsSensor(const BurstRequest &request, uint16_t frame);

struct BurstResult {
  uint16_t n_frames = 0;  // frames recorded (n_samples + 2 when complete)
  uint8_t n_vmid = 0;
  uint8_t flags = 0;
  uint32_t park_frames = 0;
  uint32_t period_min_cycles = 0;
  uint32_t period_sum_cycles = 0;
  uint32_t period_max_cycles = 0;
  uint16_t timeout_errors = 0;
  uint16_t tag_errors = 0;
  uint32_t cpu_hz = 0;
  uint32_t spi_clock_hz = 0;
};

class BurstEngine {
 public:
  // One burst: park on Vmid, connect once, sample the same channel every frame,
  // release to Vmid. Interrupts are masked while the frames run. No USB I/O.
  bool run(Ads7953Adc &adc, const BurstRequest &request, BurstResult &result);

  // Command Vmid for a few frames (leaves the ADC parked). Returns the tags and
  // 12-bit data of the last `count` results via out_words.
  bool park(Ads7953Adc &adc, uint16_t frames, uint16_t *out_words);

  const uint16_t *vmidWords() const { return vmid_words_; }
  const uint16_t *words() const { return words_; }
  const uint32_t *timestamps() const { return timestamps_; }

 private:
  uint16_t vmid_words_[kMaxVmidWords];
  uint16_t words_[kMaxFrames];
  uint32_t timestamps_[kMaxFrames];
};

// Validation helpers shared with the command parser.
bool channelRouted(uint8_t adc_number, uint8_t channel);
uint32_t nominalFramePeriodNs(uint32_t spi_clock_hz, uint32_t gap_ns);
uint16_t autoSampleCount(uint8_t adc_number, uint32_t spi_clock_hz, uint32_t gap_ns);

}  // namespace captest
