#!/usr/bin/env bash
# 服务器端部署脚本：在 /srv/chenyi 拉取最新 main，重建并重启容器，最后自检健康接口。
# 用法（服务器上）：bash deploy/deploy.sh
set -euo pipefail

REPO_DIR="${REPO_DIR:-/srv/chenyi}"
cd "$REPO_DIR"

echo "==> 拉取最新代码"
git fetch --all --prune
git reset --hard origin/main

echo "==> 准备数据目录（容器内以 uid 10001 运行，需要可写 SQLite）"
mkdir -p data deploy/certs
chown -R 10001:10001 data

echo "==> 重建并启动容器"
cd deploy
if [ -f ../.env ]; then
    docker compose --env-file ../.env up -d --build --remove-orphans
else
    docker compose up -d --build --remove-orphans
fi

echo "==> 容器状态"
docker compose ps

echo "==> 健康自检"
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS http://127.0.0.1/api/healthz; then
        echo
        echo "==> 部署完成"
        exit 0
    fi
    echo "等待服务就绪（第 ${i} 次）…"
    sleep 3
done

echo "!! 健康检查失败，最近日志如下：" >&2
docker compose logs --tail=60 >&2
exit 1