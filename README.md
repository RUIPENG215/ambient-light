# Interactive Ambient Light v2.2

一盏有「AI agent 性格」的互动氛围灯：32 颗 NeoPixel 在硫酸纸罩里呼吸、受惊、
好奇、困倦；电脑上的 DeepSeek 根据声音、接近、音乐和人的状态决定它「接下来
怎么感觉」，固件负责 20 ms 级的反射和 50 fps 的动画。

这一版的核心分工：

> **反射在固件，思考在 DeepSeek，身体在灯光与纸罩。**

v2.2 把「模式」搬到了电脑：固件不再认识 calm / alert / curious / sleepy，
只认识色场、空间波、呼吸包络、瞬态和反射；宿主用 `F` 帧以 20–30 Hz 描述
「这一刻看起来什么样」。`M` 保留成一组预设的简写，旧宿主可以不改。
契约见 [FIRMWARE-API.md](FIRMWARE-API.md)。

硬件是 Adafruit Feather nRF52840 Sense + 32 颗 WS2812B（D6）；板载 PDM 麦克风
实测无信号，所以声音由 Mac 采集，只把**数值摘要**经 USB 串口发给灯，不传音频。

```
       ┌────────────────── Mac ──────────────────┐
mic ──►│ AudioMeter ──► L,42          20 Hz       │
       │ BlackHole  ──► U,62,90,1     20 Hz       │──USB CDC──┐
       │ 空闲/曲名  ──► situation     3–10 s      │           │
       │                    │                    │           │
       │              DeepSeek agent              │           │
       │        （7 个工具：灯/眼皮/耦合/感受/记忆）│           │
       └────────────────────┼────────────────────┘           │
                            │ M / LID / C / R                │
                       ┌────▼────────────────────────────────▼──┐
                       │ Feather：状态机 + 反射 + 灯光/眼皮渲染  │
                       │ APDS9960；ToF/舵机预留                 │
                       └────────────────────────────────────────┘
```

关键取舍：DeepSeek 一次往返 1–2 s，不可能做 20 ms 的反射；所以它只决定
「策略」——mood、色相、亮度、眼皮开合、耦合增益、反射阈值——固件在这个策略
里跑 50 fps。掉线时宿主**不发命令**，固件 180 s TTL 到期后回到自主。

---

## 目录

| 路径 | 内容 |
|---|---|
| [firmware/ambient_light/ambient_light.ino](firmware/ambient_light/ambient_light.ino) | 固件：状态机、灯光/眼皮、串口 |
| [firmware/syntax_check/run_checks.sh](firmware/syntax_check/run_checks.sh) | 无 Arduino 工具链的语法 + 行为自检 |
| [host/ambient_host.py](host/ambient_host.py) | 宿主主程序：fast / music / slow 三个线程 |
| [host/agent.py](host/agent.py) | DeepSeek 工具调用、drives、记忆、状态持久化 |
| [host/sensors.py](host/sensors.py) | 麦克风节拍、BlackHole 音乐、macOS 上下文 |
| [host/audio_meter.py](host/audio_meter.py) | 麦克风 → 校准 → 0–100 等级 |
| [host/brain.py](host/brain.py) | `Decision`、`RateLimiter`、本地规则策略（旧 Claude 类保留） |
| [host/feather_link.py](host/feather_link.py) | 串口协议、遥测解析、断线重连 |
| [host/control.py](host/control.py) | 本机控制 socket（lampctl / DSH 用） |
| [host/lampctl.py](host/lampctl.py) | 命令行控灯、读状态、释放控制 |
| [host/webchat.py](host/webchat.py) | 本地 Web 聊天页（状态 + 对话 + 快捷动作） |
| [host/sim_feather.py](host/sim_feather.py) | 假 Feather（pty），无硬件联调 |
| [PROTOCOL.md](PROTOCOL.md) | 完整串口协议 v2.2 |
| [FIRMWARE-API.md](FIRMWARE-API.md) | 固件 v2.2 的接口契约与实现偏差记录 |

