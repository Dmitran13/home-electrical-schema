#!/usr/bin/env bash
# Деплой дашборда электрощита дома на dmitran.fvds.ru
# Запуск: bash deploy/deploy-electro.sh
set -e

SERVER="root@dmitran.fvds.ru"
DEPLOY_DIR="/opt/projects/home-electrical-schema"
NGINX_CONF="/etc/nginx/electro.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Копируем статику ==="
ssh "$SERVER" "mkdir -p '$DEPLOY_DIR'"
rsync -az "$PROJECT_DIR/index.html" "$PROJECT_DIR/schema_data.json" "$PROJECT_DIR/electrical_schema.svg" "$SERVER:$DEPLOY_DIR/"

echo "=== Пароль доступа (basic-auth) ==="
ssh "$SERVER" "
  if [ ! -f /etc/nginx/.htpasswd-electro ]; then
    echo '  ВНИМАНИЕ: /etc/nginx/.htpasswd-electro не найден на сервере — создайте вручную:'
    echo '  printf \"admin:%s\n\" \"\$(openssl passwd -apr1 \"ПАРОЛЬ\")\" > /etc/nginx/.htpasswd-electro'
  else
    echo '  .htpasswd-electro уже есть — не трогаем'
  fi
"

echo "=== Обновляем nginx (electro.conf) ==="
scp "$SCRIPT_DIR/electro.conf" "$SERVER:$NGINX_CONF"

for f in /etc/nginx/sites-available/dmitran /etc/nginx/sites-enabled/dmitran; do
  ssh "$SERVER" "grep -q 'electro.conf' '$f' || python3 -c \"
content = open('$f').read()
include_line = '    include /etc/nginx/electro.conf;   # home-electrical-schema (статика)'
content = content.rstrip()
if content.endswith('}'):
    content = content[:-1].rstrip() + '\n' + include_line + '\n}'
open('$f', 'w').write(content)
print('  Добавлен include electro.conf в $f')
\""
done

echo "=== robots.txt (noindex для /electro/ — личные данные щита) ==="
ssh "$SERVER" "grep -q '^Disallow: /electro/\$' /var/www/html/robots.txt 2>/dev/null || printf '\nUser-agent: *\nDisallow: /electro/\n' >> /var/www/html/robots.txt"

echo "=== nginx -t && reload ==="
ssh "$SERVER" "nginx -t && systemctl reload nginx"

echo ""
echo "✓ Деплой завершён: https://dmitran.fvds.ru/electro/"
