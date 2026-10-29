# 协议手册

三个接口面，各自独立：

```
浏览器  ──HTTP/SSE/MJPEG──►  jieji 主机  ──WebSocket──►  ESP32 遥控器桥
                                    └──────串口────────►  电机驱动板
```

所有 JSON 都是 UTF-8，不转义中文。

---

## 一、HTTP 接口（浏览器 / 脚本用）

基地址 `http://127.0.0.1:8790`。

| 方法 | 路径 | 作用 | 请求体 | 返回 |
| --- | --- | --- | --- | --- |
| GET | `/api/health` | 存活探针，给监控用 | — | `{ok, version, uptime_s, mode, driver, camera, audio, model, key_set, estop}` |
| GET | `/api/state` | 全量快照（前端每 2 秒拉一次） | — | 见下 |
| GET | `/api/events` | SSE 事件流 | — | `text/event-stream` |
| GET | `/api/stream` | MJPEG 实时画面 | — | `multipart/x-mixed-replace` |
| GET | `/api/snapshot.jpg` | 当前帧 JPEG | — | `image/jpeg` |
| GET | `/api/map.png` | 模拟器俯视图 | — | `image/png` |
| POST | `/api/chat` | 用文字说一句话 | `{text}` | `{ok, text}` |
| POST | `/api/manual` | 手动遥控（不过大脑，但过安全层） | `{action, speed?, duration_ms?, button?}` | `{ok, action, result, blocked?}` |
| POST | `/api/mode` | 切换 `idle` / `auto` | `{mode}` | `{ok, mode}` |
| POST | `/api/estop` | 拍急停（锁存） | `{reason?}` | `{ok, estop}` |
| POST | `/api/estop/clear` | 解除急停 | — | `{ok, estop}` |
| POST | `/api/understand` | "读懂这一刻"（视觉 + 听觉各一次） | `{text?}` | 见下 |
| GET/POST | `/api/settings` | 读 / 改配置 | `{settings:{...}}` | `{settings, changed, rebuilt, public}` |
| GET | `/api/llm/ping` | 测模型连通性 | — | `{ok, ms, model, reply}` |
| GET | `/api/asr/status` | 识别器状态 | — | `{provider, model, ok, detail}` |
| GET | `/api/ir/codes` | 已学的按键 | — | `{codes:{slot:record}}` |
| POST | `/api/ir/learn` | 学一个键 | `{slot}` | `{ok, record}` |
| DELETE | `/api/ir/codes/{slot}` | 忘掉一个键 | — | `{ok}` |
| GET | `/api/episodes` | 录制的片段列表 | — | `{episodes:[{id, steps, frames}]}` |
| GET | `/api/episodes/{id}` | 片段内容 | `?limit=200` | `{id, entries:[...]}` |
| GET | `/api/fs/dirs` | 列目录（设置里选文件用） | — | `{dirs:[...]}` |

### `/api/state` 结构

```json
{
  "started": 1793179000.1, "uptime_s": 42.0, "mode": "idle",
  "camera": {"source": "mock", "running": true, "frames": 320, "fps": 8.0, "age_ms": 12, "size": "480x360", "error": ""},
  "audio":  {"source": "mock", "running": true, "error": ""},
  "driver": {"name": "mock", "connection": {"kind": "sim"}, "telemetry": {"front_m": 1.4, "heading_deg": 0, "battery": 92, "moving": false}},
  "safety": {"estop": false, "actions": 3, "blocked": 1, "forced_stops": 0, "events": []},
  "brain":  {"model": "deepseek-flash", "vision": true, "stats": {"calls": 3, "tokens": 3012}, "health": {"ok": true, "ms": 780}, "history": [...]},
  "sim":    {"x": 0.9, "y": 3.45, "heading_deg": 0.0, "front_m": 1.4, "sprites": [...]},
  "utterances": [{"text": "往前走", "at": 1793179010.2, "source": "text", "duration": 0.0}],
  "actions": [{"action": {"action": "forward", "speed": 0.3, "duration_ms": 800}, "ok": true, "detail": "forward 0.30 × 800ms"}]
}
```

### `/api/understand` 返回

```json
{
  "ok": true, "frame_seq": 176,
  "vision": {"summary": "正前方近处有一个蓝色方块人形挡路", "scene": "...", "obstacles": "...",
             "people": "...", "objects": "...", "text": "无", "lighting": "...", "floor": "...", "details": "..."},
  "vision_meta": {"cached": false, "ms": 2029, "tokens": 754},
  "audio": {"speaker": "未知", "speech": "你看到什么了？", "summary": "...", "detail": "...",
            "emotion": "平静", "intent": "提问", "keywords": ["看到"], "soundscape": "...", "lang": "汉语"},
  "prompt_text": "一句话概括：...\n场景：..."
}
```

