#!/bin/bash
# Деплой бота: код на GitHub → Render Blueprint.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

GH_USER="${GITHUB_USER:-zenzenzen33378-dev}"
GH_REPO="${GITHUB_REPO:-interior-telegram-bot}"
CLEAN_URL="https://github.com/${GH_USER}/${GH_REPO}.git"

echo ""
echo "=== Деплой Telegram-бота на Render ==="
echo ""

if command -v security >/dev/null 2>&1; then
  security delete-internet-password -s github.com 2>/dev/null || true
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "Нужен GitHub Personal Access Token."
  echo ""
  echo "Создайте classic-токен (начинается с ghp_):"
  echo "  https://github.com/settings/tokens"
  echo "  → Generate new token (classic)"
  echo "  → галочка «repo»"
  echo ""
  echo "Проверить токен: ./check-github-token.sh"
  echo ""
  read -r -s -p "Вставьте токен и Enter: " GITHUB_TOKEN
  echo ""
fi

GITHUB_TOKEN="$(echo "$GITHUB_TOKEN" | tr -d '[:space:]')"

if [ -z "$GITHUB_TOKEN" ]; then
  echo "Токен не указан."
  exit 1
fi

if [ -x "$ROOT/check-github-token.sh" ]; then
  if ! "$ROOT/check-github-token.sh" "$GITHUB_TOKEN"; then
    exit 1
  fi
fi

if [ ! -d .git ]; then
  git init -b main
fi

git remote remove origin 2>/dev/null || true
git remote add origin "$CLEAN_URL"

git add -A
if ! git diff --cached --quiet; then
  git commit -m "Деплой бота на Render" || true
fi

echo ""
echo "Загрузка на GitHub…"

PUSH_URL="https://${GH_USER}:${GITHUB_TOKEN}@github.com/${GH_USER}/${GH_REPO}.git"

if ! GIT_TERMINAL_PROMPT=0 git -c credential.helper= push -u "$PUSH_URL" main 2>&1; then
  echo ""
  echo "══════════════════════════════════════════════════════"
  echo "  Ошибка push"
  echo ""
  echo "  1) Создайте репозиторий: https://github.com/new"
  echo "     Имя: ${GH_REPO}, без README"
  echo "  2) Токен classic (ghp_), галочка «repo»"
  echo "  3) ./check-github-token.sh"
  echo "══════════════════════════════════════════════════════"
  exit 1
fi

echo ""
echo "✓ Код на GitHub: https://github.com/${GH_USER}/${GH_REPO}"
echo ""
echo "=== Шаг 2: Render (в браузере) ==="
echo ""
echo "1) https://dashboard.render.com/blueprints"
echo "2) New Blueprint Instance → ${GH_USER}/${GH_REPO}"
echo "3) Apply"
echo "4) В Environment добавьте секреты (если не задали при Apply):"
echo "   TELEGRAM_BOT_TOKEN, POLZA_API_KEY, YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY"
echo "5) После деплоя — webhook ЮKassa:"
echo "   https://interior-telegram-bot-luea.onrender.com/webhook/yookassa"
echo ""
echo "⚠ Остановите бота на Mac, иначе два процесса конфликтуют:"
echo "   ./service.sh stop"
echo ""
open "https://dashboard.render.com/blueprints" 2>/dev/null || true