---

## 硬件

### 灯

| 灯带 | 接到 |
|---|---|
| `5V` | 外部 5V / 2A 电源（**不要**从 Feather 的 5V 脚带 32 颗） |
| `GND` | 外部电源 GND **和** Feather GND（共地） |
| `DIN` | Feather `D6`，串 330 Ω |
| `5V/GND` 之间 | 1000 µF 电解电容（靠近灯带） |

32 颗全白约 1.9 A。固件全局亮度锁在 40/255，实际约 0.3 A，USB 供电也能撑，
但外接电源更稳。3.3 V 数据驱动 5 V 灯带处于临界，偶尔闪烁就加 74AHCT125。

### 灯罩

- 10 cm 圆环 + 硫酸纸圆筒 + 底座；LED 环上方加一层乳白亚克力/两层纸做扩散，
  否则纸罩上会看到 32 个亮点。
- 眼皮目前是**灯光调制**：`LID` 把开合度映射成亮度（0 = 2%，100 = 100%），
  blink/wince 是快速的亮度包络。纸罩上看起来就是「闭眼、眯眼、困得垂下」。
- 以后加 SG90：把固件 `ENABLE_SERVO` 改成 1，同一套 `LID` 命令驱动舵机，
  协议和宿主都不用改。

### 传感器

- APDS9960 板载 I2C，无需接线；启动时若出现 `E,SENSOR_FAIL`，检查是否 Sense 版。
- ToF（VL53L0X，0x29）已买但**还没接进固件**；下一步加距离/接近事件，和
  APDS 分工：APDS 管 lux/颜色，ToF 管距离。
- 舵机预留 `D5`，默认关闭；接舵机时单独 5V 供电、共地。

---

## 烧录固件

1. Arduino IDE 安装 **Adafruit nRF52** 板卡包。
2. 库管理器安装 **Adafruit NeoPixel**、**Adafruit APDS9960**。
3. 板卡选 *Adafruit Feather nRF52840 Sense*，打开 `.ino` 上传。
4. 串口监视器 115200，换行 **New Line**，可以手动试：

```
T                         # 红绿蓝白自检（阻塞 1.2 s，别和 F 混用）
P                         # 应回 PONG
G,80,1,1                  # 亮度上限 80、gamma 2.2、抖动开
F,150,220,180,20,2,30,40,10,40,80,600   # 青绿、缓呼吸、沿环有色相渐变
K,0                       # 眨一下眼
K,2,255                   # 闪白一下
M,calm,150,80,180,70      # 旧写法：calm 预设，等同于一套 F 参数
U,70,100,1                # 音乐能量/节拍（只调制）
C,80,70,50,60             # 耦合增益
R,55,120,1                # 受惊阈值/接近阈值/启用
```

桌面串口监视器只能手动发单帧，看不出连续效果；要真正驱动请用宿主（见下）。
`F` 是 20–30 Hz 的流，手发一条只会看到它慢慢缓动过去。

改动固件后先在电脑上跑自检（不需要板子）：

```bash
cd firmware/syntax_check
./run_checks.sh
```

它会做 `-Wall -Wextra` 语法检查，并用假时钟、假串口驱动 `setup()/loop()`，
验证受惊、回落、45 秒入睡、靠近唤醒、链路超时、眼皮开合/眨眼、音乐脉冲、
耦合、反射阈值、参数帧、gamma、兜底缓出（67 项）。

---

## 安装宿主

```bash
cd host
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY
set -a; source .env; set +a
```

`openai` 包用于访问 DeepSeek 的 OpenAI 兼容接口；没有 key 或没装包时会自动
降级成本地规则策略并打警告。

### 音乐（可选，推荐）

```bash
brew install blackhole-2ch
sudo killall coreaudiod            # 如果设备里没出现
# Audio MIDI Setup → + → Create Multi-Output Device
#   勾选「MacBook 扬声器」+「BlackHole 2ch」，采样率都设 48 kHz
# 系统设置 → 声音 → 输出 → Multi-Output Device
python3 ambient_host.py --list-devices   # 应能看到 BlackHole
```

