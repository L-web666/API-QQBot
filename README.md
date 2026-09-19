# QQ AI Bot（Web 后台版）

> 本目录是**主开发目录（Web 后台版）**：启动后通过浏览器访问管理后台。
> 全功能图形界面（桌面窗口）的分支见 `../API_qqbot_GUI/`。

基于 QQ 开放平台官方 API 的 AI 聊天机器人。支持私聊、群聊 @ 消息自动回复，
可接入任意 **OpenAI 兼容接口**的 AI 服务（DeepSeek、OpenAI、智谱 AI、腾讯混元等），
支持多模态图片识别，并可选用 **Cloudflare D1 云同步**做跨服务器数据备份。

当前版本：**v1.4.0**（变更见 [CHANGELOG.md](CHANGELOG.md)，流程图见 [docs/](docs/)）

---

## ✨ 功能特性

**对话与消息**
- 私聊 / 群聊 @ 消息自动回复（AI 对话），群聊按群隔离上下文
- 私聊与群聊上下文**分开存储**（`data/user_context/private/`、`group/`）
- 私聊上下文一键转移到群聊（群内发 `/转移私聊到群聊`；跨账号可用转移码绑定身份）
- 关键词快捷回复、无意义消息过滤、**回复限速**、**敏感词过滤**（可打码或拦截）
- 多模态图片识别、长消息自动分段、消息队列限流（繁忙时友好提示，不丢消息）
- 断线自动重连、心跳假死检测、会话恢复（RESUME）

**AI 接入**
- 任意 OpenAI 兼容接口（填 `api_key` / `base_url` / `model` 即可）
- **内置 AI 可选**：没有 API Key 也能启动，普通消息交给插件/关键词处理
- `ai.enabled=false` 可完全关掉内置 AI（例如让本地 Ollama 插件接管，不消耗线上额度）

**管理与运维**
- **Web 管理后台**：图形化配置编辑、状态与统计图表、日志查看（按级别筛选/下载）、
  插件管理（加载/启用停用）、上下文查看、指令面板管理
- **控制台彩色日志**：时间灰色、WARNING 黄色、ERROR 红色（正文不着色），日志文件保持纯文本
- **配置热更新**：编辑 `config.json` 保存后自动生效（连接类配置仍需重启）
- **数据统计**：今日 / 7 天趋势 / 历史累计（消息、AI 调用、回复等）
- **QQ 指令面板**：私聊/群聊聊天界面显示可点选的指令
- **云同步（可选）**：把用户上下文 / 身份绑定 / 统计 / 插件数据同步到 Cloudflare D1

**插件系统**
- 往 `plugins/` 放 `.py` 文件（或一个文件夹）即可扩展功能，**无需改任何源码**
- 支持回复型与连接型插件，`on_start` / `on_stop` 生命周期钩子，Web 后台一键启停与重载

---

## 📦 环境要求

- Python 3.8+（开发环境实测 3.14）
- 依赖安装：`pip install -r requirements.txt`（`requests`、`websocket-client`）
- 已打包的 Windows 版可直接运行 `QQAIbot.exe`，**无需安装 Python**

---

## 🚀 快速开始

### 方式一：源码运行

1. 安装依赖：`pip install -r requirements.txt`
2. 首次运行会自动生成 `config.json`（也可手动新建一个空的 `config.json`，程序会补全默认项）
3. 编辑 `config.json`：
   - `qq.app_id` / `qq.app_secret`：QQ 开放平台机器人凭据 **（必填）**
   - `api_key` / `base_url` / `model`：AI 服务配置 **（可选）**
   - `qq.sandbox`：`true` 为沙箱测试，正式运行请保持 `false`
4. 启动：`python qqbot.py`（或用 `start.bat` / `start.sh`，崩溃自动重启）

> 首次运行会自动创建 `config.json`、`config配置说明文件.txt` 和 `data/` 目录，日志在 `data/logs/`。
> 单文件整合版 `qqbot_single.py` 功能与多模块版完全一致，可单独复制运行。

### 方式二：运行打包版

1. 解压发布包，双击 `QQAIbot.exe`（或 `start.bat`，退出后自动重启）
2. 首次运行会在 exe 同目录生成 `config.json` 与 `data/`，填好 `qq.app_id` / `qq.app_secret` 后重新启动

---

## ⚙️ 配置要点

配置文件是 `config.json`（含中文注释的完整说明见运行后生成的 `config配置说明文件.txt`；
也可以直接在 Web 后台「配置」页图形化修改）。常用项：

