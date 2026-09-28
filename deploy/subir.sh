#!/usr/bin/env bash
#
# Sobe o Evolution API (gateway WhatsApp nao-oficial) e conecta um chip.
# Precisa so de Docker.
#
#   ./subir.sh [nome-da-instancia]
#
# Sao dois containers numa rede Docker privada. Apenas o gateway e publicado,
# e somente em 127.0.0.1:8080. O callback usa host.docker.internal:8788, onde
# existe um listener isolado que nao possui rotas administrativas.
set -euo pipefail
umask 077
cd "$(dirname "$0")"

# Sem argumento, a instancia padrao so e criada no primeiro uso (gateway vazio).
# O atalho roda este script a cada clique: criar 'principal' sempre fazia ela
# renascer depois de apagada e ocupar vaga acima do teto de chips.
INST_PEDIDA="${1:-}"
INST="${1:-principal}"
IMAGEM="${EVO_IMAGEM:-evoapicloud/evolution-api:v2.3.7}"
PG_IMAGEM="postgres:16-alpine"
WEBHOOK_PORTA="${WEBHOOK_PORTA:-8788}"
REDE="disparador-evolution"

seg() { head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 32; }

# a apikey e a unica coisa entre quem alcanca a porta e a sua sessao de WhatsApp
if [ -f .chave ]; then CHAVE=$(cat .chave); else
  CHAVE=$(seg); printf '%s' "$CHAVE" > .chave; chmod 600 .chave
  echo "  apikey gerada -> deploy/.chave"
fi
if [ -f .pgsenha ]; then PGSENHA=$(cat .pgsenha); else
  PGSENHA=$(seg); printf '%s' "$PGSENHA" > .pgsenha; chmod 600 .pgsenha
fi
if [ -f ../.webhook-chave ]; then WEBHOOK_CHAVE=$(cat ../.webhook-chave); else
  WEBHOOK_CHAVE=$(seg); printf '%s' "$WEBHOOK_CHAVE" > ../.webhook-chave
  chmod 600 ../.webhook-chave
fi
chmod 600 .chave .pgsenha ../.webhook-chave

# As sessoes do WhatsApp vivem no volume evolution_instances. Recriar
# container preserva; perder o volume custa reler QR em todos os chips.
if ! docker volume inspect evolution_instances >/dev/null 2>&1; then
  echo "  ATENCAO: volume de sessoes ausente — os chips vao pedir QR de novo"
fi

docker network inspect "$REDE" >/dev/null 2>&1 || docker network create "$REDE" >/dev/null

subiu() { [ -n "$(docker ps -q -f name="^$1\$")" ]; }
existe() { [ -n "$(docker ps -aq -f name="^$1\$")" ]; }