没有 BlackHole 也能跑：节拍会退化成从麦克风 dB 跳变检测，演示可用，但对
音乐不如系统音频干净。

首次读取 Apple Music/Spotify 会弹一次 macOS 自动化权限，允许即可；不想授权就
只用音乐能量，不看曲名。

---

## 运行

### 1. 真机 + DeepSeek

```bash
python3 ambient_host.py --port auto
```

状态栏每 10 秒一行：

```
11:39:48  lvl  42 ####......  -38.2 dBFS  mood=curious lid= 85 prox=210  lux=   12  mus= 62  link=up  llm=7/60  812ms
```

### 2. 不联网 / 答辩兜底

```bash
python3 ambient_host.py --port auto --no-llm
```

### 3. 无硬件：假 Feather

```bash
python3 sim_feather.py --pty
# 输出 PTY=/dev/ttys012
python3 ambient_host.py --port /dev/ttys012 --no-llm --sim-audio
```

### 4. 完全干跑

```bash
python3 ambient_host.py --no-llm --sim-audio --dry-run --duration 10
```

会把 `[dry] L,42`、`[dry] U,...`、`[dry] M,...`、`[dry] LID,...` 打到终端。

### 常用参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `--model` | `deepseek-flash` | DeepSeek 模型；`deepseek-v4-pro` 更贵更慢 |
| `--api-base` | `https://api.deepseek.com` | OpenAI 兼容地址 |
| `--llm-min-interval` | 25 | 两次 API 调用的最小间隔（秒） |
| `--llm-heartbeat` | 120 | 环境没变化时，至少多久问一次 |
| `--max-calls-per-hour` | 60 | 硬上限，滑动窗口 |
| `--llm-timeout` | 8 | 单次 API 超时（秒） |
| `--slow-min/--slow-max` | 3/10 | 慢循环间隔 |
| `--span-db` | 18 | 比底噪高多少 dB 算 100 |
| `--calibrate` | 3 | 底噪校准秒数 |
| `--samplerate/--blocksize` | 48000/1024 | 音频分析参数 |
| `--sim-audio` | 关 | 合成音频，不需要麦克风权限 |
| `--no-music` | 关 | 不开 BlackHole；节拍退回麦克风 |
| `--dry-run` | 关 | 不写串口，只打印 |
| `--state-file` | `state.json` | drives / 记忆持久化 |
| `--control-socket` | `/tmp/ambient-lamp.sock` | 本机控制 socket |
| `--no-control` | 关 | 不开控制 socket |
| `--list-devices` | — | 列出音频设备和串口 |
| `-v` | 关 | 调试日志 |

---

## 从另一个进程控灯（lampctl / DSH）

`ambient_host.py` 默认在 `/tmp/ambient-lamp.sock` 开一个本机 Unix socket。
不用停 host，另开一个终端：

```bash
cd host
python3 lampctl.py observe
python3 lampctl.py mood curious --hue 150 --lid 85 --seconds 120
python3 lampctl.py lid 20 --ms 2000 --seconds 30
python3 lampctl.py release
```

- `mood` / `lid` 会取一个 **mind lease**（默认 60 秒）：在这段时间里 host 的慢循环不会覆盖你发的心情，30 秒会重发一次 M 保持固件 TTL。
- `release` 立刻把控制交回 host 自己的慢循环（本地策略或 DeepSeek）。
- `--json` 输出原始 JSON，方便脚本解析。
- 没有 host 在跑时会提示 `no control socket ...`。

在 DSH 里直接让 agent 用 bash 调它，就是「聊天控灯」的最小闭环：

```
你：把灯调成好奇、别太亮
agent: python3 lampctl.py mood curious --hue 150 --bright 120 --lid 85 --seconds 120
你：它现在什么状态？
agent: python3 lampctl.py observe
```

