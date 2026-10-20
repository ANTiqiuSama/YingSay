# 应声 YingSay

**自由、开源的 Windows 实时语音助手**：在任何软件里按住热键说话，松开后整理好的文字直接出现在光标处；也能用语音改写选中的文字、边说边翻译、直接提问拿答案。语音识别和 AI 整理都调用 **小米 MiMo** API（推荐 Token Plan），不需要本地 GPU。

> 按住 `右 Ctrl` → 说话 → 松开 → 去掉口头禅、带标点、已排版的文字出现在光标处。

## 功能

| 功能 | 说明 |
| --- | --- |
| 全局按住说话、任意应用输入 | 默认 `右 Ctrl`，可改任意键或组合键；**轻点**进入免提，再按一次结束；`Esc` 取消 |
| 边说边识别 | 在你说话的停顿处就把已说完的部分提前上传识别，松开时只等最后一小段 |
| 去口头禅、去重复、识别改口 | “明天三点，不对，四点” → “明天四点” |
| 自动排版 | 口述“第一、第二” → 列表 |
| 个人词典、强制替换 | 人名、术语、品牌按你的写法输出 |
| 按应用调整语气 | 自动识别微信/QQ/飞书/邮件/IDE/终端/AI 聊天网页，也可按 exe 自定义 |
| 边说边翻译 | `F9`，目标语言可选 |
| 语音改写选中文字 | `F8`：选中后说“翻译成英文”“改得更礼貌”“缩短一半” |
| 语音提问 | `F8` 不选中文字 = 提问，弹窗回答，可一键插入 |
| 中英混说、方言 | MiMo-V2.5-ASR 支持中英混合与粤语、吴语、闽南语、四川话等 |
| 历史记录 | 保存整理后文本、原始识别和每一步耗时 |
| 隐私 | 开源、本地运行；API Key 用 Windows DPAPI 加密；录音只发给你配置的 MiMo 接口 |

另外：润色失败自动退回原始识别（不丢字）、防止 AI “回答”你的话的安全阀、粘贴后恢复剪贴板（包括图片）、临时文本不进 Win+V 历史、开机自启、单实例。

## 速度优化

| 优化 | 效果 |
| --- | --- |
| **节点检测** | Token Plan 有中国、新加坡、欧洲三个节点，但 **Key 只在开通套餐的那个节点有效**（其余节点鉴权失败是正常的）；「自动选最快节点」只会在 Key 有效的节点里选 |
| **边说边识别** | 说满 2.5 秒后遇到 0.35 秒停顿就切段、提前上传，松开后只剩最后一小段要等 |
| **对冲慢请求**（v0.2.1） | 跨境网络偶发卡顿时，请求超过约 2 秒未返回就再发一份，谁先回来用谁，削掉“偶尔特别慢”的长尾 |
| **MP3 上传** | 16kHz/32kbps，体积约为 WAV 的 1/8，上行慢的网络明显更快（接口不接受时自动退回 WAV） |
| **连接预热与复用** | 按下热键时就建立 HTTPS 连接，并且一直复用，省掉每次的 TLS 握手 |
| **短句跳过润色** | 短句且没有口头禅时，直接用自带标点的识别结果，省掉一次 LLM 往返 |
| **前缀缓存友好** | 固定的规则放在 system 提示词，动态上下文放在用户消息，命中 MiMo 缓存后更快、更省 |
| **耗时可见** | 完成提示显示「✓ 1.6s（识别0.9 润色0.7）」，一眼看出慢在哪一步 |

## 用量统计（v0.2.1）

托盘菜单和「设置 → 基础」会显示 **今日 / 本月用量**（自动用 K / M / B 单位），按官方规则折算：ASR 30M Credits/小时（按秒计），mimo-v2.6-flash 输入 100 / 缓存命中 2 / 输出 200 Credits 每 token，北京时间 0–8 点 0.8 倍。在「高级 → 套餐」选好套餐即可显示占月额度的比例。这是本机估算，以 MiMo 控制台为准。

## 提示音

应声的标志音是温暖的低音区木质音色（G 大调五声音阶上的五度/三度），开始「啵—咚↑」、结束回落、完成轻琶音、出错低沉、取消轻“噗”；可在设置里关闭。

还嫌慢的话：设置 → 基础 → 识别引擎选 `asr_only`（不润色），或者托盘里关掉「AI 润色」。

## 为什么用 MiMo-V2.5-ASR + MiMo-V2.6-Flash

- **`mimo-v2.5-asr`（专用语音识别，默认）**：中英双语加方言，抗噪、远场、多人说话都稳定，自动带标点。Token Plan 按音频时长扣费，**每小时 3000 万 Credits**，Max 套餐约够 3 万多小时。
- **`mimo-v2.6-flash`（关闭深度思考）**：负责去口头禅、改口、排版、翻译，快而且便宜。
- **全模态 `omni` 模式**：用一个模型直接听音频、一步出结果，设置里可选；逐字准确度不如专用 ASR。