---

## 二、SSE 事件流

`GET /api/events`，标准 `text/event-stream`。连上后**第一条永远是 `snapshot`**，
之后是真事件。浏览器 `EventSource` 会自动重连，服务端每 15 秒发一个注释心跳
防止中间层掐断。

```
event: message
data: {"kind":"snapshot","mode":"idle","driver":"mock",...}
```

| kind | 何时 | 关键字段 |
| --- | --- | --- |
| `snapshot` | 连接建立 / 每个 tick（10Hz） | 同 `/api/state` |
| `hello` | 重建传感器或身体后 | `driver, camera, audio, model, rebuilt` |
| `utterance` | 听到一句话 | `text, source, duration, ignored?` |
| `decision` | 大脑给出决策 | `action{...}, latency_ms, tokens, model, mode, blocked?, user` |
| `action` | 动作执行完 | `action, result{ok, detail, duration_ms, moved_m, state}` |
| `say` | 需要朗读 | `text` |
| `mode` | 模式变化 | `mode` |
| `estop` | 急停开关 | `active, reason` |
| `error` | 出问题 | `where, error` |

`decision` 里的 `action` 就是提示词要求的那个对象：

```json
{"action": "forward", "speed": 0.3, "duration_ms": 800, "button": "",
 "say": "前面有人，我慢慢挪一点", "mood": "谨慎", "why": "正前方 1.2m 有行人", "source": "model"}
```

---

## 三、遥控器桥（WebSocket，端口 81）

主机是客户端，ESP32 是服务端。地址形如 `ws://192.168.1.42:81`。
一行一条 JSON 文本帧。

### 主机 → 桥

```json
{"cmd": "ping"}
{"cmd": "estop"}
{"cmd": "ir_learn"}
{"cmd": "ir_send", "code": "0x20DF10EF", "protocol": "NEC", "bits": 32, "repeat": 6, "interval_ms": 120}
{"cmd": "press", "servo": 0, "hold_ms": 150}
{"cmd": "servo_test", "servo": 2}
```

### 桥 → 主机

```json
{"event": "hello", "fw": "jieji-remote-1.0", "caps": ["ir", "press"], "servos": 4}
{"event": "ir_learned", "code": "0x20DF10EF", "protocol": "NEC", "bits": 32, "raw_len": 68}
{"event": "ack", "cmd": "ir_send", "ok": true}
{"event": "status", "uptime_s": 120, "wifi_rssi": -55, "clients": 1}
{"event": "learning", "on": true}
```

`ir_send` 的 `repeat` 是**重复次数**，不是 `duration_ms`：红外是一次性协议，
想让它持续动，只能一遍遍发。主机按 `ir_repeat_interval_ms`（默认 120ms）算好次数，
时长到了再发一次 `stop` 对应的码。

### 学到的码存在哪

`data/ir_codes.json`，主机侧持久化：

```json
{
  "_meta": {"version": 1, "updated": 1793179010.2},
  "forward": {"slot": "forward", "code": "0x20DF10EF", "protocol": "NEC", "bits": 32, "learned_at": 1793179010.2},
  "stop":    {"slot": "stop", "code": "0x20DF906F", "protocol": "NEC", "bits": 32, "learned_at": 1793179030.9}
}
```

`slot` 是逻辑名（`forward/backward/turn_left/turn_right/stop` 或任意自定义），
`code` 是 16 进制字符串。换一台遥控器只要重学，代码一行不用改。

---

## 四、串口协议（直接驱动电机）

115200，8N1，一行一条 JSON，`\n` 结尾。对应 `DRIVER=serial`。

```json
{"t": "drive", "l": 0.4, "r": 0.4, "ms": 800}
{"t": "stop"}
{"t": "estop"}
{"t": "ping"}
```

板子回：

```json
{"t": "hello", "fw": "jieji-motor-1.0"}
{"t": "status", "front_cm": 42, "l": 0.40, "r": 0.40}
{"t": "pong"}
{"t": "log", "msg": "stop:障碍太近"}
```

`l/r` 归一化到 `-1.0 ~ 1.0`；`front_cm` 是从超声波/ToF 读到的厘米数（0 表示没读数）。
主机把 `front_cm` 换算成 `front_m` 喂给防撞反射层。

---

## 五、错误约定

凡是 HTTP 接口出错，统一返回：

```json
{"detail": "人话解释，可以直接显示给用户"}
```

状态码：`400` 参数不对、`409` 当前状态下不允许（比如急停时想动）、
`503` 依赖没装/没连上（会说清楚装什么）。

SSE 里的错误不中断连接，只发一条：

```json
{"kind": "error", "where": "llm", "error": "HTTP 401: 检查 API Key"}
```