控制 socket 只绑定本机文件，不监听网络；`--no-control` 可以完全关掉。

---

## 本地 Web 聊天

`webchat.py` 是一个独立的小页面：左边是灯的状态，右边是和它聊天；它不碰串口，
只通过上面的控制 socket 和 host 说话。关掉页面，灯继续跑。

```bash
cd host
source .venv/bin/activate
python3 webchat.py --port 8765
# 打开 http://127.0.0.1:8765
```

- 有 `DEEPSEEK_API_KEY`（环境变量或 `host/.env`）时，页面由 DeepSeek 驱动，
  可以自然聊天、让它改心情/眼皮/恢复自主。
- 没有 key 时自动降级成固定动作：困了、好奇、眨眼、恢复、状态；页面和按钮仍可用。
- 四个快捷按钮：困了、好奇、眨眼、恢复自主。
- 页面每 2 秒刷新状态；聊天记录存在 `webchat_history.json`。
- 只绑 `127.0.0.1`，没有登录；不要直接暴露到公网。

配置 key：

```bash
cp .env.example .env
# 填 DEEPSEEK_API_KEY
# 然后重启 webchat.py
```

Web 页面只是「聊天前端」，host 的慢循环和固件反射仍然独立运行；聊天里的
`lamp_mood/lamp_lid` 会拿短 lease，过期后自动交还。

---

## DeepSeek 在决定什么

每轮给模型一个 JSON `situation`：噪音均值/峰值/趋势、音乐能量/节拍/曲名、人是否
在电脑前、空闲秒数、照度、当前 mood/lid、四个 drives、最近 6 条记忆和事件。
模型必须调用工具，最多两轮（第二轮只在它调用 `observe` 后发生）。

| 工具 | 作用 |
|---|---|
| `set_mood` | mood / hue / speed / brightness / lid / reason |
| `set_lid` | hold / breathe / blink / wince |
| `set_coupling` | audio / beat / prox / idle 四个 0–100 增益 |
| `feel` | 调整 energy / curiosity / security / bond（增量） |
| `remember` | 写一条有显著性的记忆 |
| `observe` | 需要时再读传感器/macOS/音乐 |
| `set_reflex` | 调受惊阈值、接近阈值、是否启用 |

drives 会随时间衰减（精力下降、安全感恢复），所以灯有连续性，不是每次从零开始。
`state.json` 保存 drives + 记忆，下次启动「它还记得」。

### 掉线时

```
DeepSeek 失败 → 慢循环继续用上一个策略发 M（宿主还活着）
             → 宿主真的不发了 → 1.5 s 没有 F/M → FRAME_LOST
             → 3 s 缓出到兜底 look（琥珀呼吸）
             → 再静 45 s 开始打瞌睡；手靠近会醒
```

关键是**缓出**：断流时灯不是硬停在最后一帧，而是慢慢松开成一个完整的画面。
这和 v2.1 的 180 s TTL 也不同——`F` 是流，1.5 s 就够判断它停了。

---

## 协议

完整字段、优先级、时序、容错见 [PROTOCOL.md](PROTOCOL.md)。最常用：

```
电脑 → 灯   F,150,220,180,20,2,30,40,10,40,80,600   20–30 Hz 画面参数（核心）
电脑 → 灯   K,0                          眨一下眼
电脑 → 灯   G,60,1,1                     亮度上限 / gamma / 抖动
电脑 → 灯   L,42                         20 Hz 环境声
电脑 → 灯   U,62,90,1                    20 Hz 音乐，只调制
电脑 → 灯   M,alert,8,230,200,95         旧写法：预设简写
电脑 → 灯   C,80,70,50,60                耦合增益
电脑 → 灯   R,55,120,1                   反射阈值
灯 → 电脑   S,12,345,8,43,host,70,0      遥测（origin / open / flags）
灯 → 电脑   E,STARTLE
```

