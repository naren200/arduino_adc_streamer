#include "CapBurstEngine.h"

#include "ConfigurableParameters.h"

namespace captest {
namespace {

using testboard_config::kCsPins;

uint32_t nsToCycles(uint32_t ns, uint32_t cpu_hz) {
  return static_cast<uint32_t>((static_cast<uint64_t>(ns) * cpu_hz + 999999999ULL) /
                               1000000000ULL);
}

// Same cleanup as the production engine's cancelLpspi16 (minus the TCR rewrite,
// which endLpspiSession performs).
void flushLpspi(IMXRT_LPSPI_t *lpspi) {
  lpspi->CR = 0;
  lpspi->DER = 0;
  lpspi->CR = LPSPI_CR_RRF | LPSPI_CR_RTF;
  lpspi->SR = 0x3F00;
}

}  // namespace

bool CapBurstEngine::run(Adc124s101 &bank, SpiController &spi,
                         const BurstRequest &rq, BurstResult &res) {
  res = BurstResult();
  const uint32_t cpu_hz = F_CPU_ACTUAL;
  res.cpu_hz = cpu_hz;
  res.spi_clock_hz = spi.clockHz();
  if (faulted_) {
    res.flags |= FLAG_INCOMPLETE;
    return false;
  }

  // Command words: the command clocked in during frame s selects the input
  // converted in frame s+1. Submitted word 0 is the "lead" frame: its response
  // converts whatever was selected before and is discarded. Returned frame k
  // (response of submitted word k+1) converts input commands[k % m].
  uint16_t commands[4];
  uint8_t m = 0;
  for (uint8_t input = 0; input < 4; ++input)
    if (rq.input_mask & (1u << input)) commands[m++] = static_cast<uint16_t>(input) << 11;
  const bool connect = rq.address != testboard_config::kVmidAddress;
  const uint32_t frames = static_cast<uint32_t>(rq.pre_frames) + rq.post_frames;
  if (!m || frames == 0 || frames > kMaxFrames) {
    res.flags |= FLAG_INCOMPLETE;
    return false;
  }
  if (!connect) res.flags |= FLAG_NO_SWITCH;

  IMXRT_LPSPI_t *const lpspi = &IMXRT_LPSPI4_S;
  const uint8_t cs_pin = kCsPins[rq.array - 1];
  const auto cs_set = portSetRegister(cs_pin);
  const auto cs_clear = portClearRegister(cs_pin);
  const uint32_t cs_mask = digitalPinToBitMask(cs_pin);

  // Resolved before the frames run: FIFO depths and the timeout.
  const uint32_t parameters = lpspi->PARAM;
  const uint8_t tx_log = parameters & 0xFF, rx_log = (parameters >> 8) & 0xFF;
  if (tx_log > 4 || rx_log > 4 || !spi.beginLpspiSession()) {
    ++transfer_errors_;
    faulted_ = true;
    res.flags |= FLAG_INCOMPLETE;
    res.transfer_errors = 1;
    return false;
  }
  const uint32_t tx_depth = 1u << tx_log, rx_depth = 1u << rx_log;
  const uint32_t frame_ns = static_cast<uint32_t>(16000000000ULL / res.spi_clock_hz);
  // 2 x the nominal run time plus 200 us is far beyond a healthy burst.
  const uint32_t timeout_cycles = nsToCycles(
      static_cast<uint32_t>((frames + 1) * frame_ns * 2u + 200000u), cpu_hz);

  // 1. Park: every D node of the bank sits at VMID before the first frame.
  bank.park(0);
  if (rq.park_us) delayMicroseconds(rq.park_us);

  // 2. Frames. TX is kept full; every result is time-stamped when it is popped.
  const uint32_t total = 1 + frames;  // lead frame + returned frames
  uint32_t target = total;
  const uint32_t trigger = connect ? 1u + rq.pre_frames : 0xFFFFFFFFu;
  uint32_t submitted = 0, received = 0, next_poll = 32;
  uint8_t command_index = 0;
  bool failed = false, aborted = false;

  if (!(lpspi->RSR & LPSPI_RSR_RXEMPTY)) {
    failed = true;  // stale data from an earlier session; never consume it
  } else {
    lpspi->SR = 0x3F00;
    *cs_clear = cs_mask;
    delayNanoseconds(testboard_config::kCsSetupTimeNs);
    res.start_cycles = ARM_DWT_CYCCNT;
    const uint32_t started = res.start_cycles;
    for (;;) {
      while (submitted < target && submitted - received < rx_depth &&
             (lpspi->FSR & 0x1F) < tx_depth) {
        if (submitted + 1 == target) lpspi->SR = LPSPI_SR_TCF;
        lpspi->TDR = commands[command_index];
        command_index = command_index + 1 == m ? 0 : command_index + 1;
        ++submitted;
      }
      while (received < submitted && !(lpspi->RSR & LPSPI_RSR_RXEMPTY)) {
        const uint32_t now = ARM_DWT_CYCCNT;
        const uint16_t word = static_cast<uint16_t>(lpspi->RDR);
        if (received == 0) {
          res.lead_cycles = now;
        } else {
          words_[received - 1] = word;
          stamps_[received - 1] = now;
        }
        ++received;
        if (received == trigger) {
          // Break-before-make (production routine): disable, guard, address,
          // enable, guard. Interrupt-free so the open window is deterministic.
          __disable_irq();
          res.switch_begin_cycles = ARM_DWT_CYCCNT;
          bank.switchAddress(rq.address);
          res.switch_end_cycles = ARM_DWT_CYCCNT;
          __enable_irq();
        }
      }
      if (!aborted && received >= next_poll) {
        next_poll += 32;
        if (Serial.available() > 0) {
          // A host command ends the burst at a frame boundary.
          aborted = true;
          target = submitted;
        }
      }
      const uint32_t status = lpspi->SR;
      if (status & (LPSPI_SR_TEF | LPSPI_SR_REF)) { failed = true; break; }
      // RX readiness alone can precede the final clock edge. Require every
      // word, an empty TX FIFO, hardware completion and idle before CS rises.
      if (received == target && submitted == target && !(status & LPSPI_SR_MBF) &&
          !(lpspi->FSR & 0x1F) && (aborted || (status & LPSPI_SR_TCF)))
        break;
      if (ARM_DWT_CYCCNT - started >= timeout_cycles) { failed = true; break; }
    }
  }

  if (failed) flushLpspi(lpspi);
  *cs_set = cs_mask;
  delayNanoseconds(testboard_config::kCsHighTimeNs);
  spi.endLpspiSession();

  // 3. Release: back to VMID (also after a failure) so no sensor stays connected.
  bank.switchAddress(testboard_config::kVmidAddress);

  res.n_frames = static_cast<uint16_t>(received > 0 ? received - 1 : 0);
  if (aborted) res.flags |= FLAG_ABORTED;
  if (failed || (!aborted && res.n_frames != frames)) {
    res.flags |= FLAG_INCOMPLETE;
    res.transfer_errors = 1;
    ++transfer_errors_;
    faulted_ = true;
  }

  // Statistics, after the critical part. The pop stamps include polling latency,
  // so min/max are jitter indicators; the mean over the burst is the period.
  if (res.n_frames >= 2) {
    uint32_t minimum = 0xFFFFFFFFu, maximum = 0, sum = 0;
    for (uint16_t i = 1; i < res.n_frames; ++i) {
      const uint32_t delta = stamps_[i] - stamps_[i - 1];
      if (delta < minimum) minimum = delta;
      if (delta > maximum) maximum = delta;
      sum += delta;
    }
    res.period_min_cycles = minimum;
    res.period_max_cycles = maximum;
    res.period_sum_cycles = sum;
    const uint64_t mean_period_ns = static_cast<uint64_t>(sum) * 1000000000ULL /
                                    (static_cast<uint64_t>(res.n_frames - 1) * cpu_hz);
    if (mean_period_ns * 100u < static_cast<uint64_t>(frame_ns) * 98u)
      res.flags |= FLAG_PERIOD_SHORT;
    if (static_cast<uint64_t>(maximum) * (res.n_frames - 1) > 3ULL * sum)
      res.flags |= FLAG_STAMP_GAP;
  }
  if (connect && res.switch_end_cycles != 0) {
    const uint64_t switch_ns =
        static_cast<uint64_t>(res.switch_end_cycles - res.switch_begin_cycles) *
        1000000000ULL / cpu_hz;
    if (switch_ns > kSwitchLimitNs) res.flags |= FLAG_SWITCH_SLOW;
  }
  for (uint16_t i = 0; i < res.n_frames; ++i)
    if (words_[i] & 0xF000) ++res.data_errors;
  if (res.data_errors) res.flags |= FLAG_DATA_ERRORS;
  return !failed && !aborted && res.n_frames == frames;
}

}  // namespace captest
