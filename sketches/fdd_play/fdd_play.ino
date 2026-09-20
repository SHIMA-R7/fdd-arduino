// フロッピードライブ楽器（ストリーミング再生版）
// PCから "F<周波数>\n" を送ると、その周波数でヘッドを動かし続ける。F0 で無音。
// 音符の長さはPC側が管理するので、曲の再生に向く。
// 配線は fdd_test.ino / ArduinoFDC と同じ。書き込み系の信号は使わない。

const uint8_t PIN_STEP   = 2;   // FDD 20
const uint8_t PIN_DIR    = 3;   // FDD 18
const uint8_t PIN_MOTEA  = 4;   // FDD 10
const uint8_t PIN_DRVSA  = 5;   // FDD 14
const uint8_t PIN_SIDE   = 6;   // FDD 32
const uint8_t PIN_INDEX  = 7;   // FDD 8
const uint8_t PIN_WDATA  = 9;   // FDD 22 （HIGH固定）
const uint8_t PIN_WGATE  = 10;  // FDD 24 （HIGH固定＝書き込み禁止）
const uint8_t PIN_TRK00  = 11;  // FDD 26
const uint8_t PIN_DENS   = 13;  // FDD 2
const uint8_t PIN_MOTEB  = A0;  // FDD 16
const uint8_t PIN_DRVSB  = A1;  // FDD 12

const int16_t MAX_TRACK = 78;

int8_t  dir = +1;
int16_t track = 0;
uint32_t stepPeriod = 0;   // 0 = 無音
uint32_t nextStep = 0;

char buf[12];
uint8_t blen = 0;

void outHigh(uint8_t p) { digitalWrite(p, HIGH); pinMode(p, OUTPUT); }

inline void stepOnce() {
  if (track >= MAX_TRACK) { dir = -1; digitalWrite(PIN_DIR, HIGH); }
  else if (track <= 0)    { dir = +1; digitalWrite(PIN_DIR, LOW); }
  digitalWrite(PIN_STEP, LOW);
  delayMicroseconds(3);
  digitalWrite(PIN_STEP, HIGH);
  track += dir;
}

void recalibrate() {
  digitalWrite(PIN_DIR, HIGH);
  for (uint8_t i = 0; i < 85; i++) {
    if (digitalRead(PIN_TRK00) == LOW) break;
    digitalWrite(PIN_STEP, LOW); delayMicroseconds(3); digitalWrite(PIN_STEP, HIGH);
    delay(4);
  }
  track = 0; dir = +1; digitalWrite(PIN_DIR, LOW);
}

void setFreq(uint16_t f) {
  if (f < 20 || f > 2000) { stepPeriod = 0; return; }
  stepPeriod = 1000000UL / f;
  nextStep = micros() + stepPeriod;
}

void setup() {
  outHigh(PIN_STEP); outHigh(PIN_DIR); outHigh(PIN_MOTEA); outHigh(PIN_DRVSA);
  outHigh(PIN_SIDE); outHigh(PIN_WDATA); outHigh(PIN_WGATE); outHigh(PIN_DENS);
  outHigh(PIN_MOTEB); outHigh(PIN_DRVSB);
  pinMode(PIN_INDEX, INPUT_PULLUP); pinMode(PIN_TRK00, INPUT_PULLUP);

  Serial.begin(115200);
  digitalWrite(PIN_DRVSA, LOW); digitalWrite(PIN_DRVSB, LOW);   // 選択したまま
  delay(100);
  recalibrate();
  Serial.println(F("READY"));
}

void loop() {
  if (stepPeriod && (int32_t)(micros() - nextStep) >= 0) {
    stepOnce();
    nextStep += stepPeriod;
  }

  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (blen) {
        buf[blen] = 0;
        if (buf[0] == 'F') setFreq(atoi(buf + 1));
        else if (buf[0] == 'Z') { recalibrate(); Serial.println(F("OK")); }
        blen = 0;
      }
    } else if (blen < sizeof(buf) - 1) {
      buf[blen++] = c;
    }
  }
}
