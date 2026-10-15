/*
 * 实验室安全控制盒 下位机固件（ESP32-S3-DevKitC-1，Arduino 框架）
 * firmware/lab_guard_esp32/lab_guard_esp32.ino
 *
 * 职责：
 *   1. 采集 SHT30（环境温湿度）、MLX90614（热源表面温度）、MQ-2（烟雾）
 *   2. 通过 USB 串口向香橙派上报遥测，执行上位机的断电 / 语音指令
 *   3. 本地硬规则（不依赖上位机）：热源超温、烟雾超标 → 立即断电
 *   4. 看门狗：上位机心跳丢失 30 秒且仍在加热 → 断电兜底
 *   5. 断电后锁定：只有现场长按复位键才能恢复供电（上位机只能断电，不能上电）
 *   6. 外部温湿度控制：排风扇（PWM 调速）+ 加湿雾化片，控制律在本地执行，断网也能工作；
 *      上位机只下发设定值（0x04）或 10 分钟有效的手动指令（0x05），状态用 0x13 上报。
 *      控制律与 src/core/env_control.py 的 control_law() 完全一致。
 *
 * 依赖库（Arduino 库管理器搜索安装）：
 *   - Adafruit SHT31 Library（兼容 SHT30）
 *   - Adafruit MLX90614 Library
 *   - DFRobotDFPlayerMini
 * 开发板：esp32 by Espressif（2.x 或 3.x），板型选 "ESP32S3 Dev Module"
 *   - 用板子上标 "UART/COM" 的口连接香橙派：USB CDC On Boot = Disabled
 *   - 用标 "USB" 的原生口连接：USB CDC On Boot = Enabled
 *   两种接法下 Serial 都对应连接香橙派的那个口。
 *
 * 帧格式与 LabSafetyMonitor/serial_comm.py 完全一致：
 *   AA 55 | LEN | SEQ | CMD | PAYLOAD | CRC16(LE)   CRC16-CCITT 覆盖 LEN..PAYLOAD
 */
#include <Wire.h>
#include <Adafruit_SHT31.h>
#include <Adafruit_MLX90614.h>
#include <DFRobotDFPlayerMini.h>

// ------------------------------- 引脚 -------------------------------
const int PIN_SDA    = 8;
const int PIN_SCL    = 9;
const int PIN_MQ2    = 1;    // ADC1_CH0，MQ-2 AO 需分压到 3.3V 以下
const int PIN_RELAY  = 10;
const int PIN_LED_R  = 11;
const int PIN_LED_Y  = 12;
const int PIN_LED_G  = 13;
const int PIN_BUZZER = 14;   // 有源蜂鸣器（经三极管驱动）
const int PIN_BTN    = 4;    // 复位 / 上电键，按下接地
const int PIN_DF_RX  = 18;   // ESP32 RX ← DFPlayer TX
const int PIN_DF_TX  = 17;   // ESP32 TX → 1kΩ → DFPlayer RX
const int PIN_FAN    = 15;   // 排风扇：PWM → MOSFET 模块 → 5V 风扇
const int PIN_HUMID  = 16;   // 加湿雾化片：MOSFET 模块开关

const bool RELAY_ACTIVE_HIGH = true;   // 低电平触发的继电器模块改为 false

// ------------------------------- 阈值 -------------------------------
const float    HARD_LIMIT_C     = 150.0;  // 本地硬上限，高于上位机的干烧阈值（120℃），作为最后防线
const uint16_t SMOKE_LIMIT_RAW  = 2500;   // 本地烟雾硬上限（上位机预警阈值更低）
const uint32_t MQ2_WARMUP_MS    = 60000;  // MQ-2 上电预热时间
const uint32_t HB_LOST_MS       = 5000;   // 超过该时间无心跳 → 黄灯闪烁提示失联
const uint32_t WATCHDOG_CUT_MS  = 30000;  // 失联超过该时间且仍在加热 → 断电
const float    WATCHDOG_HOT_C   = 60.0;
const uint32_t SENSOR_PERIOD_MS = 500;
const uint32_t BTN_LONG_MS      = 1000;
const uint16_t SMOKE_FAN_RAW    = 1200;   // 烟雾达到该值：排风满速、禁止加湿
const uint32_t MANUAL_MS        = 600000; // 手动指令有效期 10 分钟

