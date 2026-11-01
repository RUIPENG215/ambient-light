# Interactive Ambient Light v2.2

一盏有「AI agent 性格」的互动氛围灯：32 颗 NeoPixel 在硫酸纸罩里呼吸、受惊、
好奇、困倦；电脑上的 DeepSeek 根据声音、接近、音乐和人的状态决定它「接下来
怎么感觉」，固件负责 20 ms 级的反射和 50 fps 的动画。

这一版只有两层：

> **反射在电脑（25 Hz，不需要网络），思考在一个 agent（可选）。**

```
麦克风 / 音乐 / 接近 / 照度 / 空闲
        │
        ▼
   reflex.py        把传感器变成数字：呼吸深浅、亮度、开合、偏暖、闪烁
        │  25 Hz，串口 F 帧
        ▼
    固件 2.2        50 fps 画出来；断线时自己退回琥珀呼吸
        ▲
        │  同一个 agent 写的 11 个数字
   agent.py         DeepSeek，约 25 秒一轮；你说话时立刻一轮并回你一句
```

- **没有 agent 也活着**：没有 key、断网、或者 `--no-llm`，反射照跑，灯照样对声音
  和靠近有反应。
- **没有 host 也活着**：host 挂了，固件 1.5 秒后回到自己的琥珀呼吸。
- **只有一个意识**：webchat 不调模型，它把你说的话转给 host 里那一个 agent。

## 目录

| 路径 | 内容 |
|---|---|
| [firmware/ambient_light/ambient_light.ino](firmware/ambient_light/ambient_light.ino) | 固件：F 帧渲染、瞬态、兜底 look、串口 |
| [firmware/syntax_check/run_checks.sh](firmware/syntax_check/run_checks.sh) | 无 Arduino 工具链的语法 + 行为自检 |
| [host/ambient_host.py](host/ambient_host.py) | 宿主主程序：快循环 / 音乐 / 光流 / agent |
| [host/agent.py](host/agent.py) | 唯一的大脑：DeepSeek 工具调用、drives、记忆 |
| [host/looks.py](host/looks.py) | `Look`（11 个数字）+ 25 Hz 参数流线程 |
| [host/reflex.py](host/reflex.py) | 感知层：麦克风/节拍/接近/照度/空闲 → 光，不需要网络 |
| [host/control.py](host/control.py) | 本机控制 socket（light / blink / say / observe） |
| [host/lampctl.py](host/lampctl.py) | 命令行控灯、和它说话、读状态 |
| [host/webchat.py](host/webchat.py) | 本地网页：状态 + 聊天（它自己不调模型） |
| [host/sensors.py](host/sensors.py) | 麦克风节拍、BlackHole 音乐、macOS 上下文 |
| [host/audio_meter.py](host/audio_meter.py) | 麦克风 → 校准 → 0–100 等级 |
| [host/feather_link.py](host/feather_link.py) | 串口协议、遥测解析、断线重连 |
| [host/sim_feather.py](host/sim_feather.py) | 假 Feather（pty），无硬件联调 |
| [PROTOCOL.md](PROTOCOL.md) | 完整串口协议 v2.2 |
| [FIRMWARE-API.md](FIRMWARE-API.md) | 固件 v2.2 的接口契约与实现记录 |

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
- 开合度目前是**灯光调制**：`F` 的 `open` 映射成亮度（0 = 2%，100 = 100%），
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

### 烧完先手看一眼

串口监视器里手发一条 `F` 只能看到它缓动过去，看不出「流」的感觉。起 host、
用 `lampctl` 直接改数字：

```bash
cd host && source .venv/bin/activate
python3 ambient_host.py --port auto --span-db 30        # 另开一个终端
python3 lampctl.py observe
python3 lampctl.py light --val 200 --grad 35 --wave-hz 20
python3 lampctl.py light --val 90 --ease 3000           # 慢慢暗下去
python3 lampctl.py blink
```

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

