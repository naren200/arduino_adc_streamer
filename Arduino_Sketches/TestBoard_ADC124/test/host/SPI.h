#pragma once
#include "Arduino.h"
#include "EventResponder.h"
#include <deque>
#define SPI_HAS_TRANSFER_ASYNC 1
#define MSBFIRST 1
#define LSBFIRST 0
#define SPI_MODE0 0
#define LPSPI_CR_MEN 1u
#define LPSPI_CR_RRF 2u
#define LPSPI_CR_RTF 4u
#define LPSPI_TCR_FRAMESZ(n) (static_cast<uint32_t>(n))
#define LPSPI_RSR_RXEMPTY 1u
#define LPSPI_SR_MBF (1u << 24)
#define LPSPI_SR_TCF (1u << 10)
#define LPSPI_SR_TEF (1u << 11)
#define LPSPI_SR_REF (1u << 12)
#define LPSPI_TCR_LSBF (1u << 23)
#define LPSPI_TCR_CPOL (1u << 31)
#define LPSPI_TCR_CPHA (1u << 30)
#define LPSPI_TCR_CONT (1u << 21)
#define LPSPI_TCR_RXMSK (1u << 19)

// NXP documents that TCR reads can be incorrect while a queued command loads.
// The original plain-integer stub could not exercise that failure mode.
struct IMXRT_LPSPI_t;
struct FakeCommand {
  IMXRT_LPSPI_t *owner;
  uint32_t value = 7;
  bool unstable_read = false;
  unsigned unstable_reads = 0;
  operator uint32_t() {
    if (unstable_read) { ++unstable_reads; return value | LPSPI_TCR_CONT | LPSPI_TCR_RXMSK; }
    return value;
  }
  void operator=(uint32_t command);
};

struct IMXRT_LPSPI_t;
void fake_progress(IMXRT_LPSPI_t *owner);
void fake_clock_finished(IMXRT_LPSPI_t *owner);

// SR completion flags are write-one-to-clear; MBF is read-only.
struct FakeStatus {
  IMXRT_LPSPI_t *owner;
  uint32_t value = 0;
  unsigned completion_reads = 0;
  operator uint32_t() {
    fake_progress(owner);
    if (completion_reads && !--completion_reads) {
      value |= LPSPI_SR_TCF;
      value &= ~LPSPI_SR_MBF;
      fake_clock_finished(owner);
    }
    return value;
  }
  void operator=(uint32_t mask) {
    value &= ~(mask & 0x3F00u);
    if (mask & LPSPI_SR_TCF) completion_reads = 0;
  }
};

struct FakeReceiveStatus {
  IMXRT_LPSPI_t *owner;
  operator uint32_t();
  void operator=(uint32_t value);
};
struct FakeFifoStatus {
  IMXRT_LPSPI_t *owner;
  operator uint32_t();
};
struct FakeControl {
  IMXRT_LPSPI_t *owner;
  uint32_t value = 0;
  void operator=(uint32_t value);
  operator uint32_t() const { return value; }
};
struct FakeReceive {
  IMXRT_LPSPI_t *owner;
  operator uint32_t();
};
struct FakeTransmit {
  IMXRT_LPSPI_t *owner;
  void operator=(uint32_t command);
};
struct IMXRT_LPSPI_t {
  uint32_t PARAM = (4u << 8) | 4u, DER = 0;
  FakeControl CR{this};
  FakeReceiveStatus RSR{this};
  FakeFifoStatus FSR{this};
  FakeStatus SR{this};
  FakeCommand TCR{this};
  FakeReceive RDR{this};
  FakeTransmit TDR{this};
  bool stall = false;
  unsigned completion_reads = 0;
  std::deque<uint16_t> tx, rx;
  unsigned max_tx = 0, max_rx = 0, queued_writes = 0;
  uint32_t inject_error = 0;
  bool final_clock_pending = false;
  bool pending_command = false;
};
extern IMXRT_LPSPI_t IMXRT_LPSPI4_S, IMXRT_LPSPI3_S;
uint16_t emulate_conversion(IMXRT_LPSPI_t *bus, uint16_t command);

