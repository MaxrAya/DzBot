#!/usr/bin/env bash
# Запускать с sudo на чистом Debian/Ubuntu: sudo ./deploy.sh
# Ожидает, что bot.py лежит рядом с этим скриптом.
set -euo pipefail

APP_DIR="/opt/homework-bot"
SERVICE_USER="${SUDO_USER:-$USER}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Обновляю пакеты и ставлю системные зависимости..."
apt-get update
apt-get install -y python3 python3-venv python3-pip wget gnupg

if ! command -v google-chrome-stable >/dev/null 2>&1; then
    echo "==> Ставлю Google Chrome (для Selenium)..."
    wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y /tmp/chrome.deb
    rm -f /tmp/chrome.deb
fi

echo "==> Создаю $APP_DIR"
mkdir -p "$APP_DIR"

if [ ! -f "$SCRIPT_DIR/bot.py" ]; then
    echo "❌ Не нашёл bot.py рядом со скриптом. Положи его в ту же папку и запусти снова."
    exit 1
fi

cp "$SCRIPT_DIR/bot.py" "$APP_DIR/bot.py"

# Если у тебя уже накопилась история в bot_state.json на маке — закинь его
# в ту же папку, что и deploy.sh, скрипт подхватит и перенесёт.
if [ -f "$SCRIPT_DIR/bot_state.json" ]; then
    cp "$SCRIPT_DIR/bot_state.json" "$APP_DIR/bot_state.json"
    echo "==> Перенёс существующий bot_state.json (логины и история уведомлений сохранены)"
fi

echo "==> Ставлю виртуальное окружение и зависимости..."
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install aiogram selenium webdriver-manager beautifulsoup4

if [ ! -f "$APP_DIR/.env" ]; then
    echo "BOT_TOKEN=вставь_сюда_реальный_токен" > "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

echo ""
echo "✅ Готово. Осталось:"
echo "1. Открыть $APP_DIR/.env и вписать реальный BOT_TOKEN"
echo "2. Настроить systemd (см. homework-bot.service) и запустить сервис"
