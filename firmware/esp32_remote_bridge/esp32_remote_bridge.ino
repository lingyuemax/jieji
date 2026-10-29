/*
 * jieji · 解机 —— ESP32 遥控器桥（红外学习回放 + 舵机压键）
 *
 * 这块板子干一件事：把「主机发来的 JSON」变成「遥控器上的一个动作」。
 *
 *   红外：先学一次（把真遥控器对着接收头按一下），之后原样发出去；
 *   舵机：把真遥控器夹在架子上，用舵机压真按键（私有 2.4G/蓝牙遥控器也能用）。
 *
 * 主机 → 板子
 *   {"cmd":"ir_learn"}                                  进入学习，抓到码后回 ir_learned
 *   {"cmd":"ir_send","code":"0x20DF10EF","protocol":"NEC","bits":32,"repeat":6,"interval_ms":120}
 *   {"cmd":"press","servo":0,"hold_ms":150}             舵机按下再松开
 *   {"cmd":"servo_test","servo":0}                      只动一下，用来确认是哪个键
 *   {"cmd":"estop"}                                     立刻松开所有舵机
 *   {"cmd":"ping"}
 *
 * 板子 → 主机
 *   {"event":"hello","fw":"jieji-remote-1.0","caps":["ir","press"]}
 *   {"event":"ir_learned","code":"0x20DF10EF","protocol":"NEC","bits":32}
 *   {"event":"status","front_cm":0,"battery":0}
 *   {"event":"ack","cmd":"ir_send","ok":true}
 *
 * 依赖（Arduino 库管理器里装）：
 *   - IRremoteESP8266        （红外收/发）
 *   - ArduinoJson            （7.x 或 6.x 都行，下面用 6.x 的 API）
 *   - arduinoWebSockets       （Links2004/arduinoWebSockets，起 WS 服务）
 *   - ESP32Servo              （舵机，用 ESP32 的 LEDC）
 *
 * 安全：不管主机说什么，只要 2 秒没收到命令，所有舵机立刻松开。
 *      机器人跑飞的时候，这条比任何提示词都管用。
 */

#include <WiFi.h>
#include <WebSocketsServer.h>
#include <ArduinoJson.h>
#include <IRremoteESP8266.h>
#include <IRrecv.h>
#include <IRsend.h>
#include <IRutils.h>
#include <ESP32Servo.h>

// ---------------------------------------------------------------- 改这里
#include "config.h"   // 把 config.example.h 复制成 config.h 再填 Wi-Fi

// ---------------------------------------------------------------- 引脚
const uint16_t kIrSendPin = 26;   // 红外发射 LED（串 100Ω + 三极管更稳）
const uint16_t kIrRecvPin = 27;   // 红外接收头（VS1838B：OUT→27，VCC→3V3，GND→GND）
const uint8_t  kServoPins[] = {13, 12, 14, 15};   // 四个舵机，对应按键 0..3
const uint8_t  kServoCount = sizeof(kServoPins) / sizeof(kServoPins[0]);

const uint8_t  kServoRest = 20;    // 松开角度
const uint8_t  kServoPress = 70;   // 按下角度（按你的架子调）
const uint32_t kCommandTimeoutMs = 2000;   // 2 秒没命令 → 全部松开

// ---------------------------------------------------------------- 全局
WebSocketsServer ws(81);
IRrecv irrecv(kIrRecvPin, 1024, 15, true);
IRsend irsend(kIrSendPin);
decode_results irResults;
Servo servos[kServoCount];

bool learning = false;
uint32_t lastCmdAt = 0;
uint32_t lastStatusAt = 0;

struct PressJob {
  bool active = false;
  uint8_t index = 0;
  uint32_t releaseAt = 0;
};
PressJob pressJobs[kServoCount];

// ---------------------------------------------------------------- 工具
void broadcast(const String &payload) {
  ws.broadcastTXT(payload);
}

void sendEvent(const char *event) {
  StaticJsonDocument<128> doc;
  doc["event"] = event;
  String out;
  serializeJson(doc, out);
  broadcast(out);
}