| 配置项 | 说明 |
|---|---|
| `qq.app_id` / `qq.app_secret` | QQ 开放平台机器人凭据（必填，改后需重启） |
| `api_key` / `base_url` / `model` | AI 服务（OpenAI 兼容）；留空=不用内置 AI |
| `ai.enabled` | 内置 AI 总开关（`false` 时把回复完全交给插件/关键词） |
| `no_ai_reply` | 没有内置 AI 且没人接管时的兜底回复（留空=不回复） |
| `system_prompt` | AI 全局人设 |
| `filter.exact` / `filter.fuzzy` | 关键词精确/模糊匹配回复 |
| `group.require_mention` | 群聊是否只在被 @ 时回复 |
| `rate_limit` | 同一用户两次 AI 回复的最小间隔 |
| `sensitive_words` | 敏感词列表、替换符号、是否拦截输入 |
| `context.max_history` | 每个用户/群最多记住多少条上下文 |
| `log.console_color` | 控制台彩色输出（日志文件始终无颜色） |
| `hot_reload` | 配置热更新开关与检查间隔 |
| `admin.openids` / `alert` | 管理员名单、出错时私聊通知主人 |
| `schedule.tasks` | 定时推送任务（时间 + 目标 + 内容） |
| `web_admin` | 管理后台监听地址/端口/访问令牌 |
| `command_panel` | 向 QQ 注册的指令面板内容与可见范围 |
| `plugins` | 插件系统开关与插件目录 |
| `cloud_sync` | 云同步（见下） |

---

## 🌐 Web 管理后台

- 启动后浏览器访问 `http://127.0.0.1:8666/`
- 页面：**状态**（连接状态、运行时长、北京时间、云同步卡片、最近日志）、
  **统计**（今日 / 7 天趋势 / 历史累计）、**配置**（图形化编辑，保存即生效）、
  **插件**（加载 / 启用停用 / 重载）、**日志**（按级别筛选、选择文件下载、自动刷新）、
  **上下文**（查看各用户/群记忆）、**指令面板管理**（刷新 / 删除 / 重新注册）
- 手机访问：把 `web_admin.host` 改为 `0.0.0.0`，**并给 `web_admin.token` 填一个随机字符串**，
  然后用 `http://电脑IP:8666/?token=令牌` 访问
- 敏感字段（API 密钥等）在页面**留空显示**：留空保存=保持不变，填写新值=修改，勾选「清空此值」=清空

> ⚠️ `web_admin.token` 留空时面板**不做任何校验**。只在 `host=127.0.0.1`（仅本机）时可以留空；
> 一旦改成 `0.0.0.0`（局域网/公网可访问），务必设置令牌。

---

## ☁️ 云同步（可选）

把 `data/` 下的**用户数据**同步到 Cloudflare D1 云数据库，换服务器 / 重装系统后启动自动恢复。

- **会上云**：`user_context/**`（私聊/群聊上下文、身份绑定）、`stats.json`、
  `plugins_data/**`、`plugins_disabled.json`、`command_panel.json`
- **永不上云**：`config.json`、`config配置说明文件.txt`（含密钥），`data/bot.lock`、`data/bot.pid`；
  日志默认也不上云（`upload_logs=true` 才同步）
- 配置：`cloud_sync.enabled=true` + `account_id` / `database_id` / `api_token`（Cloudflare 令牌需「D1 → 编辑」权限）；
  数据表 `bot_data` 首次连接自动创建，旧表会自动补列
- **冲突规则：同一条记录以时间戳更新的一方为准**
  - 云端副本更新 → 用云端版本覆盖本地，并且**本轮不上传**本地旧副本（控制台 WARNING 提示）
  - 本地副本更新 → 上传覆盖云端；时间戳相同 → 以本地为准
  - 云端墓碑（删除标记）+ 本地更新 → 本地"复活"并覆盖墓碑
- **启动写缓存**（`startup_buffer`，默认开）：程序刚启动到第一次云同步完成之间，
  `data/` 下的数据改动（写入/删除/改名）先留在内存里，等云端数据拉下来之后再写盘，
  避免"启动时写的旧数据/默认值把云端数据覆盖掉"
  - 缓存期间程序内**读取能看到缓存内容**（`open()` / `os.path.exists` 等都按缓存回答）
  - 刚被第一次同步从云端恢复过的文件，默认放弃启动期间的本地改动（`startup_buffer_keep_remote=false` 可改为本地优先）
  - 不会一直等：超过 `startup_buffer_minutes`（默认 3 分钟）、超过 `startup_buffer_max_mb`
    （默认 8MB，另有 128MB 兜底硬上限）、云同步暂停、程序退出时，都会立即写回磁盘
  - **日志始终立即写盘**；**未启用云同步时这个机制完全不生效**，也不会有任何相关提示
- 失败处理：单个文件失败只告警并跳过（下个周期重试）；同一文件连续失败 3 次 → ERROR 并暂停同步
  （`error_pause_minutes` 分钟后自动恢复，`0`=手动恢复；后台「⬆️ 立即同步一次」可强制重试）
- 完整流程图：[`docs/云同步流程图.svg`](docs/云同步流程图.svg)（图形版）、
  [`docs/云同步流程图.md`](docs/云同步流程图.md)（Mermaid 文本版）
- ⚠️ 同一时间**只应运行一台机器人**（云数据库按 key 覆盖，多台会互相打架）

---

## 💬 常用指令

