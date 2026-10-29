/*
 * jieji · 解机 —— 电机直驱桥（跳过遥控器，主机直接给左右轮 PWM）
 *
 * 适用：你愿意把机器人拆开，把 Arduino/ESP32 接到电机驱动板（L298N / TB6612 /
 * DRV8833）上。这是最干净的一条路 —— 不需要遥控器，也不关心原来的协议。
 *
 * 串口协议（115200，一行一个 JSON，和 app/drivers/serial.py 完全对应）：
 *   主机 → 板子
 *     {"t":"drive","l":0.4,"r":0.4,"ms":800}   左右轮 -1.0~1.0
 *     {"t":"stop"}
 *     {"t":"estop"}
 *     {"t":"ping"}
 *   板子 → 主机（每 500ms 一次）
 *     {"t":"status","front_cm":42,"battery":7.4}
 *
 * 依赖：ArduinoJson（库管理器搜 ArduinoJson）
 *
 * 安全（写在固件里，不靠上位机）：
 *   1. 1000ms 没收到新命令 → 电机立刻停；
 *   2. 超声波 < 15cm 且正在前进 → 立刻停，并回 status；
 *   3. 上电先停 1 秒，防止舵机/电机抽风。
 *
 * 接线（L298N → Arduino Uno/Nano，ESP32 把 PWM 换成任意 GPIO 即可）：
 *   ENA→9(PWM)  IN1→8  IN2→7   左轮
 *   ENB→10(PWM) IN3→6  IN4→5   右轮
 *   HC-SR04: TRIG→A0  ECHO→A1（用 NewPing 更准，这里用最朴素的 pulseIn）
 */

#include <ArduinoJson.h>

// ---------------------------------------------------------------- 引脚
const uint8_t ENA = 9, IN1 = 8, IN2 = 7;   // 左轮
const uint8_t ENB = 10, IN3 = 6, IN4 = 5;  // 右轮
const uint8_t TRIG = A0, ECHO = A1;

// ---------------------------------------------------------------- 参数
const uint32_t kCommandTimeoutMs = 1000;   // 死手保护
const uint16_t kStopDistanceCm = 15;       // 太近就停
const uint32_t kStatusIntervalMs = 500;

uint32_t lastCmdAt = 0;
uint32_t lastStatusAt = 0;
float speedL = 0, speedR = 0;
uint16_t lastFrontCm = 0;
String lineBuf;

// ---------------------------------------------------------------- 电机
void applyMotors(float l, float r) {
  l = constrain(l, -1.0, 1.0);
  r = constrain(r, -1.0, 1.0);
  speedL = l;
  speedR = r;

  int pwmL = (int)(abs(l) * 255);
  int pwmR = (int)(abs(r) * 255);

  digitalWrite(IN1, l >= 0 ? HIGH : LOW);
  digitalWrite(IN2, l >= 0 ? LOW : HIGH);
  digitalWrite(IN3, r >= 0 ? HIGH : LOW);
  digitalWrite(IN4, r >= 0 ? LOW : HIGH);
  analogWrite(ENA, pwmL);
  analogWrite(ENB, pwmR);
}

void stopMotors(const char *why) {
  applyMotors(0, 0);
  digitalWrite(ENA, LOW);
  digitalWrite(ENB, LOW);
  if (why && *why) {
    Serial.print(F("{\"t\":\"log\",\"msg\":\"stop:"));
    Serial.print(why);
    Serial.println(F("\"}"));
  }
}

// ---------------------------------------------------------------- 测距
uint16_t readFrontCm() {
  digitalWrite(TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG, LOW);
  unsigned long us = pulseIn(ECHO, HIGH, 30000UL);
  if (us == 0) return 0;              // 没回波
  return (uint16_t)(us / 58UL);
}

// ---------------------------------------------------------------- 命令
void handleCommand(const String &text) {
  StaticJsonDocument<192> doc;
  if (deserializeJson(doc, text)) return;
  lastCmdAt = millis();
  const char *t = doc["t"] | "";

  if (strcmp(t, "drive") == 0) {
    float l = doc["l"] | 0.0f;
    float r = doc["r"] | 0.0f;
    uint32_t ms = doc["ms"] | 600;
    // 前方太近且要往前 → 拒绝
    if (l > 0 && r > 0 && lastFrontCm > 0 && lastFrontCm < kStopDistanceCm) {
      stopMotors("障碍太近");
      return;
    }
    applyMotors(l, r);
    // ms 由主机自己计时；固件只兜底死手保护
    (void)ms;
    return;
  }
  if (strcmp(t, "stop") == 0 || strcmp(t, "estop") == 0) {
    stopMotors("命令");
    return;
  }
  if (strcmp(t, "ping") == 0) {
    Serial.println(F("{\"t\":\"pong\"}"));
    return;
  }
}

// ---------------------------------------------------------------- setup
void setup() {
  pinMode(ENA, OUTPUT); pinMode(IN1, OUTPUT); pinMode(IN2, OUTPUT);
  pinMode(ENB, OUTPUT); pinMode(IN3, OUTPUT); pinMode(IN4, OUTPUT);
  pinMode(TRIG, OUTPUT); pinMode(ECHO, INPUT);

  Serial.begin(115200);
  stopMotors("上电");
  delay(1000);              // 给电调/驱动板一点时间
  lastCmdAt = millis();
  Serial.println(F("{\"t\":\"hello\",\"fw\":\"jieji-motor-1.0\"}"));
}

// ---------------------------------------------------------------- loop
void loop() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineBuf.length()) {
        handleCommand(lineBuf);
        lineBuf = "";
      }
    } else if (lineBuf.length() < 200) {
      lineBuf += c;
    } else {
      lineBuf = "";   // 太长了，丢掉重来
    }
  }

  uint32_t now = millis();

  // 死手保护：主机不吭声就自己停
  if ((speedL != 0 || speedR != 0) && now - lastCmdAt > kCommandTimeoutMs) {
    stopMotors("上位机超时");
  }

  if (now - lastStatusAt > kStatusIntervalMs) {
    lastStatusAt = now;
    lastFrontCm = readFrontCm();
    Serial.print(F("{\"t\":\"status\",\"front_cm\":"));
    Serial.print(lastFrontCm);
    Serial.print(F(",\"l\":"));
    Serial.print(speedL, 2);
    Serial.print(F(",\"r\":"));
    Serial.print(speedR, 2);
    Serial.println(F("}"));
  }
}
