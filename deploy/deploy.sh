#!/usr/bin/env bash
# Деплой на Raspberry Pi:  ./deploy/deploy.sh pi@192.168.8.112
# Требуется SSH-доступ. Файл .env копируется вместе с кодом, база data/ на Pi не трогается.
set -euo pipefail
HOST="${1:?Укажите пользователь@хост, например pi@192.168.8.112}"
cd "$(dirname "$0")/.."

rsync -az --delete \
  --exclude .venv --exclude data --exclude .git --exclude __pycache__ --exclude .pytest_cache --exclude .DS_Store \
  ./ "$HOST:weightbot/"

ssh "$HOST" 'set -e
  cd ~/weightbot
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  mkdir -p ~/.config/systemd/user
  cp deploy/weightbot.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable weightbot
  systemctl --user restart weightbot
  loginctl enable-linger "$USER"
  sleep 3
  systemctl --user --no-pager status weightbot | head -12'