| 指令 | 场景 | 说明 |
|---|---|---|
| `/帮助` | 私聊 / 群聊 | 显示帮助菜单 |
| `/clear` | 私聊 / 群聊 | 清空当前场景的对话历史 |
| `/转移私聊到群聊` | 群聊 | 把私聊上下文合并到当前群（保留群聊原有内容，自动去重） |
| `/生成转移码` | 私聊 | 生成身份绑定码（10 分钟有效，兜底方案） |
| `/绑定转移码 <码>` | 群聊 | 绑定身份并转移上下文（兜底方案） |

> 别名：`/将我的私聊上下文转移到当前群聊`、`/转移私聊上下文`、`/导入私聊上下文`。
> 更多玩法指令由插件提供，见 [plugins/README.md](plugins/README.md)。

---

## 🧩 插件系统

无需修改任何源代码，把插件放进 `plugins/` 目录即可扩展机器人功能
（自带示例：每日签到、骰子、示例插件、桥接转发示例、多文件示例、本地 Ollama AI）。

- 支持**单文件插件**（`插件名.py`）与**多文件插件**（一个文件夹，含 `__init__.py` 或 `main.py`）
- 插件可读写自己的数据：`DATA_FILE`（`data/plugins_data/<插件名>.json`）与
  `DATA_DIR`（`data/plugins_data/<插件名>/`），这些数据会随云同步一起备份
- Web 后台「插件」页可一键启用/停用（保存后生效，状态持久化）与重新加载

📖 **完整插件开发说明见 [`plugins/README.md`](plugins/README.md)**（接口标准、示例、数据存储规范）。

---

## 📁 目录结构

```
API_qqbot/
├── qqbot.py                  # 主程序入口（多模块版）
├── qqbot_single.py           # 单文件整合版（可独立运行/打包 exe）
├── config.json               # 配置文件（运行时生成/填写，已被 .gitignore 排除）
├── config配置说明文件.txt      # 自动生成的配置说明（中文）
├── start.bat / start.sh      # 启动脚本（崩溃自动重启）
├── core/
│   ├── qq_client.py          # QQ 机器人客户端（WebSocket、心跳、重连、会话恢复）
│   ├── ai_client.py          # AI 服务客户端（OpenAI 兼容、多模态、重试）
│   ├── message_processor.py  # 消息处理器（队列、分段发送、指令、插件分发、限速）
│   ├── context_manager.py    # 上下文管理（私聊/群聊分目录、身份绑定）
│   ├── message_filter.py     # 消息过滤（关键词、无意义消息）
│   ├── file_handler.py       # 附件处理（图片转 base64 等）
│   ├── config_manager.py     # 配置读写与默认值、配置说明文档
│   ├── logger.py             # 日志（自动分割、控制台彩色、北京时间）
│   ├── stats.py              # 数据统计（按天记录，保留 30 天）
│   ├── plugin_manager.py     # 插件系统（加载/分发/启用停用/数据目录）
│   ├── web_admin.py          # Web 管理后台（配置/统计/日志/插件/上下文）
│   ├── cloud_sync.py         # 云同步（Cloudflare D1、墓碑删除、暂停与重试）
│   └── deferred_writes.py    # 启动写缓存（第一次云同步完成前先缓存数据改动）
├── plugins/                  # 插件目录（用户可自行添加，无需改代码）
│   ├── README.md             # 插件接口标准说明（必看）
│   ├── 示例插件.py / 多文件示例/ / 桥接转发示例.py
│   ├── 每日签到.py / 骰子.py
│   └── ollama.py             # （可选）本地 Ollama AI 插件
├── docs/                     # 云同步流程图（SVG + Mermaid 文本版）
├── deploy/                   # Ubuntu / Docker 部署模板（systemd、docker-compose）
├── data/                     # 运行时数据（日志、上下文、统计、插件数据；已被 .gitignore 排除）
└── _build_single.py          # 把多模块版合并成 qqbot_single.py
```

---

## 📦 打包成 exe（Windows）

```bash
pip install pyinstaller
python _build_single.py                    # 先合并出最新的单文件版
python -m PyInstaller --noconfirm --clean --name QQAIbot --onedir qqbot_single.py
# 产物：dist/QQAIbot/QQAIbot.exe（连同 _internal/ 一起分发）
```

打包版会把 `config.json`、`data/`、`plugins/` 放在 **exe 所在目录**（程序启动时会自动切换工作目录），
所以整个 `QQAIbot/` 文件夹可以整体复制到任意位置运行。

---

## ⚠️ 安全提示

- `config.json` 包含 AppSecret 与 API Key，**已被 `.gitignore` 排除，请勿提交到公开仓库**
- `web_admin.token` 留空 = 面板不校验；只要 `host` 不是 `127.0.0.1`，就一定要设置令牌
- 云同步的 `api_token` 建议只给「D1 → 编辑」权限，不要用全局 API Key
- 插件代码会被直接执行，只放你信任的文件

---

## 📄 其他

- 变更记录：[CHANGELOG.md](CHANGELOG.md)　·　版本说明：[RELEASE_NOTES.md](RELEASE_NOTES.md)
- 云同步流程图：[docs/云同步流程图.svg](docs/云同步流程图.svg)
- 部署模板（Ubuntu systemd / Docker）：[deploy/README-部署说明.md](deploy/README-部署说明.md)
