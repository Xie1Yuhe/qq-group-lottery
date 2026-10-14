# NapCat 接入指南（给抽奖工具提供实时群消息）

这份指南带你从零装好 NapCat，并让抽奖工具通过它实时读取群消息、自动把开奖结果发到群里。

**为什么推荐这条路**：不用再导出聊天记录，不受本地缓存限制，一次配置长期可用。

---

## ⚡ 你这台机器的当前进度（已替你完成的部分）

**NapCat 已经安装并成功启动了**，以下是本机的实际信息，照着用即可：

| 项目 | 值 |
|---|---|
| 安装目录 | `D:\NapCat` |
| NapCat 版本 | **v4.18.38** |
| 自带 QQ 版本 | **9.9.33.52230**（独立安装，**你原来的 `D:\QQ` 完全没被动过**） |
| Shell 目录 | `D:\NapCat\NapCat.52230.Shell` |
| 启动方式 | 双击 `D:\NapCat\NapCat.52230.Shell\napcat.bat` |
| WebUI 地址 | **http://127.0.0.1:6099/webui?token=7bcd704a2b79** |
| WebUI token | `7bcd704a2b79`（存在 `versions\9.9.33-52230\resources\app\napcat\config\webui.json`） |
| 配置文件目录 | `D:\NapCat\NapCat.52230.Shell\versions\9.9.33-52230\resources\app\napcat\config` |
| 控制台日志 | `D:\NapCat\napcat-console.log` |

NapCat 启动时识别出机器上可快速登录的三个账号：

```
1. 10086 示例账号C
2. 10001 示例账号A
3. 10002 示例账号B
```

**还差两步（必须你本人操作，涉及你的账号，我无法代做）**：

