#pragma once

#include "SpiController.h"
#include "AcquisitionProfiler.h"

enum class SpiEngine : uint8_t { BLOCKING, DMA, LPSPI };

// A route identifies an ADC/array, its MUX, and a zero-based external input.
struct SensorRoute {
  uint8_t array;  // 1..2 (also ADC number)
  uint8_t mux;    // 1..4
  uint8_t input;  // 0..6; input 6 is a sensor only on MUX4
};

bool validSensorRoute(const SensorRoute &route);

class Adc124s101 {
 public:
  struct PreparedBatch {
    uint16_t commands[13] = {};
    uint8_t destinations[13] = {};  // 0xFF discards a selector prime.
    uint8_t count = 0;
  };
  Adc124s101(SpiController &spi, uint8_t array_index);
  void begin();
  void refreshTiming();
  void disable();
  void selectAddress(uint8_t address, uint32_t settle_us);
  bool switchAddress(uint8_t address);
  static bool switchTogether(Adc124s101 *first, Adc124s101 *second, uint8_t address);
  void park(uint32_t settle_us);
  bool transfer(uint8_t adc_input, SpiEngine engine, uint16_t &sample);
  bool prepareBatch(const SensorRoute *routes, uint8_t count, uint8_t repeat,
                    PreparedBatch &batch) const;
  bool readPrepared(const PreparedBatch &batch, SpiEngine engine, uint16_t *samples, bool burst = false);
  bool readVmid(SpiEngine engine, bool burst);
#if TESTBOARD_PROFILE
  void setProfiler(AcquisitionProfiler &profile) { profile_ = &profile; }
#endif
  // All routes in a batch must have this ADC and the same MUX address. Prime
  // the one-frame channel selector once, then pipeline the four ADC inputs.
  bool readBatch(const SensorRoute *routes, uint8_t count, uint8_t repeat,
                 SpiEngine engine, uint16_t *samples);
  bool sampleVmid(uint32_t settle_us, SpiEngine engine);
  uint32_t transferErrors() const { return transfer_errors_; }
  uint32_t dataErrors() const { return data_errors_; }

 private:
  template <SpiEngine Engine> bool transferWord(uint16_t command, uint16_t &sample);
  template <SpiEngine Engine> bool executeBatch(const PreparedBatch &batch, uint16_t *samples);
  bool executeBurst(const PreparedBatch &batch, SpiEngine engine, uint16_t *samples);
  bool consumeResponses(const PreparedBatch &batch, const uint16_t *responses, uint16_t *samples);
  bool waitComplete(bool dma);
  bool decode(uint16_t response, uint16_t &sample);
  void enable(bool enabled);
  struct Gpio {
    uint8_t pin = 0;
#if defined(__IMXRT1062__)
    decltype(portSetRegister(0)) set = nullptr, clear = nullptr;
    uint32_t mask = 0;
#endif
    void resolve(uint8_t value);
    void write(bool high) const;
  };
  Gpio enable_gpio_, address_gpio_[3];
#if defined(__IMXRT1062__)
  struct RegisterWrite { decltype(portSetRegister(0)) reg = nullptr; uint32_t mask = 0; };
  struct AddressWrites { RegisterWrite writes[6]; uint8_t count = 0; };
  struct AddressChange { uint32_t clear[6] = {}, set[6] = {}; AddressWrites sequence; };
  AddressChange address_changes_[9][8];  // row 8 means unknown startup address
  static AddressWrites pair_changes_[9][9][8];
  static Adc124s101 *cached_banks_[2];
  void cacheAddressChanges();
#endif
  SpiController &spi_;
  uint8_t index_;
  SpiController::ChipSelect chip_select_;
  uint8_t address_ = 0xFF;
  bool enabled_ = false;
  // DMA abort cannot reset SPIClass's private DMA state. Latch the fault and
  // require a reset instead of reusing buffers that may still be DMA-owned.
  bool faulted_ = false;
  uint32_t transfer_errors_ = 0;
  uint32_t data_errors_ = 0;
  uint32_t timeout_ticks_ = 0, service_ticks_ = 0;
#if TESTBOARD_PROFILE
  AcquisitionProfiler *profile_ = nullptr;
#endif
};