`F` 一行就是「这一刻长什么样」：色相、饱和度、感知亮度、沿环的色相跨度、
空间波、呼吸、开合度、缓动时间。宿主以 25 Hz 连发，固件在 50 fps 上插值。
`M` 保留为预设简写，旧宿主不用改。

---

## 成本

DeepSeek `deepseek-flash`：[官方定价](https://api-docs.deepseek.com/quick_start/pricing)
约 **$0.15 / $0.30 每百万输入**（off-peak/peak）、**$0.60 / $1.20 每百万输出**；
缓存命中输入仅 $0.003/$0.006。

一次调用约 2–3k 输入 + 200 输出，约 **$0.0005–0.001**。默认 60 次/小时上限，
约 **$0.03/小时**，24 小时不停约 **$0.7/天**；实际演示频率远低于此。
系统提示词和工具表稳定，能吃到上下文缓存。

---

## 调试手册

| 现象 | 先查 | 处理 |
|---|---|---|
| 灯完全不亮 | 发 `T` | 有自检 → 查 5V/共地/数据脚；无自检 → 查供电与上传 |
| 颜色乱 / 闪烁 | 数据线 | 串 330 Ω、加电平转换、缩短线、共地 |
| 找不到串口 | `--list-devices` | 关掉 Arduino 串口监视器；双击 reset |
| 麦克风打不开 | 系统设置 | 给终端麦克风权限；先用 `--sim-audio` 排除 |
| 底噪校准离谱 | 启动日志 `baseline` | 校准 3 秒内保持安静；正常约 -55 ~ -45 dBFS |
| 灯不跟随声音 | 状态栏 `lvl` | 不动 → 音频没进来；动但灯不动 → `link=--` |
| DeepSeek 401/404 | key 与模型名 | `echo $DEEPSEEK_API_KEY`；`--model deepseek-flash` |
| 一直 `holding state` | 日志 `skip/error` | 看是否 `hourly cap` / `circuit open`；调 `--max-calls-per-hour` |
| 音乐没节拍 | `--list-devices` | 装 BlackHole + Multi-Output；或接受麦克风回退 |
| 灯不睡 | `mood=` | 宿主若持续发 `alert/curious` 固件不会睡 |
| 串口断开后不恢复 | 日志 | 宿主每 2 秒重连；灯 12 秒后转自主，属于预期 |

---

## 演示脚本（约 3 分钟）

1. 安静房间启动，纸罩青色呼吸，日志打出 drives。
2. 拍手：固件闪白 + wince（不联网也成立）；1–2 s 后 DeepSeek 决定 alert，
   日志给出 reason。
3. 手靠近：prox 变化，灯变暖/变亮；模型说「someone is near」。
4. 放音乐：`U` 节拍脉冲，灯跟着鼓点；模型看到曲名后调整 beat 增益和色板。
5. 人离开 1 分钟：idle 上升，灯慢慢闭眼、drives 变化、写一条记忆。
6. 回来：灯慢慢睁眼；日志里有「记得你离开过」。
7. 拔网线：不再有 M，180 s 后固件回自主；插回后恢复。

---

## 已知限制

- 板载 PDM 麦克风无信号，声音依赖电脑；电脑休眠 = 灯失去听觉。
- 只有数值摘要离开电脑（等级、趋势、接近、照度、曲名），没有音频文件。
- 眼皮目前是灯光调制，不是机械翻板；舵机代码保留但未启用。
- ToF 已买未接；固件目前仍用 APDS9960 的粗接近值。
- 固件只在主机侧做了语法/行为仿真，**没有用真实 nRF52 工具链编译过**；第一次
  上板若有 API 差异，按 IDE 报错微调。
- 32 颗全白电流很大，亮度锁 40；不要直接改到 255。

---

## 下一步

- 接 VL53L0X：距离/接近事件，APDS 只管 lux。
- 加 SG90 眼皮模块：`ENABLE_SERVO 1` + 机械翻板，协议不变。
- 把 drives/事件导出 JSONL，直接进报告图。
- 多灯同步：把 `M/LID/C` 广播到多个串口。