> 旧的 `mimo-v2.5` / `mimo-v2.5-pro` 模型 ID 将于 2026-10-21 失效，本项目默认使用 `mimo-v2.6-*`。

## 安装

### 方式一：下载 exe（推荐）

1. 打开 [Releases](https://github.com/ANTiqiuSama/YingSay/releases/latest)，下载 `YingSay.exe`。
2. 双击运行（SmartScreen 提示时点“更多信息 → 仍要运行”）。
3. 首次启动会弹出设置：填入 Token Plan 的 **API Key（tp-xxxx）**，点 **「自动选最快节点」**，再点「测试连接」，最后「保存」。

### 方式二：PowerShell 一键安装

```powershell
irm https://raw.githubusercontent.com/ANTiqiuSama/YingSay/main/scripts/install.ps1 | iex
```

从源码安装（自动用 winget 装 Python 3.12）：

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ANTiqiuSama/YingSay/main/scripts/install.ps1))) -FromSource
```

从旧版 FreeTypeless 升级：会自动迁移配置、API Key 和历史记录，并清理旧的自启项和快捷方式。

### 方式三：开发者

```powershell
git clone https://github.com/ANTiqiuSama/YingSay.git
cd YingSay
py -3.12 -m venv .venv; .\.venv\Scripts\activate
pip install -e .[dev]
python -m yingsay --set-key tp-xxxx --set-base-url https://token-plan-sgp.xiaomimimo.com/v1
python -m yingsay --test           # 测试连接，并测量三个节点的时延
python -m yingsay --transcribe a.wav
python -m yingsay                  # 启动
```

## 使用

| 操作 | 默认热键 |
| --- | --- |
| 听写 | 按住 `右 Ctrl` 说话，松开输入 |
| 免提听写 | 轻点 `右 Ctrl`，说完再按一次 |
| 改写选中文字 / 语音提问 | 按住 `F8` |
| 边说边翻译 | 按住 `F9` |
| 取消 | `Esc` |

托盘菜单：设置、历史记录、重新粘贴上一条、复制上一条、重新处理上一段录音、AI 润色开关、退出。配置和日志在 `%APPDATA%\YingSay\`。

## 常见问题

- **鉴权失败**：Token Plan 的 Key（tp-）要搭配 Token Plan 节点；按量付费的 Key（sk-）用 `https://api.xiaomimimo.com/v1`。「自动选最快节点」会显示每个节点的 Key 是否有效。
- **在管理员权限的窗口里没反应**：以管理员身份运行 YingSay。
- **某些终端或游戏里 Ctrl+V 不生效**：设置 → 高级 → 粘贴方式改为 `shift_insert` 或 `type`。
- **笔记本 F8/F9 要按 Fn**：在设置 → 热键里点「录制」换成别的键。
- **VS Code 没选中也被当作“选中”**：关闭 `editor.emptySelectionClipboard`。
- **杀毒软件误报**：改用源码安装。

## 路线图

- [ ] 实时字幕浮窗：说话时就逐字显示识别结果
- [ ] 持续监听模式：会议或系统声音的实时转写与要点摘要
- [ ] 多 API 接入：ASR 和 LLM 都可插拔（MiMo、OpenAI 兼容接口、DeepSeek、通义、本地 Whisper / SenseVoice 等），支持多模型同时回答再汇总
- [ ] 多种答案输出：浮窗显示、语音朗读（MiMo TTS）、执行动作（搜索、记笔记、发消息）

## 参考的开源项目

[cjpais/Handy](https://github.com/cjpais/Handy) · [OpenWhispr/openwhispr](https://github.com/OpenWhispr/openwhispr) · [tover0314-w/opentypeless](https://github.com/tover0314-w/opentypeless) · [HaujetZhao/CapsWriter-Offline](https://github.com/HaujetZhao/CapsWriter-Offline)

## 开发

```
yingsay/
  app.py       主程序、线程编排、节点测速、CLI
  live.py      边说边识别（停顿切段、提前上传、按顺序拼接）
  hotkeys.py   全局热键状态机
  audio.py     录音、重采样、WAV/MP3 编码、长音频切片
  mimo.py      MiMo OpenAI 兼容接口（连接复用、预热、测速）
  prompts.py   提示词与应用场景识别
  pipeline.py  录音 → 文本的处理管线
  winutil.py   前台窗口、剪贴板、模拟粘贴、DPAPI、自启
  ui.py        浮窗、设置、历史、问答窗口、托盘
```

`python -m pytest -q tests`。CI 会在 Windows 上跑单元测试和真实的 UI、托盘、键盘钩子、剪贴板、MP3 编码冒烟测试，然后用 PyInstaller 打包发布。

MIT License
