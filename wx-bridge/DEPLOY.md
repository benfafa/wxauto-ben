# 部署与运维手册（Windows 服务器）

这份文档对应实际跑起来的那台机器：腾讯云轻量 `lhins-l3kkwlkb`（Windows Server 2025，
4C4G），项目在 `C:\wxb`。内容都是踩过坑之后写下来的，照做即可，不需要再猜。

配置字段的逐项说明在 [README.md](README.md) 的「配置参考」，这里只讲部署和运维。

---

## 1. 进程拓扑

```
微信 4.1.8.107（Administrator 桌面会话）
  └─ collector  python -m collector.wx_collector --config config.json   :8765
        └─ 单 UI 线程，wxauto4 41.1.7，UI 自动化 + HTTP API
  └─ bridge     node src/bridge.js config.json
        ├─ 长轮询 :8765/poll → 过滤 @ / 主动插话 → 组提示词
        └─ DSH web  :3080（node，@deepseek-ai/dsh）→ 模型 → 回：8765/send
```

三件事决定了它能不能跑：

1. **微信必须开着主窗口**（不是托盘、不是登录页）。UI 自动化没有窗口就没有一切。
2. **Administrator 会话必须有可渲染的桌面**。RDP 客户机一关，会话就没有桌面了，见第 7 节。
3. **collector 必须在微信同一个会话里**（session 2 / 控制台），不能在 session 0。

---

## 2. 服务器前置

| 项 | 值 | 说明 |
| --- | --- | --- |
| 系统 | Windows Server 2025 Datacenter | 桌面会话是 Administrator |
| Python | `C:\Python311\python.exe` | 装 `collector/requirements.txt`；wxauto4>=41.1.7 |
| Node | `C:\Program Files\nodejs\node.exe` | ≥ 22.13；`npm i` 装 DSH 客户端依赖 |
| DSH | `C:\npm-global\node_modules\@deepseek-ai\dsh` | 全局装，`dsh web --no-open` |
| 微信 | 4.1.8.107（固定版本） | 与 wxauto4 41.1.7 匹配，别随便升级 |
| 账户 | Administrator 已登录且保持登录 | 计划任务用 Interactive 身份 |

> 安装包别放在 `C:\` 根下再解压出 `python.exe`——PATH 会优先命中它，导致后续 pip/python
> 命令指向错误位置（这个坑真的把 Python 卸过一次）。

---

## 3. 目录、计划任务与日志

```
C:\wxb\
  src\ collector\ preset\ scripts\ deploy\    代码
  config.json                                  唯一配置（不入库）
  sticker-index.json                           表情索引（798 条）
  stickers\                                    上线用的表情图片
  sticker-lab\                                 878 张原始素材（只读，供重拼）
  state\                                       sessions.json / messages.jsonl / activity.jsonl / memory.jsonl
  logs\                                        bridge.log / collector.log / stack.log / watchdog.log / deploy-stickers.log
  deploy\start-stack.ps1                       守护脚本（循环拉起 微信/DSH/collector/bridge）
  deploy\watchdog.ps1                          每分钟：断线重挂 console + 拉起守护脚本
  deploy\install-sticker-library.ps1           表情库装机脚本
```

| 计划任务 | 身份 | 触发 | 作用 |
| --- | --- | --- | --- |
| `wxb-start` | Administrator / Interactive | AtLogOn | 启动 `start-stack.ps1`（守护循环） |
| `wxb-watchdog` | SYSTEM / Highest | 每 1 分钟 | 会话断开→`tscon /dest:console`；守护脚本不在→`Start-ScheduledTask wxb-start` |

三层自愈，从下往上兜底：`start-stack.ps1` 每 30 秒检查四个进程 → `wxb-watchdog` 每分钟检查守护脚本和桌面会话 → Kernel 级重启后由 `wxb-start` 的 AtLogOn 触发（前提是配了自动登录，没配就需要人登一次）。

---

## 4. 首次部署

1. **装运行时**：Python 3.11 到 `C:\Python311`、Node 到默认路径、`npm i -g @deepseek-ai/dsh`。
2. **放代码**：把仓库的 `wx-bridge` 解压到 `C:\wxb`（不要带 `node_modules`、`state`）。
3. **装依赖**：
   ```powershell
   & 'C:\Python311\python.exe' -m pip install -r C:/wxb/collector/requirements.txt
   cd C:\wxb; npm install
   ```
4. **写配置**：`copy config.example.json config.json`，至少改 `wechat.token`（随机串）、
   `wechat.groups`、`wechat.mentionAliases`（**用群昵称，不是账号昵称**）、`dsh.*`。
5. **装 preset**：`node scripts/install-preset.mjs`，它会往 DSH profile 的 `cordis.patch.yml`
   写 `wx-chat` 无工具 preset；bridge 启动时会校验（`dsh.verifyPreset`）。
6. **登录微信和 DSH**：微信扫码登录后**保持主窗口打开**；DSH 首次要在 `:3080` 登录一次。
7. **装两个计划任务**（TAT / 管理员 PowerShell）：
   ```powershell
   # start-stack：登录后自动跑
   $a = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' `
        -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\wxb\deploy\start-stack.ps1'
   $p = New-ScheduledTaskPrincipal -UserId 'Administrator' -LogonType Interactive -RunLevel Highest
   Register-ScheduledTask -TaskName 'wxb-start' -Action $a -Principal $p `
        -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Force
   # watchdog：看门狗，注册命令见 deploy/watchdog.ps1 顶部注释
   ```