# Versoes antigas usavam --network host e deixavam a 8080 em todas as
# interfaces. Recriar os containers preserva os volumes e fecha essa exposicao.
if existe evolution && [ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' evolution)" != "$REDE" ]; then
  echo "  migrando containers antigos para a rede privada (volumes preservados)"
  docker rm -f evolution evolution-pg >/dev/null 2>&1 || true
fi

# --- banco (loopback do host, porta alternativa para nao brigar com nada) ---
if existe evolution-pg; then
  subiu evolution-pg || docker start evolution-pg >/dev/null
else
  echo "  subindo o Postgres na rede privada"
  docker run -d --name evolution-pg --restart unless-stopped \
    --network "$REDE" \
    -e POSTGRES_PASSWORD="$PGSENHA" \
    -e POSTGRES_DB=evolution \
    -v evolution_pg:/var/lib/postgresql/data \
    "$PG_IMAGEM" >/dev/null
fi
printf "  esperando o banco"
for _ in $(seq 40); do
  docker exec evolution-pg pg_isready -q -h 127.0.0.1 -p 5432 2>/dev/null && break
  printf "."; sleep 1
done; echo

# --- gateway ----------------------------------------------------------------
if existe evolution; then
  subiu evolution || docker start evolution >/dev/null
  echo "  container do gateway reaproveitado"
else
  echo "  subindo o gateway ($IMAGEM)"
  docker run -d --name evolution --restart unless-stopped \
    --network "$REDE" \
    --add-host=host.docker.internal:host-gateway \
    -p 127.0.0.1:8080:8080 \
    -v evolution_instances:/evolution/instances \
    -e AUTHENTICATION_API_KEY="$CHAVE" \
    -e SERVER_URL="http://localhost:8080" \
    -e DATABASE_ENABLED=true \
    -e DATABASE_PROVIDER=postgresql \
    -e DATABASE_CONNECTION_URI="postgresql://postgres:${PGSENHA}@evolution-pg:5432/evolution?schema=public" \
    -e DATABASE_CONNECTION_CLIENT_NAME=evolution \
    -e DATABASE_SAVE_DATA_INSTANCE=true \
    -e DATABASE_SAVE_DATA_NEW_MESSAGE=false \
    -e DATABASE_SAVE_MESSAGE_UPDATE=false \
    -e DATABASE_SAVE_DATA_CONTACTS=false \
    -e DATABASE_SAVE_DATA_CHATS=false \
    -e DATABASE_SAVE_DATA_LABELS=false \
    -e DATABASE_SAVE_DATA_HISTORIC=false \
    -e CACHE_REDIS_ENABLED=false \
    -e CACHE_LOCAL_ENABLED=true \
    -e LOG_LEVEL=ERROR \
    -e DEL_INSTANCE=false \
    -e QRCODE_LIMIT=60 \
    `# webhook: e por aqui que o "SAIR" do lead chega no disparador.` \
    `# sem ele nao existe opt-out automatico.` \
    -e WEBHOOK_GLOBAL_ENABLED=true \
    -e WEBHOOK_GLOBAL_URL="http://host.docker.internal:${WEBHOOK_PORTA}/webhook/evolution?token=${WEBHOOK_CHAVE}" \
    -e WEBHOOK_GLOBAL_WEBHOOK_BY_EVENTS=false \
    -e WEBHOOK_EVENTS_MESSAGES_UPSERT=true \
    -e WEBHOOK_EVENTS_MESSAGES_UPDATE=true \
    -e WEBHOOK_EVENTS_SEND_MESSAGE=true \
    -e WEBHOOK_EVENTS_CONNECTION_UPDATE=true \
    -e WEBHOOK_EVENTS_QRCODE_UPDATED=false \
    -e WEBHOOK_EVENTS_PRESENCE_UPDATE=false \
    -e WEBHOOK_EVENTS_CHATS_SET=false \
    -e WEBHOOK_EVENTS_CONTACTS_SET=false \
    "$IMAGEM" >/dev/null
fi

api() { curl -sf -m 15 -H "apikey: $CHAVE" "$@"; }
printf "  esperando o gateway"
for _ in $(seq 60); do
  api http://127.0.0.1:8080/instance/fetchInstances >/dev/null 2>&1 && break
  printf "."; sleep 2
done; echo
api http://127.0.0.1:8080/instance/fetchInstances >/dev/null 2>&1 || {
  echo "  o gateway nao respondeu:"; docker logs --tail 20 evolution; exit 1; }

INSTANCIAS=$(api http://127.0.0.1:8080/instance/fetchInstances)
if printf '%s' "$INSTANCIAS" | grep -q "\"$INST\""; then
  echo "  instancia '$INST' ja existe"
elif [ -z "$INST_PEDIDA" ] && printf '%s' "$INSTANCIAS" | grep -q '"name"'; then
  echo "  chips ja cadastrados no gateway; nenhuma instancia nova criada"
else
  echo "  criando a instancia '$INST'"
  api -X POST http://127.0.0.1:8080/instance/create -H "Content-Type: application/json" \
    -d "{\"instanceName\":\"$INST\",\"integration\":\"WHATSAPP-BAILEYS\",\"qrcode\":true}" >/dev/null
fi

cat <<FIM

  gateway de pe: http://127.0.0.1:8080

  1) LER O QR — pegue o codigo e leia com o WhatsApp do chip
     (Aparelhos conectados -> Conectar um aparelho):

       ./qr.sh $INST

  2) NO PAINEL do disparador, aba config -> evolution:
       url        http://127.0.0.1:8080
       instancia  $INST
       apikey     leia localmente em deploy/.chave

  3) O Evolution local entrega callbacks em 8788 com segredo. Para Meta/Twilio,
     exponha APENAS a porta de webhook:
       cloudflared tunnel --url http://127.0.0.1:${WEBHOOK_PORTA}

  painel 8777 e gateway 8080 ficam somente no loopback.

  parar:  docker stop evolution evolution-pg
  log:    docker logs -f evolution
FIM
