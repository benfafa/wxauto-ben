# wx-bridge：微信群 @ 机器人

把微信群里的 @ 消息接到 DSH agent，再把 agent 的回复发回群里。

整体思路和 [qq-bridge](https://github.com/Derpyu520/qq-bridge) 一样，只把 QQ / OneBot 换成了微信 / wxauto：

```
微信 Windows 客户端 (3.9.11.17)
        │  UI Automation（wxauto，Python）
        ▼
collector（本目录 collector/，只绑 127.0.0.1）
        │  HTTP：/cursor /poll /send
        ▼
wx-bridge（Node，本目录 src/）
  ├─ 群白名单 + @ 检测 + 去重 + 限速
  ├─ 每个群一个 DSH 会话（state/sessions.json）
  ├─ 最近群聊上下文
  ├─ 每群长期记忆（本地 memory.jsonl，按当前消息检索）
  └─ 调用 DSH Web API，收 turn 文本
        │
        ▼
DSH (dsh web, 默认 127.0.0.1:3080) ──> 微信回复（@ 发送者）
```

## 先读：这不是官方微信机器人

- 个人微信没有可用的官方机器人 API。本方案用 **Windows 微信桌面版的 UI 自动化**（wxauto），不是协议库，也不是官方授权接口。
- 本仓库的 `wxauto` 是 2021–2025 的归档版，`VERSION = 3.9.11.17`，**只支持 Windows + 微信 3.9.11.17**。UI 元素布局一变，自动化就可能失效。
- 使用非官方自动化接入微信**可能违反腾讯服务条款并导致账号受限**。请使用专门的测试号/小号，先小范围白名单试运行，不要群发、不要高频回复。仓库里的 wxauto 原 README 也明确写了“禁止用于实际生产项目”。
- 微信群消息（包括没有 @ 的消息）默认会写入 `state/messages.jsonl`，并可能发送给 DSH/模型服务。请自行评估隐私、合规和数据处理要求。
- Windows 上 `chmod 0600` 不生效，`state/` 会继承所在目录的 ACL。多人共用的机器请用 NTFS 权限限制 `wx-bridge\state`，或放在独立用户目录/加密卷里。

## 当前验证状态

在 macOS 上完成并通过：

```bash
cd wx-bridge
npm test                                             # 42 项通过
python3 -m unittest discover -s collector/tests      # 11 项通过
```

覆盖：配置 fail-closed、@ 检测、上下文、Markdown 清理/分条、每群串行与限速、DSH 会话创建/not-found 重建、preset 安装与工具守卫、wxauto 消息归一化、Node ↔ Python 采集端真实 HTTP 联调。

**尚未在真实 Windows 微信 + 真实 DSH 上跑过**（本次开发环境没有 Windows 和 DSH 模型）。真机验收清单见文末。

## 前置条件

| 组件 | 要求 |
| --- | --- |
| 操作系统 | Windows 10/11 或 Windows Server（需要交互式桌面会话） |
| 微信 | 3.9.11.17（与本仓库归档 wxauto 匹配） |
| Python | 3.10–3.12 建议；安装仓库根目录 wxauto 的依赖 |
| Node.js | ≥ 22.13（推荐 22 LTS 或 24） |
| DSH | 0.2.x，`dsh web` 可访问（默认 `http://127.0.0.1:3080`），且已配置可用模型 |
| 网络 | 微信、DSH、collector、bridge 在同一台 Windows 主机上最省事 |

> 云主机注意：wxauto 依赖桌面 UI。RDP 断开后交互式会话可能被挂起，UI Automation 会失效。需要保持控制台会话/自动登录，或用云厂商的 VNC 控制台维持会话。不要把微信窗口锁屏。

## 目录

```
wx-bridge/
  src/                  Node 桥接
    bridge.js           主循环：白名单、@、去重、限速、上下文、发送
    proactive.js        没人 @ 时的主动插话判定与提示词（默认关闭）
    stickers.js         表情包白名单、索引粗筛与输出协议
    vision.js           群图片元数据与内联图片校验
    dsh-agent.js        DSH 会话管理 + turn 文本收集 + 自动拒绝工具审批
    config.js           配置加载与 fail-closed 校验
    vendor/             vendored 自 qq-bridge 的 MIT DSH 客户端
  collector/            Windows 采集端（Python）
    wx_collector.py     启动入口
    server.py           单 UI 线程 + HTTP API + 长轮询
    backends/           wxauto 后端 / fake 后端
  preset/wx-chat.preset.yml   DSH 无工具 preset
  scripts/install-preset.mjs  写入 DSH profile 的 cordis.patch.yml
  scripts/check-vision.mjs    验证 DSH 是否真能读图（一条命令一次探针）
  scripts/label-stickers-minimax.py  本地 MiniMax 视觉打标，产出 sticker-index.json
  scripts/build-sticker-library.py   按标签整理可部署曲库（过滤不安全/被拒审/命中剔除词）
  scripts/caption-stickers.mjs       在服务器上用 DSH 打标的备选实现
  deploy/watchdog.ps1             服务器侧每分钟看门狗：断线重挂 console、拉起守护脚本
  test/                 Node 测试
  collector/tests/      Python 测试
  config.example.json
  start.bat
```

## Windows 快速开始

下面的命令都在 `wx-bridge` 目录执行（仓库根目录是 `wxauto-ben`）。

### 1. 准备微信

1. 安装 **微信 3.9.11.17**（本仓库根目录 README 的归档说明里有一个 3.9.X 安装包链接，请自行判断来源可信度）。
2. 登录一个专门用于测试的微信号，打开微信主窗口。
3. 在微信设置里打开群聊的 **“显示群成员昵称”**；否则群消息拿不到发送人。
4. 保持微信窗口打开、桌面会话不锁屏。

### 2. 安装 Python 依赖

```bat
cd wx-bridge
python -m pip install -r collector\requirements.txt
```

如果 Python 3.10+ 的 `comtypes`/`pywin32` 安装失败，先升级 pip：

```bat
python -m pip install --upgrade pip
```

### 3. 安装 Node 依赖

```bat
cd wx-bridge
npm install
```

### 4. 安装 DSH 的无工具 preset

先确认 `dsh web` 已经至少启动过一次（会生成 `%USERPROFILE%\.dsh\profiles\web\cordis.patch.yml`），然后：

```bat
node scripts\install-preset.mjs --profile web
```

脚本会：

- 读取 `preset/wx-chat.preset.yml`；
- 把 `wx-chat` preset 作为一条 `@deepseek-ai/dsh-agent-preset` 行，插入 profile 的 `cordis.patch.yml`；
- 只替换自己的 marker 区块，保留你其它 patch 内容；
- 写之前备份，写完做 YAML 校验。

装完 **必须重启 `dsh web`**。然后在 DSH 里确认 preset 列表里有 `wx-chat`（显示名「微信群聊助手」）。

这个 preset 的关键点：**不挂载任何本地工具**（没有 bash、文件读写、子代理、MCP），并且额外挂了一个 `wx-tool-guard` **执行期守卫**：即使 DSH 的 profile/全局层给会话继承了 `read_mcp_resource`、`working_directory`、dev_* 或用户自挂的 MCP 工具，任何工具调用都会在 `tools.guard` 层被拒绝。安装脚本会把 `preset/wx-bridge-tool-guard.mjs` 复制到 profile 目录并在 preset 里引用它。

不要为了“让它能查资料”而换成带工具的 preset。这个桥接会自动拒绝工具审批，但审批拒绝不等于工具不会执行；真正的硬边界是上面的 guard。换 preset 前请自己确认工具面。

### 5. 配置

```bat
copy config.example.json config.json
```

生成采集端 token（同时填到 `config.json` 的 `wechat.token`）：

```bat
node -e "console.log(require('crypto').randomBytes(24).toString('hex'))"
```

至少修改：

- `wechat.groups`：要监听的群，**必须是微信里显示的完整群名**；
- `wechat.mentionAliases`：机器人在群里的 @ 文本，例如 `["@小助手"]`；
- `wechat.token`：上面生成的随机串（≥16 字符，不能用示例占位符）；
- `dsh.baseUrl` / `dsh.agentPreset`：默认 `http://127.0.0.1:3080` / `wx-chat`；
- `dsh.model` / `dsh.provider`：改成你 DSH 里实际可用的模型。

`wechat.collectorUrl` 的端口就是采集端监听端口，默认 `8765`；采集端只绑 `127.0.0.1`。

### 6. 启动

先确保微信已登录、DSH 已运行，然后：

```bat
start.bat
```

或手动两个窗口：

```bat
python -m collector.wx_collector --config config.json
node src\bridge.js config.json
```

桥接会等采集端最多 60 秒，所以启动顺序不严格。看到类似日志即成功：

```
[collector] url=http://127.0.0.1:8765
[collector] backend=wxauto nickname='...'
[bridge] 采集端已连接：...
[bridge] 已验证 DSH preset：wx-chat
[bridge] DSH 事件流已连接
```

### 7. 测试

在 `wechat.groups` 里的群 @ 机器人并带一句话。预期：

- 群里出现一条带 @发送者 的回复；
- `state/messages.jsonl` 同时出现 `direction: "in"` 和 `"out"`；
- `state/activity.jsonl` 出现 `trigger` / `reply`；
- DSH WebUI 的「微信群聊」工作区出现对应会话。

## 被 @ 了但机器人不回复？

wxauto 3.9 不提供结构化的 at 列表，只能看消息可见文本。最常见原因是 **@ 别名没配对**（账号昵称 ≠ 群昵称）。

1. 打开 `wx-bridge/state/messages.jsonl`，找到那条消息；
2. 看它的 `text` 字段，例如 `"@小助手\u2005在吗"`；
3. 把 `@` 后面、特殊空格前面的那段完整表情/昵称加入 `wechat.mentionAliases`；
4. 重启桥接。

其他排查点：

- 群名是否和微信左侧/群设置里完全一致（含空格、emoji）；
- 群名里是否包含 `.*+?[](){}^$|\` 这类正则字符（wxauto 内部用 `RegexName`，可能误匹配）；
- 微信是否显示了群成员昵称；
- 采集端窗口是否提示某个群 `AddListenChat` 失败；
- 是否被 `behavior.minReplyIntervalMs` / `maxRepliesPerMinute` 限速（看 `activity.jsonl` 的 `skip`）。

## 常见故障

| 现象 | 处理 |
| --- | --- |
| 采集端一启动就退出，提示无法导入 wxauto | 采集端必须在 Windows 上运行；先 `pip install -r collector\requirements.txt`，并确认微信 3.9.11.17 已登录 |
| 采集端日志出现 `AddListenChat 后聊天窗口不存在` | 群名与微信里不一致、微信窗口被关掉，或版本布局不匹配；改名/重开微信后重启采集端 |
| @ 了但没回复 | 见上一节：先看 `messages.jsonl` 的原始文本，补全 `mentionAliases`；再确认群在白名单且未被限速 |
| 桥接提示 `DSH 没有 preset "wx-chat"` | 运行 `node scripts\install-preset.mjs --profile web` 并重启 `dsh web`；注意 profile 名要和你启动的一致 |
| DSH 日志提示找不到 `wx-bridge-tool-guard.mjs` | 重跑安装脚本；确认 guard 文件在 profile 目录。不要删掉 preset 里的 guard 行来“绕过”启动错误 |
| `DSH 鉴权不可用` / 401 | 桥接和 `dsh web` 必须用同一个 Windows 用户（读取同一个 `~/.dsh/.credentials.yaml`）；或用 `dsh.authToken` 显式提供启动 token |
| 采集端返回 401 | `config.json` 的 `wechat.token` 与采集端实际 token 不一致；改完后同时重启两个进程 |
| 发送超时 | 微信 UI 被锁屏/弹窗挡住，或监听的群太多导致一次轮询过久；先减少群数量、解锁桌面 |
| 重复回复 | 检查任务管理器里是否有两个 `node src\bridge.js`；`state/bridge.lock` 会阻止第二个实例 |
| 回复的是很早以前的消息 | 停机/排队后恢复；`maxTriggerAgeMs` 会丢弃过期触发，检查 `activity.jsonl` 的 `skip` |
| DSH 回合超时 | 模型/网络太慢；调大 `dsh.turnTimeoutMs`，或换更快的模型 |
| 端口被占用 | 改 `wechat.collectorUrl` 的端口并同步 `--port`；采集端默认从 URL 推导端口 |

## 配置参考

### wechat

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `collectorUrl` | 采集端地址；端口即监听端口 | `http://127.0.0.1:8765` |
| `token` | 采集端 Bearer token；必填且 ≥16 字符 | 无 |
| `groups` | 群白名单；空数组拒绝启动 | 无 |
| `mentionAliases` | @ 别名数组；不带 `@` 会自动补 | 无 |
| `pollMs` | 采集端 UI 轮询间隔 | `800` |
| `pollWaitMs` | 桥接长轮询等待时间；0 表示短轮询 | `15000` |
| `requestTimeoutMs` | 采集端 HTTP 超时 | `30000` |
| `contextMessages` | 每个群保留的最近消息条数 | `20` |
| `contextMaxChars` | 上下文最大字符数 | `4000` |

### dsh

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `baseUrl` | DSH Web API | `http://127.0.0.1:3080` |
| `authToken` | 通常留空：客户端用 `~/.dsh/.credentials.yaml` 离线铸造 Cookie。它是 DSH 启动 token，不是模型 API key | 空 |
| `allowRemote` | 是否允许把 launch token 发往非回环地址 | `false` |
| `provider` / `model` / `reasoningEffort` | 会话模型 | `deepseek-official` / `deepseek-flash` / `max` |
| `agentPreset` | 必须是无本地工具的 preset | `wx-chat` |
| `verifyPreset` | 启动时通过 `agentPresets/list` 校验 preset 存在 | `true` |
| `allowDefaultPreset` | 允许空 preset（会落到 DSH 默认 preset，**危险**） | `false` |
| `workspaceTitle` | DSH WebUI 里的工作区名 | `微信群聊` |
| `sessionCwd` | DSH 会话工作目录；空则 `state/agents` | 空 |
| `turnTimeoutMs` | 等待一个 agent 回合的上限 | `120000` |
| `streamReconnectMs` | 事件流断开后的重连间隔 | `3000` |

### behavior

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `requireMention` | 是否只在 @ 时触发；设 false 会回复群里每条消息 | `true` |
| `replyWithMention` | 回复时是否 @ 发送者 | `true` |
| `minReplyIntervalMs` | 同群两次触发的最小间隔 | `1500` |
| `maxRepliesPerMinute` | 全局每分钟最多回复次数 | `12` |
| `maxTriggerAgeMs` | 超过这个年龄的 @ 不再回复（防止停机/排队后回旧消息）；0 关闭 | `300000` |
| `maxReplyChars` | 单条微信消息最大字符数，超出会分条 | `1200` |
| `globalConcurrency` | 同时调用 DSH 的群数上限 | `2` |
| `maxQueuePerGroup` | 每群排队上限，满了丢弃新触发 | `10` |
| `partDelayMs` | 分条发送之间的间隔 | `500` |
| `memoryMaxEntries` | 每群保留的长期记忆条数 | `1000` |
| `memoryResults` | 每次 @ 最多注入的相关记忆条数 | `6` |
| `memoryMaxChars` | 相关记忆最大字符数 | `1800` |
| `sendErrorNotice` | agent/发送失败时是否在群里提示 | `false` |
| `logAllMessages` | 是否把白名单群的全部消息写入 JSONL | `true` |

### stickers（可选，默认关闭）

让 agent 在正常聊天里按语境自己选一张本地图片配图，不需要用户说“发表情包”。

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `enabled` | 是否启用表情包 | `false` |
| `directory` | 图片目录，相对配置文件 | `stickers` |
| `autoLoad` | `enabled` 且 `items` 为空时自动扫描目录（仅一层，按文件名排序，最多 200 张 jpg/jpeg/png/gif）；`items` 非空时忽略 | `true` |
| `cooldownMs` | 同群配图间隔，`0` 表示不限制 | `0` |
| `items[].id` | 白名单 ID，`[A-Za-z0-9_-]`，模型只能返回它 | 必填 |
| `items[].file` | 相对 `directory` 的 jpg/jpeg/png/gif，≤5MB | 必填 |
| `items[].keywords` | 给模型看的用途关键词 | 必填 |
| `items[].description` | 更细的语境描述，可选，≤500 字 | 空 |
| `indexFile` | 视觉打标产出的 `sticker-index.json`（相对配置文件）；设了它就用索引当曲库，`autoLoad` 只在索引为空时兜底 | 空 |
| `shortlistLimit` | 每轮从索引里挑多少张候选喂给模型（1~80） | `20` |

`indexFile` 的搭配用法：`scripts/label-stickers-minimax.py` 在本地给图片生成 caption/keywords/mood/safe，`scripts/build-sticker-library.py` 过滤出可发布的一份（剔除 `safe:false`、被服务商拒审的、以及 `--drop 红包` 这类命中词），把 `stickers/` 和 `sticker-index.json` 一起放到服务器上，`directory` 指向前者、`indexFile` 指向后者。ID 由文件名决定，Node 与 collector 用同一套规则，改索引文件后无需重启（每轮自动热更新）。大曲库不会整份塞进提示词：本地先按关键词 + 描述二元字组粗筛，只把 `shortlistLimit` 张候选交给模型定夺。

collector 会再校验一次扩展名、magic、大小、真实路径和目录越界；模型返回的 ID 不在白名单时只发文字。`SendFiles` 是文件发送接口，微信端是否显示为原生表情、GIF 是否保留动画需要真机确认。

### vision（可选，默认关闭）

被 @ 的人在此前 `contextWindowMs` 内发过图片时，把图片作为 DSH 的多模态输入一起投递。

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `enabled` | 是否启用图片识别 | `false` |
| `contextWindowMs` | 往前找图片的时间窗 | `120000` |
| `maxImages` | 一次最多附带几张（1~4） | `2` |
| `maxBytes` | 单张原图上限 | `5242880` |

图片只从 wxauto4 下载，落盘后立即清理；会剥离 EXIF、限制像素并重新编码。当前模型是否真的支持图片要看 DSH 的模型能力，缺失时按普通文本降级并提示。群图片属于敏感数据，启用前请自行评估。

### proactive（可选，默认关闭）

没人 @ 的时候，按概率主动接一句。默认档位是「保守」：只在群里确实有人刚聊、且离上次说话足够久时才摇号。

| 字段 | 说明 | 默认 |
| --- | --- | --- |
| `enabled` | 是否允许主动插话 | `false` |
| `chance` | 每条符合条件的群消息触发插话判定的概率（0~100，摇号在**所有**便宜闸门之后） | `12` |
| `minIntervalMs` | 同群两次主动插话的最小间隔，也是「刚回过别人」后的静默期（60000~3600000） | `360000` |
| `maxPerHour` | 同群每小时最多主动说几次（1~20） | `3` |
| `activityWindowMs` | 多久内必须有人说过话（10000~600000） | `120000` |
| `minRecentMessages` | 上述窗口内至少几条群友消息（1~20） | `2` |
| `groups` | 只在这些群里允许插话；空数组表示所有监听群 | `[]` |

行为要点：主动回合不 @ 任何人；提示词把「沉默」设成默认答案，模型返回空 `text` 就什么都不发；只有严格按 `{"text":"...","stickerId":...}` 协议返回且 `text` 非空才会发送——散文、坏 JSON、多余字段一律当沉默，绝不把提示词或控制串漏进群。主动回合同样可以按语境带一张表情（走同一份白名单与索引）。`state/activity.jsonl` 里会看到 `proactive-trigger` / `proactive-sent` / `proactive-declined` / `proactive-skip`（`reason` 为 `chance` 或 `hourly-cap`），用来回看它为什么说话或为什么憋住。自己发的消息不计入活跃度，有 @ 回合在排队或正在处理时也不会插队。

### stateDir

`state/` 保存：

- `sessions.json`：群名 → DSH sessionId。文件损坏会拒绝启动，避免静默丢上下文；
- `messages.jsonl`：白名单群里所有收发文本（敏感数据，5MB 轮转）；
- `activity.jsonl`：触发、回复、跳过、失败；
- `bridge.log`：桥接运行日志；
- `bridge.lock`：单实例锁。

想彻底重置上下文：停止桥接，把 `state/sessions.json` 移到别处，再启动。旧 DSH 会话仍会留在「微信群聊」工作区，可在 DSH 里归档。

## 行为与安全边界

- **只监听白名单群**。群里的任何成员 @ 机器人都会触发（当前没有成员白名单）；如果担心 token 消耗，可以后续在 `handleEvent` 里加 `allowedSenders`。
- **只在 @ 时回复**（`requireMention=true`），非 @ 消息只进入上下文和日志。
- **每群串行**：同一群不会同时跑两个 DSH 回合；不同群最多 `globalConcurrency` 个并行。
- **去重**：采集端消息 id + 2.5 秒内相同文本双重去重，防止 wxauto UI 重绘导致重复回复。
- **长期记忆**：白名单群消息按群写入 `state/memory.jsonl`；收到 @ 时按当前内容检索相关消息，和最近上下文一起交给 agent。记忆不会跨群混用。
- **限速与过期**：同群最小间隔 + 全局每分钟上限 + 每群队列上限；超过 `maxTriggerAgeMs` 的旧 @（例如停机后恢复、排队过久）直接丢弃，避免回一条几小时前的消息。
- **无本地工具**：`wx-chat` preset 不挂载任何本地工具；即使 agent 被诱导，也没有命令/文件能力。桥接还会自动拒绝意外的工具审批和提问。
- **回复审计**：把 Markdown 清成纯文本；把 `@所有人` / `@all` 改成全角，避免模型输出造成全体通知的歧义；超长回复分条，只有第一条 @ 发送者。
- **提示注入仍可能影响回复内容**：群友可以让 agent 说出不合适的话（在群聊范围内）。这是聊天机器人的固有限制，不是本桥接能完全消除的。不要给这个 preset 加工具。

## 已知限制

- wxauto 3.9 是轮询 + UI 自动化：发送时会切换窗口焦点；微信窗口被关掉/锁屏/RDP 会话断开都可能失效。
- 每个被监听群需要一个独立聊天窗口；群很多时不稳定，建议先控制在 10–20 个以内。
- 群名就是唯一标识：群改名会导致监听失败，且 `sessions.json` 会新建会话（旧会话留在 DSH）。
- 同名群、群名含正则特殊字符、成员昵称含特殊字符时，@ 和窗口匹配可能不准确。
- 没有引用回复；回复是「@发送者 + 文本」。
- 引用消息里如果出现机器人的 @ 别名，可能被当成直接 @ 触发（wxauto 3.9 没有结构化的 at 信息）。
- 图片/语音/文件默认仍以 `[图片]` 等占位文本进入上下文；只有显式开启 `vision` 且发送者在此前窗口内发过图时，图片才会作为 DSH 多模态输入。语音内容不转写。
- DSH 事件流在回合中途断开时，这一轮回复可能丢失（等待器超时）；下一条 @ 会恢复正常。qq-bridge 对断线回合有更复杂的重放，本 MVP 没搬。
- 没有 Web 控制台；排障靠 `state/*.log` 和 `state/*.jsonl`。

## 真机验收清单（Windows）

按顺序做一遍，任何一步失败都先看 `state/bridge.log`、`state/activity.jsonl` 和采集端窗口：

1. 白名单群 @ 机器人 → 收到一条带 @ 发送者的回复，DSH 工作区出现会话。
2. 非白名单群 @ → 无回复、无 DSH 会话。
3. 白名单群普通消息 → 无回复，但 `messages.jsonl` 有记录，下一条 @ 的 prompt 上下文里能看到它。
4. 2 秒内连续两次 @ → 只回复一次（`minReplyIntervalMs`）。
5. 关掉某个群的独立聊天窗口 → 30 秒内采集端重新打开；再 @ 能回复。
6. 停止采集端 → 桥接打印重试但不崩；重启采集端 → 不补发停机期间的旧 @（从当前游标开始）。
7. 让 agent 返回超过 `maxReplyChars` 的内容 → 分多条发送，只有第一条 @。
8. 停掉 DSH → 桥接记录 agent 错误、不在群里乱发；DSH 恢复后下一条 @ 正常。
9. 检查 `messages.jsonl` 里的原始 @ 文本，确认 `mentionAliases` 与实际一致。
10. 用测试号观察 1–2 天，确认没有触发微信风控（不要用主号直接上）。

## 本机联调（无 Windows）

macOS/Linux 可以用 fake 后端跑通整条 HTTP/调度链路：

```bash
cd wx-bridge
npm install
npm test
python3 -m unittest discover -s collector/tests
```

手动起一个 fake 采集端：

```bash
python3 -m collector.wx_collector --backend fake --groups 测试群 \
  --token 0123456789abcdef0123456789abcdef --port 0 --allow-inject
```

它会在 stdout 打印 `[collector] url=http://127.0.0.1:<port>`。然后用 curl 注入消息：

```bash
curl -s http://127.0.0.1:<port>/inject \
  -H 'authorization: Bearer 0123456789abcdef0123456789abcdef' \
  -H 'content-type: application/json' \
  -d '{"group":"测试群","sender":"张三","text":"@小助手 在吗"}'
```

fake 后端不会真的发微信；它只把发送记录写到 stdout/`--send-log`，适合测桥接的过滤、限速和 DSH 会话逻辑。

## 与 qq-bridge 的差异

- 只做「群白名单 + @ 触发 + DSH 回复」，没有仿真群友、黑话学习、语音、Web 控制台和花费看板；表情包和视觉理解是可选功能，默认关闭。
- 工具面更窄：`wx-chat` 用 guard 拒绝**所有**工具；qq-bridge 的 `qq-chat` 允许一组 QQ MCP 工具。这里不打算让模型在群里执行动作。
- 审批/提问：自动拒绝/空答案，不转发到微信；群聊里不做审批交互。
- 会话映射、白名单、限速、去重、上下文和 DSH 协议客户端的思路与 qq-bridge 一致。

## 可选扩展

- **成员白名单**：在 `Bridge.handleEvent` 里加 `allowedSenders`，只允许指定群友触发。
- **引用回复**：collector 增加 `/quote` 端点，用 wxauto 的 `FriendMessage.quote()`；需要把消息 control 和 id 关联起来。
- **语音**：TTS 生成音频文件相对容易，微信原生语音气泡是否可用要另测。
- **更好的 @ 检测**：升级到维护中的 wxauto+ / wxautox（付费，支持较新微信），或改用 hook 型协议库（WeChatFerry 等，封号风险更高）。换后端时只需要改 `collector/backends/`。

## 上游与许可

- `src/vendor/dsh-client.js` vendored 自 [qq-bridge](https://github.com/Derpyu520/qq-bridge)（MIT），保留原始版权；
- `src/reply.js` 的 Markdown 清理/分条思路、`preset/wx-bridge-tool-guard.mjs` 的执行期工具限制思路也改编自 qq-bridge（MIT）；见 [NOTICE.md](NOTICE.md)。
- 本仓库根目录的 `wxauto` 是 Cluic 的归档版，其 README 的免责声明仍然适用。
- DSH / DeepSeek Harness 为 MIT（以官方仓库为准）。
