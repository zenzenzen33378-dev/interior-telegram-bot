#!/bin/bash
# Полный деплой: GitHub (если нужно) → Render API → остановка Mac.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo ""
echo "=== Деплой бота на Render ==="
echo ""

# 1. GitHub — push если есть токен
if [ -n "${GITHUB_TOKEN:-}" ]; then
  echo "→ Загрузка на GitHub…"
  "$ROOT/deploy-online.sh" || true
else
  echo "→ GitHub: код уже на https://github.com/zenzenzen33378-dev/interior-telegram-bot"
  echo "  (для обновления: export GITHUB_TOKEN=ghp_... && ./deploy-online.sh)"
fi

echo ""
echo "→ Синхронизация с Render…"
chmod +x "$ROOT/render-sync.sh"
"$ROOT/render-sync.sh"
