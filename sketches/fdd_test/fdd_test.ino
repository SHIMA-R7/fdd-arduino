// 34ピン FDD テスト用スケッチ (Arduino Nano)
// モーター回転・ヘッドシーク・トラック0検出・回転数測定・各種ステータス確認
// シリアルモニタ 115200bps / 改行コード「LF」または「CRおよびLF」
//
// FDD信号はすべてアクティブLOW。FDDの出力はオープンコレクタなのでプルアップが必要。

// ピン割り当ては ArduinoFDC (github.com/dhansel/ArduinoFDC) と同一。
// 配線を変えずにこのテストと ArduinoFDC(読み書き) を切り替えられる。

// ---- Arduino -> FDD (出力) ----
const uint8_t PIN_STEP   = 2;   // FDD 20  Step
const uint8_t PIN_DIR    = 3;   // FDD 18  Direction (LOW=内周方向)
const uint8_t PIN_MOTEA  = 4;   // FDD 10  Motor Enable A
const uint8_t PIN_DRVSA  = 5;   // FDD 14  Drive Select A
const uint8_t PIN_SIDE   = 6;   // FDD 32  Side Select (LOW=裏面 side1)
const uint8_t PIN_WDATA  = 9;   // FDD 22  Write Data (OC1A)  このテストでは HIGH 固定
const uint8_t PIN_WGATE  = 10;  // FDD 24  Write Gate         このテストでは HIGH 固定(書き込まない)
const uint8_t PIN_DENSEL = 13;  // FDD 2   Density Select
const uint8_t PIN_MOTEB  = A0;  // FDD 16  Motor Enable B
const uint8_t PIN_DRVSB  = A1;  // FDD 12  Drive Select B

// ---- FDD -> Arduino (入力) ----
const uint8_t PIN_INDEX  = 7;   // FDD 8   Index
const uint8_t PIN_RDATA  = 8;   // FDD 30  Read Data (ICP1)   ※外付け1kΩプルアップ推奨
const uint8_t PIN_TRK00  = 11;  // FDD 26  Track 0
const uint8_t PIN_WPT    = 12;  // FDD 28  Write Protect
const uint8_t PIN_DSKCHG = A2;  // FDD 34  Disk Change

const uint16_t STEP_RATE_MS  = 4;   // 3.5インチは3ms程度。安全側に4ms
const uint16_t SETTLE_MS     = 20;  // ヘッド安定待ち
const uint8_t  MAX_TRACK     = 83;

bool motorOn = false;
bool side1 = false;
int  curTrack = -1;  // -1 = 不明(未キャリブレーション)

// INDEX の立ち下がりを待つ。タイムアウトで false
bool waitIndexFall(uint32_t timeoutUs) {
  uint32_t t0 = micros();
  while (digitalRead(PIN_INDEX) == LOW)
    if (micros() - t0 > timeoutUs) return false;
  while (digitalRead(PIN_INDEX) == HIGH)
    if (micros() - t0 > timeoutUs) return false;
  return true;
}

// 出力ピンを「非アクティブ(HIGH)」にしてから OUTPUT にする(起動時の誤動作防止)
void outHigh(uint8_t pin) {
  digitalWrite(pin, HIGH);
  pinMode(pin, OUTPUT);
}

void setMotor(bool on) {
  // ドライブがDS0/DS1どちらに設定されていても動くよう A/B 両方をアサート
  uint8_t v = on ? LOW : HIGH;
  digitalWrite(PIN_DRVSA, v);
  digitalWrite(PIN_DRVSB, v);
  digitalWrite(PIN_MOTEA, v);
  digitalWrite(PIN_MOTEB, v);
  motorOn = on;
  if (on) delay(500);  // スピンアップ待ち
  Serial.println(on ? F("Motor ON") : F("Motor OFF"));
}

void stepOnce(bool inward) {
  digitalWrite(PIN_DIR, inward ? LOW : HIGH);
  delayMicroseconds(10);          // DIR セットアップ時間
  digitalWrite(PIN_STEP, LOW);
  delayMicroseconds(5);           // STEP パルス幅 (>=1us)
  digitalWrite(PIN_STEP, HIGH);
  delay(STEP_RATE_MS);
}

bool atTrack0() { return digitalRead(PIN_TRK00) == LOW; }

bool recalibrate() {
  for (int i = 0; i < 90; i++) {
    if (atTrack0()) {
      delay(SETTLE_MS);
      curTrack = 0;
      return true;
    }
    stepOnce(false);
  }
  curTrack = -1;
  return false;
}

