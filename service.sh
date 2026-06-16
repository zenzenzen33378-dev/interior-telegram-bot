#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.makearoom.telegram-bot"
PLIST_SRC="$DIR/com.makearoom.telegram-bot.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
RUNTIME="$HOME/makearoom-bot"
RUNTIME_APP="$RUNTIME/app"
RUNTIME_VENV="$RUNTIME/venv"

bot_pids() {
  for pid in $(pgrep -f "Python.*bot.py" 2>/dev/null); do
    cwd=$(lsof -a -d cwd -p "$pid" 2>/dev/null | awk 'NR==2 {print $NF}')
    if [ "$cwd" = "$DIR" ] || [ "$cwd" = "$RUNTIME_APP" ]; then
      echo "$pid"
    fi
  done
}

stop_manual() {
  for pid in $(bot_pids); do
    kill "$pid" 2>/dev/null || true
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  done
  rm -f "$DIR/.bot.pid"
}

sync_app() {
  mkdir -p "$RUNTIME_APP"
  cp "$DIR/bot.py" "$DIR/subscriptions.py" "$DIR/requirements.txt" "$RUNTIME_APP/"
  if [ -f "$DIR/.env" ]; then
    cp "$DIR/.env" "$RUNTIME_APP/.env"
  fi
}

ensure_runtime() {
  sync_app
  if [ ! -d "$RUNTIME_VENV" ]; then
    python3 -m venv "$RUNTIME_VENV"
  fi
  "$RUNTIME_VENV/bin/pip" install -q -r "$RUNTIME_APP/requirements.txt"
}

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$RUNTIME"
  cat >"$RUNTIME/run-bot.sh" <<'EOF'
#!/bin/bash
set -euo pipefail
cd "$HOME/makearoom-bot/app"
exec "$HOME/makearoom-bot/venv/bin/python3" -u bot.py
EOF
  chmod +x "$RUNTIME/run-bot.sh"
  sed \
    -e "s|__WORKDIR__|$RUNTIME|g" \
    -e "s|__HOME__|$HOME|g" \
    "$PLIST_SRC" >"$PLIST_DST"
}

launch_loaded() {
  launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

bot_running() {
  [ -n "$(bot_pids)" ]
}

cmd_install() {
  ensure_runtime
  stop_manual
  write_plist

  if launch_loaded; then
    launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST_DST"

  ready=0
  for _ in $(seq 1 30); do
    if bot_running; then
      ready=1
      break
    fi
    if [ -f "$HOME/makearoom-bot.log" ] && grep -q "Run polling for bot" "$HOME/makearoom-bot.log" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 2
  done

  if [ "$ready" = 1 ]; then
    echo "Сервис установлен и запущен."
    echo "Бот стартует автоматически при входе в macOS и перезапускается при сбоях."
    cmd_status
  else
    echo "Не удалось запустить сервис. Смотрите лог:"
    tail -20 "$HOME/makearoom-bot.error.log" 2>/dev/null || true
    exit 1
  fi
}

cmd_uninstall() {
  if launch_loaded; then
    launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
  fi
  rm -f "$PLIST_DST"
  stop_manual
  echo "Сервис удалён. Файлы в $RUNTIME сохранены."
}

cmd_start() {
  if ! launch_loaded; then
    echo "Сервис не установлен. Запустите: ./service.sh install"
    exit 1
  fi
  launchctl kickstart -k "$DOMAIN/$LABEL"
  sleep 3
  cmd_status
}

cmd_stop() {
  if launch_loaded; then
    launchctl bootout "$DOMAIN" "$PLIST_DST" 2>/dev/null || true
  fi
  stop_manual
  echo "Бот остановлен."
}

cmd_restart() {
  ensure_runtime
  if launch_loaded; then
    launchctl kickstart -k "$DOMAIN/$LABEL"
  else
    stop_manual
    nohup "$RUNTIME/run-bot.sh" >>"$HOME/makearoom-bot.log" 2>&1 </dev/null &
    echo $! >"$DIR/.bot.pid"
  fi
  sleep 5
  cmd_status
}

cmd_status() {
  echo "Проект: $DIR"
  echo "Сервис: $RUNTIME"
  if launch_loaded; then
    echo "LaunchAgent: установлен ($PLIST_DST)"
    launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -E "state =|pid =|last exit code =" || true
  else
    echo "LaunchAgent: не установлен"
  fi

  pids=$(bot_pids | tr '\n' ' ')
  if [ -n "$pids" ]; then
    echo "Процесс бота: запущен (PID $pids)"
  else
    echo "Процесс бота: не запущен"
  fi

  if [ -f "$HOME/makearoom-bot.log" ]; then
    echo "--- последние строки лога ---"
    tail -5 "$HOME/makearoom-bot.log"
  fi
}

cmd_logs() {
  tail -f "$HOME/makearoom-bot.log"
}

usage() {
  cat <<EOF
Управление фоновым сервисом бота (macOS LaunchAgent).

  ./service.sh install    — установить автозапуск (рекомендуется)
  ./service.sh uninstall  — убрать автозапуск
  ./service.sh status     — статус
  ./service.sh restart    — обновить код и перезапустить
  ./service.sh stop       — остановить
  ./service.sh logs       — смотреть лог в реальном времени

После изменений в bot.py запускайте: ./service.sh restart
EOF
}

case "${1:-}" in
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  start) cmd_start ;;
  stop) cmd_stop ;;
  restart) cmd_restart ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  *) usage; exit 1 ;;
esac
