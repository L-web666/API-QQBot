# Ubuntu Server 部署说明（Ollama 在 Docker 容器内）

> 本目录只是**模板**，按需取用，不需要可以删掉。
> 程序本身在 Linux 上无需改代码：单实例锁用 fcntl、日志时间自己按北京时间算、路径都是相对工作目录。

## 一、结论：插件要不要改？

代码层面已完成两件事（`plugins/ollama.py`，v2.2.0）：

1. **支持环境变量配置**，服务器上不必改插件源码：
   | 环境变量 | 说明 | 示例 |
   |---|---|---|
   | `BOT_OLLAMA_HOST` | Ollama 地址，`ollama`、`ollama:11434`、`http://ollama:11434` 都能自动规范化 | `http://127.0.0.1:11434` |
   | `BOT_OLLAMA_MODEL` | 模型名 | `qwen3:4b` |
   | `BOT_OLLAMA_THINK` | `false`(默认) / `true` / `none` | `false` |
   | `BOT_OLLAMA_HISTORY` | 每用户记忆轮数 | `6` |
   | `BOT_OLLAMA_TIMEOUT` | 单次请求超时秒数（CPU 跑大模型要调大） | `600` |
2. 也兼容系统里已有的 `OLLAMA_HOST`（ollama CLI 的 `主机:端口` 写法会自动补 `http://`）。
   连不上时的报错会直接提示该用哪个地址（127.0.0.1 还是容器名）。

**唯一必须注意的是「机器人怎么访问 Ollama」**：

| 机器人在哪 | Ollama 在哪 | `BOT_OLLAMA_HOST` 填什么 |
|---|---|---|
| 宿主机（python 直接跑） | Docker 容器 | `http://127.0.0.1:11434`（容器需 `-p 11434:11434`） |
| Docker 容器 | Docker 容器 | `http://ollama:11434`（同一 compose 网络里用服务名/容器名） |
| Docker 容器 | 宿主机 | `http://host.docker.internal:11434`（Linux 需加 `--add-host=host.docker.internal:host-gateway`） |
| 宿主机 | 另一台机器 | `http://内网IP:11434`（Ollama 那边要监听 `0.0.0.0`） |

## 二、方案 A：Ollama 用 Docker，机器人在宿主机（推荐，改动最小）

```bash
# 1. 起 Ollama 容器（只对宿主机开放端口，避免公网暴露）
docker run -d --name ollama --restart unless-stopped \
  -p 127.0.0.1:11434:11434 \
  -v ollama_models:/root/.ollama \
  -e OLLAMA_HOST=0.0.0.0:11434 \
  -e OLLAMA_KEEP_ALIVE=30m \
  ollama/ollama

# 2. 拉模型（在容器里执行）
docker exec -it ollama ollama pull qwen3:4b
docker exec -it ollama ollama list

# 3. 验证接口（宿主机上）
curl http://127.0.0.1:11434/api/tags
curl http://127.0.0.1:11434/api/chat -d '{"model":"qwen3:4b","messages":[{"role":"user","content":"你好"}],"stream":false,"think":false}'
```

部署机器人：

```bash
sudo adduser --system --group --home /opt/qqbot qqbot
sudo mkdir -p /opt/qqbot && sudo chown -R qqbot:qqbot /opt/qqbot
# 上传项目到 /opt/qqbot（config.json / plugins/ / core/ / qqbot.py ...）

cd /opt/qqbot
sudo -u qqbot python3 -m venv .venv
sudo -u qqbot .venv/bin/pip install -r requirements.txt     # requests、websocket-client
sudo -u qqbot .venv/bin/python qqbot.py                     # 先前台试跑，确认能连上 QQ 与 Ollama

# 注册为 systemd 服务
sudo cp deploy/qqbot.service /etc/systemd/system/qqbot.service
sudo systemctl daemon-reload && sudo systemctl enable --now qqbot
journalctl -u qqbot -f
```

## 三、方案 B：两个都在 Docker（见 `docker-compose.yml`）

```bash
docker compose up -d
docker exec -it ollama ollama pull qwen3:4b
docker compose logs -f qqbot
```

要点：
- 机器人容器里 `BOT_OLLAMA_HOST=http://ollama:11434`（**不能用 127.0.0.1**，那指向机器人自己）
- `ollama` 容器必须监听 `0.0.0.0:11434`（模板里已设置）
- 模型存 `ollama_models` 卷；项目目录挂载到 `/app`，所以 `plugins/` 改了重启容器即可

## 四、Web 管理后台的访问方式（服务器上）

默认 `web_admin.host = 127.0.0.1`、`port = 8666`，即只允许本机访问，服务器上推荐这样用：

```bash
# 本地电脑上建立 SSH 隧道，然后浏览器打开 http://127.0.0.1:8666/
ssh -L 8666:127.0.0.1:8666 用户名@服务器IP
```

需要公网/内网直接访问时：
1. `config.json` 里 `web_admin.host` 改为 `0.0.0.0`，并**务必设置一个强 `token`**（令牌为空=谁都能进）
2. 防火墙只放行必要端口：`sudo ufw allow OpenSSH`；**不要**把 11434 暴露到公网（Ollama 无鉴权）
3. 更稳妥：用 Nginx 反向代理 + HTTPS + Basic Auth，只暴露 443

## 五、性能与常见坑

- **模型常驻**：`OLLAMA_KEEP_ALIVE=30m`，否则每条消息都要重新加载模型（很慢）
- **CPU 服务器**：选小模型（`qwen2.5:3b`、`qwen3:4b`），并把 `BOT_OLLAMA_TIMEOUT` 调到 600~900
- **内存**：4B 模型约需 4~6GB；OOM 时 `docker logs ollama` 会看到 killed
- **显卡**：宿主装 `nvidia-container-toolkit`，compose 里打开 `deploy.resources` 的 GPU 段
- **插件串行**：插件内部已有锁，同一时刻只处理一个 Ollama 请求（避免打爆容器），所以并发消息会排队
- **数据目录权限**：`data/` 必须对运行用户可写（日志、上下文、插件数据、单实例锁都在里面）
- **工作目录**：systemd 必须设 `WorkingDirectory`，否则 `config.json`/`plugins/` 找不到
- **重复连接**：程序自带单实例锁，别同时用两种方式启动（systemd + 手动）

## 六、部署后自检清单

```bash
curl http://127.0.0.1:11434/api/tags                     # Ollama 通不通
sudo -u qqbot .venv/bin/python -c "import requests,websocket;print('deps ok')"
journalctl -u qqbot -n 50                                # 看启动日志
```
然后在 QQ 里私聊机器人发一句普通问题，日志里应出现 `Ollama 回答(<模型名>): ...`；
若报“连接不上 Ollama”，按报错里给出的地址逐项核对容器端口映射或容器名。
