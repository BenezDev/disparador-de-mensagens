#!/usr/bin/env bash
#
# Copia as sessoes do WhatsApp (credenciais do Baileys) para fora do Docker.
#
#   ./backup-sessoes.sh            gera dados/backups/sessoes/evolution-<data>.sql.gz
#
# No Evolution v2 com Postgres as sessoes moram no banco `evolution` (tabelas
# Instance e Session), nao no volume evolution_instances. Perder esse banco
# custa ler o QR em todos os chips de novo; este dump e o seguro contra isso.
# Roda em segundos e guarda os ultimos 30. Restaurar: ./restaurar-sessoes.sh
set -euo pipefail
umask 077
cd "$(dirname "$0")"

DESTINO="../dados/backups/sessoes"
MANTER="${MANTER:-30}"
mkdir -p "$DESTINO"; chmod 700 "$DESTINO"

docker ps -q -f name='^evolution-pg$' | grep -q . || {
  echo "  evolution-pg nao esta rodando; nada para copiar"; exit 1; }

ARQ="$DESTINO/evolution-$(date +%Y%m%dT%H%M).sql.gz"
docker exec evolution-pg pg_dump -U postgres --clean --if-exists evolution | gzip > "$ARQ"
chmod 600 "$ARQ"

# so vale a pena avisar se o dump esta suspeito de vazio
if [ "$(stat -c %s "$ARQ")" -lt 2000 ]; then
  echo "  ATENCAO: dump pequeno demais ($ARQ)"; exit 1
fi

ls -1t "$DESTINO"/evolution-*.sql.gz | tail -n +"$((MANTER + 1))" | xargs -r rm -f
echo "  sessoes copiadas -> $ARQ"
