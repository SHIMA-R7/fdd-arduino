// Arduino Nano 自己診断（何も配線していない状態で実行）
// 1) 各ピンの出力 HIGH/LOW が実際にピンに出るか（PINレジスタで読み返し）
// 2) 内蔵プルアップで HIGH が読めるか（入力バッファ・プルアップ）
// 3) 1本だけ LOW にしたとき、他のピンが引きずられないか（ピン間ショート）
// 4) 電源電圧（内部1.1V基準から逆算）
// 結果はシリアル 115200bps
// delay() は Timer0 に依存するため、タイマーを使わない _delay_ms() で待つ

#include <util/delay.h>

const uint8_t pins[] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, A0, A1, A2, A3, A4, A5};
const char* names[]  = {"D2","D3","D4","D5","D6","D7","D8","D9","D10","D11","D12","D13","A0","A1","A2","A3","A4","A5"};
const uint8_t N = sizeof(pins);
uint16_t failures = 0;
uint8_t resetCause;

void fail(const char* pin, const __FlashStringHelper* msg) {
  Serial.print(F("  NG ")); Serial.print(pin); Serial.print(F(": ")); Serial.println(msg);
  failures++;
}

void allPullup() {
  for (uint8_t i = 0; i < N; i++) pinMode(pins[i], INPUT_PULLUP);
}

long readVcc() {
  ADMUX = _BV(REFS0) | _BV(MUX3) | _BV(MUX2) | _BV(MUX1);  // AVcc基準で1.1Vバンドギャップを測る
  _delay_ms(5);
  ADCSRA |= _BV(ADSC);
  while (bit_is_set(ADCSRA, ADSC));
  uint16_t v = ADC;
  return 1125300L / v;  // mV
}

void setup() {
  resetCause = MCUSR; MCUSR = 0;
  Serial.begin(115200);
  _delay_ms(300);
  Serial.print(F("\n=== Nano self test === MCUSR=0x")); Serial.println(resetCause, HEX);
  Serial.flush();

  // 1) 出力の読み返し
  Serial.println(F("[1] 出力 HIGH/LOW"));
  for (uint8_t i = 0; i < N; i++) {
    Serial.print(F("  ")); Serial.print(names[i]); Serial.flush();
    pinMode(pins[i], OUTPUT);
    digitalWrite(pins[i], HIGH); delayMicroseconds(50);
    if (digitalRead(pins[i]) != HIGH) fail(names[i], F("HIGH が出ない（GNDとショート?）"));
    digitalWrite(pins[i], LOW);  delayMicroseconds(50);
    if (digitalRead(pins[i]) != LOW)  fail(names[i], F("LOW が出ない（5Vとショート?）"));
    pinMode(pins[i], INPUT);
  }
  Serial.println(); Serial.flush();

  // 2) プルアップ
  Serial.println(F("[2] 内蔵プルアップ"));
  allPullup();
  _delay_ms(5);
  for (uint8_t i = 0; i < N; i++) {
    if (pins[i] == 13) continue;  // D13 は基板のLEDに引かれてプルアップでは HIGH にならないのが正常
    if (digitalRead(pins[i]) != HIGH) fail(names[i], F("プルアップで HIGH にならない"));
  }

  // 3) ピン間ショート
  Serial.println(F("[3] ピン間ショート"));
  for (uint8_t i = 0; i < N; i++) {
    allPullup();
    pinMode(pins[i], OUTPUT);
    digitalWrite(pins[i], LOW);
    delayMicroseconds(200);
    for (uint8_t j = 0; j < N; j++) {
      if (j == i || pins[j] == 13) continue;
      if (digitalRead(pins[j]) != HIGH) {
        Serial.print(F("  NG ")); Serial.print(names[i]); Serial.print(F(" と "));
        Serial.print(names[j]); Serial.println(F(" がショート"));
        failures++;
      }
    }
  }
  allPullup();

  // 4) 電源電圧
  long vcc = readVcc();
  Serial.print(F("[4] Vcc = ")); Serial.print(vcc); Serial.println(F(" mV（USB給電なら 4500〜5250 程度）"));
  if (vcc < 4300 || vcc > 5400) { Serial.println(F("  NG 電源電圧が範囲外")); failures++; }

  Serial.println(failures == 0 ? F("RESULT: OK") : F("RESULT: NG"));
  pinMode(13, OUTPUT);
}

void loop() {
  // OKなら1秒周期、NGなら速く点滅
  digitalWrite(13, !digitalRead(13));
  if (failures) _delay_ms(100); else _delay_ms(500);
}
