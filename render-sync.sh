#!/bin/bash
# Синхронизация .env → Render + деплой. Нужен RENDER_API_KEY в .env или в переменной окружения.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
SERVICE_NAME="${RENDER_SERVICE_NAME:-interior-telegram-bot-luea}"
HEALTH_URL="https://${SERVICE_NAME}.onrender.com/health"

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

API_KEY="${RENDER_API_KEY:-}"
if [ -z "$API_KEY" ]; then
  echo "Нужен Render API Key."
  echo "1) https://dashboard.render.com/u/settings#api-keys → Create API Key"
  echo "2) Добавьте в .env: RENDER_API_KEY=rnd_..."
  echo "   или: export RENDER_API_KEY=rnd_..."
  exit 1
fi

api() {
  curl -sS -H "Authorization: Bearer $API_KEY" -H "Accept: application/json" "$@"
}

echo "Ищу сервис «$SERVICE_NAME» на Render…"
SERVICES_JSON="$(api "https://api.render.com/v1/services?limit=100")"
SERVICE_ID="$(echo "$SERVICES_JSON" | python3 -c "
import json, sys
name = sys.argv[1]
data = json.load(sys.stdin)
# Точное имя или префикс (Render добавляет суффикс: interior-telegram-bot-luea)
candidates = []
for item in data:
    s = item.get('service') or item
    n = s.get('name', '')
    if n == name or n.startswith('interior-telegram-bot'):
        candidates.append((n, s['id']))
if not candidates:
    sys.exit(0)
# Предпочитаем точное совпадение
for n, sid in candidates:
    if n == name:
        print(sid)
        break
else:
    print(candidates[0][1])
" "$SERVICE_NAME" 2>/dev/null || true)"

if [ -z "$SERVICE_ID" ]; then
  echo ""
  echo "Сервис «$SERVICE_NAME» не найден на Render."
  echo ""
  echo "Создайте Blueprint вручную (один раз):"
  echo "  1) https://dashboard.render.com/blueprints"
  echo "  2) New Blueprint Instance → zenzenzen33378-dev/interior-telegram-bot"
  echo "  3) Blueprint Name: makearoom-bot, Branch: main, Path: render.yaml"
  echo "  4) Apply → введите 4 секрета (или запустите этот скрипт снова после создания)"
  echo ""
  open "https://dashboard.render.com/blueprints" 2>/dev/null || true
  exit 1
fi

echo "✓ Сервис найден: $SERVICE_ID"

# Переменные для Render (из .env)
ENV_KEYS=(
  TELEGRAM_BOT_TOKEN
  POLZA_API_KEY
  POLZA_MODEL
  POLZA_STRENGTH
  YOOKASSA_SHOP_ID
  YOOKASSA_SECRET_KEY
  SUBSCRIPTION_PRICE_RUB
  SUBSCRIPTION_DAYS
  SUBSCRIPTION_GENERATIONS
  SUBSCRIPTION_ADMIN_USERNAMES
)

PAYLOAD="$(python3 - <<'PY'
import json, os
keys = [
    "TELEGRAM_BOT_TOKEN", "POLZA_API_KEY", "POLZA_MODEL", "POLZA_STRENGTH",
    "YOOKASSA_SHOP_ID", "YOOKASSA_SECRET_KEY", "SUBSCRIPTION_PRICE_RUB",
    "SUBSCRIPTION_DAYS", "SUBSCRIPTION_GENERATIONS", "SUBSCRIPTION_ADMIN_USERNAMES",
]
items = []
for k in keys:
    v = os.environ.get(k, "")
    if v:
        items.append({"key": k, "value": v})
print(json.dumps(items))
PY
)"

if [ "$PAYLOAD" = "[]" ]; then
  echo "В .env нет переменных для Render."
  exit 1
fi

echo "Обновляю переменные окружения…"
RESP="$(api -X PUT "https://api.render.com/v1/services/${SERVICE_ID}/env-vars" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD")" || true

if echo "$RESP" | grep -qi "error\|unauthorized\|forbidden"; then
  echo "Ошибка API: $RESP"
  exit 1
fi

echo "Запускаю деплой…"
api -X POST "https://api.render.com/v1/services/${SERVICE_ID}/deploys" \
  -H "Content-Type: application/json" \
  -d '{"clearCache":"do_not_clear"}' >/dev/null

echo "Жду готовности (до 8 мин)…"
for i in $(seq 1 48); do
  CODE="$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$HEALTH_URL" 2>/dev/null || echo "000")"
  if [ "$CODE" = "200" ]; then
    echo ""
    echo "✓ Бот в облаке: $HEALTH_URL → ok"
    echo "  Webhook ЮKassa: https://${SERVICE_NAME}.onrender.com/webhook/yookassa"
    echo ""
    echo "Останавливаю локальный бот на Mac…"
    "$ROOT/service.sh" stop 2>/dev/null || true
    echo "Готово. Проверьте @MakeaRoomBot в Telegram."
    exit 0
  fi
  printf "."
  sleep 10
done

echo ""
echo "Деплой ещё идёт или сервис спит. Проверьте логи:"
echo "  https://dashboard.render.com"
echo "После «Live» остановите Mac-бота: ./service.sh stop"
