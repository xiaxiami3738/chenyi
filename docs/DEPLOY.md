# 部署手册

> 项目：chenyi · AI 开发入门向导
> 公网：http://118.178.182.215（Demo 模式，无需 Key）

## 部署链路

```
本地 push main ──> GitHub ──> Actions(deploy workflow) ──SSH──> ECS root@118.178.182.215
                                                                    │
                                                                    v
                                              /srv/chenyi/deploy/deploy.sh
                                              （fetch + reset + docker compose up -d --build）
```

- Push 到 `main` 自动触发 `.github/workflows/deploy.yml`。
- 服务器从 gh-proxy.com 镜像拉取仓库代码（服务器直连 GitHub 超时，仓库 remote 已配置为 `https://gh-proxy.com/https://github.com/xiaxiami3738/chenyi.git`）。
- Workflow 通过 `appleboy/ssh-action` 以 root 身份连接 ECS 执行部署脚本。

## GitHub Secrets（仓库 Settings → Secrets → Actions）

| Name | 值 |
| --- | --- |
| `SERVER_HOST` | `118.178.182.215` |
| `SERVER_USER` | `root` |
| `SERVER_SSH_KEY` | ECS root 私钥（`~/.ssh/dsh_ecs` 内容） |

> 本地 push 用的 GitHub SSH（443 端口）：`~/.ssh/config` 中 `Host github.com` 指向 `ssh.github.com:443`，IdentityFile 为 `~/.ssh/github_chenyi`。两者是不同用途：一个是本地→GitHub，一个是 Actions→ECS。

## 服务器布局（/srv/chenyi）

| 路径 | 说明 |
| --- | --- |
| `app/` | FastAPI 应用源码 |
| `deploy/` | Dockerfile / docker-compose.yml / nginx.conf / deploy.sh |
| `data/` | SQLite 数据库 + seed 数据（属主 `10001:10001`，容器非 root 用户需可写） |
| `deploy/certs/` | HTTPS 证书预留目录 |

## 服务拓扑（Docker Compose，项目名 chenyi）

| 服务 | 镜像 | 端口 | 说明 |
| --- | --- | --- | --- |
| `app` | python:3.12-slim 构建 | 8000（仅容器内） | uvicorn 运行 FastAPI，非 root（uid 10001），HEALTHCHECK /api/healthz |
| `nginx` | nginx:1.27-alpine | 80:80, 443:443 | 反向代理 + SSE 关闭缓冲 + 限流 60r/m + 安全头；443 段注释预留 HTTPS |

数据卷：`../data` 挂载到容器 `/app/data`（SQLite 持久化）。

## 常用操作

```bash
# 手动部署（服务器上）
bash /srv/chenyi/deploy/deploy.sh

# 查看日志
docker compose -f /srv/chenyi/deploy/docker-compose.yml logs -f app

# 服务器本机健康检查
curl http://127.0.0.1:8000/api/healthz

# 重建单个服务
docker compose -f /srv/chenyi/deploy/docker-compose.yml up -d --build app
```

## 回滚

```bash
cd /srv/chenyi
git log --oneline -5          # 找到上一个正常提交
git reset --hard <commit>
bash deploy/deploy.sh
```

## 环境变量

通过 `.env` 文件注入（Compose `${VAR:-default}` 语法），不传任何 LLM Key：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `APP_MODE` | `demo` | `demo`/`live`；live 需访客自带 Key |
| `DB_PATH` | `/app/data/app.db` | SQLite 路径 |
| `RATE_LIMIT_PER_MINUTE` | `10` | 写接口限流 |
| `MAX_CONCURRENCY` | `2` | LLM 并发上限 |
| `REQUEST_TIMEOUT_SECONDS` | `120` | LLM 请求超时 |
| `MAX_INPUT_CHARS` | `8000` | 用户目标最大长度 |
| `TRUST_PROXY_HEADERS` | `true` | 信任 Nginx X-Forwarded-For |

## HTTPS 升级（预留）

1. 买域名 + 备案后，把证书放进 `deploy/certs/fullchain.pem` / `privkey.pem`。
2. 取消 nginx.conf 结尾 443 server 段注释，加 HSTS 与 80→301 跳转。
3. `docker compose up -d nginx` 生效。