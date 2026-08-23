# 更新日志 (Changelog)

本项目所有重要变更都会记录在此文件中。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [v1.3.0] - 2026-08-23

### 自 v1.2.0 的重要更新

本版本在 v1.2.0 基础上，新增跨平台单实例保护、日志脱敏与 Markdown 清理等能力。

#### ✨ 新增（第一部分）

- **单实例保护（跨平台三级锁）**（`qqbot.py`）
  - 文件锁（`fcntl` / `msvcrt`）→ 目录锁（原子 `mkdir`，兼容 Android 共享存储 FUSE）→ 均不可用时提示后继续启动
  - 被占用时拒绝启动，提示按平台显示（Windows：`taskkill /F /PID`；手机/Linux：`pkill -f qqbot.py`）
  - 进程退出/崩溃自动释放锁；目录锁带残留检测（锁主进程已死则自动清理）
- **转移码日志脱敏**（`core/logger.py`、`core/qq_client.py`、`core/message_processor.py`）：新增 `mask_transfer_code()`，日志中的转移码显示为 `******`，不再明文落盘
- **转移码防堆积**（`core/context_manager.py`）：生成新码时自动作废该用户旧码，同一用户同时最多一个有效码
- **AI 回复 Markdown 清理**（`core/ai_client.py`、`core/message_processor.py`）：新增 `strip_markdown()`，去除加粗/标题/列表/代码块/链接/表格等格式符号；表格转为可读文本（`a | b`）并删除分隔行

#### ✨ 新增（第二部分）

- **QQ 指令面板（聊天界面指令列表）**（`core/qq_client.py`、`core/web_admin.py`、`qqbot.py`）
  - 接入官方 `v2/panels` 接口：创建 / 更新 / 查询列表 / 删除指令面板，私聊(c2c)与群聊(group)面板分别注册
  - 修复列表查询：官方返回字段为 `records` 且需按 `scope` 分页查询，之前解析不到面板导致"看不到/删不掉、额度一直被占"的问题
  - 面板注册改为"先查官方列表、有则复用"：本地缓存丢失或面板失效时自动复用/重建，不再重复创建占额度；创建失败提示到后台清理旧面板
  - 启动时先注册面板再连接 WebSocket（此前顺序颠倒导致永远不执行）
- **Web 管理后台 UI 全面改版**（`core/web_admin.py`）
  - 现代化视觉：靛蓝渐变头部、毛玻璃吸顶导航、圆角卡片与阴影、渐变按钮、自定义滚动条、淡入动画
  - 日志页新增**日志级别筛选**（全部 / DEBUG / INFO / WARNING / ERROR / CRITICAL），后端按级别过滤
  - 新增 **📈 统计页**：今日统计（消息/私聊/群聊/AI调用/关键词/指令/过滤/回复等）、最近 7 天纯 CSS 柱状趋势图、历史累计
  - 修复状态栏"当前北京时间"快 8 小时的问题（`localtime` 误用改为 `gmtime` 读取 UTC+8）
  - 指令面板编辑器改为 QQ 官方格式：每行 = 类型（指令/链接）+ 名称 + 描述或链接，保存生成 `{"type":"command","name","desc"}` 或 `{"type":"link","name","link"}`
- **图片识别**（`core/qq_client.py`、`core/file_handler.py`）：兼容 QQ 富媒体 `media` 字段（此前只读 `attachments` 导致收不到图），`file_info` 自动换取下载链接，图片下载转 base64 交给多模态 AI 识别；纯图片消息自动补提示"请描述这张图片"
- **语音识别**（`core/ai_client.py`、`core/message_processor.py`、`core/config_manager.py`）：收到语音自动下载并调用 OpenAI 兼容 ASR（`/audio/transcriptions`）转文字后交给 AI；新增 `asr_base_url` / `asr_api_key` / `asr_model` 配置，Web 后台可填
- **回复限速**（`core/message_processor.py`）：同一用户两次 AI 回复的最小间隔（默认 3 秒），防刷屏；`rate_limit.enabled` / `rate_limit.interval_seconds` 可配，支持热更新
- **敏感词过滤**（`core/message_processor.py`）：输入拦截（`block_input`）与输出打码（替换为 `***`）双向过滤；`sensitive_words.list` / `replacement` / `block_input` 可配，支持热更新
- **插件系统**（新增 `core/plugin_manager.py`，`plugins/` 目录）
  - 用户**无需改源代码**即可扩展：把插件放进 `plugins/` 目录（单文件 `.py` 或一个文件夹多文件），Web 后台一键重新加载
  - 插件接口 v1：`PLUGIN` 元信息 + `COMMANDS` / `KEYWORDS` / `match(msg)` 匹配 + `on_message(msg[, bot])` 处理
  - 支持**连接型/桥接型插件**：`bot` 上下文可主动发消息/读配置/记日志，`on_start(bot)` / `on_stop(bot)` 生命周期钩子（启动后台线程、监听外部服务等）
  - Web 后台 **🧩 插件页**：插件列表（名称/说明/类型/匹配规则/状态）、重新加载、启用/停用（**保存后生效**，状态持久化到 `data/plugins_disabled.json`，重启保持）
  - 插件目录不存在时自动创建；插件加载日志精简（逐条为 DEBUG，控制台只显示"加载完成：共 N 个（另有 N 个被禁用）"）
  - 附带示例：`示例插件.py`（回复型）、`多文件示例/`（多文件）、`桥接转发示例.py`（连接型）、`Ollama本地AI.py`
