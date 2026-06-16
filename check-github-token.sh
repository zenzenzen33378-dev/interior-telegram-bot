#!/bin/bash
# Проверка GitHub-токена перед git push.
set -euo pipefail

GH_USER="${GITHUB_USER:-zenzenzen33378-dev}"
GH_REPO="${GITHUB_REPO:-interior-telegram-bot}"

if [ -z "${1:-}" ]; then
  read -r -s -p "GitHub token (ghp_...): " TOKEN
  echo ""
else
  TOKEN="$1"
fi

TOKEN="$(echo "$TOKEN" | tr -d '[:space:]')"

if [ -z "$TOKEN" ]; then
  echo "Токен пустой."
  exit 1
fi

AUTH_HEADER="Authorization: token ${TOKEN}"
if [[ "$TOKEN" == github_pat_* ]]; then
  AUTH_HEADER="Authorization: Bearer ${TOKEN}"
fi

echo ""
echo "Проверка токена…"

USER_JSON="$(curl -s -H "$AUTH_HEADER" -H "Accept: application/vnd.github+json" https://api.github.com/user)"
LOGIN="$(echo "$USER_JSON" | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('login',''))" 2>/dev/null || true)"
MSG="$(echo "$USER_JSON" | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null || true)"

if [ -z "$LOGIN" ]; then
  echo "✗ Токен не принят: ${MSG:-неизвестная ошибка}"
  echo ""
  echo "Создайте новый classic-токен:"
  echo "  https://github.com/settings/tokens → Generate new token (classic) → repo"
  exit 1
fi

echo "✓ Вошли как: $LOGIN"

if [ "$LOGIN" != "$GH_USER" ]; then
  echo "⚠ Логин токена ($LOGIN) ≠ репозиторий ($GH_USER)"
  echo "  Токен должен быть от аккаунта $GH_USER"
fi

REPO_JSON="$(curl -s -H "$AUTH_HEADER" -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${GH_USER}/${GH_REPO}")"

REPO_MSG="$(echo "$REPO_JSON" | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('message',''))" 2>/dev/null || true)"
CAN_PUSH="$(echo "$REPO_JSON" | /usr/bin/python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('permissions',{}).get('push', False))" 2>/dev/null || true)"

if [ "$REPO_MSG" = "Not Found" ]; then
  echo "✗ Репозиторий ${GH_USER}/${GH_REPO} не найден"
  echo "  Создайте: https://github.com/new (имя: ${GH_REPO}, без README)"
  exit 1
fi

if [ "$CAN_PUSH" != "True" ]; then
  echo "✗ Нет права push в репозиторий"
  echo ""
  if [[ "$TOKEN" == github_pat_* ]]; then
    echo "Fine-grained токен: откройте настройки токена и добавьте:"
    echo "  Repository access → ${GH_REPO}"
    echo "  Permissions → Contents: Read and write"
  else
    echo "Classic токен: при создании отметьте галочку «repo» (полный доступ)"
  fi
  exit 1
fi

echo "✓ Push в ${GH_USER}/${GH_REPO} разрешён"
echo ""
echo "Теперь запустите: ./deploy-online.sh"
echo "(или export GITHUB_TOKEN='...' && ./deploy-online.sh)"
