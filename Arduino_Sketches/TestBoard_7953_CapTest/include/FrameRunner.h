#pragma once

#include <Arduino.h>

#include "Ads7953Adc.h"
#include "BurstEngine.h"
#include "ConfigurableParameters.h"
#include "SpiController.h"

// One 16-bit SPI frame and the context it needs, shared by the burst engine and the
// continuous-capture scan engine. Moved verbatim out of BurstEngine.cpp: the tight loop
// performs no division, allocation or USB access, and all values are resolved before
// interrupts are masked.
namespace captest {

struct FrameContext {
  SpiController *spi;
  const SpiController::ChipSelect *cs;
  uint32_t timeout_cycles;
  uint32_t guard_wait_cycles;  // extra wait after finishLpspi16's own CS-high delay
};

// `timestamp` is the cycle counter right after CS falls and the command is queued (a few
// tens of ns after the CS edge; constant offset).
static inline __attribute__((always_inline)) bool runFrame(
    const FrameContext &ctx, uint16_t command, uint16_t &response,
    uint32_t &timestamp) {
  if (!ctx.spi->startLpspi16(*ctx.cs, command)) return false;
  const uint32_t start = ARM_DWT_CYCCNT;
  timestamp = start;
  while (!ctx.spi->lpspiComplete()) {
    if (ARM_DWT_CYCCNT - start >= ctx.timeout_cycles) {
      ctx.spi->cancelLpspi16();
      return false;
    }
  }
  ctx.spi->finishLpspi16(response);
  const uint32_t released = ARM_DWT_CYCCNT;
  while (ARM_DWT_CYCCNT - released < ctx.guard_wait_cycles) {
  }
  return true;
}

inline uint32_t nsToCycles(uint32_t ns, uint32_t cpu_hz) {
  return static_cast<uint32_t>((static_cast<uint64_t>(ns) * cpu_hz + 999999999ULL) /
                               1000000000ULL);
}

inline uint32_t cyclesToNs(uint32_t cycles, uint32_t cpu_hz) {
  return static_cast<uint32_t>(static_cast<uint64_t>(cycles) * 1000000000ULL / cpu_hz);
}

inline FrameContext makeContext(Ads7953Adc &adc, uint32_t gap_ns, uint32_t cpu_hz) {
  FrameContext ctx;
  ctx.spi = &adc.spiController();
  ctx.cs = &adc.chipSelect();
  const uint32_t frame_ns = nominalFramePeriodNs(ctx.spi->clockHz(), gap_ns);
  // Four nominal frames (plus 20 us) is far beyond a healthy transfer.
  ctx.timeout_cycles = nsToCycles(frame_ns * 4u + 20000u, cpu_hz);
  const uint32_t guard_ns = testboard_config::kCsHighTimeNs;
  ctx.guard_wait_cycles = gap_ns > guard_ns ? nsToCycles(gap_ns - guard_ns, cpu_hz) : 0;
  return ctx;
}

}  // namespace captest