inline FakeReceive::operator uint32_t() {
  assert(!owner->rx.empty()); const auto value = owner->rx.front(); owner->rx.pop_front(); return value;
}
inline FakeReceiveStatus::operator uint32_t() {
  fake_progress(owner); return owner->rx.empty() ? LPSPI_RSR_RXEMPTY : 0;
}
inline void FakeReceiveStatus::operator=(uint32_t value) { if (value & LPSPI_RSR_RXEMPTY) owner->rx.clear(); }
inline FakeFifoStatus::operator uint32_t() {
  return uint32_t(owner->tx.size() + owner->pending_command) | (uint32_t(owner->rx.size()) << 16);
}
inline void FakeCommand::operator=(uint32_t command) { value = command; owner->pending_command = true; }
inline void FakeControl::operator=(uint32_t command) {
  value = command;
  if (command & (LPSPI_CR_RRF | LPSPI_CR_RTF)) {
    owner->tx.clear(); owner->rx.clear(); owner->SR.value = 0;
    owner->SR.completion_reads = 0; owner->final_clock_pending = false;
    owner->pending_command = false;
  }
}
inline unsigned fake_stalled_words = 0;
inline void FakeTransmit::operator=(uint32_t command) {
  if (owner->stall || (owner->TCR.value & LPSPI_TCR_RXMSK)) {
    ++fake_stalled_words;
    owner->SR.value |= LPSPI_SR_MBF; return;
  }
  assert((owner->TCR.value & 0xFFFu) == 15);
  assert(owner->tx.size() + owner->pending_command < (1u << (owner->PARAM & 0xFF)));
  if (!owner->tx.empty() || !owner->rx.empty()) ++owner->queued_writes;
  owner->tx.push_back(static_cast<uint16_t>(command));
  owner->max_tx = std::max(owner->max_tx, unsigned(owner->tx.size()));
  owner->SR.value |= LPSPI_SR_MBF; owner->SR.completion_reads = 0;
  owner->final_clock_pending = false;
}
inline void fake_progress(IMXRT_LPSPI_t *owner) {
  if (!owner->stall) owner->pending_command = false;
  if (owner->stall || owner->tx.empty()) return;
  // Complete queued clocks before the CPU drains RX, exercising worst-case
  // receive occupancy as well as asymmetric FIFO capacities.
  while (!owner->tx.empty()) {
    assert(owner->rx.size() < (1u << ((owner->PARAM >> 8) & 0xFF)));
    owner->rx.push_back(emulate_conversion(owner, owner->tx.front())); owner->tx.pop_front();
    owner->max_rx = std::max(owner->max_rx, unsigned(owner->rx.size()));
  }
  owner->SR.value |= owner->inject_error;
  if (owner->tx.empty()) {
    owner->final_clock_pending = true;
    owner->SR.completion_reads = owner->completion_reads ? owner->completion_reads : 1;
  }
}
inline void fake_clock_finished(IMXRT_LPSPI_t *owner) { owner->final_clock_pending = false; }

struct SPISettings { SPISettings(uint32_t, uint8_t, uint8_t) {} };
class SPIClass {
 public:
  explicit SPIClass(IMXRT_LPSPI_t *registers) : regs(registers) {}
  void setMISO(uint8_t) {} void setMOSI(uint8_t) {} void setSCK(uint8_t) {} void begin() {}
  void beginTransaction(SPISettings) { ++begins; regs->TCR = 7; regs->RSR = LPSPI_RSR_RXEMPTY; }
  void endTransaction() { ++ends; }
  uint16_t transfer16(uint16_t command) { return emulate_conversion(regs, command); }
  bool transfer(uint8_t *tx, uint8_t *rx, size_t n, EventResponder &event) {
    assert(n >= 2 && n <= 26 && n % 2 == 0);
    for (size_t i = 0; i < n; i += 2) {
      auto word = emulate_conversion(regs, static_cast<uint16_t>((tx[i] << 8) | tx[i + 1]));
      rx[i] = word >> 8; rx[i + 1] = word & 255;
    }
    event.ready = true; return true;
  }
  IMXRT_LPSPI_t *regs;
  unsigned begins = 0, ends = 0;
};
extern SPIClass SPI, SPI1;