8. **表情库**：把 `sticker-index.json` 和图片放到位，或者用装机脚本从 `sticker-lab` 重拼（见 5.2）。

验证顺序（任何一步不对就先停下）：

```powershell
$cfg = Get-Content 'C:/wxb/config.json' -Raw -Encoding UTF8 | ConvertFrom-Json
$h = @{ authorization = ('Bearer ' + $cfg.wechat.token) }
Invoke-RestMethod 'http://127.0.0.1:8765/health' -Headers $h | ConvertTo-Json -Compress
& 'C:\Python311\python.exe' 'C:/wxb/scripts/check-sticker-catalog.py' 'C:/wxb/config.json'
& 'C:\Program Files\nodejs\node.exe' 'C:/wxb/scripts/check-sticker-config.mjs' 'C:/wxb/config.json'
```

`health` 里 `ok=true`、`backendHealth.listening` 含目标群、`selfNickname` 是机器人账号，
两侧自检的 ids 数量一致，才算部署成功。

---

## 5. 日常更新

### 5.1 只改代码

1. 本地跑测试：`npm test` + `python -m pytest collector/tests -q`。
2. 打包（排除 `node_modules/state/config.json`）后上传到服务器，覆盖 `src/ collector/ preset/ scripts/`。
3. 重启两个进程（守护脚本会在 30 秒内重新拉起）：
   ```powershell
   Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'node.exe' -and $_.CommandLine -like '*bridge.js*') -or ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*wx_collector*') } |
     ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
   ```
4. 看 `logs/bridge.log` 有没有 `采集端已连接：微信 X，监听群 N 个`。

### 5.2 更新表情库 / 索引

服务器上已经有 `sticker-lab` 的 878 张原图，**不要重传图片**，只传索引：

1. 本地：`python scripts/build-sticker-library.py --index <打标索引> --src <原图目录> --out <发布目录> --drop 红包`
2. 把 `release/` 打进 zip，连同代码一起上传（现在的 `sticker-release.zip` 就是这样）。
3. 装机脚本按索引 + sha256 现拼曲库，多余文件只列进 `logs/sticker-prune.txt`，加 `-Prune` 才删：
   ```powershell
   powershell -File C:\wxb\deploy\install-sticker-library.ps1 -Zip C:\sticker-release.zip -LabDirectory C:\wxb\sticker-lab
   ```
4. 想删掉曲库里不该有的图（比如某张红包），先看 `sticker-prune.txt` 的清单，确认后再带 `-Prune` 跑一次。

### 5.3 改配置

改完 `config.json` 后**必须重启 bridge 才生效**（配置在启动时校验，fail-closed：写错直接退出，
不会带病运行）。表情索引是例外，它每轮热更新，不用重启。

---

## 6. 配置要点（默认值都偏保守）

- `wechat.token`：只在本机回环上用的 Bearer，≥16 位随机串；不要复用别处的口令。
- `wechat.mentionAliases`：群里 @ 出来的是**群昵称**，常见坑是填了账号昵称导致永远不触发。
- `behavior.requireMention`：`true` 时只回 @；主动插话是独立开关，不受它影响。
- `stickers`：`indexFile` 指向索引，`shortlistLimit` 控制每轮喂给模型多少张候选（默认 20），
  大曲库靠本地粗筛，不会把 800 条塞进提示词。
