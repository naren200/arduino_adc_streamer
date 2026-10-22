// Execute the production application with tagged ADC/CS/GPIO/register models.
#include "../../src/diagnostic_main.cpp"
using namespace pcbdiag;
#ifdef NDEBUG
#error Runtime checks require assertions
#endif
#include <iostream>
#include <tuple>

FakeSerial Serial;
uint32_t fake_us = 0;
int pin_values[64] = {};
IMXRT_LPSPI_t IMXRT_LPSPI4_S, IMXRT_LPSPI3_S;
SPIClass SPI(&IMXRT_LPSPI4_S), SPI1(&IMXRT_LPSPI3_S);
uint8_t selected_input[2] = {3, 2};
bool bad_bits = false;
unsigned conversions = 0;
uint64_t last_enable[2] = {}, last_cs = 0;
std::vector<uint32_t> conversion_times;

void verify_spi_cs_release(uint8_t) {
  assert(IMXRT_LPSPI4_S.tx.empty());
  assert(!(IMXRT_LPSPI4_S.SR.value & LPSPI_SR_MBF));
  assert(!IMXRT_LPSPI4_S.final_clock_pending);
}

extern "C" int usb_serial_try_write_frame(const void *buffer, uint32_t size) {
  ++Serial.live_write_calls;
  if (!buffer || !size || size > 2048 || Serial.write_limit < size) return -1;
  if (!Serial.connected || Serial.write_capacity < static_cast<int>(size)) return 0;
  const auto *bytes = static_cast<const uint8_t *>(buffer);
  Serial.bytes.insert(Serial.bytes.end(), bytes, bytes + size);
  return size;
}

uint8_t address(int array) {
  const auto &pins = kMuxAddressPins[array];
  return pin_values[pins[0]] | (pin_values[pins[1]] << 1) | (pin_values[pins[2]] << 2);
}

uint16_t emulate_conversion(IMXRT_LPSPI_t *bus, uint16_t command_word) {
  assert(bus == &IMXRT_LPSPI4_S);
  int adc = -1;
  for (int i = 0; i < 2; ++i) if (pin_values[kCsPins[i]] == LOW) { assert(adc == -1); adc = i; }
  assert(adc != -1 && pin_values[kMuxEnablePins[adc]] == kMuxEnableActiveHigh[adc]);
  // Fresh selector latency persists across CS edges, ADC switches and bursts.
  assert(!(command_word & ~0x1800));
  uint16_t value = (adc + 1) * 1000 + selected_input[adc] * 100 + address(adc);
  selected_input[adc] = (command_word >> 11) & 3;
  ++conversions; fake_us += 2;
  conversion_times.push_back(fake_us);
  return bad_bits ? value | 0x8000 : value;
}


uint16_t le16(size_t at) {return Serial.bytes[at]|(Serial.bytes[at+1]<<8);}
uint32_t le32(size_t at) {return le16(at)|(uint32_t(le16(at+2))<<16);}
int main() {
 setup();
 unsigned cases=0;
 for(unsigned banks: {1,2,3}) for(unsigned first:{0,1}) for(unsigned order:{228,27,57})
 for(unsigned mask=1;mask<16;++mask) for(unsigned repeats:{1,2,3}) {
  std::string cfg="16000000 "+std::to_string(banks)+" "+std::to_string(first)+" "+std::to_string(order)+" 1000";
  assert(command("clear",""));assert(command("config",cfg.c_str()));
  for(unsigned a:{2,5,0,6,7}) {
   auto s=std::to_string(a)+" 5 100 "+std::to_string(mask)+" "+std::to_string(repeats)+" 1";
   assert(command("step",s.c_str()));
  }
  assert(command("run","1000"));
  for(unsigned cycle=0;cycle<3;++cycle) {
   Serial.bytes.clear();assert(capture());assert(Serial.bytes.size()==frame_size);
   assert(le16(4)==frame_size && le16(6)==event_count);
   assert(le32(8)==cycle);
   assert(le32(frame_size-4)==hash(Serial.bytes.data(),frame_size-4));
   size_t at=24;unsigned events=0;
   for(unsigned i=0;i<step_count;++i)for(unsigned j=0;j<2;++j) {
    unsigned b=(first+j)%2;if(!(banks&(1u<<b)))continue;
    assert(Serial.bytes[at]==i && Serial.bytes[at+1]==b && Serial.bytes[at+2]==steps[i].address);
    auto n=Serial.bytes[at+3];assert(n==steps[i].batch.count);at+=16;unsigned k=0;
    for(unsigned o=0;o<4;++o){auto in=(order>>(2*o))&3;if(mask&(1u<<in))for(unsigned r=0;r<repeats;++r){
      assert(le16(at)==(b+1)*1000+in*100+steps[i].address);at+=2;++k;
    }}assert(k==n);++events;
   }
   assert(events==event_count && at+4==frame_size);
  }
  stop();assert(pin_values[9]==HIGH && pin_values[24]==HIGH);assert(address(0)==7 && address(1)==7);++cases;
 }
 assert(command("clear",""));assert(command("config","8000000 3 1 228 1200"));
 assert(command("step","7 100 120 0 1 0"));assert(command("step","2 1000 1100 0 1 2"));
 assert(command("step","2 5 100 4 8 9"));assert(command("run","1000"));Serial.bytes.clear();assert(capture());stop();
 assert(!command("config","16000000 3 0 0 0"));assert(!command("step","2 1 0 15 8 1"));
 assert(!command("step","2 1 0 1 1 2"));assert(!command("step","8 1 0 1 1 1"));
 assert(command("run","1000"));Serial.write_capacity=0;assert(capture());assert(dropped==1);stop();Serial.write_capacity=2048;
 assert(command("clear",""));assert(command("config","16000000 3 1 228 0"));
 assert(command("step","2 5 0 4 10 1"));
 assert(!command("single","30000001"));assert(command("single","20000000"));
 auto before=conversions;Serial.bytes.clear();loop();
 assert(!running && cycles==1 && sent==1 && dropped==0);
 assert(uint32_t(last_hold_end_us-last_hold_start_us)>=20000000);
 assert(conversions-before==20 && le32(8)==0);
 assert(le32(12)>=last_hold_end_us);
 for(unsigned i=before;i<conversion_times.size();++i)assert(conversion_times[i]>=last_hold_end_us);
 assert(Serial.bytes[27]==10 && le16(40)==2202);
 assert(le16(76)==1202);
 assert(command("single","0"));before=conversions;Serial.bytes.clear();loop();
 assert(!running && cycles==1 && conversions-before==20);
 assert(!command("step","2 5 0 1 11 1"));
 assert(command("run","1000"));bad_bits=true;assert(!capture());bad_bits=false;stop();assert(!command("run","1000"));
 std::cout<<cases<<" diagnostic tagged pipeline/order/ADC/GPIO cases passed; malformed, isolation, checksum and failure cases passed\n";
}
