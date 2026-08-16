# QQ AI Bot

基于 QQ 开放平台官方 API 的 AI 聊天机器人。支持私聊、群聊 @ 消息自动回复，可接入任意 OpenAI 兼容接口的 AI 服务（DeepSeek、OpenAI、智谱 AI、腾讯混元等），支持多模态图片识别。

## ✨ 功能特性

- 私聊 / 群聊 @ 消息自动回复（AI 对话）
- **私聊与群聊上下文分开存储**，群聊按群隔离（`data/user_context/private/`、`group/`）
- **私聊上下文一键转移到群聊**：群内发送 `/转移私聊到群聊`
- 关键词快捷回复、无意义消息过滤
- 多模态图片识别（OpenAI 兼容格式，失败自动降级为文本描述）
- 消息队列限流：繁忙时友好提示，不丢消息
- 断线自动重连、心跳假死检测、会话恢复（RESUME）

## 📦 环境要求

- Python 3.8+
- 依赖安装：`pip install -r requirements.txt`

## 🚀 快速开始

1. 复制配置模板：`cp config.json.example config.json`
2. 编辑 `config.json`：
   - `qq.app_id` / `qq.app_secret`：QQ 开放平台机器人凭据（未开通请先申请）
   - `api_key` / `base_url` / `model`：AI 服务配置（OpenAI 兼容接口）
   - `qq.sandbox`：`true` 为沙箱测试环境，正式运行请保持 `false`
3. 运行：`python qqbot.py`

> 首次运行会自动创建 `config.json` 和 `data/` 目录。日志位于 `data/logs/`。

## 💬 常用指令

| 指令 | 场景 | 说明 |
|---|---|---|
| `/clear` | 私聊 / 群聊 | 清空当前场景的对话历史 |
| `/转移私聊到群聊` | 群聊 | 把私聊上下文合并到当前群（保留群聊原有内容，自动去重） |
| `/生成转移码` | 私聊 | 生成身份绑定码（兜底用，10 分钟有效） |
| `/绑定转移码 <码>` | 群聊 | 绑定身份并转移上下文（兜底用） |

> 别名：`/将我的私聊上下文转移到当前群聊`、`/转移私聊上下文`、`/导入私聊上下文`。

## 📁 目录结构

```
API_qqbot/
├── qqbot.py                  # 主程序入口
├── config.json               # 配置文件（运行时生成/填写，已被 .gitignore 排除）
├── core/
│   ├── qq_client.py          # QQ 机器人客户端（WebSocket、心跳、重连、会话恢复）
│   ├── ai_client.py          # AI 服务客户端（OpenAI 兼容、多模态、重试）
│   ├── message_processor.py  # 消息处理器（队列、分段发送、指令）
│   ├── context_manager.py    # 上下文管理（私聊/群聊分文件夹、身份绑定）
│   ├── message_filter.py     # 消息过滤（关键词、无意义消息）
│   ├── file_handler.py       # 附件处理（图片转 base64 等）
│   ├── config_manager.py     # 配置读写
│   └── logger.py             # 日志（自动分割）
└── data/                     # 运行时数据（日志、用户上下文，已被 .gitignore 排除）
```

## ⚠️ 安全提示

- `config.json` 包含 AppSecret 与 API Key，**已被 `.gitignore` 排除，请勿强行提交到公开仓库**
- 部署请使用自己的密钥，谨防泄露

## 📄 其他

- 变更记录见 [CHANGELOG.md](CHANGELOG.md)
- 版本说明见 [RELEASE_NOTES.md](RELEASE_NOTES.md)
