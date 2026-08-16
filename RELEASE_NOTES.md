# Release Notes — v1.2.0

QQ AI Bot 在 v1.1.0 基础上的重要更新版本。完成一批问题修复与功能增强，可正式部署使用。

## ✨ 新功能

- **私聊 / 群聊上下文分开存储**：`data/user_context/private/` 与 `data/user_context/group/`，群聊按群隔离，互不串扰
- **私聊上下文一键转移到群聊**：群内发送 `/转移私聊到群聊`，私聊历史合并进当前群（保留群聊原有内容、自动去重）
- **队列限流**：同时请求过多时回复"机器人忙，请稍后再试"，消息不再被静默丢弃
- **身份绑定兜底**：`/生成转移码`（私聊）+ `/绑定转移码 <码>`（群聊），应对 openid 命名空间不一致的情况

## 🐛 主要修复

- ✅ 修复**收不到私聊消息**（intents 订阅缺失）
- ✅ 修复**鉴权失败时无限重连**（AppID/Secret 填错时死循环）
- ✅ 新增**心跳超时检测**（断网假死后自动重连）
- ✅ 修复**沙箱环境无法连接**（WebSocket 网关未切换）
- ✅ 修复 **access_token 泄露**到 URL 的问题
- ✅ 修复**纯数字消息无法被无意义过滤**的逻辑矛盾
- ✅ 修复 **`max_queue_size` 配置无效**（队列实际无上限）
- ✅ 修复频道私信回复接口错误、分段发送多余等待、思考过程误删正文、URL 扩展名解析错误等

## 📦 依赖

- Python 3.8+
- `pip install -r requirements.txt`（requests、websocket-client）

## 🚀 快速开始

```bash
pip install -r requirements.txt
cp config.json.example config.json
# 编辑 config.json：填写 QQ AppID/AppSecret 与 AI 的 api_key/base_url/model
python qqbot.py
```
