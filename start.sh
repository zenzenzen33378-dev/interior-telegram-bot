#!/bin/bash
cd "$(dirname "$0")"
DIR="$(pwd)"

if [ -f "$HOME/Library/LaunchAgents/com.makearoom.telegram-bot.plist" ]; then
  echo "Используется фоновый сервис. Обновляю и перезапускаю..."
  exec ./service.sh restart
fi

PIDFILE=".bot.pid"
LOGFILE="bot.log"

bot_pids() {
  for pid in $(pgrep -f "Python.*bot.py" 2>/dev/null); do
    cwd=$(lsof -a -d cwd -p "$pid" 2>/dev/null | awk 'NR==2 {print $NF}')
    if [ "$cwd" = "$DIR" ]; then
      echo "$pid"
    fi
  done
}

stop_bots() {
  if [ -f "$PIDFILE" ]; then
    old_pid=$(cat "$PIDFILE")
    kill "$old_pid" 2>/dev/null
    sleep 1
    kill -9 "$old_pid" 2>/dev/null || true
    rm -f "$PIDFILE"
  fi
  for pid in $(bot_pids); do
    kill "$pid" 2>/dev/null
    sleep 1
    kill -9 "$pid" 2>/dev/null || true
  done
}

stop_bots

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

nohup .venv/bin/python -u bot.py >>"$LOGFILE" 2>&1 </dev/null &
new_pid=$!
disown "$new_pid" 2>/dev/null || true
echo "$new_pid" >"$PIDFILE"

ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$new_pid" 2>/dev/null; then
    echo "Ошибка запуска: процесс завершился. Последние строки лога:"
    tail -20 "$LOGFILE" 2>/dev/null
    exit 1
  fi
  if grep -q "Run polling for bot" "$LOGFILE" 2>/dev/null; then
    ready=1
    break
  fi
  sleep 2
done

if [ "$ready" = 1 ]; then
  echo "Бот запущен (PID $new_pid). Лог: $LOGFILE"
else
  echo "Бот запускается (PID $new_pid), Telegram ещё не ответил. Смотрите лог: $LOGFILE"
fi
