#include "Adc124s101.h"

using namespace testboard_config;

bool validSensorRoute(const SensorRoute &r) {
  return r.array >= 1 && r.array <= 2 && r.mux >= 1 && r.mux <= 4 &&
         (r.input <= 5 || (r.mux == 4 && r.input == 6));
}

Adc124s101::Adc124s101(SpiController &spi, uint8_t array_index)
    : spi_(spi), index_(array_index) {}

void Adc124s101::enable(bool enabled) {
  if (enabled_ == enabled) return;
  enable_gpio_.write(enabled == kMuxEnableActiveHigh[index_]);
  enabled_ = enabled;
}

void Adc124s101::begin() {
  refreshTiming();
  chip_select_ = spi_.registerChipSelect(kCsPins[index_]);
  // Set the output latch before changing direction, especially active-low EN.
  enable_gpio_.resolve(kMuxEnablePins[index_]);
  enable_gpio_.write(!kMuxEnableActiveHigh[index_]);
  enabled_ = false;
  pinMode(kMuxEnablePins[index_], OUTPUT);
  for (uint8_t bit = 0; bit < 3; ++bit) {
    address_gpio_[bit].resolve(kMuxAddressPins[index_][bit]);
    pinMode(kMuxAddressPins[index_][bit], OUTPUT);
  }
  park(kDefaultMuxSettleUs);
}

void Adc124s101::refreshTiming() {
#if defined(__IMXRT1062__)
  timeout_ticks_ = uint64_t(F_CPU_ACTUAL) * kSpiTransferTimeoutUs / 1000000;
  service_ticks_ = F_CPU_ACTUAL / 1000000 * 10;
#endif
}

void Adc124s101::disable() {
  if (!enabled_) return;
  enable(false);
  delayNanoseconds(kMuxDisableTimeNs);
}

void Adc124s101::selectAddress(uint8_t address, uint32_t settle_us) {
  if (switchAddress(address) && settle_us) delayMicroseconds(settle_us);
}

void Adc124s101::Gpio::resolve(uint8_t value) {
  pin = value;
#if defined(__IMXRT1062__)
  set = portSetRegister(pin); clear = portClearRegister(pin); mask = digitalPinToBitMask(pin);
#endif
}

void Adc124s101::Gpio::write(bool high) const {
#if defined(__IMXRT1062__)
  *(high ? set : clear) = mask;
#else
  digitalWrite(pin, high ? HIGH : LOW);
#endif
}

bool Adc124s101::switchAddress(uint8_t address) {
  if (address > 7 || (enabled_ && address_ == address)) return false;
  disable();  // Required turn-off guard precedes address changes.
  const uint8_t changed = address_ == 0xFF ? 7 : address_ ^ address;
  for (uint8_t bit = 0; bit < 3; ++bit)
    if (changed & (1 << bit)) address_gpio_[bit].write((address >> bit) & 1);
  address_ = address;
  enable(true);
  // Even settle_us=0 includes a switch turn-on allowance; the configurable
  // delay is additional analog settling, to be measured on the real board.
  delayNanoseconds(kMuxDisableTimeNs);
  return true;
}

void Adc124s101::park(uint32_t settle_us) {
  selectAddress(kVmidAddress, settle_us);
}

bool Adc124s101::transfer(uint8_t adc_input, SpiEngine engine, uint16_t &sample) {
  if (adc_input > 3) return false;
  // ADD1:ADD0 occupy DIN bits 12:11 in a 16-clock frame. This command
  // selects the NEXT conversion; the response has four zero bits + 12 data.
  const uint16_t command = static_cast<uint16_t>(adc_input) << 11;
  if (engine == SpiEngine::BLOCKING) return transferWord<SpiEngine::BLOCKING>(command, sample);
  if (engine == SpiEngine::DMA) return transferWord<SpiEngine::DMA>(command, sample);
  return transferWord<SpiEngine::LPSPI>(command, sample);
}