// ------------------------------- 协议 -------------------------------
const uint8_t CMD_HEARTBEAT = 0x01, CMD_POWER_OFF = 0x02, CMD_PLAY_VOICE = 0x03;
const uint8_t CMD_SET_ENV = 0x04, CMD_SET_ACT = 0x05;
const uint8_t MSG_TELEMETRY = 0x10, MSG_ACK = 0x11, MSG_EVENT = 0x12, MSG_ENV_STATUS = 0x13;
const int16_t  INVALID_I16 = 0x7FFF;
const uint16_t INVALID_U16 = 0xFFFF;

// 断电原因 / 事件码（与 serial_comm.py 中 CUT_REASONS / EVENT_CODES 对应）
enum { CUT_NONE = 0, CUT_OVERTEMP = 1, CUT_SMOKE = 2, CUT_WATCHDOG = 3, CUT_PC = 4 };
enum { EV_LOCAL_OVERTEMP = 1, EV_LOCAL_SMOKE = 2, EV_WATCHDOG = 3, EV_PC_CUT = 4,
       EV_RESET_OK = 5, EV_RESET_REFUSED = 6, EV_BOOT = 7 };

Adafruit_SHT31 sht;
Adafruit_MLX90614 mlx;
DFRobotDFPlayerMini df;
bool shtOk = false, mlxOk = false, dfOk = false;

float tempC = NAN, humi = NAN, objC = NAN;
uint16_t smokeRaw = 0;
bool relayOn = false, latched = false;
uint8_t cutReason = CUT_NONE;
uint8_t alertLevel = 0, prevAlertLevel = 0;
uint32_t lastHb = 0, lastSensor = 0, btnDownAt = 0;
bool btnHandled = false;
uint8_t txSeq = 0;

// 外部温湿度控制
float envTSet = 26.0, envHLow = 40, envHHigh = 70;
uint8_t fanPwm = 0;
bool humidOn = false;
uint32_t manualUntil = 0;

uint8_t rxBuf[96];
int rxLen = 0;

// ------------------------------- 工具 -------------------------------
uint16_t crc16(const uint8_t *d, int n) {
  uint16_t crc = 0xFFFF;
  for (int i = 0; i < n; i++) {
    crc ^= (uint16_t)d[i] << 8;
    for (int b = 0; b < 8; b++) crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
  }
  return crc;
}

void sendFrame(uint8_t cmd, const uint8_t *payload, uint8_t len) {
  uint8_t f[80];
  f[0] = 0xAA; f[1] = 0x55; f[2] = len; f[3] = ++txSeq; f[4] = cmd;
  memcpy(f + 5, payload, len);
  uint16_t c = crc16(f + 2, 3 + len);
  f[5 + len] = c & 0xFF; f[6 + len] = c >> 8;
  Serial.write(f, 7 + len);
}

void sendEvent(uint8_t code, int16_t value) {
  uint8_t p[3] = { code, (uint8_t)(value & 0xFF), (uint8_t)((uint16_t)value >> 8) };
  sendFrame(MSG_EVENT, p, 3);
}

void beep(int times, int ms) {
  for (int i = 0; i < times; i++) {
    digitalWrite(PIN_BUZZER, HIGH); delay(ms);
    digitalWrite(PIN_BUZZER, LOW);  if (i + 1 < times) delay(ms);
  }
}

void setRelay(bool on) {
  relayOn = on;
  digitalWrite(PIN_RELAY, (on == RELAY_ACTIVE_HIGH) ? HIGH : LOW);
}

bool smokeReady() { return millis() > MQ2_WARMUP_MS; }

bool localHazard() {
  bool hot = mlxOk && !isnan(objC) && objC >= HARD_LIMIT_C;
  bool smoke = smokeReady() && smokeRaw >= SMOKE_LIMIT_RAW;
  return hot || smoke;
}

void cutPower(uint8_t reason, uint8_t eventCode) {
  setRelay(false);
  bool first = !latched;
  latched = true;
  cutReason = reason;
  if (first) {
    sendEvent(eventCode, isnan(objC) ? 0 : (int16_t)(objC * 10));
    beep(3, 120);
  }
}

// ------------------------------- 指令处理 -------------------------------
void handleFrame(uint8_t seq, uint8_t cmd, const uint8_t *p, uint8_t len) {
  if (cmd == CMD_HEARTBEAT && len >= 1) {
    lastHb = millis();
    alertLevel = p[0] > 2 ? 2 : p[0];
  } else if (cmd == CMD_POWER_OFF) {
    cutPower(CUT_PC, EV_PC_CUT);
    uint8_t ack[3] = { seq, cmd, 0 };
    sendFrame(MSG_ACK, ack, 3);
  } else if (cmd == CMD_PLAY_VOICE && len >= 1) {
    if (dfOk) df.playMp3Folder(p[0]);   // 播放 TF 卡 /mp3/000N.mp3（按文件名，不受拷贝顺序影响）
  } else if (cmd == CMD_SET_ENV && len >= 5) {
    int16_t t10; memcpy(&t10, p + 1, 2);
    envTSet = t10 / 10.0; envHLow = p[3]; envHHigh = p[4];
    if (p[0] == 0) manualUntil = 0;     // mode 0 = 回到自动
  } else if (cmd == CMD_SET_ACT && len >= 2) {
    fanPwm = p[0] > 100 ? 100 : p[0];
    humidOn = p[1] != 0;
    manualUntil = millis() + MANUAL_MS;
  }
}

