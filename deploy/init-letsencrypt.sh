#!/usr/bin/env bash
# 一键签发 Let's Encrypt 证书并切换到 HTTPS。
#
# 前置条件：
#   1. 域名已解析到本机公网 IP（A 记录 @ 和 www）；
#   2. 安全组放行 80/443；
#   3. ICP 备案已通过（大陆服务器未备案的域名 80 端口会被拦，certbot 校验会失败）。
#
# 用法（服务器上 /srv/chenyi/deploy 目录执行）：
#   EMAIL=you@example.com bash init-letsencrypt.sh              # 正式签发
#   EMAIL=you@example.com STAGING=1 bash init-letsencrypt.sh    # 演练（不占正式额度，浏览器会报不受信任）
#
# 证书落在 deploy/certs/（compose 已把它挂到容器的 /etc/nginx/certs 与 /etc/letsencrypt）。
set -euo pipefail

DOMAIN="${DOMAIN:-aibeginner.cn}"
WWW_DOMAIN="${WWW_DOMAIN:-www.${DOMAIN}}"
EMAIL="${EMAIL:?请提供邮箱，例如：EMAIL=you@example.com bash init-letsencrypt.sh}"
STAGING="${STAGING:-0}"

cd "$(dirname "$0")"

COMPOSE="docker compose"
if [ -f ../.env ]; then
    COMPOSE="docker compose --env-file ../.env"
fi

echo "==> 域名：${DOMAIN} / ${WWW_DOMAIN}"
echo "==> 邮箱：${EMAIL}（STAGING=${STAGING}）"

echo "==> 1/5 准备目录与 HTTP 配置"
mkdir -p certs certbot-www runtime
cp nginx.conf runtime/nginx.conf

echo "==> 2/5 启动 nginx（HTTP，提供 acme 校验目录）"
$COMPOSE up -d --build app
$COMPOSE up -d --no-deps --force-recreate nginx
sleep 3

echo "==> 3/5 本地自检 acme 目录可访问"
if ! curl -fsS -o /dev/null "http://127.0.0.1/.well-known/acme-challenge/"; then
    echo "    !! acme 目录不可访问（404 也算失败），请检查 nginx 配置" >&2
fi

echo "==> 4/5 申请证书"
EXTRA=""
if [ "${STAGING}" = "1" ]; then
    EXTRA="--staging"
fi
$COMPOSE run --rm --entrypoint certbot certbot certonly \
    --webroot -w /var/www/certbot \
    -d "${DOMAIN}" -d "${WWW_DOMAIN}" \
    --email "${EMAIL}" --agree-tos --no-eff-email \
    --non-interactive --keep-until-expiring ${EXTRA}

echo "==> 5/5 切换到 HTTPS 配置并重建 nginx"
if [ ! -f certs/live/"${DOMAIN}"/fullchain.pem ]; then
    echo "!! 未找到证书文件 certs/live/${DOMAIN}/fullchain.pem，放弃切换" >&2
    exit 1
fi
# nginx 配置里引用的是稳定路径 /etc/nginx/certs/{fullchain,privkey}.pem
ln -sf "live/${DOMAIN}/fullchain.pem" certs/fullchain.pem
ln -sf "live/${DOMAIN}/privkey.pem" certs/privkey.pem

cp nginx-https.conf runtime/nginx.conf
$COMPOSE up -d --no-deps --force-recreate nginx
sleep 3

echo "==> 验证"
$COMPOSE exec -T nginx nginx -t
curl -sS -o /dev/null -w "https://${DOMAIN} -> %{http_code}\n" --resolve "${DOMAIN}:443:127.0.0.1" "https://${DOMAIN}/api/healthz" || true
curl -sS -o /dev/null -w "http://${DOMAIN} -> %{http_code}（应为 301）\n" --resolve "${DOMAIN}:80:127.0.0.1" "http://${DOMAIN}/" || true

echo
echo "==> 完成。后续 renewal 由 certbot 容器每 12 小时检查。"

echo "==> 安装证书重载定时任务（每天 04:17 让 nginx 重新读取证书）"
DEPLOY_DIR="$(pwd)"
cat >/etc/cron.d/chenyi-certbot-reload <<EOF
# chenyi: certbot 续期后让 nginx 重新加载证书
17 4 * * * root cd ${DEPLOY_DIR} && docker compose exec -T nginx nginx -s reload >/dev/null 2>&1 || true
EOF
chmod 644 /etc/cron.d/chenyi-certbot-reload
echo "    已写入 /etc/cron.d/chenyi-certbot-reload"

echo
echo "==> 后续如需手动续期：docker compose run --rm --entrypoint certbot certbot renew --webroot -w /var/www/certbot"
echo "    续期后生效：docker compose exec nginx nginx -s reload"
