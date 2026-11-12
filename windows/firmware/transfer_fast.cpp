// ATmega328P, 16 MHz, Arduino Uno pin mapping.
// Requires the ArduinoFDC.cpp/.h library by David Hansel (GPL-3.0).
// The port starts at 115200 and returns to it after idle or an H/0 request.
#include <Arduino.h>
#include "ArduinoFDC.h"

static byte sector_buffer[516];
static byte next_sector_buffer[515];
static unsigned long last_request;
static const unsigned long baud_rates[] = {115200, 250000, 500000, 1000000};
static byte rate_index = 0;

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

static void send_sector(const byte *data) {
  const uint16_t crc = wire_crc(data, 512);
  Serial.write(data, 512);
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

void setup() {
  Serial.begin(baud_rates[0]);
  Serial.setTimeout(2000);
  ArduinoFDC.begin();
  ArduinoFDC.selectDrive(0);
  last_request = millis();
}

void loop() {
  if (!Serial.available()) {
    const unsigned long idle = millis() - last_request;
    if (idle > 3000 && ArduinoFDC.motorRunning()) ArduinoFDC.motorOff();
    if (idle > 5000 && rate_index) change_rate(0);
    return;
  }
  if (Serial.read() != 0xa5) return;
  byte h[3];
  if (Serial.readBytes(h, 3) != 3) return;
  const byte op = h[0];
  const uint16_t address = h[1] | ((uint16_t)h[2] << 8);
  last_request = millis();

  if (op == 'O') {
    ArduinoFDC.motorOff();
    reply(0);
    return;
  }
  if (op == 'H') {
    if (address >= sizeof(baud_rates) / sizeof(baud_rates[0])) { reply(99); return; }
    reply(0);
    if (address != rate_index) change_rate(address);
    return;
  }
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
  if (op == 'F' || op == 'G') {
    const byte first = address;
    const byte last = op == 'G' ? address >> 8 : first;
    if ((op == 'F' && address >= 80) || first > last || last >= 80) { reply(99); return; }
    ArduinoFDC.motorOn();
    pinMode(5, OUTPUT);
    delay(2);
    reply(ArduinoFDC.formatDisk(sector_buffer, first, last));
    last_request = millis();
    return;
  }
  if (op == 'D') {
    if (address >= 2880 || address % 18 == 17) { reply(99); return; }
    ArduinoFDC.motorOn();
    const byte track = address / 36;
    const byte side = (address / 18) % 2;
    const byte sector = address % 18 + 1;
    byte status = ArduinoFDC.readSector(track, side, sector, sector_buffer);
    if (!status) status = ArduinoFDC.readSector(track, side, sector + 1, next_sector_buffer);
    reply(status);
    if (!status) {
      send_sector(sector_buffer + 1);
      send_sector(next_sector_buffer + 1);
    }
    last_request = millis();
    return;
  }
  if (op != 'T' && (address >= 2880 || (op != 'R' && op != 'W' && op != 'w'))) {
    reply(99);
    return;
  }
  if (op == 'W' || op == 'w' || op == 'T') {
    if (Serial.readBytes(sector_buffer + 1, 512) != 512) return;
    byte checksum[2];
    if (Serial.readBytes(checksum, 2) != 2) return;
    if (wire_crc(sector_buffer + 1, 512) != (checksum[0] | ((uint16_t)checksum[1] << 8))) {
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
  const byte track = address / 36;
  const byte side = (address / 18) % 2;
  const byte sector = address % 18 + 1;
  const byte status = op == 'R' ? ArduinoFDC.readSector(track, side, sector, sector_buffer)
    : ArduinoFDC.writeSector(track, side, sector, sector_buffer, op == 'W');
  reply(status);
  if (op == 'R' && !status) send_sector(sector_buffer + 1);
  last_request = millis();
}
