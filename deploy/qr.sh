#!/usr/bin/env bash
# Mostra o QR da instancia: salva o PNG e desenha no terminal, se der.
#
# O QR do WhatsApp gira a cada ~20s. Se expirar, rode de novo.
set -euo pipefail
umask 077
cd "$(dirname "$0")"
INST="${1:-principal}"
CHAVE=$(cat .chave)
PNG="../dados/qr/qr-$INST.png"
mkdir -p ../dados/qr
chmod 700 ../dados/qr

curl -sf -H "apikey: $CHAVE" "http://127.0.0.1:8080/instance/connect/$INST" \
 | PNG="$PNG" INST="$INST" python3 -c "
import base64, json, os, shutil, subprocess, sys
d = json.load(sys.stdin)
png, inst = os.environ['PNG'], os.environ['INST']

estado = d.get('instance', {}).get('state') or d.get('state')
if estado == 'open':
    print('  ja conectado — nao precisa de QR.')
    raise SystemExit

b64 = d.get('base64') or ''
if b64.startswith('data:image'):
    open(png, 'wb').write(base64.b64decode(b64.split(',', 1)[1]))
    print(f'  QR salvo em: {os.path.abspath(png)}')
    for visual in (['xdg-open', png],):
        if shutil.which(visual[0]):
            subprocess.Popen(visual, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('  (abrindo no visualizador de imagens)')
            break

codigo = d.get('code') or ''
if codigo and shutil.which('qrencode'):
    print()
    subprocess.run(['qrencode', '-t', 'ANSIUTF8', '-m', '1', codigo])
elif not b64:
    print('  sem QR na resposta:', json.dumps(d)[:200])

print()
print('  Leia com o WhatsApp do chip:')
print('  Configuracoes > Aparelhos conectados > Conectar um aparelho')
print()
print('  O QR expira em ~20s. Expirou? rode de novo:  ./qr.sh ' + inst)
"