**一个命令起全部**（host + 网页，日志进 `host/logs/`）：

```bash
cd host
./lamp.sh start
#   host      started (pid 12345)  agent: DeepSeek
#   网页      http://127.0.0.1:8765

./lamp.sh status              # 现在在跑什么、灯被喂了什么
./lamp.sh logs                # 跟着看两边日志
./lamp.sh say "暗一点"         # 不打开网页也能跟它说话
./lamp.sh stop                # 两个一起停（包括不是它起的）
```

关掉终端不会停（`nohup`），用 `./lamp.sh stop`。

手动起也可以：

```bash
cd host
source .venv/bin/activate
set -a; source .env; set +a          # 有 key 才会有 agent
python3 ambient_host.py --port auto --span-db 30
```

启动日志会说明现在是哪一层在管灯：

```
reflex on | stream 25 Hz cap 80 | agent deepseek-flash | slow 3..10 s | port auto | span 30.0 dB
```

状态栏每 10 秒一行：

```
22:57:23  lvl   8 ..........  -54.4 dBFS  hue= 30 val= 90 open= 55  prox=  0  lux=    0  mus=  5  link=up  stream=lamp  llm=3/60  812ms
```

### 三种降级，都还能用

| 情况 | 你会看到 | 灯还是活的吗 |
|---|---|---|
| 有 key、有网 | `agent deepseek-flash` | 是：agent 写样子，反射做反应 |
| 没 key / 断网 / `--no-llm` | `agent off` | 是：只有反射，灯照样呼吸、跟着声音和手 |
| host 挂了 | 固件 1.5 s 后 `FRAME_LOST` | 是：固件退回自己的琥珀呼吸 |

```bash
python3 ambient_host.py --port auto --no-llm      # 只要反射，不要大脑
```

### 无硬件

```bash
python3 sim_feather.py --pty
# 输出 PTY=/dev/ttys012
python3 ambient_host.py --port /dev/ttys012 --no-llm --sim-audio
```

### 完全干跑

```bash
python3 ambient_host.py --no-llm --sim-audio --dry-run --duration 10
```

会把 `[dry] G,...`、`[dry] C,0,0,0,0`、`[dry] F,...`、`[dry] K,...` 打到终端。

### 常用参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `--no-llm` | 关 | 不要 agent，只跑反射 |
| `--model` | `deepseek-flash` | DeepSeek 模型 |
| `--api-base` | `https://api.deepseek.com` | OpenAI 兼容地址 |
| `--llm-min-interval` | 25 | 两次自主决策的最小间隔（秒） |
| `--llm-heartbeat` | 120 | 环境没变化时，至少多久想一次 |
| `--max-calls-per-hour` | 60 | 硬上限，滑动窗口 |
| `--llm-timeout` | 8 | 单次 API 超时（秒） |
| `--look-hz` | 25 | 参数流频率 |
| `--cap` | 80 | 通过 `G` 下发的通道上限（亮度） |
| `--gamma` | 0 | 0 线性 / 1 = 2.2 / 2 = 2.8 |
| `--no-reflex` | 关 | 关掉感知层（只放 agent 写的样子） |
| `--span-db` | 18 | 比底噪高多少 dB 算 100（放音乐建议 30） |
| `--calibrate` | 3 | 底噪校准秒数 |
| `--sim-audio` | 关 | 合成音频，不需要麦克风权限 |
| `--no-music` | 关 | 不开 BlackHole；节拍退回麦克风 |
| `--dry-run` | 关 | 不写串口，只打印 |
| `--state-file` | `state.json` | drives / 记忆持久化 |
| `--control-socket` | `/tmp/ambient-lamp.sock` | 本机控制 socket |
| `--list-devices` | — | 列出音频设备和串口 |

---

## 从另一个进程控灯（lampctl / DSH）