const char *protocolName(decode_type_t type) {
  switch (type) {
    case NEC: return "NEC";
    case SONY: return "SONY";
    case SAMSUNG: return "SAMSUNG";
    case RC5: return "RC5";
    case RC6: return "RC6";
    case LG: return "LG";
    case PANASONIC: return "PANASONIC";
    case JVC: return "JVC";
    case DISH: return "DISH";
    default: return "UNKNOWN";
  }
}

bool sendByProtocol(const String &proto, uint64_t code, uint16_t bits) {
  if (proto == "NEC") { irsend.sendNEC(code, bits ? bits : 32); return true; }
  if (proto == "SONY") { irsend.sendSony(code, bits ? bits : 12); return true; }
  if (proto == "SAMSUNG") { irsend.sendSAMSUNG(code, bits ? bits : 32); return true; }
  if (proto == "RC5") { irsend.sendRC5(code, bits ? bits : 12); return true; }
  if (proto == "RC6") { irsend.sendRC6(code, bits ? bits : 20); return true; }
  if (proto == "LG") { irsend.sendLG(code, bits ? bits : 28); return true; }
  if (proto == "PANASONIC") { irsend.sendPanasonic(0x4004, code, bits ? bits : 48); return true; }
  if (proto == "JVC") { irsend.sendJVC(code, bits ? bits : 16, false); return true; }
  return false;
}

void servoPress(uint8_t index, uint32_t holdMs) {
  if (index >= kServoCount) return;
  servos[index].write(kServoPress);
  pressJobs[index].active = true;
  pressJobs[index].index = index;
  pressJobs[index].releaseAt = millis() + (holdMs ? holdMs : 120);
}

void releaseAll(const char *why) {
  for (uint8_t i = 0; i < kServoCount; i++) {
    servos[i].write(kServoRest);
    pressJobs[i].active = false;
  }
  Serial.printf("[safe] 松开全部舵机：%s\n", why);
}

// ---------------------------------------------------------------- 命令
void handleCommand(const String &text) {
  StaticJsonDocument<512> doc;
  DeserializationError err = deserializeJson(doc, text);
  if (err) {
    Serial.printf("[ws] JSON 解析失败：%s\n", err.c_str());
    return;
  }
  lastCmdAt = millis();
  const char *cmd = doc["cmd"] | "";

  if (strcmp(cmd, "ping") == 0) {
    sendEvent("pong");
    return;
  }

  if (strcmp(cmd, "estop") == 0) {
    releaseAll("主机急停");
    broadcast("{\"event\":\"ack\",\"cmd\":\"estop\",\"ok\":true}");
    return;
  }

  if (strcmp(cmd, "ir_learn") == 0) {
    learning = true;
    irrecv.enableIRIn();
    irrecv.resume();
    broadcast("{\"event\":\"learning\",\"on\":true}");
    Serial.println("[ir] 进入学习，请按遥控器");
    return;
  }

  if (strcmp(cmd, "servo_test") == 0 || strcmp(cmd, "press") == 0) {
    uint8_t index = doc["servo"] | 0;
    uint32_t hold = doc["hold_ms"] | 150;
    servoPress(index, hold);
    broadcast("{\"event\":\"ack\",\"cmd\":\"press\",\"ok\":true}");
    return;
  }

  if (strcmp(cmd, "ir_send") == 0) {
    const char *codeHex = doc["code"] | "0";
    const char *proto = doc["protocol"] | "NEC";
    uint16_t bits = doc["bits"] | 32;
    uint16_t repeat = doc["repeat"] | 1;
    uint16_t interval = doc["interval_ms"] | 120;
    uint64_t code = strtoull(codeHex, nullptr, 16);

    bool ok = false;
    for (uint16_t i = 0; i < repeat; i++) {
      ok = sendByProtocol(String(proto), code, bits);
      if (!ok) break;
      // 重复帧之间留出间隔，别和接收端的解码窗口打架
      if (i + 1 < repeat) delay(interval);
    }
    StaticJsonDocument<128> ack;
    ack["event"] = "ack";
    ack["cmd"] = "ir_send";
    ack["ok"] = ok;
    String out;
    serializeJson(ack, out);
    broadcast(out);
    return;
  }

  Serial.printf("[ws] 不认识的命令：%s\n", cmd);
}

