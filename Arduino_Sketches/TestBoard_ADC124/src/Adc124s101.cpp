#include "Adc124s101.h"

using namespace testboard_config;

namespace {
#if defined(__IMXRT1062__)
struct AddressPort {
  decltype(portSetRegister(0)) set = nullptr, clear = nullptr;
};
AddressPort address_ports[6];
uint8_t address_port_count = 0;
#endif
}

#if defined(__IMXRT1062__)
Adc124s101::AddressWrites Adc124s101::pair_changes_[9][9][8];
Adc124s101 *Adc124s101::cached_banks_[2] = {};
#endif

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
#if defined(__IMXRT1062__)
  cacheAddressChanges();
#endif
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
  disable();
#if defined(__IMXRT1062__)
  const AddressWrites &sequence = address_changes_[address_ == 0xFF ? 8 : address_][address].sequence;
  for (uint8_t i = 0; i < sequence.count; ++i)
    *sequence.writes[i].reg = sequence.writes[i].mask;
#else
  const uint8_t changed = address_ == 0xFF ? 7 : address_ ^ address;
  for (uint8_t bit = 0; bit < 3; ++bit)
    if (changed & (1 << bit)) address_gpio_[bit].write((address >> bit) & 1);
#endif
  address_ = address; enable(true);
  delayNanoseconds(kMuxDisableTimeNs);
  return true;
}

#if defined(__IMXRT1062__)
void Adc124s101::cacheAddressChanges() {
  for (uint8_t bit = 0; bit < 3; ++bit) {
    const Gpio &gpio = address_gpio_[bit];
    uint8_t port = 0;
    while (port < address_port_count && address_ports[port].set != gpio.set) ++port;
    if (port == address_port_count) {
      address_ports[port] = {gpio.set, gpio.clear}; ++address_port_count;
    }
    for (uint8_t from = 0; from < 9; ++from) for (uint8_t to = 0; to < 8; ++to) {
      if (from != 8 && !((from ^ to) & (1 << bit))) continue;
      AddressChange &change = address_changes_[from][to];
      (to & (1 << bit) ? change.set[port] : change.clear[port]) |= gpio.mask;
    }
  }
  for (uint8_t from = 0; from < 9; ++from) for (uint8_t to = 0; to < 8; ++to) {
    AddressChange &change = address_changes_[from][to];
    change.sequence.count = 0;
    for (uint8_t port = 0; port < address_port_count; ++port) {
      if (change.clear[port]) change.sequence.writes[change.sequence.count++] = {address_ports[port].clear, change.clear[port]};
      if (change.set[port]) change.sequence.writes[change.sequence.count++] = {address_ports[port].set, change.set[port]};
    }
  }
  cached_banks_[index_] = this;
  if (!cached_banks_[0] || !cached_banks_[1]) return;
  // Precompute all histories: sparse plans may leave the banks at different
  // addresses. No mask assembly or pin/register lookup remains in a sweep.
  for (uint8_t a = 0; a < 9; ++a) for (uint8_t b = 0; b < 9; ++b)
    for (uint8_t to = 0; to < 8; ++to) {
      AddressWrites &sequence = pair_changes_[a][b][to]; sequence.count = 0;
      const AddressChange &ca = cached_banks_[0]->address_changes_[a][to];
      const AddressChange &cb = cached_banks_[1]->address_changes_[b][to];
      for (uint8_t port = 0; port < address_port_count; ++port) {
        const uint32_t clear = ca.clear[port] | cb.clear[port], set = ca.set[port] | cb.set[port];
        if (clear) sequence.writes[sequence.count++] = {address_ports[port].clear, clear};
        if (set) sequence.writes[sequence.count++] = {address_ports[port].set, set};
      }
    }
}
#endif

bool Adc124s101::switchTogether(Adc124s101 *first, Adc124s101 *second, uint8_t address) {
  if (address > 7 || (!first && !second)) return false;
  if (first == second) second = nullptr;
  if (!first) return second->switchAddress(address);
  if (!second) return first->switchAddress(address);
  const bool change_a = first && (!first->enabled_ || first->address_ != address);
  const bool change_b = second && (!second->enabled_ || second->address_ != address);
  if (!change_a && !change_b) return false;
  const bool off_guard = (change_a && first->enabled_) || (change_b && second->enabled_);
  if (change_a) first->enable(false);
  if (change_b) second->enable(false);
  // The guard starts after the LAST disable, so every changing bank gets it.
  if (off_guard) delayNanoseconds(kMuxDisableTimeNs);
#if defined(__IMXRT1062__)
  const auto *a = first->index_ == 0 ? first : second;
  const auto *b = first->index_ == 1 ? first : second;
  const AddressWrites &sequence = pair_changes_[a->address_ == 0xFF ? 8 : a->address_][b->address_ == 0xFF ? 8 : b->address_][address];
  for (uint8_t i = 0; i < sequence.count; ++i)
    *sequence.writes[i].reg = sequence.writes[i].mask;
#else
  for (auto adc : {change_a ? first : nullptr, change_b ? second : nullptr}) if (adc) {
    const uint8_t changed = adc->address_ == 0xFF ? 7 : adc->address_ ^ address;
    for (uint8_t bit = 0; bit < 3; ++bit)
      if (changed & (1 << bit)) adc->address_gpio_[bit].write((address >> bit) & 1);
  }
#endif
  if (change_a) { first->address_ = address; first->enable(true); }
  if (change_b) { second->address_ = address; second->enable(true); }
  // Likewise wait after the last enable. Analog settling remains additional.
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
    success = spi_.transferLpspiBurst(chip_select_, batch.commands, responses,
                                    batch.count, timeout_ticks_, service_ticks_);
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