`ambient_host.py` 默认在 `/tmp/ambient-lamp.sock` 开一个本机 Unix socket。
不用停 host，另开一个终端：

```bash
cd host
python3 lampctl.py observe                              # 灯现在被喂了什么
python3 lampctl.py light --val 90 --ease 2500           # 只改一两个数字，其余不动
python3 lampctl.py light --grad 30 --wave-amp 40 --wave-hz 14   # 让它流动起来
python3 lampctl.py blink                                # 眨一下（瞬态，不改样子）
python3 lampctl.py say "暗一点，慢一点"                  # 和那个 agent 说话
```

`light` 写的就是 agent 写的那 11 个数字，所以它是**调试工具**，不是第二套控制
逻辑：agent 下一轮看到 `lamp.light`，可以接着改。`say` 会走 host 里那**同一个**
agent（所以要 host 有 key），最多等 25 秒。

- `--json` 输出原始 JSON，方便脚本解析。
- 没有 host 在跑时会提示 `no control socket ...`。

### 感知层：传感器直接改光，不经过 agent

`host/reflex.py` 在 25 Hz 的帧循环里跑，把传感器的读数叠到当前 look 上。
**它不联网、不调用模型**，所以就算 DeepSeek 挂了、断网了，灯照样会反应。

| 传感器 | 它改什么 | 感觉上像 |
|---|---|---|
| 房间声音 / 系统音频 | 两者**取较大者**，一起驱动呼吸深度（安静 ~60%，大声 ~150%）和亮度 | 房间变吵、或者开始放音乐，呼吸都变深 |
| 音乐节拍 | `val` 上一个 ~200 ms 的脉冲 | 跟着鼓点跳 |
| 音乐能量 | 眼皮多睁开一点点，**并把光"铺开"**：色相跨度、空间波深度、波的流速都上去 | 有音乐时颜色开始在环上流动 |
| 音乐节拍 | 除亮度外，眼皮也跟着鼓点抬一下 | 鼓点不只是变亮，还像被"顶"了一下 |
| 接近（APDS） | `open` + 亮度 + 色相偏暖 | 手靠近时"看过来" |
| 照度 | 亮度下限 | 房间亮时它更亮，夜里更暗 |
| 空闲秒数 | 缓慢变暗（最多 35%） | 你走了它就困了 |
| 电平突跳 | 发 `K,2,255` + `K,1` | 拍手：闪白 + 缩一下 |

几条设计上的取舍：

- **叠加，不接管**：反射只调制 agent 给的基底，不替换它。谁在控制这盏灯，
  任何时候都只有一句话能说完。
- **关掉板子的耦合**：反射层启动时会发 `C,0,0,0,0`，否则固件会再叠一次
  音乐增益，变成双重调制。
- **受惊要求"边缘"**：必须一帧内跳 10 个电平以上，慢慢变吵不会触发。
- **音乐永远不会吓到它**：受惊只看麦克风，不看系统音频——重低音再猛也不会让它跳一下。
- **房间和音乐取 max 而不是相加**：大声放音乐时麦克风也会听到，相加就重复计数了。
- 自动变暗、照度这些参数在 `ReflexConfig` 里，改数字就行。

