// フロッピードライブ楽器
// ヘッドを動かすSTEPパルスを音の周波数で送り、ステッピングモーターを鳴らす。
// 配線は fdd_test.ino / ArduinoFDC と同じ。書き込み系の信号は一切使わない。
// シリアル 115200bps

const uint8_t PIN_STEP   = 2;   // FDD 20
const uint8_t PIN_DIR    = 3;   // FDD 18
const uint8_t PIN_MOTEA  = 4;   // FDD 10
const uint8_t PIN_DRVSA  = 5;   // FDD 14
const uint8_t PIN_SIDE   = 6;   // FDD 32
const uint8_t PIN_INDEX  = 7;   // FDD 8
const uint8_t PIN_WDATA  = 9;   // FDD 22  （HIGH固定）
const uint8_t PIN_WGATE  = 10;  // FDD 24  （HIGH固定＝書き込み禁止）
const uint8_t PIN_TRK00  = 11;  // FDD 26
const uint8_t PIN_DENS   = 13;  // FDD 2
const uint8_t PIN_MOTEB  = A0;  // FDD 16
const uint8_t PIN_DRVSB  = A1;  // FDD 12

const uint8_t MAX_TRACK = 78;   // 端に当てないよう少し内側で折り返す

int8_t dir = +1;                // +1=内周へ, -1=外周へ
int16_t track = 0;

void outHigh(uint8_t p) { digitalWrite(p, HIGH); pinMode(p, OUTPUT); }

// 1ステップ。折り返しは自動
void stepOnce() {
  if (track >= MAX_TRACK) dir = -1;
  else if (track <= 0)    dir = +1;
  digitalWrite(PIN_DIR, dir > 0 ? LOW : HIGH);
  digitalWrite(PIN_STEP, LOW);
  delayMicroseconds(3);
  digitalWrite(PIN_STEP, HIGH);
  track += dir;
}

// 周波数 freq Hz の音を ms ミリ秒鳴らす。freq=0 は休符
void playTone(uint16_t freq, uint16_t ms) {
  if (freq == 0) { delay(ms); return; }
  uint32_t period = 1000000UL / freq;       // 1ステップあたりの間隔(us)
  uint32_t end = millis() + ms;
  while (millis() < end) {
    stepOnce();
    delayMicroseconds(period > 20 ? period - 20 : 1);
  }
}

// 音名 → 周波数（C4=ド）
const uint16_t NOTES[] PROGMEM = {
  262, 277, 294, 311, 330, 349, 370, 392, 415, 440, 466, 494,    // C4..B4
  523, 554, 587, 622, 659, 698, 740, 784, 831, 880, 932, 988,    // C5..B5
  1047,1109,1175,1245,1319,1397,1480,1568                        // C6..G6
};
uint16_t note(uint8_t n) { return pgm_read_word(&NOTES[n]); }

// きらきら星（伝承曲）: 音番号と長さ
const uint8_t MEL[] PROGMEM = {
  0,0,7,7,9,9,7,  5,5,4,4,2,2,0,
  7,7,5,5,4,4,2,  7,7,5,5,4,4,2,
  0,0,7,7,9,9,7,  5,5,4,4,2,2,0
};
const uint8_t MEL_LEN[] PROGMEM = {
  4,4,4,4,4,4,8,  4,4,4,4,4,4,8,
  4,4,4,4,4,4,8,  4,4,4,4,4,4,8,
  4,4,4,4,4,4,8,  4,4,4,4,4,4,8
};
const uint8_t MEL_N = sizeof(MEL);

void recalibrate() {
  digitalWrite(PIN_DIR, HIGH);           // 外周へ
  for (uint8_t i = 0; i < 85; i++) {
    if (digitalRead(PIN_TRK00) == LOW) break;
    digitalWrite(PIN_STEP, LOW); delayMicroseconds(3); digitalWrite(PIN_STEP, HIGH);
    delay(4);
  }
  track = 0; dir = +1;
}

void selectDrive(bool on) {
  uint8_t v = on ? LOW : HIGH;
  digitalWrite(PIN_DRVSA, v); digitalWrite(PIN_DRVSB, v);
}

void help() {
  Serial.println(F("--- フロッピー楽器 ---"));
  Serial.println(F(" d       デモ演奏（きらきら星）"));
  Serial.println(F(" s       ドレミファソラシド"));
  Serial.println(F(" n NN    音番号NN(0-31)を0.4秒"));
  Serial.println(F(" p F,MS  F Hz を MS ミリ秒"));
  Serial.println(F(" g       グリッサンド（低→高）"));
  Serial.println(F(" z       ヘッドをトラック0へ"));
}

void setup() {
  outHigh(PIN_STEP); outHigh(PIN_DIR); outHigh(PIN_MOTEA); outHigh(PIN_DRVSA);
  outHigh(PIN_SIDE); outHigh(PIN_WDATA); outHigh(PIN_WGATE); outHigh(PIN_DENS);
  outHigh(PIN_MOTEB); outHigh(PIN_DRVSB);
  pinMode(PIN_INDEX, INPUT_PULLUP); pinMode(PIN_TRK00, INPUT_PULLUP);

  Serial.begin(115200);
  selectDrive(true);        // 選択したままにする（モーターは回さない）
  delay(100);
  recalibrate();
  help();
}

void loop() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (!line.length()) return;

  switch (line[0]) {
    case 'd':
      for (uint8_t i = 0; i < MEL_N; i++) {
        uint8_t n = pgm_read_byte(&MEL[i]);
        uint16_t len = pgm_read_byte(&MEL_LEN[i]) * 70;
        playTone(note(n), len - 20);
        delay(20);
      }
      Serial.println(F("演奏終了"));
      break;
    case 's':
      for (uint8_t i = 0; i < 8; i++) {
        const uint8_t scale[] = {0,2,4,5,7,9,11,12};
        playTone(note(scale[i]), 300); delay(40);
      }
      Serial.println(F("音階終了"));
      break;
    case 'n': {
      int n = line.substring(1).toInt();
      if (n < 0 || n > 31) { Serial.println(F("0-31で指定")); break; }
      Serial.print(F("音番号 ")); Serial.print(n);
      Serial.print(F(" = ")); Serial.print(note(n)); Serial.println(F(" Hz"));
      playTone(note(n), 400);
      break;
    }
    case 'p': {
      int comma = line.indexOf(',');
      uint16_t f = line.substring(1, comma < 0 ? line.length() : comma).toInt();
      uint16_t ms = comma < 0 ? 500 : line.substring(comma + 1).toInt();
      Serial.print(f); Serial.print(F(" Hz / ")); Serial.print(ms); Serial.println(F(" ms"));
      playTone(f, ms);
      break;
    }
    case 'g':
      for (uint16_t f = 120; f < 1200; f += 8) playTone(f, 12);
      Serial.println(F("グリッサンド終了"));
      break;
    case 'z':
      recalibrate();
      Serial.println(F("トラック0"));
      break;
    default:
      help();
      return;
  }
  Serial.print(F("現在のトラック=")); Serial.println(track);
}
