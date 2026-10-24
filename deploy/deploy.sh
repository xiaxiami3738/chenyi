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
mkdir -p data deploy/certs deploy/certbot-www deploy/runtime
chown -R 10001:10001 data

echo "==> 选择 nginx 配置（证书存在则启用 HTTPS）"
if [ -f deploy/certs/fullchain.pem ] && [ -f deploy/certs/privkey.pem ]; then
    cp deploy/nginx-https.conf deploy/runtime/nginx.conf
    echo "    已检测到证书 -> 使用 nginx-https.conf"
else
    cp deploy/nginx.conf deploy/runtime/nginx.conf
    echo "    未检测到证书 -> 使用 nginx.conf（HTTP，待备案通过后签发证书）"
fi

echo "==> 重建并启动容器"
cd deploy
if [ -f ../.env ]; then
    docker compose --env-file ../.env up -d --build --remove-orphans
else
    docker compose up -d --build --remove-orphans
fi

# nginx.conf 是以单文件 bind mount 挂进容器的，git reset --hard 替换文件 inode 后
# 旧容器仍指向旧 inode，不会自动加载新配置，因此每次部署都强制重建 nginx 容器。
if [ -f ../.env ]; then
    docker compose --env-file ../.env up -d --no-deps --force-recreate nginx
else
    docker compose up -d --no-deps --force-recreate nginx
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