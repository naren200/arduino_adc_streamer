#include "BurstEngine.h"

#include "ConfigurableParameters.h"
#include "FrameRunner.h"

namespace captest {
namespace {

static constexpr uint16_t kContinueCommand = 0x0000;

}  // namespace

bool channelRouted(uint8_t adc_number, uint8_t channel) {
  if (adc_number < 1 || adc_number > testboard_config::kAdcCount) return false;
  // ADC1 and ADC3 route sensor inputs 0..9, ADC2 and ADC4 inputs 0..14.
  // Input 15 is the board-fixed Vmid and is never a sensor channel.
  const uint8_t last = (adc_number == 1 || adc_number == 3) ? 9 : 14;
  return channel <= last;
}

bool commandsSensor(const BurstRequest &request, uint16_t frame) {
  if (frame >= request.n_samples) return false;  // the last two commands release
  if (request.vmid_run == 0) return true;
  return frame % (request.sensor_run + request.vmid_run) < request.sensor_run;
}

uint32_t nominalFramePeriodNs(uint32_t spi_clock_hz, uint32_t gap_ns) {
  if (spi_clock_hz == 0) return 0;
  return static_cast<uint32_t>(16000000000ULL / spi_clock_hz) + gap_ns;
}

uint16_t autoSampleCount(uint8_t adc_number, uint32_t spi_clock_hz, uint32_t gap_ns) {
  // Cover 6 tau of a guessed 100 pF total capacitance behind the ADC's bias
  // resistor, using the nominal frame period.
  const uint32_t r_ohm = testboard_config::kAdcBiasResistorOhms[adc_number - 1];
  const uint64_t six_tau_ns = 6ULL * r_ohm * 100ULL / 1000ULL;  // R * 100 pF in ns
  const uint32_t period = nominalFramePeriodNs(spi_clock_hz, gap_ns);
  if (period == 0) return kDefaultAutoCap;
  const uint64_t count = six_tau_ns / period + 1;
  if (count > kDefaultAutoCap) return kDefaultAutoCap;
  if (count < 32) return 32;
  return static_cast<uint16_t>(count);
}

bool BurstEngine::park(Ads7953Adc &adc, uint16_t frames, uint16_t *out_words) {
  SpiController &spi = adc.spiController();
  if (!spi.beginLpspiSession()) return false;
  const uint32_t cpu_hz = F_CPU_ACTUAL;
  const FrameContext ctx = makeContext(adc, 1000, cpu_hz);
  const uint16_t command = adc.manualCommand(testboard_config::kDefaultVmidChannel);
  bool ok = true;
  uint32_t scratch_ts;
  uint16_t word = 0;
  __disable_irq();
  for (uint16_t frame = 0; frame < frames && ok; ++frame) {
    ok = runFrame(ctx, command, word, scratch_ts);
    if (out_words != nullptr) out_words[frame] = word;
  }
  __enable_irq();
  spi.endLpspiSession();
  return ok;
}

bool BurstEngine::run(Ads7953Adc &adc, const BurstRequest &request,
                      BurstResult &result) {
  result = BurstResult();
  SpiController &spi = adc.spiController();
  if (!spi.beginLpspiSession()) return false;

  const uint32_t cpu_hz = F_CPU_ACTUAL;
  result.cpu_hz = cpu_hz;
  result.spi_clock_hz = spi.clockHz();
  const FrameContext ctx = makeContext(adc, request.gap_ns, cpu_hz);
  const uint16_t park_command = adc.manualCommand(testboard_config::kDefaultVmidChannel);
  const uint16_t sample_command = adc.manualCommand(request.channel);
  const uint16_t frames_wanted = request.n_samples + kLeadFrames;
  const uint32_t park_cycles = nsToCycles(request.park_us * 1000u, cpu_hz);

  uint16_t word = 0;
  uint32_t scratch_ts = 0;
  uint32_t park_frames = 0;
  bool ok = true;

  __disable_irq();

  // 1. Park on Vmid so the shared MXO node settles at Vmid. Keep the last
  //    kMaxVmidWords results; they are Vmid readings (tag 15) taken before the
  //    connection.
  const uint32_t park_start = ARM_DWT_CYCCNT;
  while (ok && (park_frames < 4 || ARM_DWT_CYCCNT - park_start < park_cycles)) {
    ok = runFrame(ctx, park_command, word, scratch_ts);
    vmid_words_[park_frames % kMaxVmidWords] = word;
    ++park_frames;
  }

  // 2. Connect once and keep commanding the same channel. Frames 0 and 1 still
  //    return Vmid results; frame j >= 2 returns the sample taken at CS fall.
  uint16_t recorded = 0;
  // capalt phase within one sensor/Vmid cycle; a counter, so the loop needs no
  // division (commandsSensor gives the same pattern for the tag check below).
  const uint8_t cycle_frames = request.sensor_run + request.vmid_run;
  uint8_t phase = 0;
  // capalt with gap_long_ns: every second cycle (odd cycles) the frames during
  // which the sensor is connected use the long gap. The mux follows a command
  // one frame late, so the sensor commanded in phases 0..S-1 is connected
  // during the frames of phases 1..S.
  const bool two_gaps = request.vmid_run != 0 && request.gap_long_ns != 0;
  const FrameContext ctx_long =
      makeContext(adc, two_gaps ? request.gap_long_ns : request.gap_ns, cpu_hz);
  bool long_cycle = false;
  while (ok && recorded < frames_wanted) {
    // Command the sensor for the first n_samples frames only; the last two
    // commands release to Vmid (their mux effect lands after the last sample).
    // Continue mode re-commands nothing after frame 0 (SLAS605C Table 7: DI15-12
    // = 0000 keeps the last selected channel in manual mode).
    // capalt: commands follow the sensor/Vmid pattern, so frame j >= 2 returns
    // the input commanded in frame j - 2.
    uint16_t command = park_command;
    if (recorded < request.n_samples &&
        (request.vmid_run == 0 || phase < request.sensor_run)) {
      command = request.continue_mode && recorded > 0 ? kContinueCommand : sample_command;
    }
    const bool stretched = two_gaps && long_cycle && phase >= 1 && phase <= request.sensor_run;
    ok = runFrame(stretched ? ctx_long : ctx, command, words_[recorded], timestamps_[recorded]);
    if (ok) {
      ++recorded;
      if (++phase == cycle_frames) {
        phase = 0;
        long_cycle = !long_cycle;
      }
    }
  }

  // 3. Release: the two release commands above plus enough extra Vmid frames
  //    that the sensor is certainly disconnected (>= kReleaseFrames in total).
  for (uint8_t frame = 0; frame < kReleaseFrames - 2 && ok; ++frame) {
    ok = runFrame(ctx, park_command, word, scratch_ts);
  }
  if (!ok) {
    // Leave the converter parked even after a failed frame.
    for (uint8_t frame = 0; frame < 4; ++frame) {
      if (!runFrame(ctx, park_command, word, scratch_ts)) break;
    }
  }

  __enable_irq();
  spi.endLpspiSession();

  result.park_frames = park_frames;
  result.n_vmid = park_frames < kMaxVmidWords ? park_frames : kMaxVmidWords;
  // Re-order the ring buffer chronologically.
  if (park_frames > kMaxVmidWords) {
    uint16_t ordered[kMaxVmidWords];
    for (uint8_t index = 0; index < kMaxVmidWords; ++index) {
      ordered[index] = vmid_words_[(park_frames + index) % kMaxVmidWords];
    }
    memcpy(vmid_words_, ordered, sizeof(ordered));
  }
  result.n_frames = recorded;
  if (request.continue_mode) result.flags |= FLAG_CONTINUE_MODE;
  if (request.vmid_run != 0) result.flags |= FLAG_ALTERNATE;
  if (!ok) {
    result.flags |= FLAG_INCOMPLETE;
    result.timeout_errors = 1;
  }

  // Statistics, computed after the critical section.
  if (recorded >= 2) {
    uint32_t minimum = 0xFFFFFFFFu, maximum = 0, sum = 0;
    for (uint16_t index = 1; index < recorded; ++index) {
      const uint32_t delta = timestamps_[index] - timestamps_[index - 1];
      if (delta < minimum) minimum = delta;
      if (delta > maximum) maximum = delta;
      sum += delta;
    }
    result.period_min_cycles = minimum;
    result.period_max_cycles = maximum;
    result.period_sum_cycles = sum;

    const uint32_t min_period_ns = cyclesToNs(minimum, cpu_hz);
    if (min_period_ns < kMinFramePeriodNs) result.flags |= FLAG_PERIOD_TOO_SHORT;
    // Acquisition: from the 14th rising SCLK edge (first_clock_delay + 13 clock
    // periods after CS fall) to the next CS fall.
    const uint32_t clock_period_ns = 1000000000UL / result.spi_clock_hz;
    const uint32_t edge_ns = request.first_clock_delay_ns + 13u * clock_period_ns;
    if (min_period_ns < edge_ns + kMinAcquisitionNs) {
      result.flags |= FLAG_ACQUISITION_SHORT;
    }
  }
  for (uint16_t index = kLeadFrames; index < recorded; ++index) {
    const uint8_t expected = commandsSensor(request, index - kLeadFrames)
                                 ? request.channel
                                 : testboard_config::kDefaultVmidChannel;
    if (adc.returnedChannel(words_[index]) != expected) ++result.tag_errors;
  }
  if (result.tag_errors != 0) result.flags |= FLAG_TAG_ERRORS;
  return recorded == frames_wanted;
}

}  // namespace captest