- `vision`：`maxImages` 1~4，`maxBytes` 单张上限；图片读完即删，剥 EXIF。
- `proactive`：默认关闭。开启后受四道闸门约束——最近 `activityWindowMs` 内有人聊、
  同群 `minIntervalMs` 内不重复、每小时 `maxPerHour` 条、最后才按 `chance` 摇号。
  模型返回空 `text` 就是沉默，这是设计上的默认结果。

---

## 7. 排障对照表

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `bridge.log` 一直 `采集端请求失败：fetch failed` | collector 没起来 | 看 `collector.log`；确认微信主窗口在；确认守护脚本活着 |
| collector 报 `未找到已登录的客户端主窗口` | 微信在托盘/登录页，或桌面不可用 | 在桌面上把微信窗口点开（必要时重新扫码） |
| collector 报 `_ctypes.COMError -2147220991` | 会话没有可渲染的桌面（RDP 关了/锁了） | 跑 `deploy/watchdog.ps1` 把会话挂回 console；watchdog 正常情况下每分钟自动做 |
| collector 报 `'ProfileWnd' object has no attribute 'topui'` | 微信窗口尺寸异常（RDP 换分辨率后比屏幕还高） | 把微信窗口调回屏幕内；必要时重启微信 |
| `/send-sticker` 回 `图片未确认送达` | 发送后没看到自己的新消息 | 4.x 的发送者在 `attr`，不是 `type`；确认跑的是修过的版本，看 `activity.jsonl` 的 `sticker-error` |
| `/send-sticker` 回 504 | UI 线程被阻塞（SendFiles/GetAllMessage 卡住） | 重启 collector；如果反复出现，检查微信是否弹了模态框 |
| TTS 回 502 `certificate verify failed: self-signed certificate in certificate chain` | 这台机器的 Python 默认 CA 存储认不了 `api.minimaxi.com` 的链（curl/node 都正常） | 已在 `collector/tts.py` 里显式用 `certifi` 的根证书；确认 `C:\Python311` 里装了 certifi |
| TTS 回 502 `TTS 服务拒绝：2054` | 音色 ID 不存在 | 用 `/v1/get_voice` 查准确 ID（注意是 `qiaopi_mengmei` 这类下划线命名） |
| 表情一张都不发，`stickerId` 总是 null | 索引为空 / 全被 `safe:false` 滤掉 | 跑 `check-sticker-config.mjs`，看 `items` 与 `missingFiles` |
| 模型答非所问、把提示词念出来 | preset 没装上或模型被换 | 跑 `scripts/install-preset.mjs`，确认 `dsh.agentPreset=wx-chat` |
| 主动插话一直不触发 | 闸门没过（消息不够/间隔不够/摇号没中） | 看 `activity.jsonl` 的 `proactive-skip.reason` |
| TAT 命令超时 | `执行命令` 只有 60 秒 | 长任务一律写成 .ps1，用 `Start-Process` 分离跑 |
| TAT 输出中文乱码 | 控制台 GBK | 让程序写日志文件，再用 `Get-Content -Encoding UTF8` 读 |

TL;DR 的排查顺序：`health` → `collector.log` 最后 20 行 → 微信窗口在不在 → 会话在不在 console。

---

## 8. 安全

- `config.json` 含 token 和 DSH 凭据，**不入库**；`state/`、`logs/` 也不入。
- `messages.jsonl` 是群里全部消息的明文，属于敏感数据；磁盘占用和访问权限自己评估。
- MiniMax 打标的 Key 只放本地（`%USERPROFILE%\.wx-bridge-minimax-key`），不上服务器、不进索引。
- `/send-sticker` 只接受 `{group, stickerId}`，路径永远不出现在 HTTP 里；collector 会再校验
  扩展名、magic、大小、真实路径和目录越界。
- 不要把 Administrator 密码写进脚本、日志或提交记录。

---

## 9. 回滚

代码回滚：`git checkout <上一个 tag/commit>` 后重新打包覆盖，重启两个进程（第 5.1 步）。

配置回滚：装机脚本每次会覆盖 `stickers` 段；`proactive`/`vision` 段由人手工改，
建议改动前 `copy config.json config.json.bak`。

表情库回滚：`install-sticker-library.ps1` 只会新增/覆盖索引里的文件，不会删额外的；
想回到旧索引，重传旧的 `sticker-index.json` 再跑一次脚本即可（多余文件仍在 `sticker-lab`）。
