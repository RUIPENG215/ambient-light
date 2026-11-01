# Snapshot v0.10.0 — 2026-11-01 23:10 (+08)

第一个「**只有反射和一个 agent**」的版本。固件 2.2.0 + 宿主重构。
上一版是 `SNAPSHOT-v0.9.1.md`（固件 2.1.0，四个 mood 状态机）。

## 这一版的核心

> **反射在电脑（25 Hz，不需要网络），思考在一个 agent（可选）。**

```
传感器（声音/音乐/接近/照度/空闲）
   │
   ▼
reflex.py        25 Hz，叠在 11 个数字上
   │  F 帧
   ▼
固件 2.2.0       50 fps 画出来；断线 1.5 s 后自己回琥珀呼吸
   ▲
   │  同一组 11 个数字
agent.py         DeepSeek，约 25 秒一轮；你说话时立刻一轮并回一句
```

三层各自独立，任意一层挂掉灯都不会变成黑砖：

| 情况 | 谁在管 | 灯的状态 |
|---|---|---|
| 有 key、有网 | agent 定样子 + 反射做反应 | 完整 |
| 没 key / 断网 / `--no-llm` | 只有反射 | 照样呼吸、跟声音、跟手 |
| host 挂了 | 固件兜底 | 1.5 s 后回到琥珀呼吸 |

## 固件 2.2.0

- 只认识 **色场 / 空间波 / 呼吸包络 / 瞬态 / 反射**，不认识 mood
- `F` 参数帧：`hue sat val grad wave_n wave_amp wave_hz breath_hz breath_amp open ease`
- `K` 瞬态：眨眼 / 缩一下 / 闪白
- `G`：通道上限、gamma、时域抖动
- 兜底 look + 缓慢打瞌睡；`FRAME_LOST` / `FRAME_OK` 事件
- 8 字段遥测：`S,prox,als,lux,energy,origin,open,flags`
- 源码里仍有 `M` / `LID` / `L` / `U` / `C` / `R` 的老入口，**宿主已不再发送**，
  计划在 v2.3 删除（需要重烧）

## 宿主

| 文件 | 行数 | 作用 |
|---|---|---|
| `host/ambient_host.py` | 600 | 快循环 / 音乐 / 光流 / agent 循环 |
| `host/agent.py` | 652 | 唯一的大脑：6 个工具、drives、记忆、限流 |
| `host/reflex.py` | 214 | 感知层：声音→呼吸、节拍→亮度、接近→开眼、照度→亮度、空闲→变暗、突跳→受惊 |
| `host/looks.py` | 284 | `Look`（11 个浮点）+ 25 Hz 引擎 + 宿主侧缓动 |
| `host/control.py` | 267 | 本机 socket：`light` / `blink` / `say` / `observe` |
| `host/lampctl.py` | ~200 | 命令行：控灯、说话、读状态 |
| `host/webchat.py` | 561 | 网页：状态 + 聊天（**自己不调模型**） |
| `host/lamp.sh` | — | 一条命令起停全部（start/stop/status/logs/say） |

agent 的工具：`set_light`（11 个连续值，没有枚举）/ `blink` / `set_reflex`
（调自己的反射灵敏度）/ `feel` / `remember` / `observe`。

## 已删除

`brain.py`（Decision / LocalBrain / build_context / fingerprint）、
`stream_look.py`、`look` 名字目录、`flow` 命令、webchat 里的 DeepSeek 与提示词、
mind lease / `release`、`state 2.json`、`.DS_Store`。

宿主代码 5434 → 3807 行（−30%）；全仓 6626 → 4999 行（−25%）。

## 已验证

- 固件自检：**70 pass / 0 fail**（`-Wall -Wextra` 干净）
- 宿主测试：**76 pass**（`python -m unittest discover -s tests`）
- 真机：固件 2.2.0 已烧录并运行；亮度、gamma、抖动、兜底缓出、受惊、节拍、
  接近、照度、空闲逐项在实物上看过
- 端到端 dry-run：251 帧 / 10 s，串口上只有 `G` ×1、`C,0,0,0,0` ×1、`F` 25 Hz、`K` 瞬态
- 实机自主决策：无人说话时 agent 自行判断「深夜、安静」→ `hue 22 / val 95 /
  wave_hz 4 / ease 6000`

## 未验证 / 已知问题

- **网页聊天的新回复路径没有在真机上确认过**。`tool_choice` 从 `required` 改成
  说话时 `auto`，理论上能回文字；需要重启 host 后说一句话验证。
- `say` 依赖 host 里的 agent（有 key）。没有 key 时网页只会显示「我现在只有反射」。
- 固件里 `M`/`LID`/`L`/`U`/`C`/`R` 是死代码，等 v2.3 清（要重烧）。
- 板载 PDM 麦克风无信号，声音依赖电脑；BlackHole 未装，节拍退回麦克风。
- ToF（VL53L0X）已买未接。
- `tests/smoke.sh` 需要真实终端（pty），沙箱里跑不了。

## 跑起来

```bash
cd ambient-light/host
./lamp.sh start          # host + 网页；日志在 host/logs/
./lamp.sh status
./lamp.sh say "暗一点"
./lamp.sh stop
```

固件改了之后先在电脑上自检（不需要板子）：

```bash
cd ambient-light/firmware/syntax_check && ./run_checks.sh
```

## 不包含在快照里

`host/.env`（API key）、`.venv/`、`state.json`、`webchat_history.json`、
`logs/`、`__pycache__/`。

## 下一步

1. 验证网页聊天能回话（`./lamp.sh` 重启后说一句）
2. 固件 v2.3：删掉 `M`/`LID`/`L`/`U`/`C`/`R` 和 preset 表（要重烧）
3. BlackHole：让节拍真的「听见」鼓（现在笔记本扬声器放不出低频）
4. ToF 接进来，给接近一个真正的距离
5. 网页文案收尾；`lamp.sh` 已经在，终端不用再开三个
