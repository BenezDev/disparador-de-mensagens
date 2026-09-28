#!/usr/bin/env bash
# Instala o painel como servico: sobe no boot e reinicia se cair.
set -euo pipefail
cd "$(dirname "$0")"
sudo cp disparador.service /etc/systemd/system/disparador.service
sudo systemctl daemon-reload
sudo systemctl enable --now disparador
sleep 2
systemctl --no-pager status disparador | head -6
echo
echo "  painel: http://127.0.0.1:8777"
echo "  log:    journalctl -u disparador -f"
