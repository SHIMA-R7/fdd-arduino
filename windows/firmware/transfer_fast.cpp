// ATmega328P, 16 MHz, Arduino Uno pin mapping.
// Requires the ArduinoFDC.cpp/.h library by David Hansel (GPL-3.0).
// The port starts at 115200 and returns to it after idle or an H/0 request.
#include <Arduino.h>
#include "ArduinoFDC.h"

static constexpr byte disk_led = A3, disk_change_pin = A2;
static constexpr byte drive_motor = 4, drive_select = 5, drive_index = 7;
static bool disk_present = false, change_armed = false;
static bool probing_disk = false, probe_motor = false, probe_selected = false;
static bool index_high_seen = false;
static byte media_generation = 0;
static unsigned long probe_started = 0, next_probe = 0, next_change_check = 0;

static void set_disk_present(bool present) {
  if (disk_present != present) ++media_generation;
  disk_present = present;
  digitalWrite(disk_led, present ? HIGH : LOW);
}

static bool drive_selected() {
  return DDRD & _BV(DDD5);
}

static bool sample_disk_change() {
  const bool selected = drive_selected();
  pinMode(drive_select, OUTPUT);
  delayMicroseconds(30);
  const bool changed = digitalRead(disk_change_pin) == LOW;
  if (!selected) pinMode(drive_select, INPUT);
  return changed;
}

static void stop_disk_probe() {
  if (!probing_disk) return;
  if (probe_motor) pinMode(drive_motor, INPUT);
  if (!probe_selected) pinMode(drive_select, INPUT);
  probing_disk = false;
  next_probe = millis() + 1000;
}

static void service_disk_presence() {
  const unsigned long now = millis();
  if (probing_disk) {
    const bool index_high = digitalRead(drive_index) == HIGH;
    const bool detected = index_high_seen && !index_high;
    index_high_seen |= index_high;
    if (!detected && now - probe_started < 1200) return;
    stop_disk_probe();
    if (change_armed && sample_disk_change()) {
      set_disk_present(false);
      change_armed = false;
    }
    set_disk_present(detected);
    if (detected && !change_armed && sample_disk_change()) {
      const bool selected = drive_selected();
      // A drive can keep /DSKCHG low until a step with media inserted.
      ArduinoFDC.diskChanged();
      if (selected) pinMode(drive_select, OUTPUT);
    }
    change_armed = detected && !sample_disk_change();
    next_probe = now + (change_armed ? 30000UL : 3000UL);
    return;
  }
  if (change_armed && (int32_t)(now - next_change_check) >= 0) {
    next_change_check = now + 250;
    if (sample_disk_change()) {
      set_disk_present(false);
      change_armed = false;
      next_probe = now;
    }
  }
  if ((int32_t)(now - next_probe) < 0) return;
  probe_selected = drive_selected();
  probe_motor = !ArduinoFDC.motorRunning();
  pinMode(drive_select, OUTPUT);
  if (probe_motor) pinMode(drive_motor, OUTPUT);
  probe_started = now;
  index_high_seen = false;
  probing_disk = true;
}

// XDF needs one 1024-byte sector. Two 512-byte buffers share that storage.
static union {
  byte large[1028];
  struct { byte first[516], second[515]; } pair;
} sectors_buffer;
static unsigned long last_request;
static const unsigned long baud_rates[] = {115200, 250000, 500000, 1000000, 666667};
static byte rate_index = 0;
#ifdef ARDUINOFDC_IBM_PROBE
static int8_t ibm_position = -1;
#endif

// Same open-collector pins as ArduinoFDC's Uno configuration.
static constexpr byte music_step = 2, music_dir = 3, music_select = 5;
static constexpr byte music_write_gate = 10, music_track0 = 11;
// A shorter sweep bounds position drift when rapid pulses miss a step.
static constexpr byte music_last_track = 36, music_max_corrections = 24;
static constexpr unsigned long music_settle_us = 4000;
static constexpr unsigned long music_timeout_ms = 1000;
static bool music_active = false, music_inward = true, music_fault = false;
static bool music_recovering = false;
static byte music_track = 0, music_corrections = 0;
static unsigned long music_period = 0, music_next = 0, music_last = 0;

static void music_pulse() {
  pinMode(music_step, OUTPUT);
  delayMicroseconds(4);
  pinMode(music_step, INPUT);
}

static void music_direction(bool inward) {
  music_inward = inward;
  pinMode(music_dir, inward ? OUTPUT : INPUT);
  delayMicroseconds(16);
}