void readSerial() {
  while (Serial.available()) {
    if (rxLen >= (int)sizeof(rxBuf)) rxLen = 0;
    rxBuf[rxLen++] = Serial.read();
  }
  // 与上位机相同的解帧方式：找包头 → 校验长度 → 校验 CRC，失败则跳过包头继续同步
  while (true) {
    int idx = -1;
    for (int i = 0; i + 1 < rxLen; i++) if (rxBuf[i] == 0xAA && rxBuf[i + 1] == 0x55) { idx = i; break; }
    if (idx < 0) {
      if (rxLen > 0 && rxBuf[rxLen - 1] == 0xAA) { rxBuf[0] = 0xAA; rxLen = 1; } else rxLen = 0;
      return;
    }
    if (idx > 0) { memmove(rxBuf, rxBuf + idx, rxLen - idx); rxLen -= idx; }
    if (rxLen < 5) return;
    uint8_t len = rxBuf[2];
    if (len > 64) { memmove(rxBuf, rxBuf + 2, rxLen - 2); rxLen -= 2; continue; }
    int total = 7 + len;
    if (rxLen < total) return;
    uint16_t c = rxBuf[5 + len] | (rxBuf[6 + len] << 8);
    if (crc16(rxBuf + 2, 3 + len) == c) handleFrame(rxBuf[3], rxBuf[4], rxBuf + 5, len);
    memmove(rxBuf, rxBuf + total, rxLen - total);
    rxLen -= total;
  }
}

// ------------------------------- 传感器与规则 -------------------------------
void readSensors() {
  if (shtOk) {
    tempC = sht.readTemperature();
    humi = sht.readHumidity();
    if (isnan(tempC) || isnan(humi)) shtOk = sht.begin(0x44);
  } else {
    shtOk = sht.begin(0x44);
  }
  if (mlxOk) {
    objC = mlx.readObjectTempC();
    if (isnan(objC) || objC < -70 || objC > 380) { objC = NAN; mlxOk = mlx.begin(); }
  } else {
    mlxOk = mlx.begin();
  }
  smokeRaw = analogRead(PIN_MQ2);
}

void evaluateLocalRules() {
  if (mlxOk && !isnan(objC) && objC >= HARD_LIMIT_C) cutPower(CUT_OVERTEMP, EV_LOCAL_OVERTEMP);
  if (smokeReady() && smokeRaw >= SMOKE_LIMIT_RAW)   cutPower(CUT_SMOKE, EV_LOCAL_SMOKE);
  bool hot = !mlxOk || isnan(objC) || objC > WATCHDOG_HOT_C;   // 测不到温度时按"可能很热"保守处理
  if (relayOn && millis() - lastHb > WATCHDOG_CUT_MS && hot) cutPower(CUT_WATCHDOG, EV_WATCHDOG);
}

void sendTelemetry() {
  int16_t t  = (shtOk && !isnan(tempC)) ? (int16_t)(tempC * 10) : INVALID_I16;
  uint16_t h = (shtOk && !isnan(humi)) ? (uint16_t)(humi * 10) : INVALID_U16;
  int16_t o  = (mlxOk && !isnan(objC)) ? (int16_t)(objC * 10) : INVALID_I16;
  uint8_t flags = (relayOn << 0) | (latched << 1) | ((millis() - lastHb < HB_LOST_MS) << 2)
                | (shtOk << 3) | (mlxOk << 4) | (smokeReady() << 5) | (localHazard() << 6);
  uint8_t p[10];
  memcpy(p + 0, &t, 2); memcpy(p + 2, &h, 2); memcpy(p + 4, &o, 2); memcpy(p + 6, &smokeRaw, 2);
  p[8] = flags; p[9] = cutReason;
  sendFrame(MSG_TELEMETRY, p, 10);
}

// ------------------------------- 外部温湿度控制 -------------------------------
bool manualActive() { return manualUntil != 0 && (int32_t)(manualUntil - millis()) > 0; }

