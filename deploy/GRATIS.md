# Deploy local sem serviço pago

Esta receita usa Python, Evolution API e Postgres locais. "Sem mensalidade" não
significa canal autorizado nem entrega garantida: o Evolution não é a API
oficial do WhatsApp e uma conta ainda pode sofrer restrições da plataforma.

## Subir

Na raiz do projeto:

```bash
./RODAR.sh
```

Ou apenas o gateway:

```bash
cd deploy
./subir.sh
```

O script mantém dados nos volumes `evolution_instances` e `evolution_pg`, cria
uma rede Docker privada e publica apenas:

- Evolution em `127.0.0.1:8080`;
- painel em `127.0.0.1:8777`;
- webhook autenticado em `0.0.0.0:8788`.

O Postgres não é publicado no host. O container acessa o webhook pelo endereço
de gateway do Docker e leva um segredo próprio na URL.

## Conectar uma conta

```bash
cd deploy
./qr.sh
```

Leia o QR pelo aparelho dedicado. Não use número pessoal ou conta cujo histórico
você não pode perder. A aplicação aceita no máximo 25 instâncias e deixa novas
contas sem envio até configuração explícita de cota e rampa.

## Servidor remoto

Não abra painel nem Evolution na internet. Para administrar:

```bash
ssh -L 8777:127.0.0.1:8777 usuario@servidor
```

Se o provedor precisar alcançar callbacks, encaminhe somente a porta `8788`
por proxy/túnel HTTPS. Preserve corpo bruto e query string: as assinaturas são
validadas antes de qualquer alteração no banco.

## Segredos e permissões

```bash
chmod 700 dados dados/logs deploy/qr 2>/dev/null || true
chmod 600 .painel-chave .webhook-chave deploy/.chave config.json 2>/dev/null || true
```

Variáveis de ambiente têm prioridade sobre `config.json`. Nunca versionar token,
SID, API key, cookie, `app_secret` ou arquivo de chave.

## Operação

```bash
./RODAR.sh status
docker logs --tail 100 evolution
docker logs --tail 100 evolution-pg
./RODAR.sh parar
```

`docker stop` preserva dados. Remover containers também preserva os volumes,
mas remover `evolution_instances` ou `evolution_pg` apaga sessão e banco do
gateway. Isso não é comando de manutenção; faça backup e confirme o alvo antes.

## Regras que continuam valendo

- nada de lista fria só porque veio de fonte pública;
- opt-in comprovado para WhatsApp;
- opt-out imediato e blocklist local;
- campanha de um contato para teste real;
- rampa limita tráfego, não cria conversa artificial;
- resultado ambíguo é conferido no provedor antes de reenviar.