template <SpiEngine Engine>
bool Adc124s101::transferWord(uint16_t command, uint16_t &sample) {
  if (faulted_) return false;
  uint16_t response = 0;
  if (Engine == SpiEngine::BLOCKING) {
    response = spi_.transfer16(kCsPins[index_], command);
  } else {
    const bool dma = Engine == SpiEngine::DMA;
    const bool transfer_started = dma ? spi_.startDma16(kCsPins[index_], command)
                             : spi_.startLpspi16(chip_select_, command);
    if (!transfer_started) {
      ++transfer_errors_;
      faulted_ = true;
      return false;
    }
#if defined(__IMXRT1062__)
    const uint32_t started = dma ? micros() : ARM_DWT_CYCCNT;
    const uint32_t timeout = dma ? kSpiTransferTimeoutUs : timeout_ticks_;
#else
    const uint32_t started = micros(), timeout = kSpiTransferTimeoutUs;
#endif
    uint32_t last_service = started;
    while (!(dma ? spi_.dmaComplete() : spi_.lpspiComplete())) {
#if defined(__IMXRT1062__)
      const uint32_t now = dma ? micros() : ARM_DWT_CYCCNT;
#else
      const uint32_t now = micros();
#endif
      if (static_cast<uint32_t>(now - started) >= timeout) {
        if (dma) spi_.cancelDma16(); else spi_.cancelLpspi16();
        ++transfer_errors_;
        faulted_ = true;
        return false;
      }
#if defined(__IMXRT1062__)
      if (dma || now - last_service >= service_ticks_) {
        yield();
        last_service = now;
      }
#else
      (void)last_service;
      yield();
#endif
    }
    if (!(dma ? spi_.finishDma16(response) : spi_.finishLpspi16(response))) {
      if (dma) spi_.cancelDma16(); else spi_.cancelLpspi16();
      ++transfer_errors_;
      faulted_ = true;
      return false;
    }
  }
#if TESTBOARD_PROFILE
  if (profile_ != nullptr) {
    AcquisitionProfiler::Scope validation(*profile_, AcquisitionProfiler::Validate);
    return decode(response, sample);
  }
#endif
  return decode(response, sample);
}

bool Adc124s101::decode(uint16_t response, uint16_t &sample) {
  if (response & 0xF000) {
    ++data_errors_;
    return false;
  }
  sample = response;
  return true;
}

bool Adc124s101::readBatch(const SensorRoute *routes, uint8_t count,
                          uint8_t repeat, SpiEngine engine, uint16_t *samples) {
  PreparedBatch batch;
  return prepareBatch(routes, count, repeat, batch) && readPrepared(batch, engine, samples);
}

bool Adc124s101::prepareBatch(const SensorRoute *routes, uint8_t count,
                            uint8_t repeat, PreparedBatch &batch) const {
  batch.count = 0;
  if (!count || count > 4 || repeat < 1 || repeat > 3) return false;
  for (uint8_t i = 0; i < count; ++i)
    if (!validSensorRoute(routes[i]) || routes[i].array != index_ + 1 ||
        routes[i].input != routes[0].input) return false;
  // Flush the preceding input selection after any MUX/address/ADC switch.
  batch.commands[0] = uint16_t(kMuxToAdcInput[routes[0].mux - 1]) << 11;
  batch.destinations[0] = 0xFF;
  batch.count = 1;
  for (uint8_t i = 0; i < count; ++i) {
    for (uint8_t r = 0; r < repeat; ++r) {
      const uint8_t next = (r + 1 == repeat && i + 1 < count) ? i + 1 : i;
      batch.commands[batch.count] = uint16_t(kMuxToAdcInput[routes[next].mux - 1]) << 11;
      batch.destinations[batch.count++] = r + 1 == repeat ? i : 0xFF;
    }
  }
  return true;
}

