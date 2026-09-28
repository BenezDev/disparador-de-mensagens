#!/usr/bin/env bash
#
# Devolve as sessoes do WhatsApp a partir de um dump do backup-sessoes.sh.
#
#   ./restaurar-sessoes.sh [arquivo.sql.gz]     sem argumento: usa o mais recente
#
# SUBSTITUI o banco do Evolution pelo conteudo do dump: o que estiver la agora
# (inclusive chips pareados depois do backup) e perdido. Por isso exige
# confirmacao. Chip cujo WhatsApp encerrou a sessao (401) nao volta por aqui --
# o dump so reergue sessao que ainda e valida no WhatsApp.
set -euo pipefail
cd "$(dirname "$0")"

DESTINO="../dados/backups/sessoes"
ARQ="${1:-$(ls -1t "$DESTINO"/evolution-*.sql.gz 2>/dev/null | head -1)}"
[ -n "$ARQ" ] && [ -f "$ARQ" ] || { echo "  nenhum dump em $DESTINO"; exit 1; }

echo "  vai restaurar: $ARQ"
echo "  o banco atual do Evolution sera substituido."
read -r -p "  digite RESTAURAR para confirmar: " ok
[ "$ok" = "RESTAURAR" ] || { echo "  cancelado"; exit 1; }

# antes de sobrescrever, guarda o estado atual: arrependimento tem volta
MANTER=1000 ./backup-sessoes.sh || true

docker stop evolution >/dev/null
gunzip -c "$ARQ" | docker exec -i evolution-pg psql -U postgres -q -d evolution >/dev/null
docker start evolution >/dev/null
echo "  restaurado. O gateway esta subindo e vai reconectar os chips sozinho."
echo "  acompanhe: docker logs -f evolution"
