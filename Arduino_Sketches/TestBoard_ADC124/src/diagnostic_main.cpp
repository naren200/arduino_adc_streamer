// Isolated experimental microprogram runner. Not compiled into production.
#if TESTBOARD_DIAGNOSTIC
#include <Arduino.h>
#include <SPI.h>
#include <cstdio>
#include <cstring>
#include "Adc124s101.h"
#include "ApiProtocol.h"
#include "UsbSerialController.h"

using namespace testboard_config;
namespace pcbdiag {
constexpr unsigned kSteps = 32, kFrame = 2048;
// Flags: 1 = retain conversions, 2 = bank disabled during wait,
// 4 = disable immediately after conversions; 8 = disable at end of slot.
struct Step {
  uint32_t wait_us, slot_us;
  uint8_t address, mask, repeat, flags;
  Adc124s101::PreparedBatch batch;
};
UsbSerialController usb;
SpiController spi(SPI, kMisoPin, kMosiPin, kSckPin);
Adc124s101 adc1(spi, 0), adc2(spi, 1);
Adc124s101 *adcs[] = {&adc1, &adc2};
Step steps[kSteps];
uint8_t step_count = 0, arrays = 3, first_bank = 0;
uint8_t input_order = 228;  // packed 0,1,2,3 in low-to-high bit pairs
uint32_t period_us = 0, duration_ms = 0, began_ms = 0, next_us = 0;
uint32_t cycles = 0, sent = 0, dropped = 0, overruns = 0, session_errors = 0;
uint16_t frame_size = 0, event_count = 0;
bool running = false;
bool single_mode = false;
uint32_t requested_hold_us = 0, pending_hold_us = 0;
uint32_t last_hold_start_us = 0, last_hold_end_us = 0;
uint8_t frame[kFrame];

uint32_t hash(const uint8_t *p, unsigned n) {
  uint32_t h = 2166136261u;
  while (n--) { h ^= *p++; h *= 16777619u; }
  return h;
}
void put16(unsigned at, uint16_t v) { frame[at] = v; frame[at+1] = v >> 8; }
void put32(unsigned at, uint32_t v) { put16(at, v); put16(at+2, v >> 16); }
void waitUs(uint32_t us) {
  // Keep USB/interrupt service alive during controlled leakage intervals.
  if (us > 500) {
    const uint32_t target = micros()+us;
    while (int32_t(target-micros()) > 500) { delayMicroseconds(500); yield(); }
    const int32_t left=target-micros();
    if(left>0) delayMicroseconds(left);
    return;
  }
  if (us) delayMicroseconds(us);
}
void waitUntil(uint32_t target) {
  while (int32_t(target - micros()) > 0) {
    const int32_t left = target - micros();
    if (left > 20) waitUs(uint32_t(left - 5));
  }
}
bool selected(unsigned bank) { return arrays & (1u << bank); }
void disable() { for (unsigned b=0; b<2; ++b) if (selected(b)) adcs[b]->disable(); }
void park() { spi.endLpspiSession(); for (auto adc : adcs) adc->park(5); }
void stop() { running = false; pending_hold_us = 0; park(); }
bool switchBanks(uint8_t address) {
  return Adc124s101::switchTogether(selected(0) ? &adc1 : nullptr,
                                  selected(1) ? &adc2 : nullptr, address);
}
bool ordered(uint8_t packed) {
  uint8_t seen = 0;
  for (unsigned i=0; i<4; ++i) seen |= 1u << ((packed >> (2*i)) & 3);
  return seen == 15;
}
bool prepare() {
  unsigned bytes = 28, events = 0, readings = 0;
  uint32_t budget = 0;
  for (unsigned i=0; i<step_count; ++i) {
    auto &s = steps[i];
    unsigned count = 0;
    uint8_t sequence[12];
    for (unsigned p=0; p<4; ++p) {
      const uint8_t input = (input_order >> (p*2)) & 3;
      if (s.mask & (1u << input)) for (unsigned r=0; r<s.repeat; ++r) {
        if (count == 12) return false;
        sequence[count++] = input;
      }
    }
    s.batch.count = count;
    if (count) {
      s.batch.first_input = sequence[0];
      for (unsigned j=0; j<count; ++j) {
        s.batch.destinations[j] = j;
        s.batch.commands[j] = uint16_t(sequence[(j+1)%count]) << 11;
      }
      if (s.flags & 1) {
        const unsigned banks = (selected(0) ? 1 : 0) + (selected(1) ? 1 : 0);
        events += banks; bytes += banks * (16 + count*2); readings += banks*count;
      }
    }
    if ((s.flags & 2) && count) return false;
    budget += s.slot_us > s.wait_us ? s.slot_us : s.wait_us;
    if (budget > 2000000) return false;
  }
  if (!step_count || !readings || bytes > kFrame || period_us > 2000000) return false;
  // Prepare true one-conversion lookahead across all steps and cycle wrap.
  for (unsigned i=0; i<step_count; ++i) if (steps[i].batch.count) {
    unsigned next = (i+1)%step_count;
    while (!steps[next].batch.count) next = (next+1)%step_count;
    auto &batch = steps[i].batch;
    batch.commands[batch.count-1] = uint16_t(steps[next].batch.first_input) << 11;
  }
  frame_size = bytes; event_count = events;
  return true;
}
void status() {
  Serial.println("# board=ADC124_PCB_DIAGNOSTIC");
  Serial.println("# firmware_revision=adc124-pcbdiag-v2");
  Serial.println("# protocol_version=pcbdiag1");
  Serial.println("# spiengine=lpspi");
  Serial.println("# frame_magic=ADG1");
  Serial.print("# cpu_hz="); Serial.println(F_CPU_ACTUAL);
  Serial.print("# clock_hz="); Serial.println(spi.clockHz());
  Serial.print("# arrays="); Serial.println(arrays);
  Serial.print("# first_bank="); Serial.println(first_bank);
  Serial.print("# input_order="); Serial.println(input_order);
  Serial.print("# period_us="); Serial.println(period_us);
  Serial.print("# step_count="); Serial.println(step_count);
  Serial.print("# frame_bytes="); Serial.println(frame_size);
  Serial.print("# event_count="); Serial.println(event_count);
  Serial.print("# running="); Serial.println(running ? "true" : "false");
  Serial.print("# single_mode="); Serial.println(single_mode ? "true" : "false");
  Serial.print("# requested_hold_us="); Serial.println(requested_hold_us);
  Serial.print("# hold_start_us="); Serial.println(last_hold_start_us);
  Serial.print("# hold_end_us="); Serial.println(last_hold_end_us);
  Serial.print("# cycles="); Serial.println(cycles);
  Serial.print("# sent="); Serial.println(sent);
  Serial.print("# dropped="); Serial.println(dropped);
  Serial.print("# overruns="); Serial.println(overruns);
  Serial.print("# transfer_errors="); Serial.println(adc1.transferErrors()+adc2.transferErrors()+session_errors);
  Serial.print("# data_errors="); Serial.println(adc1.dataErrors()+adc2.dataErrors());
  Serial.print("# usb_write_errors="); Serial.println(usb.writeErrors());
  for (unsigned i=0; i<step_count; ++i) {
    const auto &s = steps[i];
    Serial.print("# step"); Serial.print(i); Serial.print('=');
    Serial.print(s.address); Serial.print(','); Serial.print(s.wait_us); Serial.print(',');
    Serial.print(s.slot_us); Serial.print(','); Serial.print(s.mask); Serial.print(',');
    Serial.print(s.repeat); Serial.print(','); Serial.println(s.flags);
  }
}
bool command(const String &name, const String &args) {
  if (name == "mcu") { Serial.println("# ADC124_PCB_DIAGNOSTIC"); return !args.length(); }
  if (name == "status") { status(); return !args.length(); }
  if (name == "stop") { stop(); return !args.length(); }
  if (name == "clear") { step_count=0; frame_size=event_count=0; return !args.length(); }
  char text[256], extra;
  args.toCharArray(text, sizeof(text));
  unsigned a,b,c,d,e,f;
  if (name == "config") {
    if (sscanf(text,"%u %u %u %u %u %c",&a,&b,&c,&d,&e,&extra)!=5 ||
        b<1 || b>3 || c>1 || d>255 || !ordered(d) || e>2000000 || !spi.setClockHz(a)) return false;
    arrays=b; first_bank=c; input_order=d; period_us=e; return true;
  }
  if (name == "step") {
    if (step_count==kSteps || sscanf(text,"%u %u %u %u %u %u %c",&a,&b,&c,&d,&e,&f,&extra)!=6 ||
        a>7 || b>1500000 || c>1500000 || d>15 || e<1 || e>10 || f>15 || ((f&2)&&d)) return false;
    unsigned count=0; for(unsigned bit=0;bit<4;++bit) if(d&(1u<<bit)) count+=e;
    if(count>12) return false;
    auto &s=steps[step_count++]; s.address=a; s.wait_us=b; s.slot_us=c; s.mask=d; s.repeat=e; s.flags=f;
    return true;
  }
  if (name == "run" || name == "single") {
    const bool once = name == "single";
    if (sscanf(text,"%u %c",&a,&extra)!=1 || (once ? a>30000000 : (!a || a>3600000)) ||
        adc1.transferErrors() || adc2.transferErrors() || adc1.dataErrors() || adc2.dataErrors() ||
        session_errors || usb.writeErrors() || !prepare()) return false;
    park();
    unsigned first=0; while(!steps[first].batch.count) ++first;
    for(unsigned bank=0;bank<2;++bank) {
      if (selected(bank)) {
        if(!adcs[bank]->selectInput(steps[first].batch.first_input,SpiEngine::LPSPI)) {stop();return false;}
      } else adcs[bank]->disable();
    }
    cycles=sent=dropped=overruns=0;
    single_mode=once;
    requested_hold_us=pending_hold_us=once ? a : 0;
    last_hold_start_us=last_hold_end_us=0;
    duration_ms=once ? 0 : a; began_ms=millis(); next_us=micros(); running=true;
    return true;
  }
  return false;
}
bool capture() {
  if (period_us) waitUntil(next_us);
  const uint32_t start=micros(), tick=ARM_DWT_CYCCNT;
  next_us=start+period_us;
  if(!spi.beginLpspiSession()) {++session_errors;return false;}
  unsigned at=24;
  for(unsigned i=0;i<step_count;++i) {
    const auto &s=steps[i];
    const uint32_t phase_start=micros();
    if(s.flags&2) disable(); else switchBanks(s.address);
    const uint32_t switched=ARM_DWT_CYCCNT-tick;
    waitUs(s.wait_us);
    for(unsigned j=0;j<2;++j) {
      const unsigned bank=(first_bank+j)%2;
      if(!selected(bank)||!s.batch.count) continue;
      uint16_t values[12];
      const uint32_t batch_start=ARM_DWT_CYCCNT-tick;
      if(!adcs[bank]->readPrepared(s.batch,SpiEngine::LPSPI,values,true)) {spi.endLpspiSession();return false;}
      const uint32_t batch_end=ARM_DWT_CYCCNT-tick;
      if(s.flags&1) {
        frame[at]=i; frame[at+1]=bank; frame[at+2]=s.address; frame[at+3]=s.batch.count;
        put32(at+4,switched); put32(at+8,batch_start); put32(at+12,batch_end);
        at+=16;
        for(unsigned n=0;n<s.batch.count;++n) {put16(at,values[n]);at+=2;}
      }
    }
    if(s.flags&4) disable();
    if(s.slot_us) {
      if(uint32_t(micros()-phase_start)>s.slot_us+2) ++overruns;
      waitUntil(phase_start+s.slot_us);
    }
    if(s.flags&8) disable();
  }
  spi.endLpspiSession();
  const uint32_t end=micros();
  if(period_us && uint32_t(end-start)>period_us) ++overruns;
  memcpy(frame,"ADG1",4); put16(4,frame_size); put16(6,event_count);
  put32(8,cycles); put32(12,start); put32(16,end); put32(20,overruns);
  if(at+4!=frame_size) {++session_errors;return false;}
  put32(at,hash(frame,at));
  ++cycles;
  const auto result=usb.tryWriteBinaryBlock(frame,frame_size);
  if(result==UsbSerialController::FrameWrite::Sent) ++sent;
  else if(result==UsbSerialController::FrameWrite::Discarded) ++dropped;
  else return false;
  return true;
}
} // namespace pcbdiag

void setup() {
  using namespace pcbdiag;
  usb.begin(kUsbSerialBaud); spi.begin();
  adc1.begin(); adc2.begin();
  Serial.println("# ADC124_PCB_DIAGNOSTIC");
}
void loop() {
  using namespace pcbdiag;
  String line;
  while(usb.readCommand(line)) {
    String name,args; api_protocol::splitCommand(line,name,args);
    if(running) stop();
    const bool ok=command(name,args); usb.writeAck(ok);
  }
  if(running) {
    // Prime selectors BEFORE this no-conversion VMID hold. Keep long holds
    // outside the ADG1 cycle's 32-bit CPU-tick timeline (wraps around 7 s).
    if(pending_hold_us) {
      last_hold_start_us=micros();
      waitUs(pending_hold_us);
      last_hold_end_us=micros();
      pending_hold_us=0;
    }
    if(!single_mode && uint32_t(millis()-began_ms)>=duration_ms) stop();
    else if(!capture() || single_mode) stop();
  }
  yield();
}
#endif