template <SpiEngine Engine>
bool Adc124s101::executeBatch(const PreparedBatch &batch, uint16_t *samples) {
  if (!batch.count || batch.count > 13) return false;
  for (uint8_t i = 0; i < batch.count; ++i) {
    uint16_t sample;
    if (!transferWord<Engine>(batch.commands[i], sample)) return false;
    if (batch.destinations[i] != 0xFF) samples[batch.destinations[i]] = sample;
  }
  return true;
}

bool Adc124s101::readPrepared(const PreparedBatch &batch, SpiEngine engine, uint16_t *samples, bool burst) {
  if (burst) return executeBurst(batch, engine, samples);
  if (engine == SpiEngine::BLOCKING) return executeBatch<SpiEngine::BLOCKING>(batch, samples);
  if (engine == SpiEngine::DMA) return executeBatch<SpiEngine::DMA>(batch, samples);
  return executeBatch<SpiEngine::LPSPI>(batch, samples);
}

bool Adc124s101::sampleVmid(uint32_t settle_us, SpiEngine engine) {
  park(settle_us);
  return readVmid(engine, false);
}

bool Adc124s101::readVmid(SpiEngine engine, bool burst) {
  static const PreparedBatch batch{{0, 1 << 11, 2 << 11, 3 << 11, 3 << 11},
                                   {0xFF, 0xFF, 0xFF, 0xFF, 0xFF}, 5};
  return readPrepared(batch, engine, nullptr, burst);
}

bool Adc124s101::waitComplete(bool dma) {
#if defined(__IMXRT1062__)
  const uint32_t started = dma ? micros() : ARM_DWT_CYCCNT;
#else
  const uint32_t started = micros();
#endif
  uint32_t last_service = started;
#if defined(__IMXRT1062__)
  const uint32_t timeout = dma ? kSpiTransferTimeoutUs : timeout_ticks_;
#else
  const uint32_t timeout = kSpiTransferTimeoutUs;
#endif
  while (!(dma ? spi_.dmaComplete() : spi_.lpspiComplete())) {
#if defined(__IMXRT1062__)
    const uint32_t now = dma ? micros() : ARM_DWT_CYCCNT;
#else
    const uint32_t now = micros();
#endif
    if (uint32_t(now - started) >= timeout) return false;
    if (dma || now - last_service >= service_ticks_) { yield(); last_service = now; }
  }
  return true;
}

bool Adc124s101::executeBurst(const PreparedBatch &batch, SpiEngine engine, uint16_t *samples) {
  if (faulted_ || !batch.count || batch.count > 13) return false;
  uint16_t responses[13];
  bool success = false;
  if (engine == SpiEngine::BLOCKING) {
    success = spi_.transferBurst(kCsPins[index_], batch.commands, responses, batch.count);
  } else if (engine == SpiEngine::DMA) {
    success = spi_.startDmaBurst(kCsPins[index_], batch.commands, batch.count) &&
              waitComplete(true) && spi_.finishDmaBurst(responses, batch.count);
    if (!success) spi_.cancelDma16();
  } else {
    success = spi_.startLpspiBurst(chip_select_, batch.commands[0]);
    for (uint8_t i = 0; success && i < batch.count; ++i) {
      if (i) success = spi_.continueLpspiBurst(batch.commands[i]);
      success = success && waitComplete(false) && spi_.finishLpspi16(responses[i]);
    }
    if (success) spi_.endLpspiBurst(); else spi_.cancelLpspi16();
  }
  if (!success) { ++transfer_errors_; faulted_ = true; return false; }
#if TESTBOARD_PROFILE
  if (profile_ != nullptr) {
    AcquisitionProfiler::Scope validation(*profile_, AcquisitionProfiler::Validate);
    return consumeResponses(batch, responses, samples);
  }
#endif
  return consumeResponses(batch, responses, samples);
}

bool Adc124s101::consumeResponses(const PreparedBatch &batch, const uint16_t *responses, uint16_t *samples) {
  for (uint8_t i = 0; i < batch.count; ++i) {
    uint16_t value;
    if (!decode(responses[i], value)) return false;
    if (batch.destinations[i] != 0xFF) samples[batch.destinations[i]] = value;
  }
  return true;
}