```bash
```bash
python3 ambient_host.py --port auto --span-db 30     # 反射 + agent
python3 ambient_host.py --port auto --no-reflex      # 只要 agent 写的样子，不感知
```

> 光流从启动那一刻就在跑（基底是那团琥珀），所以反射**永远**有东西可调制。
> host 死了才会停，那时固件自己接手。

## 本地 Web 聊天

`webchat.py` 是一扇窗：左边是灯的状态，右边是和它聊天。**它自己没有任何模型**，
只把你说的话通过控制 socket 转给 host 里那一个 agent，再把回答显示出来。关掉页面，
灯继续跑；重启它也不会换一个性格。

```bash
cd host
source .venv/bin/activate
python3 webchat.py --port 8765 --open
# http://127.0.0.1:8765
```

- 聊天 = 调 host 的 `say`，所以 host 必须有 key 才会有回答；没有时会说
  「我现在只有反射」
- 四个按钮是**写参数**，不是切模式：暗一点 / 亮一点 / 流动一点 / 眨一下
- 页面每 2 秒刷新状态；聊天记录存在 `webchat_history.json`
- 只绑 `127.0.0.1`，没有登录；不要直接暴露到公网

---

## agent 在决定什么

每轮给模型一个 JSON `situation`：噪音均值/峰值/趋势、音乐能量/节拍/曲名、人是否
在电脑前、空闲秒数、照度、**它自己上一次写的 11 个参数**、四个 drives、最近 6 条
记忆和事件。你说话时，`situation` 里会多一个 `said` 字段，那一轮它要回你一句话。

| 工具 | 作用 |
|---|---|
| `set_light` | **写 11 个数字**：hue / sat / val / grad / wave_n / wave_amp / wave_hz / breath_hz / breath_amp / open / ease。没有枚举，漏写的字段保持原值 |
| `blink` | blink / wince，一次性瞬态，不改变样子 |
| `set_reflex` | 调自己的反射：多容易被吓到、手靠近影响多大、走开变多暗、音乐进光多少 |
| `feel` | 调整 energy / curiosity / security / bond（增量） |
| `remember` | 写一条有显著性的记忆 |
| `observe` | 需要时再读传感器/macOS/音乐 |

**它不是从菜单里选一个。** `set_light` 接受 11 个连续值，所以「暗一点」「流动
起来」「别那么刺」都是改一两个数字。感知层（[reflex.py](host/reflex.py)）再叠加
在上面——agent 定「是什么样」，反射决定「对当下怎么反应」。

---

## 协议

完整字段、优先级、时序、容错见 [PROTOCOL.md](PROTOCOL.md)。最常用：

```
电脑 → 灯   F,150,220,180,20,2,30,40,10,40,80,600   20–30 Hz 画面参数（核心）
电脑 → 灯   K,0                          眨一下眼
电脑 → 灯   G,60,1,1                     亮度上限 / gamma / 抖动
电脑 → 灯   L,42                         20 Hz 环境声
电脑 → 灯   U,62,90,1                    20 Hz 音乐，只调制
电脑 → 灯   K,0 / K,1 / K,2,255          眨眼 / 缩一下 / 闪白
电脑 → 灯   C,80,70,50,60                耦合增益
电脑 → 灯   R,55,120,1                   反射阈值
灯 → 电脑   S,12,345,8,43,host,70,0      遥测（origin / open / flags）
灯 → 电脑   E,STARTLE
```

`F` 一行就是「这一刻长什么样」：色相、饱和度、感知亮度、沿环的色相跨度、
空间波、呼吸、开合度、缓动时间。宿主以 25 Hz 连发，固件在 50 fps 上插值。
host 现在只发 `F` / `K` / `G`；`M` 和 `LID` 是固件保留的旧入口，宿主导不再用。

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
| 灯对音乐不敏感 | `lampctl.py observe` 的 `senses level/music` | `level` 长期 <10 → 启动时房间有声音，基线被抬高；**安静状态下重启 host** |
| DeepSeek 401/404 | key 与模型名 | `echo $DEEPSEEK_API_KEY`；`--model deepseek-flash` |
| 一直 `holding state` | 日志 `skip/error` | 看是否 `hourly cap` / `circuit open`；调 `--max-calls-per-hour` |
| 音乐没节拍 | `--list-devices` | 装 BlackHole + Multi-Output；或接受麦克风回退 |
| 灯不睡（不暗） | `lampctl observe` 的 `light.val` | agent 一直写高 `val`；说「暗一点」或 `lampctl light --val 70` |
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
- 多灯同步：把 `F`/`K` 广播到多个串口。
