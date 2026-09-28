#!/usr/bin/env bash
#
# O botao. Sobe tudo o que o disparador precisa e abre o painel.
#
# Pode rodar quantas vezes quiser: o script reconcilia a topologia segura do
# gateway e reaproveita os dados persistentes. Serve tanto para o clique quanto para o
# terminal (`./RODAR.sh`).
#
#   ./RODAR.sh          sobe tudo e abre o painel
#   ./RODAR.sh parar    derruba painel e gateway
#   ./RODAR.sh status   so mostra a situacao
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

PORTA=8777
LOG="dados/logs/painel.log"
mkdir -p dados/logs

# ---- como falar com o usuario -----------------------------------------------
# Clicando no icone nao ha terminal: o aviso tem que ir para a tela.
tem_tela() { [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; }
avisar() {   # avisar <titulo> <texto>
  echo "  $2"
  if tem_tela && command -v zenity >/dev/null; then
    zenity --error --title="$1" --text="$2" --width=430 2>/dev/null &
  elif tem_tela && command -v notify-send >/dev/null; then
    notify-send -u critical "$1" "$2" 2>/dev/null &
  fi
}
ok() { echo "  ok   $1"; }
passo() { echo; echo "» $1"; }

painel_pid() { pgrep -f "disparar.py painel" | head -1; }
painel_vivo() { curl -sf -m 3 -o /dev/null "http://127.0.0.1:$PORTA/api/saude"; }

# ---- parar -------------------------------------------------------------------
if [ "${1:-}" = "parar" ]; then
  p=$(painel_pid); [ -n "$p" ] && kill "$p" && echo "  painel parado"
  docker stop evolution evolution-pg >/dev/null 2>&1 && echo "  gateway parado"
  exit 0
fi

echo "════ DISPARADOR ════"

# ---- 1. docker ---------------------------------------------------------------
passo "gateway do WhatsApp"
if ! command -v docker >/dev/null; then
  avisar "Disparador" "Docker não está instalado.\n\nsudo apt install docker.io"
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  avisar "Disparador" "O Docker não está rodando.\n\nsudo systemctl start docker"
  exit 1
fi
echo "  conferindo a rede privada do gateway..."
if ! ./deploy/subir.sh >/dev/null 2>&1; then
  avisar "Disparador" "O gateway do WhatsApp não subiu.\n\nVeja: docker logs evolution"
  exit 1
fi
ok "gateway de pé"
# copia das sessoes a cada subida: perder o banco do gateway custa QR em todos
if ./deploy/backup-sessoes.sh >/dev/null 2>&1; then ok "sessões copiadas (dados/backups/sessoes)"; fi

# ---- 2. painel ---------------------------------------------------------------
passo "painel"

# O painel carrega o Python uma vez, ao subir. Sem esta checagem o atalho so
# abria o navegador no processo antigo e uma atualizacao nunca entrava.
codigo_novo() {   # codigo_novo <pid>: ha .py em disco mais novo que o processo?
  local idade inicio novo
  idade=$(ps -o etimes= -p "$1" 2>/dev/null | tr -d ' ')
  [ -n "$idade" ] || return 1
  inicio=$(( $(date +%s) - idade ))
  novo=$(find disparar.py disparador -name '*.py' -printf '%T@\n' | sort -n | tail -1)
  [ "${novo%.*}" -gt "$inicio" ]
}
campanha_rodando() {   # reiniciar para o motor: nao se faz com campanha no ar
  python3 - <<'PY_'
import sqlite3, sys
try:
    c = sqlite3.connect("file:dados/disparador.db?mode=ro", uri=True, timeout=5)
    n = c.execute("SELECT COUNT(*) FROM campanha WHERE estado='rodando'").fetchone()[0]
except Exception:
    n = 0
sys.exit(0 if n else 1)
PY_
}
subir_painel() {
  if command -v setsid >/dev/null; then
    setsid -f sh -c 'exec python3 disparar.py painel "$1" >> "$2" 2>&1' sh "$PORTA" "$LOG"
  else
    nohup python3 disparar.py painel "$PORTA" >> "$LOG" 2>&1 &
  fi
  for _ in $(seq 30); do painel_vivo && break; sleep 0.5; done
  if ! painel_vivo; then
    avisar "Disparador" "O painel não respondeu.\n\nVeja: $(pwd)/$LOG"
    exit 1
  fi
}

if painel_vivo; then
  p=$(painel_pid)
  if [ -n "$p" ] && codigo_novo "$p"; then
    if campanha_rodando; then
      ok "já estava rodando — atualização pendente: pause as campanhas e abra de novo"
      if tem_tela && command -v notify-send >/dev/null; then
        notify-send "Disparador" "Há uma atualização. Pause as campanhas e abra o atalho de novo para aplicar." 2>/dev/null &
      fi
    else
      # SIGINT, nao SIGTERM: o painel encerra o motor esperando a mensagem em
      # voo terminar. A sessao dos chips vive no gateway e nao e tocada.
      kill -INT "$p" 2>/dev/null
      for _ in $(seq 40); do kill -0 "$p" 2>/dev/null || break; sleep 0.5; done
      kill -0 "$p" 2>/dev/null && kill "$p" 2>/dev/null
      subir_painel
      ok "painel atualizado para a versão nova"
    fi
  else
    ok "já estava rodando"
  fi
else
  [ -n "$(painel_pid)" ] && kill "$(painel_pid)" 2>/dev/null   # travado: recomeca
  subir_painel
  ok "painel no ar"
fi

# ---- 3. situacao -------------------------------------------------------------
passo "situação"
python3 - <<'PY'
from disparador import db, servidor
db.conn()
d = servidor.estado_geral()
c = d.get("chips") or {}
print(f"  {d['contatos']:,} contatos na base".replace(",", "."))
print(f"  chips: {c.get('conectados',0)} de {c.get('total',0)} conectados"
      + (f" · {c.get('capacidade_hoje',0)} msg/dia" if c.get("capacidade_hoje") else ""))
j = d["janela"]
print(f"  janela: {'aberta' if j['aberta'] else j['motivo']}")
print(f"  motor: {d['motor'].get('situacao')}")
fila = (d.get("envios") or {}).get("fila", 0)
if fila:
    print(f"  {fila:,} mensagens na fila".replace(",", "."))
if not c.get("conectados"):
    print()
    print("  » nenhum chip conectado — aba CHIPS do painel, botão [ler QR]")
PY

# ---- 4. abrir ----------------------------------------------------------------
if tem_tela && command -v xdg-open >/dev/null && [ "${1:-}" != "status" ]; then
  xdg-open "http://127.0.0.1:$PORTA" >/dev/null 2>&1 &
fi
echo
echo "  painel: http://127.0.0.1:$PORTA"
echo "  parar:  ./RODAR.sh parar"