static void stop_music() {
  if (!music_active) return;
  music_active = false;
  music_period = 0;
  music_recovering = false;
  pinMode(music_step, INPUT);
  pinMode(music_dir, INPUT);
  pinMode(music_select, INPUT);
}

static byte set_music_note(uint16_t frequency) {
  if (frequency && (frequency < 20 || frequency > 2000)) return 99;
  if (music_fault) return 7;
  if (!music_active && !frequency) return 0;
  if (!music_active) {
    ArduinoFDC.motorOff();
    pinMode(music_write_gate, INPUT);
    pinMode(music_select, OUTPUT);
    music_active = true;
    delay(2);
    music_direction(false);
    for (byte n = 0; n < 82 && digitalRead(music_track0); ++n) {
      music_pulse();
      delay(4);
    }
    if (digitalRead(music_track0)) {
      stop_music();
      return 7;
    }
    music_track = 0;
    music_direction(true);
  }
  const unsigned long period = frequency ? 1000000UL / frequency : 0;
  if (period != music_period) {
    music_period = period;
    if (!music_recovering) music_next = micros() + period;
  }
  music_last = millis();
  return 0;
}

static void service_music() {
  if (millis() - music_last >= music_timeout_ms) {
    stop_music();
    return;
  }
  const unsigned long now = micros();
  if (!music_period || (int32_t)(now - music_next) < 0) return;
  const bool at_track0 = !digitalRead(music_track0);
  if (!music_inward && at_track0) {
    music_track = 0;
    music_recovering = false;
    music_direction(true);
  } else if (!music_inward && !music_track) {
    // Wait for the carriage to settle, then take slow outward steps if needed.
    if (!music_recovering) {
      music_recovering = true;
      music_corrections = 0;
    } else if (music_corrections < music_max_corrections) {
      music_pulse();
      ++music_corrections;
    } else {
      stop_music();
      music_fault = true;
      return;
    }
    music_next = micros() + music_settle_us;
    return;
  } else if (music_inward && at_track0 && music_track >= 3) {
    // A stuck TRACK0 signal invalidates the position estimate.
    stop_music();
    music_fault = true;
    return;
  } else if (music_inward && music_track >= music_last_track) {
    music_direction(false);
  }
  music_pulse();
  music_track += music_inward ? 1 : -1;
  music_next += music_period;
  if ((int32_t)(now - music_next) >= 0) music_next = now + music_period;
}

static uint16_t wire_crc(const byte *data, uint16_t count) {
  uint16_t crc = 0xffff;
  while (count--) {
    crc ^= (uint16_t)*data++ << 8;
    for (byte i = 0; i < 8; ++i)
      crc = crc & 0x8000 ? (crc << 1) ^ 0x1021 : crc << 1;
  }
  return crc;
}

static void reply(byte status) {
  Serial.write(0x5a);
  Serial.write(status);
}

static void send_sector(const byte *data, uint16_t size = 512) {
  const uint16_t crc = wire_crc(data, size);
  Serial.write(data, size);
  Serial.write((byte)crc);
  Serial.write((byte)(crc >> 8));
}

static void change_rate(byte index) {
  Serial.flush();
  delay(25);
  Serial.end();
  rate_index = index;
  Serial.begin(baud_rates[index]);
  last_request = millis();
}

// Disk-free test for IBM XDF: sustain the 62,500 B/s required by an
// 8192-byte data field at 500 kbit/s. Timer1 runs freely at 16 MHz.
static void probe_xdf_stream() {
  constexpr uint16_t length = 8192, capacity = 1024, lead = 384;
  constexpr uint16_t byte_ticks = F_CPU / 62500UL;
  byte *const ring = sectors_buffer.large;
  uint16_t received = 0, consumed = 0;
  byte sum = 0, check = 0, result = 0, startup_windows = 0;
  reply(0);
  Serial.flush();
  noInterrupts();
  TCCR1A = 0;
  TCCR1B = _BV(CS10);
  TCCR1C = 0;
  uint16_t last_rx = TCNT1, next = last_rx;
  bool started = false;
  while (consumed < length) {
    const uint16_t now = TCNT1;
    const byte uart = UCSR0A;
    if (received < length && (uart & _BV(RXC0))) {
      const byte value = UDR0;
      if (uart & (_BV(FE0) | _BV(DOR0) | _BV(UPE0))) { result = 1; break; }
      if (received - consumed == capacity) { result = 2; break; }
      ring[received++ & (capacity - 1)] = value;
      last_rx = now;
    }
    if (!started && received >= lead) {
      started = true;
      next = now + byte_ticks;
    }
    if (started && (int16_t)(now - next) >= 0) {
      if (received == consumed) { result = 3; break; }
      const byte value = ring[consumed++ & (capacity - 1)];
      // Two rolling sums give the host a way to check the consumed bytes.
      sum += value;
      check += sum;
      next += byte_ticks;
    }
    if (received < length && (uint16_t)(now - last_rx) >= 64000) {
      // Timer1 wraps every 4.096 ms. Allow a separate startup window below.
      if (started || received) { result = 4; break; }
      last_rx = now;
      if (++startup_windows == 125) { result = 4; break; }
    }
  }
  TCCR1B = 0;
  interrupts();
  reply(result);
  Serial.write(sum);
  Serial.write(check);
}