- **Ollama 本地 AI 插件**（`plugins/ollama.py`）：默认接管普通文字消息，调用本机 Ollama 回答，**不消耗 API 额度**；任何 `/` 开头的指令（内置或其他插件的）一律放行；每个用户保留最近 6 轮对话记忆
- **单文件整合版**（`qqbot_single.py`）：全部模块合并为单文件（保留注释），修复打包脚本误删函数体内局部 import 的问题

#### 🐛 修复

- 修复 Android 共享存储（FUSE）不支持 `flock` 导致手机端一直误判"已在运行"而无法启动的问题（目录锁兜底）
- 修复单实例拒绝提示与平台不匹配的问题（Windows 上显示 pkill 命令）
- 修复"转移码无效"提示不准确的问题（区分无效 / 已过期 / 已被新码覆盖）
- 修复指令面板订阅未授权 intent 位（`1<<31`）导致 WebSocket 鉴权失败、程序无法连接的问题（恢复默认 `1<<25|1<<26`，欢迎新成员功能因平台权限限制移除）
- 修复插件停用后从后台列表消失、无法重新启用的问题（列表改为同时展示已停用插件）
- 修复插件说明多行时"单文件/运行中"等徽章被撑成多行的问题（徽章 nowrap + 表格固定列宽）
- 修复 Web 面板插件页按钮引号嵌套导致点击报错的问题（改用 `data-` 属性传参）

## [v1.2.0] - 2026-08-16

### 自 v1.1.0 的重要更新

本版本在 v1.1.0 原始版本基础上，修复了若干会导致功能失效的问题，并新增上下文管理、队列限流等能力。

#### 🐛 修复

**核心连接 (`core/qq_client.py`)**

- 修复 WebSocket intents 订阅缺失导致**收不到私聊(C2C)消息**的问题：由仅订阅群@消息（`1<<25`）改为同时订阅单聊消息（`1<<26`）
- 修复鉴权失败时**无限重连**的问题：重连计数改为在 READY（鉴权成功）事件后重置，AppID/Secret 无效时最多重试 5 次即放弃
- 新增**心跳超时检测**：连续 3 个心跳周期未收到服务器任何响应即判定连接假死，主动断开以触发重连
- 修复 access_token 拼接在 WebSocket URL 中导致**凭据可能泄露**到代理/网关日志的问题，鉴权改由连接内 Identify 包完成
- 修复沙箱环境下 WebSocket 仍连接正式网关导致**沙箱无法使用**的问题，沙箱/正式环境的 REST 与 WebSocket 网关统一切换
- 简化断线重连的 RESUME 会话恢复逻辑：非主动断开且存在历史会话即尝试 RESUME，失败自动回退重新 IDENTIFY

**消息处理 (`core/message_processor.py`)**

- 修复 `max_queue_size` 配置无效的问题：消息队列改为**有界队列**，队列满时不再静默丢弃，而是回复"🤖 机器人正在处理其他消息，请稍后再试。"（独立线程发送，不阻塞消息接收）
- 修复频道私信回复调用错误接口的问题，改为走单聊消息接口
- 修复分段发送时最后一段之后多余等待 0.5 秒的问题
- 清理分段逻辑中的死代码

**消息过滤 (`core/message_filter.py`)**

- 修复逻辑矛盾导致**纯数字、单字符等无意义消息永远无法被过滤**的问题
- 修复单字符正则误伤单个中文字符（如"好"）的问题
- 扩充纯表情过滤的正则覆盖范围（支持主流 emoji 区块）

**AI 客户端 (`core/ai_client.py`)**

- 修复重试最后一次失败后仍空等指数退避时间的问题
- 修复"思考过程"过滤正则可能误删回复正文的问题

**文件处理 (`core/file_handler.py`)**

- 修复 URL 带查询参数（如 `a.jpg?sign=xxx`）时文件扩展名解析错误的问题

#### ✨ 新增

- **私聊/群聊上下文分文件夹存储**：`data/user_context/private/{user_openid}.json` 与 `data/user_context/group/{群ID}_{用户ID}.json`，群聊上下文按群隔离，`/clear` 只清当前作用域
- **私聊上下文转移到群聊**：群内发送 `/转移私聊到群聊` 即可把私聊历史合并进当前群上下文——保留群聊原有内容、仅追加群聊中不存在的私聊记录（按内容去重，重复转移不产生重复记录），私聊上下文本身保留
- **身份绑定兜底机制**：私聊 `/生成转移码` → 群聊 `/绑定转移码 <码>`，用于平台 openid 命名空间不一致的场景（当前已实测 `member_openid` 与 `user_openid` 值相同，通常无需绑定）
- 群聊事件优先读取 `author.user_openid` 作为统一身份标识，兼容平台行为变化（取不到时回退 `member_openid`）

#### 🗑️ 移除

- 移除群聊回复中手动添加的 @用户 前缀，并删除相关配置项 `reply_with_mention`（`config.json`、配置说明、默认配置模板同步清理）

#### ⚠️ 已知限制

- `require_mention: false`（回复所有群消息）需要 QQ 开放平台开通"群消息全量"权限并额外订阅事件，当前未实现
- 群聊**被动回复**会被 QQ 平台自动 @ 原消息发送者，属官方服务端行为，无法通过代码或参数关闭

## [v1.1.0] - 原始版本基线

- 初始内部版本：基本 QQ 机器人功能（群@/私聊回复、上下文存储、关键词过滤、多模态图片、日志与配置管理）
- 本仓库首次公开发布即从该版本更新而来，变更明细见上方 [v1.2.0]
