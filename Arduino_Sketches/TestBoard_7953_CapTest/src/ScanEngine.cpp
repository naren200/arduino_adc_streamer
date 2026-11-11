#include "ScanEngine.h"

#include "BurstEngine.h"
#include "ConfigurableParameters.h"
#include "FrameRunner.h"

namespace captest {
namespace {

uint16_t crc16CcittFalse(const uint8_t *data, size_t length) {
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

}  // namespace

uint8_t ScanEngine::blockCount(const ScanSetup &setup) {
  uint8_t blocks = 0;
  for (uint8_t i = 0; i < setup.n_routes; ++i) {
    if (i == 0 || setup.adc[i] != setup.adc[i - 1]) ++blocks;
  }
  return blocks;
}

uint32_t ScanEngine::framesPerSweep(const ScanSetup &setup) {
  return 2u * setup.n_routes + 2u * blockCount(setup);
}

bool ScanEngine::valid(const ScanSetup &setup, uint32_t spi_clock_hz) {
  if (setup.n_routes < 1 || setup.n_routes > kMaxScanRoutes || spi_clock_hz == 0) return false;
  if (setup.gap_ns > kMaxGapNs || setup.first_clock_delay_ns > kMaxFirstClockDelayNs ||
      setup.lag_us > kMaxScanLagUs) {
    return false;
  }
  uint8_t seen = 0;  // bit per ADC: one contiguous block each
  for (uint8_t i = 0; i < setup.n_routes; ++i) {
    const uint8_t adc = setup.adc[i];
    if (!channelRouted(adc, setup.channel[i])) return false;
    for (uint8_t j = 0; j < i; ++j) {
      if (setup.adc[j] == adc && setup.channel[j] == setup.channel[i]) return false;
    }
    if (i == 0 || adc != setup.adc[i - 1]) {
      if (seen & (1u << adc)) return false;
      seen |= 1u << adc;
    }
  }
  const uint32_t period_ns = nominalFramePeriodNs(spi_clock_hz, setup.gap_ns);
  const uint32_t clock_period_ns = 1000000000UL / spi_clock_hz;
  if (period_ns < kMinFramePeriodNs) return false;
  if (period_ns < setup.first_clock_delay_ns + 13u * clock_period_ns + kMinAcquisitionNs) {
    return false;
  }
  // One ADC block runs with interrupts masked: bound it.
  uint8_t block = 0;
  for (uint8_t i = 0; i < setup.n_routes; ++i) {
    block = (i > 0 && setup.adc[i] == setup.adc[i - 1]) ? block + 1 : 1;
    const bool last = i + 1 == setup.n_routes || setup.adc[i + 1] != setup.adc[i];
    if (last && static_cast<uint64_t>(2u * block + 2u) * period_ns / 1000u > kMaxBlockCriticalUs) {
      return false;
    }
  }
  return true;
}

void ScanEngine::begin(const ScanSetup &setup, uint32_t session, uint32_t boot_id,
                       uint32_t max_sweeps) {
  setup_ = setup;
  session_ = session;
  boot_id_ = boot_id;
  max_sweeps_ = max_sweeps;
  sweep_no_ = dropped_ = dropped_reported_ = seq_ = 0;
  head_ = tail_ = 0;
  tag_error_ = error_ = stopped_ = final_pending_ = chunk_ready_ = false;
  pending_sweeps_ = 0;
  const uint16_t per = (kMaxChunkBytes - kChunkHeaderBytes - 2) /
                       (static_cast<uint16_t>(setup.n_routes) * kChunkRecordBytes);
  per_chunk_ = per < 1 ? 1 : (per > kMaxSweepsPerChunk ? kMaxSweepsPerChunk : per);
  last_send_ms_ = millis();
  state_ = ScanState::Running;
}

bool ScanEngine::runSweep(Ads7953Adc *const *adcs, ScanRecord *records) {
  const uint32_t cpu_hz = F_CPU_ACTUAL;
  uint8_t i = 0;
  while (i < setup_.n_routes) {
    const uint8_t adc_number = setup_.adc[i];
    uint8_t end = i;
    while (end < setup_.n_routes && setup_.adc[end] == adc_number) ++end;
    const uint8_t routes = end - i;
    Ads7953Adc &adc = *adcs[adc_number - 1];
    SpiController &spi = adc.spiController();
    if (!spi.beginLpspiSession()) return false;
    const FrameContext ctx = makeContext(adc, setup_.gap_ns, cpu_hz);
    const uint16_t vmid_command = adc.manualCommand(testboard_config::kDefaultVmidChannel);
    uint16_t sensor_command[kMaxScanRoutes];
    for (uint8_t r = 0; r < routes; ++r) {
      sensor_command[r] = adc.manualCommand(setup_.channel[i + r]);
    }
    const uint8_t frames = 2 * routes + 2;
    uint16_t response[2 * kMaxScanRoutes + 2];
    uint32_t stamp[2 * kMaxScanRoutes + 2];
    bool ok = true;
    __disable_irq();
    for (uint8_t frame = 0; frame < frames && ok; ++frame) {
      const bool sensor_frame = frame < 2 * routes && (frame & 1u) == 0;
      ok = runFrame(ctx, sensor_frame ? sensor_command[frame >> 1] : vmid_command,
                    response[frame], stamp[frame]);
    }
    __enable_irq();
    if (!ok) {
      for (uint8_t frame = 0; frame < 4; ++frame) {  // leave the converter parked
        uint16_t scratch;
        uint32_t scratch_ts;
        __disable_irq();
        const bool sent = runFrame(ctx, vmid_command, scratch, scratch_ts);
        __enable_irq();
        if (!sent) break;
      }
      spi.endLpspiSession();
      return false;
    }
    spi.endLpspiSession();
    // Response j is the conversion of command j-2: route r's sensor command is frame 2r, so
    // its sample returns in frame 2r+2 and the Vmid reset after it in frame 2r+3. The mux
    // connects at frame 2r+1 and the Vmid command releases it at frame 2r+2.
    for (uint8_t r = 0; r < routes; ++r) {
      ScanRecord &rec = records[i + r];
      rec.ts_conn = stamp[2 * r + 1];
      rec.ts_rel = stamp[2 * r + 2];
      rec.sensor = response[2 * r + 2];
      rec.vmid = response[2 * r + 3];
      if (adc.returnedChannel(rec.sensor) != setup_.channel[i + r] ||
          adc.returnedChannel(rec.vmid) != testboard_config::kDefaultVmidChannel) {
        tag_error_ = true;
      }
    }
    i = end;
  }
  return true;
}

bool ScanEngine::sweep(Ads7953Adc *const *adcs) {
  if (state_ != ScanState::Running) return true;
  if (max_sweeps_ != 0 && sweep_no_ >= max_sweeps_) {
    startDraining(false);
    return true;
  }
  if (head_ - tail_ >= kRingSweeps) {  // ring full: drop this sweep and say so
    ++dropped_;
    ++sweep_no_;
    return true;
  }
  const uint32_t slot = head_ % kRingSweeps;
  if (!runSweep(adcs, ring_[slot])) {
    error_ = true;
    startDraining(false);
    return false;
  }
  sweep_stamp_[slot] = sweep_no_;
  ++head_;
  ++sweep_no_;
  return true;
}

void ScanEngine::startDraining(bool stopped_by_host) {
  if (state_ != ScanState::Running) return;
  stopped_ = stopped_by_host;
  state_ = ScanState::Draining;
  final_pending_ = true;
}

void ScanEngine::requestStop() { startDraining(true); }

void ScanEngine::abort() {
  state_ = ScanState::Done;
  chunk_ready_ = false;
  head_ = tail_ = 0;
}

bool ScanEngine::nextChunk(const uint8_t *&data, uint16_t &length) {
  if (!active()) return false;
  if (!chunk_ready_) {
    uint32_t count = 0;
    uint32_t first = 0;
    if (head_ != tail_) {
      first = sweep_stamp_[tail_ % kRingSweeps];
      // Whole sweeps only, consecutive sweep numbers: a gap starts a new chunk.
      while (count < per_chunk_ && tail_ + count < head_ &&
             sweep_stamp_[(tail_ + count) % kRingSweeps] == first + count) {
        ++count;
      }
    } else if (state_ == ScanState::Draining && final_pending_) {
      first = sweep_no_;  // final chunk: no sweeps
    } else {
      return false;
    }
    // Keep sending data before the final chunk; the final chunk only when the ring is empty.
    uint16_t flags = 0;
    if (dropped_ != dropped_reported_) flags |= FLAG_CHUNK_OVERRUN;
    if (tag_error_) flags |= FLAG_CHUNK_TAG_ERRORS;
    if (error_) flags |= FLAG_CHUNK_ERROR;
    const bool final_chunk = count == 0;
    if (final_chunk) {
      flags |= FLAG_CHUNK_FINAL;
      if (stopped_) flags |= FLAG_CHUNK_STOPPED;
    }
    uint8_t *p = chunk_;
    p[0] = 'C';
    p[1] = 'K';
    p[2] = kCaptureChunkVersion;
    p[3] = kChunkHeaderBytes;
    put32(p + 4, session_);
    put32(p + 8, boot_id_);
    put32(p + 12, seq_);
    put32(p + 16, first);
    put16(p + 20, static_cast<uint16_t>(count));
    put16(p + 22, setup_.n_routes);
    put16(p + 24, flags);
    put16(p + 26, 0);
    put32(p + 28, dropped_);
    put32(p + 32, F_CPU_ACTUAL);
    uint16_t offset = kChunkHeaderBytes;
    for (uint32_t s = 0; s < count; ++s) {
      const ScanRecord *rec = ring_[(tail_ + s) % kRingSweeps];
      memcpy(p + offset, rec, static_cast<size_t>(setup_.n_routes) * kChunkRecordBytes);
      offset += static_cast<uint16_t>(setup_.n_routes) * kChunkRecordBytes;
    }
    put16(p + offset, crc16CcittFalse(p, offset));
    offset += 2;
    chunk_length_ = offset;
    pending_sweeps_ = static_cast<uint8_t>(count);
    chunk_ready_ = true;
  }
  data = chunk_;
  length = chunk_length_;
  return true;
}

void ScanEngine::chunkSent() {
  if (!chunk_ready_) return;
  chunk_ready_ = false;
  last_send_ms_ = millis();
  const bool final_chunk = pending_sweeps_ == 0;
  tail_ += pending_sweeps_;
  ++seq_;
  dropped_reported_ = dropped_;
  tag_error_ = false;
  if (final_chunk) {
    final_pending_ = false;
    state_ = ScanState::Done;
  }
}

}  // namespace captest