void setup() {
  pinMode(disk_led, OUTPUT);
  set_disk_present(false);
  Serial.begin(baud_rates[0]);
  Serial.setTimeout(2000);
  ArduinoFDC.begin();
  ArduinoFDC.selectDrive(0);
  last_request = millis();
}

void loop() {
  if (!Serial.available()) {
    if (music_active) service_music();
    const unsigned long idle = millis() - last_request;
    if (idle > 3000 && !probing_disk && ArduinoFDC.motorRunning()) ArduinoFDC.motorOff();
    if (idle > 5000) {
      ArduinoFDC.setDriveType(ArduinoFDCClass::DT_3_HD);
      if (rate_index) change_rate(0);
    }
    if (!music_active && !rate_index) service_disk_presence();
    return;
  }
  if (Serial.read() != 0xa5) return;
  byte h[3];
  if (Serial.readBytes(h, 3) != 3) return;
  const byte op = h[0];
  const uint16_t address = h[1] | ((uint16_t)h[2] << 8);
  if (op == 'E') {
    if (address) { reply(99); return; }
    reply(0);
    Serial.write((byte)disk_present);
    Serial.write(media_generation);
    return;
  }
  stop_disk_probe();
  last_request = millis();
#ifdef ARDUINOFDC_IBM_PROBE
  if (op != 'I' && op != 'u' && op != 'U') ibm_position = -1;
#endif

  if (op == 'M') {
    reply(set_music_note(address));
    return;
  }
  stop_music();
  music_fault = false;

  if (op == 'O') {
    ArduinoFDC.motorOff();
    ArduinoFDC.setDriveType(ArduinoFDCClass::DT_3_HD);
    reply(0);
    return;
  }
  if (op == 'J') {
    if (address > 2) { reply(99); return; }
    const auto type = address == 2 ? ArduinoFDCClass::DT_3_X68 :
                      address == 1 ? ArduinoFDCClass::DT_3_DMF : ArduinoFDCClass::DT_3_HD;
    ArduinoFDC.setDriveType(type);
    reply(0);
    return;
  }
  if (op == 'H') {
    if (address >= sizeof(baud_rates) / sizeof(baud_rates[0])) { reply(99); return; }
    reply(0);
    if (address != rate_index) change_rate(address);
    return;
  }
  if (op == 'Y') {
    if (address || rate_index != 4) { reply(99); return; }
    ArduinoFDC.motorOff();
    probe_xdf_stream();
    last_request = millis();
    return;
  }
#ifdef ARDUINOFDC_IBM_PROBE
  if (op == 'q') {
    reply(address == 0 ? 0 : 99);
    return;
  }
  if (op == 'B' || op == 'K' || op == 'b' || op == 'V' ||
      op == 'I' || op == 'u' || op == 'U') {
    const bool scratch = op == 'B' || op == 'K' || op == 'b' || op == 'V';
    const bool format = op == 'B' || op == 'K' || op == 'I';
    const bool write = op == 'V' || op == 'U';
    const byte track = address & 0x7f;
    const byte side = (address >> 7) & 1;
    const byte sector = address >> 8;
    const byte sizeCode = sector & 0x7f;
    const bool mixedId = (sector & 0x80) &&
        (sizeCode == 2 || sizeCode == 3 || sizeCode == 4 || sizeCode == 6);
    const bool bootId = (sector >= 129 && sector <= (side ? 147 : 139)) ||
        (!side && sector >= 1 && sector <= 8);
    const bool valid = scratch ? track == 76 && !side && mixedId && (!format || sizeCode == 6)
        : track < 80 && (format ? sector == 0 : track ? mixedId : bootId);
    if (rate_index != 4 || !valid) { reply(99); return; }
    if (!format) {
      reply(0);
      Serial.flush();
    }
    if (write && Serial.readBytes(sectors_buffer.large + 1, 512) != 512) {
      reply(11);
      return;
    }
    const byte result = scratch ? ArduinoFDC.ibmProbe(sectors_buffer.large, op, sizeCode)
        : ArduinoFDC.ibmAccess(sectors_buffer.large, op, track, side, sector, ibm_position);
    if (write && result == 10) {
      // The host may still be transmitting the rest of the raw data block.
      const unsigned long started = millis();
      while (millis() - started < 175) {
        while (Serial.read() >= 0) {}
      }
    }
    reply(result);
    last_request = millis();
    return;
  }
#endif
  if (op == 'P') {
    byte v[6];
    pinMode(5, OUTPUT);
    v[0] = digitalRead(12); v[1] = digitalRead(8);
    delayMicroseconds(100);
    v[2] = digitalRead(12); v[3] = digitalRead(8);
    delay(2);
    v[4] = digitalRead(12); v[5] = digitalRead(8);
    pinMode(5, INPUT);
    reply(0);
    Serial.write(v, sizeof(v));
    return;
  }
  const byte sectors = ArduinoFDC.numSectors();
  const byte sectors_per_cylinder = sectors * 2;
  const byte cylinders = ArduinoFDC.numTracks();
  const bool x68 = ArduinoFDC.getDriveType() == ArduinoFDCClass::DT_3_X68;
  byte *const sector_buffer = x68 ? sectors_buffer.large : sectors_buffer.pair.first;
  const uint16_t size = x68 && op != 'T' ? 1024 : 512;
  const bool dmf = ArduinoFDC.getDriveType() == ArduinoFDCClass::DT_3_DMF;
  if (op == 'F' || op == 'G') {
    const byte first = address;
    const byte last = op == 'G' ? address >> 8 : first;
    if ((op == 'F' && address >= cylinders) || first > last || last >= cylinders) { reply(99); return; }
    ArduinoFDC.motorOn();
    pinMode(5, OUTPUT);
    delay(2);
    reply(ArduinoFDC.formatDisk(sector_buffer, first, last, dmf ? 2 : 1));
    last_request = millis();
    return;
  }
  if (op == 'D') {
    if (x68 || address >= cylinders * sectors_per_cylinder || address % sectors == sectors - 1) { reply(99); return; }
    ArduinoFDC.motorOn();
    const byte track = address / sectors_per_cylinder;
    const byte side = (address / sectors) % 2;
    const byte sector = address % sectors + 1;
    byte status = ArduinoFDC.readSector(track, side, sector, sector_buffer);
    if (!status) status = ArduinoFDC.readSector(track, side, sector + 1, sectors_buffer.pair.second);
    reply(status);
    if (!status) {
      send_sector(sector_buffer + 1);
      send_sector(sectors_buffer.pair.second + 1);
    }
    last_request = millis();
    return;
  }
  if (op != 'T' && (address >= cylinders * sectors_per_cylinder || (op != 'R' && op != 'W' && op != 'w'))) {
    reply(99);
    return;
  }
  if (op == 'W' || op == 'w' || op == 'T') {
    if (Serial.readBytes(sector_buffer + 1, size) != size) return;
    byte checksum[2];
    if (Serial.readBytes(checksum, 2) != 2) return;
    if (wire_crc(sector_buffer + 1, size) != (checksum[0] | ((uint16_t)checksum[1] << 8))) {
      reply(98);
      return;
    }
    if (op == 'T') {
      reply(0);
      send_sector(sector_buffer + 1);
      last_request = millis();
      return;
    }
  }
  ArduinoFDC.motorOn();
  if (op != 'R') {
    pinMode(5, OUTPUT);
    delay(2);
  }
  const byte track = address / sectors_per_cylinder;
  const byte side = (address / sectors) % 2;
  const byte sector = address % sectors + 1;
  const byte status = op == 'R' ? ArduinoFDC.readSector(track, side, sector, sector_buffer)
    : ArduinoFDC.writeSector(track, side, sector, sector_buffer, op == 'W');
  reply(status);
  if (op == 'R' && !status) send_sector(sector_buffer + 1, size);
  last_request = millis();
}
