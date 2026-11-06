# 自托管

在自己的电脑或家里的服务器上跑起来，给自己和朋友用。**这是单人 / 小圈子用的形态：没有登录，别放到公网上**（见最后一节）。

## 需要什么

- Docker（推荐），或 Python 3.11+ 和 Node 20+。
- 可选：[Ollama](https://ollama.com) 加 `ollama pull qwen2.5:7b-instruct`，让模型把说明写顺。没有它也能用：行程照常生成，说明文字改用模板，页面会提示。
- 可选：高德 Key（国内行程的地图和搜索）、小红书 Cookie（读单篇笔记）。都不配也能用，见下面「配置」。

## 方式一：Docker

```bash
git clone https://github.com/Kylie08ky/itinerary-forge.git
cd itinerary-forge
docker compose up --build
```

打开 http://localhost:5173（接口文档在 http://localhost:8137/docs）。

- 后端 `api` 在 8137，前端 `web`（nginx 托管静态页面）在 5173；`web` 要等 `api` 健康检查通过才启动。
- **浏览器直接连后端**，地址在构建前端时写死。要让别的设备访问，把 `docker-compose.yml` 里 `web.build.args.VITE_API_BASE` 改成 `http://<这台机器的地址>:8137` 再重新构建。
- 模型在宿主机上跑 Ollama，容器经 `host.docker.internal:11434` 连它。换模型改 `docker-compose.yml` 里的 `OLLAMA_MODEL`。不想用模型，把 `LLM_BACKEND` 设成 `stub`。
- 行程数据库在命名卷 `plan-store`（容器里的 `/app/data/plans`），重建镜像不会丢。

## 方式二：直接跑

```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -e .
python -m playwright install chromium     # 导出 PDF 要用
python -m uvicorn app.api:app --host 127.0.0.1 --port 8137
```

另开终端：`cd web && npm ci && npm run dev`（开发）或 `npm run build`（把 `web/dist` 交给任何静态服务器）。PDF 里的中文需要系统装有中文字体（Docker 镜像已带 Noto CJK）。

## 配置

复制 `.env.example` 为 `.env`（方式二）；Docker 里 `.env` 不会进镜像，要用 `environment:` 传，例如放在 `docker-compose.override.yml`：

```yaml
services:
  api:
    environment:
      AMAP_WEB_KEY: 你的 Key
```

| 变量 | 作用 | 默认 |
|---|---|---|
| `LLM_BACKEND` | `ollama` 或 `stub`（不用模型） | `ollama` |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | Ollama 地址和模型 | `http://localhost:11434` / `qwen2.5:7b-instruct` |
| `NOTE_OLLAMA_MODEL` | 笔记抽取用的模型 | `qwen2.5:14b-instruct` |
| `LLM_TIMEOUT` / `LLM_TEMPERATURE` | 模型超时（秒）和温度 | `240` / `0.3` |
| `AMAP_WEB_KEY` | 高德 Web 服务 Key（只在后端用） | 不配则国内地图退回 OpenStreetMap |
| `AMAP_JS_KEY` / `AMAP_JS_SECRET` | 高德 JS Key 和安全码（下发给浏览器，域名白名单填你的访问域名） | 同上 |
| `XHS_COOKIE` | 你自己的小红书登录 Cookie，只用来读你粘贴的单篇链接 | 不配则只能粘贴文本 |
| `ATP_DATA_DIR` | 行程库和附件放哪 | `data/plans` |
| `CITY_GENERATED_DIR` | 自动现建的城市数据放哪 | `data/generated` |
| `TILE_CACHE_DIR` | 导出用的地图瓦片缓存 | `data/tile-cache` |

高德的调用量有自留上限（`app/maps/quota.py`），到了自动退回 OpenStreetMap。Key 和 Cookie 只放 `.env` 或环境变量，不要提交，也不要发给别人。

## 备份和升级

- **备份**：行程、版本、附件都在 `ATP_DATA_DIR`（Docker 里是卷 `plan-store`）。备份就是复制这个目录；Docker 可以用 `docker run --rm -v plan-store:/d -v "$PWD":/b alpine tar czf /b/plans-backup.tgz -C /d .`。
- **升级**：`git pull`，然后 `docker compose up --build`（方式二重新 `pip install -e .` 和 `npm ci`）。
- 自动现建的城市数据在 `CITY_GENERATED_DIR`，也值得一起备份（丢了要重新建，每座城市要几分钟）。

## 安全

- **没有账号和登录**：任何能访问这个服务的人都能看、改所有行程（`/plans`）。接口允许任意来源的跨域请求。
- 所以只在自己的电脑、家庭局域网里用；要给外地朋友用，放在带认证的反向代理后面（例如 Caddy / nginx 加 Basic Auth，或 Tailscale 这类私有网络），不要直接开放到公网。
- 服务器上的 `.env` 里有你的 Key 和 Cookie，权限只给自己。

## 出问题时

- 打不开页面：先看 http://localhost:8137/health 是否返回正常；Docker 里 `docker compose logs api`。
- 说明文字全是模板：Ollama 没连上。确认 `ollama serve` 在跑、模型已拉取；Docker 里还要确认能解析 `host.docker.internal`（Linux 靠 `extra_hosts`，已在 compose 里写好）。
- PDF 里中文是方块：缺中文字体，装 Noto CJK。
- 地图是 OpenStreetMap 而不是高德：没配 Key，或本月额度到了。
- 前端连不上后端：`VITE_API_BASE` 写的地址浏览器访问不到，改后要重新构建 `web`。

## 验证状态

`docker compose config` 能通过；镜像上次完整构建和启动实测是 2026-10-09，之后代码有变化，这次整理文档时本机 Docker 没有运行，没有重新构建。升级后第一次构建如果遇到问题，请以 `/health` 和 `docker compose logs` 为准反馈。
