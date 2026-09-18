#!/usr/bin/env bash
# Деплой дашборда электрощита дома на dmitran.fvds.ru
# Запуск: bash deploy/deploy-electro.sh
set -e

SERVER="root@dmitran.fvds.ru"
DEPLOY_DIR="/opt/projects/home-electrical-schema"
NGINX_CONF="/etc/nginx/electro.conf"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Копируем статику и бэкенд кнопки 'Обновить' ==="
ssh "$SERVER" "mkdir -p '$DEPLOY_DIR'"
rsync -az "$PROJECT_DIR/index.html" "$PROJECT_DIR/electrical_schema.svg" \
  "$PROJECT_DIR/refresh_server.py" "$PROJECT_DIR/tuya_monitor.py" "$SERVER:$DEPLOY_DIR/"

# schema_data.json на сервере — живой файл, его пишет кнопка "Обновить" через Tuya Cloud.
# Кладём из git только при первом деплое, иначе каждый деплой затирал бы live-данные старым снимком.
ssh "$SERVER" "[ -f '$DEPLOY_DIR/schema_data.json' ] || echo NEED_SEED" | grep -q NEED_SEED \
  && rsync -az "$PROJECT_DIR/schema_data.json" "$SERVER:$DEPLOY_DIR/" \
  && echo "  schema_data.json отсутствовал на сервере — залили стартовый снимок из git" \
  || echo "  schema_data.json на сервере уже live — не трогаем"

# rsync льёт файлы от root, а бэкенд (systemd User=www-data) должен иметь право их перезаписывать
ssh "$SERVER" "chown -R www-data:www-data '$DEPLOY_DIR'"

echo "=== Ключи Tuya Cloud (.env на сервере) ==="
ssh "$SERVER" "
  if [ ! -f '$DEPLOY_DIR/.env' ]; then
    echo '  ВНИМАНИЕ: $DEPLOY_DIR/.env не найден — создайте вручную на сервере:'
    echo '  cat > $DEPLOY_DIR/.env <<ENVEOF'
    echo '  TUYA_API_KEY=...'
    echo '  TUYA_API_SECRET=...'
    echo '  TUYA_REGION=eu'
    echo '  ENVEOF'
  else
    echo '  .env уже есть — не трогаем'
  fi
"

echo "=== systemd-сервис бэкенда (electro-refresh) ==="
scp "$SCRIPT_DIR/electro-refresh.service" "$SERVER:/etc/systemd/system/electro-refresh.service"
ssh "$SERVER" "systemctl daemon-reload && systemctl enable --now electro-refresh"

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