1. **登录 QQ** → 打开上面的 WebUI 地址，扫码或快速登录
2. **启用 HTTP 服务端** → 见 [第 4 节](#4-配置-onebot-http-服务关键一步)

完成后跑一次诊断确认：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\napcat_check.ps1 -Group 你的群号
```

---

## 0. 先说清楚三件事

1. **需要一个小号**。NapCat 要用一个 QQ 号登录后才能在群里收发消息。用你的主号也可以（协议合规），
   但更稳妥的做法是**用不常用的小号当机器人**，把它拉进群即可——万一账号被风控，损失最小。
2. **这是第三方客户端**。OneBot 生态都是通过模拟 QQ 客户端实现，**理论上存在账号被限制的风险**，
   腾讯官方不提供也不背书。请自行权衡，不要用于刷屏、营销等可能触发风控的行为。
3. **不要在公网暴露**。NapCat 的 WebUI 和 OneBot 端口只监听本机就够了，本指南的配置都按本机使用来写。

---

## 1. 先选安装方式（针对你的机器给的建议）

你的机器上装的是 **QQ 9.9.31**（`D:\QQ`），而最新版 NapCat 适配的是较新的 QQ 版本。
所以**不要用 Framework 版本**（它要注入你现有的 QQ，版本不匹配会失败）。

| 方式 | 适用 | 建议 |
|---|---|---|
| **一键安装（推荐）** | Windows amd64，**无需预先安装 QQ** | ⭐ 用它。自带配套 QQ，不影响你现有的 QQ 和聊天记录 |
| Shell 手动安装 | 自己已有匹配版本的 QQ | 版本不对会起不来，先别用 |
| Framework | 想复用现有 QQ 安装 | 你的 9.9.31 版本偏旧，容易失败，不推荐 |

**为什么推荐一键安装**：它会装一份独立的 QQ 给 NapCat 用，你原来的 `D:\QQ` 完全不动，
出问题删掉目录即可，零污染。

> 顺带说一句：NapCat 官方文档里手动安装那节写着「先安装 QQ」，而一键安装那节明确写了「无需预先安装 QQ」，
> 两者说法不一致。**以一键安装为准**，这条路不依赖你现有 QQ 的版本。

---

## 2. 安装（✅ 本机已完成）

> **这一步在你这台机器上已经做完了**：zip 已下载、CRC 校验通过、安装器已运行完毕，
> 产出 `D:\NapCat\NapCat.52230.Shell`（约 2.3 GB）并自带 QQ 9.9.33.52230。
> 下面记录当时的做法，供你以后在别的机器上复现，或需要重装时参考。

### 2.1 下载

最新版是 **v4.18.38**（2026-10-12 发布）。这个文件**只有 0.9 MB**，它是下载器而非完整包：

**`NapCat.Shell.Windows.OneKey.zip`**

- 官方直链：
  `https://github.com/NapNeko/NapCatQQ/releases/download/v4.18.38/NapCat.Shell.Windows.OneKey.zip`
- 或者打开 Releases 页面自己选：<https://github.com/NapNeko/NapCatQQ/releases/latest>

> 已验证：你这台机器**能直连 github.com 和 api.github.com**（实际下载走的是官方 CDN
> `release-assets.githubusercontent.com`，994 KB 用了 0.7 秒），所以不需要加速代理。
> 万一下载卡住，在链接前面加 `https://ghproxy.net/` 也能通。

### 2.2 解压并安装

1. 把 zip 解压到一个**固定目录**（含中文/空格的路径可能有坑，我用的是 `D:\NapCat`）
2. 双击 `NapCatInstaller.exe`
3. 等它自己下载并部署配套 QQ（约 2.3 GB，实测 3~4 分钟）
4. 完成后得到 `NapCat.52230.Shell` 目录

> 安装器**不需要管理员权限**（清单里是 `asInvoker`），当前用户直接运行即可。
> 安装过程中目录体积会先卡在 3.2 MB 一段时间（在下载），然后一次性跳到 1.5 GB 以上，
> **看起来像卡住其实是正常的**，别急着关掉。

### 2.3 启动

双击 **`D:\NapCat\NapCat.52230.Shell\napcat.bat`** 即可。

> 注意：一键安装版**没有** `launcher.bat`（那是手动安装版才有的），启动脚本就叫
> `napcat.bat`。同目录还有 `napcat.quick.bat`（快速登录，需要先手动登录过一次）。

也可以用命令行启动并把日志留档（我就是这么做的）：

```powershell
cd /d D:\NapCat\NapCat.52230.Shell
.\NapCatWinBootMain.exe > D:\NapCat\napcat-console.log 2>&1
```

---

## 3. 登录 QQ（WebUI）

启动后，**控制台日志里会打印 WebUI 地址和 token**，长这样：

```
[info] [NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxxxxx
```

直接用浏览器打开这个带 token 的地址（`token` 别丢，丢了就去 `config\webui.json` 里找）。

登录流程：

1. 进入「QQ 登录」，有两种方式：
   - **快速登录**：直接选 NapCat 列出的账号（本机识别到 10086 / 10001 / 10002）。
     这条路需要该账号在**本机 NapCat 里成功登录过一次**之后才可用
   - **扫码登录**：点 **QRCode**，用手机 QQ 扫码（**首次登录建议用这个**）
2. 扫码后 **token 会刷新一次**——去手机 QQ 的「自身消息」或 NapCat 控制台里拿新的 token
3. 再次进入 WebUI 时会**强制要求你改密码**，不改的话大部分功能会被禁用
4. 密码改完后就能正常操作了

> 手机 QQ 扫码时如果提示「登录设备异常」，按提示在手机上确认即可。
>
> 建议**用不常用的小号**当机器人（比如上面那个 10086），万一被风控损失最小。

---

## 4. 配置 OneBot HTTP 服务（关键一步）

抽奖工具是通过 **HTTP 服务端**调用 NapCat 的，必须开这个。

1. WebUI 里进入 **「网络配置」**
2. 点 **「新建」** → 选择 **HTTP 服务端**
3. 填写：
   - **名称**：随便，比如 `lottery`（不能和其它配置重名）
   - **端口**：`3000`（默认值，记住它）
   - **主机**：`127.0.0.1`（只给本机用，别填 `0.0.0.0`）
   - **Token**：留空最简单；如果要填，**必须和抽奖工具里填的一致**
   - **消息格式**：`array`
4. ⚠️ **一定要勾选「保存时启用」**，或者保存后手动把它打开
5. 保存

配置文件会写到本机的
`D:\NapCat\NapCat.52230.Shell\versions\9.9.33-52230\resources\app\napcat\config\onebot11_<你的QQ号>.json`
（一键安装版的配置藏在 `versions\<版本号>\resources\app\napcat\config` 下，**不是** Shell 根目录的 `config`），内容类似：

```json5
{
  "network": {
    "httpServers": [
      {
        "name": "lottery",
        "enable": true,      // ← 必须是 true，这是最容易漏的地方
        "port": 3000,
        "host": "127.0.0.1",
        "messagePostFormat": "array",
        "token": ""
      }
    ]
  }
}
```

> **最常见的失败原因就是第 4 步没做**：配置建好了但 `enable` 是 `false`，
> 端口不监听，抽奖工具自然连不上。

---

## 5. 验证（用我写的诊断脚本）

装好登录完，在项目目录运行。**注意第一行是必须的**——Windows 默认禁止运行 `.ps1` 脚本：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\scripts\napcat_check.ps1 -Group 你的群号
```

> 如果看到「因为在此系统上禁止运行脚本」，就是漏了上面第一行。
> 它只对当前这个 PowerShell 窗口生效，不会改动系统全局设置。
> 或者干脆绕过策略直接跑：
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\napcat_check.ps1 -Group 你的群号
> ```

它会自动完成这些事，并告诉你问题出在哪：

| 检查项 | 说明 |
|---|---|
| 找安装目录 | 扫常见位置，读出版本号 |
| 读配置 | 从 `webui.json` 取 WebUI 端口和 token；从 `onebot11_<QQ>.json` 读 HTTP 端口与启用状态 |
| 端口监听 | 探测 WebUI（默认 6099）与 OneBot HTTP（默认 3000） |
| 真实调用接口 | `get_login_info`（确认机器人身份）+ `get_group_msg_history`（**抽奖工具赖以工作的核心接口**） |
| 输出填写参数 | 直接把该填进界面的四行参数打印出来 |

**通过的样子**：

```
== 4. Live OneBot API test ==
  [OK]   port 3000 works - bot is 抽奖机器人 (10001)
  [OK]   get_group_msg_history OK for group 123456789 : 20 messages
  .      newest: 2026-10-12 15:02 from 某群友
```

**如果 HTTP 服务端没启用**，脚本会直接点出来：

```
  [FAIL] QQ 10001 has NO enabled HTTP server - this is exactly why the lottery tool cannot connect
```

---

## 6. 接进抽奖工具

打开界面（双击 `启动界面.bat`），左侧：

| 界面字段 | 填写内容 |
|---|---|
| 数据来源 | 切到 **「机器人接口」** |
| 群号 | 你要抽奖的群号 |
| 机器人 HTTP 地址 | `http://127.0.0.1:3000` |
| access_token | 与 NapCat 里一致；NapCat 没设就留空 |

然后：

1. 点 **「连接测试」** → 弹出「已连接机器人 xxx」就通了
2. 点 **「拉取群成员」** → 可以直接从成员列表里点选要排除的人
3. 点 **「刷新数据」** → 看「活跃度」页确认候选名单对不对劲
4. 点 **「开始抽奖」** → 开奖

想抽完自动发群，把左侧 **「抽完自动发到群里」** 打开即可。

命令行同样可用：

```powershell
python run_lottery.py draw --source onebot --group 你的群号 -n 3 --post
```

---

## 7. 排错对照表

| 现象 | 原因 | 解决 |
|---|---|---|
| 诊断脚本说「no enabled HTTP server」 | 网络配置建了但没启用 | 回 WebUI 勾选「保存时启用」 |
| 「连接测试」报 `无法连接 OneBot 服务` | NapCat 没启动 / 端口不对 | 看 NapCat 控制台确认端口，再跑一次诊断脚本 |
| 报 `HTTP 403` 或鉴权失败 | token 不一致 | 两边填成一样的，或都留空 |
| 接口通了但拉不到消息 | 机器人号没进群，或群号填错 | 把机器人号拉进群；用 `-Group` 指定正确群号再测 |
| 拉到的消息不全 | 机器人上线之前的消息不在 NapCat 缓存里 | 属正常现象。抽奖只看近 3 天，一般够用 |
| WebUI 打不开 | 端口被占用会自动 +1 | 看启动日志里实际打印的端口 |
| 协议端频繁掉线 | 网络抖动或风控 | 检查日志；确认没有换 IP 频繁登录 |

---

## 8. 日常使用

### 8.1 开机自动启动（✅ 本机已配置好）

NapCat **不会自己开机启动**——它是普通程序，电脑重启后需要重新运行。你这台机器已经配好了免扫码自启：

| 项目 | 内容 |
|---|---|
| 自启脚本 | `D:\NapCat\NapCat.52230.Shell\napcat-autostart.bat`（内含账号 `10086`） |
| 启动方式 | 启动文件夹里的快捷方式，窗口最小化 |
| 快捷方式位置 | `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\NapCat 群抽奖机器人.lnk` |
| 运行日志 | `D:\NapCat\NapCat.52230.Shell\napcat-autostart.log` |

**免扫码是怎么实现的**（已实测通过）：脚本把 QQ 号作为参数传给启动器，日志里能看到：

```
Boot Command: "D:\NapCat\NapCat.52230.Shell\QQ.exe" --enable-logging -q 10086
```

`-q 10086` 就是「快速登录」参数。实测重启实例后 **5 秒内自动上线**，无需再扫码。

**前提**：该账号必须**至少手动登录成功过一次**（你已经完成了）。如果哪天改了密码或换了账号，需要重新手动扫码一次。

**想临时取消自启**：删掉启动文件夹里那个 `.lnk` 快捷方式即可，或者改用 `napcat.bat` 手动启动。

**改用计划任务**（不需要登录就运行）在管理员 PowerShell 里执行：

```powershell
$action  = New-ScheduledTaskAction -Execute 'D:\NapCat\NapCat.52230.Shell\napcat-autostart.bat' `
           -WorkingDirectory 'D:\NapCat\NapCat.52230.Shell'
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'NapCat-群抽奖机器人' -Action $action -Trigger $trigger `
  -Description 'QQ 群抽奖机器人的 NapCat 服务'
```

> 两种方式选一种就行，别同时用（会启动两个实例打架）。
> 计划任务默认以当前用户身份在登录后运行；若要在**未登录**状态下也运行，需要勾选
> 「不管用户是否登录都要运行」，并且注意 NapCat 需要图形会话，通常仍以「登录后启动」更稳。

### 8.2 攒够「近三天」数据

前面 [第 8.3 节](#83-关于历史消息深度的重要限制) 说过：**NapCat 只能拿到它上线之后收到的消息**。所以要真正实现「近三天发言」，就得让它**连续在线三天**。

- **重要实测结论**：消息缓存上限是**固定条数**（本机实测约 **1628 条**），不是固定时长。
  本群语速约 20 条/分钟，因此缓存只能覆盖约 **79 分钟**。
  这意味着**「近三天」在活跃群里拿不到完整数据**——3 天约有 8.7 万条，缓存只装得下 1628 条。
- **短暂离线（分钟级）的消息会在重新登录时补回来**，实测时间线没有空洞；
  但**关机一晚这种长离线会永久丢失**（而且就算不关机，长窗口也一样会丢，因为缓存会被新消息挤掉）。
- 因此建议在抽奖界面开启「**自动适配统计窗口**」（默认已开启）：
  以缓存能完整覆盖的时段为抽奖范围，保证公平性前提成立。代价是窗口短于三天。
- **更彻底的办法是启用「消息存档」**（界面左侧面板，默认已开启），并让「无界面常驻」跟着开机启动：
  工具每 5 分钟把消息增量写入本地 `data/archive.db`，旧消息不再被缓存挤掉，
  时间窗口会随运行时长越来越完整。注意它只累积「工具在线期间」的消息，
  关机期间的空洞依然补不回来。
  常驻进程由启动文件夹里的「QQ群抽奖历史存档」负责，日志在 `data/archive-watch.log`。

### 8.3 关于历史消息深度的重要限制

实测数据（本机、群 123456789）：

```
全量可拿: 183 条，15:22 ~ 15:40
缓存深度: 18.6 分钟
该群语速: 约 10 条/分钟
```

**结论**：抽奖工具报告里写的「近 3 天」是统计窗口的上限，**实际能拿到多少取决于
NapCat 已经在线多久**。刚登录时窗口内只有最近十几分钟的消息，候选人是这段时间里说过话的人。

两种分页游标（`message_seq` 和 `real_seq`）都试过，**都无法翻到更早的历史**——
这是 QQ 协议层面的限制，不是工具或配置问题。

### 8.4 测试绝不会往群里发消息

这一点单独说明，因为它曾经出过事故：

| 防护 | 说明 |
|---|---|
| `QQLOTTERY_NO_SEND=1` | 环境变量一旦置位，**任何发送调用被直接拒绝**。两套自检脚本都会自设它 |
| 只许内网 | 发送目标必须是本机/内网地址，公网地址一律拒绝 |
| 隔离配置 | 自检在临时目录用独立配置，不读写你真实的 `config.json` |

所以**你自己跑 `python scripts/selftest.py` / `uitest.py` 是安全的**，不会打扰任何真实群。
需要真正发群时，正常使用工具即可（不加那个环境变量就允许发送）。

### 8.5 日常操作

- NapCat 要**保持运行**（抽奖时它必须在）；最小化在托盘/任务栏都行
- 电脑重启后**会自动起来**，无需手动操作
- 更新 NapCat：直接覆盖新版本目录即可，`config` 目录别删
- 只想离线抽奖时：NapCat 不开也能用「离线记录」模式，配合
  `python run_lottery.py fetch --source onebot -o data/messages.json` 把消息存下来留档

---

## 附：下载清单（v4.18.38，已核实存在）

| 文件 | 大小 | 用途 |
|---|---|---|
| `NapCat.Shell.Windows.OneKey.zip` | 0.9 MB | ⭐ 一键安装下载器（推荐，无需预装 QQ） |
| `NapCat.Shell.zip` | 34.2 MB | Shell 手动安装，需自备匹配版本的 QQ |
| `NapCat.Shell.Windows.Node.zip` | 118.3 MB | 自带 Node 运行时的 Shell 版 |
| `NapCat.Framework.zip` | 35.0 MB | 注入现有 QQ 安装（你的 QQ 版本偏旧，不建议） |

有图形化管理需求的话，官方还提供了一个可视化管理工具
[**NapCatQQ-Desktop**](https://github.com/NapNeko/NapCatQQ-Desktop)：单 exe、支持多账号管理、
一键启停、自动检查和更新版本，对不熟悉命令行的场景更友好。

---

**参考来源**（均为官方渠道）：

- 项目仓库：<https://github.com/NapNeko/NapCatQQ>
- 文档站：<https://napneko.github.io/>
- Shell 安装说明：<https://napneko.github.io/guide/boot/Shell>
- WebUI 与网络配置：<https://doc.napneko.icu/config/basic>