void onWsEvent(uint8_t num, WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED: {
      Serial.printf("[ws] #%u 连上了\n", num);
      StaticJsonDocument<192> doc;
      doc["event"] = "hello";
      doc["fw"] = "jieji-remote-1.0";
      JsonArray caps = doc.createNestedArray("caps");
      caps.add("ir");
      caps.add("press");
      doc["servos"] = kServoCount;
      String out;
      serializeJson(doc, out);
      ws.sendTXT(num, out);
      break;
    }
    case WStype_TEXT:
      handleCommand(String((char *)payload).substring(0, length));
      break;
    case WStype_DISCONNECTED:
      Serial.printf("[ws] #%u 断开\n", num);
      releaseAll("控制端断开");
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------- setup
void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\n=== jieji remote bridge ===");

  irsend.begin();
  irrecv.enableIRIn();

  for (uint8_t i = 0; i < kServoCount; i++) {
    servos[i].setPeriodHertz(50);
    servos[i].attach(kServoPins[i], 500, 2400);
    servos[i].write(kServoRest);
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("连接 Wi-Fi %s", WIFI_SSID);
  uint32_t started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
    delay(400);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n连上了，IP = %s\n", WiFi.localIP().toString().c_str());
  } else {
    // 连不上就自己开热点，主机直连 192.168.4.1
    WiFi.mode(WIFI_AP);
    WiFi.softAP("jieji-robot", "jieji1234");
    Serial.printf("热点模式：%s / jieji1234\n", WiFi.softAPIP().toString().c_str());
  }

  ws.begin();
  ws.onEvent(onWsEvent);
  Serial.println("WebSocket 端口 81，等主机连过来");
  lastCmdAt = millis();
}

// ---------------------------------------------------------------- loop
void loop() {
  ws.loop();

  // 红外学习：抓到就回报
  if (learning && irrecv.decode(&irResults)) {
    StaticJsonDocument<256> doc;
    doc["event"] = "ir_learned";
    doc["protocol"] = protocolName(irResults.decode_type);
    doc["bits"] = irResults.bits;
    char codeHex[24];
    snprintf(codeHex, sizeof(codeHex), "0x%llX", (unsigned long long)irResults.value);
    doc["code"] = codeHex;
    doc["raw_len"] = irResults.rawlen;
    String out;
    serializeJson(doc, out);
    broadcast(out);
    Serial.printf("[ir] 学到 %s %s (%u bits)\n", codeHex, protocolName(irResults.decode_type), irResults.bits);
    learning = false;
    irrecv.resume();
  }
  if (!learning && irrecv.decode(&irResults)) {
    irrecv.resume();   // 平时把接收缓冲清掉，别涨
  }

  uint32_t now = millis();

  // 舵机松开（非阻塞，不许 delay）
  for (uint8_t i = 0; i < kServoCount; i++) {
    if (pressJobs[i].active && (int32_t)(now - pressJobs[i].releaseAt) >= 0) {
      servos[i].write(kServoRest);
      pressJobs[i].active = false;
    }
  }

  // 2 秒没命令 → 全部松开
  if (now - lastCmdAt > kCommandTimeoutMs) {
    for (uint8_t i = 0; i < kServoCount; i++) {
      if (pressJobs[i].active) {
        releaseAll("命令超时");
        break;
      }
    }
  }

  // 每 5 秒发一次状态，让主机知道板子还活着
  if (now - lastStatusAt > 5000) {
    lastStatusAt = now;
    StaticJsonDocument<128> doc;
    doc["event"] = "status";
    doc["uptime_s"] = now / 1000;
    doc["wifi_rssi"] = WiFi.RSSI();
    doc["clients"] = ws.connectedClients();
    String out;
    serializeJson(doc, out);
    broadcast(out);
  }
}