void seek(int target) {
  if (target < 0 || target > MAX_TRACK) {
    Serial.println(F("範囲外のトラックです"));
    return;
  }
  if (curTrack < 0 && !recalibrate()) {
    Serial.println(F("Track0 が見つかりません"));
    return;
  }
  while (curTrack < target) { stepOnce(true);  curTrack++; }
  while (curTrack > target) { stepOnce(false); curTrack--; }
  delay(SETTLE_MS);
}

void printStatus() {
  Serial.print(F("Motor="));   Serial.print(motorOn ? F("ON") : F("OFF"));
  Serial.print(F(" Track="));  Serial.print(curTrack);
  Serial.print(F(" Side="));   Serial.print(side1 ? 1 : 0);
  Serial.print(F(" TRK00="));  Serial.print(atTrack0() ? 1 : 0);
  Serial.print(F(" WPT="));    Serial.print(digitalRead(PIN_WPT) == LOW ? 1 : 0);
  Serial.print(F(" DSKCHG=")); Serial.print(digitalRead(PIN_DSKCHG) == LOW ? 1 : 0);
  Serial.print(F(" INDEX=")); Serial.println(digitalRead(PIN_INDEX) == LOW ? 1 : 0);
}

void measureRpm() {
  if (!motorOn) { Serial.println(F("先にモーターをONにしてください (m)")); return; }
  if (!waitIndexFall(1000000UL)) {
    Serial.println(F("INDEX パルスなし (ディスク未挿入 or 配線/プルアップ不良)"));
    return;
  }
  uint32_t t0 = micros();
  if (!waitIndexFall(1000000UL)) {
    Serial.println(F("INDEX パルスが1回しか来ません"));
    return;
  }
  uint32_t p = micros() - t0;
  Serial.print(F("周期 ")); Serial.print(p);
  Serial.print(F(" us  => ")); Serial.print(60.0e6 / p, 1);
  Serial.println(F(" rpm (3.5インチは約300rpm)"));
}

void help() {
  Serial.println(F("--- FDD テスト ---"));
  Serial.println(F(" m      モーター/ドライブ選択 ON/OFF"));
  Serial.println(F(" z      トラック0へ(リキャリブレート)"));
  Serial.println(F(" g NN   トラックNNへシーク"));
  Serial.println(F(" + / -  1ステップ内周 / 外周"));
  Serial.println(F(" s      サイド切替"));
  Serial.println(F(" d      密度信号(pin2)切替"));
  Serial.println(F(" r      回転数測定"));
  Serial.println(F(" i      ステータス表示"));
  Serial.println(F(" t      シークテスト(0<->79往復)"));
}

void setup() {
  outHigh(PIN_MOTEA); outHigh(PIN_DRVSB); outHigh(PIN_DRVSA); outHigh(PIN_MOTEB);
  outHigh(PIN_DIR);   outHigh(PIN_STEP);  outHigh(PIN_SIDE);  outHigh(PIN_DENSEL);
  outHigh(PIN_WDATA); outHigh(PIN_WGATE);

  pinMode(PIN_INDEX,  INPUT_PULLUP);
  pinMode(PIN_RDATA,  INPUT_PULLUP);
  pinMode(PIN_TRK00,  INPUT_PULLUP);
  pinMode(PIN_WPT,    INPUT_PULLUP);
  pinMode(PIN_DSKCHG, INPUT_PULLUP);

  Serial.begin(115200);
  help();
}

void loop() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  if (line.length() == 0) return;
  char c = line[0];

  if (c != 'm' && c != 'i' && c != 'd' && c != 'h' && c != '?' && !motorOn) {
    // ステップ等はドライブ選択中でないと受け付けられない
    Serial.println(F("先にモーターをONにしてください (m)"));
    return;
  }

  switch (c) {
    case 'm': setMotor(!motorOn); break;
    case 'z': Serial.println(recalibrate() ? F("Track0 OK") : F("Track0 が見つかりません")); break;
    case 'g': seek(line.substring(1).toInt()); break;
    case '+': stepOnce(true);  if (curTrack >= 0) curTrack++; break;
    case '-': stepOnce(false); if (curTrack > 0) curTrack--; break;
    case 's':
      side1 = !side1;
      digitalWrite(PIN_SIDE, side1 ? LOW : HIGH);
      break;
    case 'd': {
      bool low = digitalRead(PIN_DENSEL) == HIGH;
      digitalWrite(PIN_DENSEL, low ? LOW : HIGH);
      Serial.print(F("DENSEL=")); Serial.println(low ? F("LOW") : F("HIGH"));
      break;
    }
    case 'r': measureRpm(); break;
    case 't':
      for (int n = 0; n < 3; n++) { seek(79); seek(0); }
      Serial.println(F("シークテスト完了"));
      break;
    case 'i': break;
    default: help(); return;
  }
  printStatus();
}