void envControl() {
  bool smoke = smokeReady() && smokeRaw >= SMOKE_FAN_RAW;
  if (smoke) {                           // 烟雾优先：满速排风、禁止加湿（手动指令也不例外）
    fanPwm = 100; humidOn = false;
  } else if (!manualActive()) {
    manualUntil = 0;
    int fan = 0;
    if (shtOk && !isnan(tempC) && tempC > envTSet + 1) fan = max(fan, (int)min(100.0f, 40 + 20 * (tempC - envTSet - 1)));
    if (shtOk && !isnan(humi) && humi > envHHigh)      fan = max(fan, (int)min(100.0f, 40 + 6 * (humi - envHHigh)));
    if (fan == 0 && fanPwm > 0 && ((shtOk && tempC > envTSet) || (shtOk && humi > envHHigh - 3))) fan = 30;  // 回差
    fanPwm = fan;
    if (shtOk && !isnan(humi)) {
      if (humi < envHLow - 2) humidOn = true;
      else if (humi > envHLow + 2) humidOn = false;
      if (humi > envHHigh) humidOn = false;
    }
  }
  analogWrite(PIN_FAN, (int)fanPwm * 255 / 100);
  digitalWrite(PIN_HUMID, humidOn ? HIGH : LOW);
  uint8_t st[3] = { fanPwm, (uint8_t)humidOn, (uint8_t)(manualActive() ? 1 : 0) };
  sendFrame(MSG_ENV_STATUS, st, 3);
}

// ------------------------------- 按键与指示 -------------------------------
void handleButton() {
  bool down = digitalRead(PIN_BTN) == LOW;
  if (down) {
    if (btnDownAt == 0) { btnDownAt = millis(); btnHandled = false; }
    if (!btnHandled && millis() - btnDownAt >= BTN_LONG_MS) {
      btnHandled = true;
      if (localHazard()) {
        sendEvent(EV_RESET_REFUSED, 0);
        beep(5, 80);
      } else {
        latched = false;
        cutReason = CUT_NONE;
        setRelay(true);
        lastHb = millis();       // 给上位机留出完整的看门狗窗口
        sendEvent(EV_RESET_OK, 0);
        beep(1, 300);
      }
    }
  } else {
    btnDownAt = 0;
  }
}

void updateIndicators() {
  uint32_t now = millis();
  bool r = false, y = false, g = false;
  if (latched) {
    r = true;
    digitalWrite(PIN_BUZZER, (now % 2000) < 150 ? HIGH : LOW);   // 锁定期间每 2 秒短鸣一次
  } else {
    digitalWrite(PIN_BUZZER, LOW);
    if (now - lastHb > HB_LOST_MS) y = (now / 500) % 2;           // 上位机失联：黄灯闪烁
    else if (alertLevel == 2) r = true;
    else if (alertLevel == 1) y = true;
    else g = true;
    if (alertLevel > prevAlertLevel) beep(alertLevel, 80);
  }
  prevAlertLevel = alertLevel;
  digitalWrite(PIN_LED_R, r);
  digitalWrite(PIN_LED_Y, y);
  digitalWrite(PIN_LED_G, g);
}

// ------------------------------- 主程序 -------------------------------
void setup() {
  pinMode(PIN_RELAY, OUTPUT);
  setRelay(false);                       // 上电默认断开：失效安全
  pinMode(PIN_LED_R, OUTPUT); pinMode(PIN_LED_Y, OUTPUT); pinMode(PIN_LED_G, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  pinMode(PIN_BTN, INPUT_PULLUP);
  pinMode(PIN_FAN, OUTPUT);   analogWrite(PIN_FAN, 0);
  pinMode(PIN_HUMID, OUTPUT); digitalWrite(PIN_HUMID, LOW);
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  Serial.begin(115200);
  Wire.begin(PIN_SDA, PIN_SCL);
  shtOk = sht.begin(0x44);
  mlxOk = mlx.begin();
  Serial1.begin(9600, SERIAL_8N1, PIN_DF_RX, PIN_DF_TX);
  dfOk = df.begin(Serial1);
  if (dfOk) df.volume(25);

  beep(1, 100);
  sendEvent(EV_BOOT, 0);
}

void loop() {
  readSerial();
  uint32_t now = millis();
  if (now - lastSensor >= SENSOR_PERIOD_MS) {
    lastSensor = now;
    readSensors();
    evaluateLocalRules();
    sendTelemetry();
    envControl();
  }
  handleButton();
  updateIndicators();
  delay(5);
}
