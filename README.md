# Disparador de mensagens

Aplicação local para campanhas de SMS e WhatsApp destinadas a contatos com base
válida e, no WhatsApp, **opt-in comprovado**. O projeto trabalha com até 25 contas,
mas não fabrica conversa entre chips nem tenta esconder automação: a chamada
"rampa" apenas limita volume e ritmo nos primeiros dias.



## Primeiros passos (tutorial no painel)

Ao abrir o painel pela primeira vez, um **tutorial** aparece sozinho com os 6
passos — pode fechar em **pular tutorial** e reabrir a qualquer momento no botão
**? tutorial** no topo. Resumo:

1. **config** → credenciais do provedor (Evolution para WhatsApp).
2. **chips** → conectar os números (ler QR).
3. **nova campanha** → mensagem, variáveis e foto opcional.
4. **envio de teste** → digite o **seu** número e clique em *enviar 1 mensagem
   de teste*: chega no seu WhatsApp igual ao que o contato vai receber (com a
   foto). Precisa de ao menos um chip conectado.
5. **campanhas → público → montar fila** → escolhe os contatos. **Sem este
   passo a fila fica vazia**, e ao rodar a campanha só aparece como *concluída*
   sem enviar nada.
6. **rodar** → confirma e dispara.

> Campanha de WhatsApp só dispara para contatos com **opt-in** na lista
> importada (`consentimento_em`/`consentimento_origem`). A base da Receita não
> tem — por isso a fila dela fica em zero.

## Foto no disparo (WhatsApp)

No formulário de campanha, canal **WhatsApp**, há o campo **imagem**. Escolha um
JPG, PNG ou WEBP (até 5 MB): a foto vai junto com a mensagem numa só, e o texto
do modelo — com `{nome}`, `{cidade}` etc. já resolvidos — vira a **legenda**.

- Só WhatsApp via Evolution. SMS não carrega foto; o campo some ao trocar o canal.
- A imagem é guardada em `dados/midia/` pelo hash do conteúdo (a mesma foto em
  várias campanhas ocupa espaço uma vez só) e lida do disco uma vez por campanha.
- Se a foto for apagada do disco no meio de uma campanha, o motor **pausa** em
  vez de mandar só o texto — a mensagem que sai é sempre a que você montou.

## O que é obrigatório

- WhatsApp só entra na fila quando `consentimento_em` e
  `consentimento_origem` estão preenchidos.
- `SAIR`, `PARAR`, `CANCELAR` e equivalentes entram na blocklist imediatamente.
- Blocklist, cooldown, janela de envio, cota diária e velocidade são aplicados
  no servidor; o painel não consegue contorná-los.
- Envio interrompido sem resposta conclusiva do provedor vira
  `indeterminado`; não é repetido automaticamente.
- Webhooks são autenticados, deduplicados e vinculados ao provedor e à conta.
- O painel administrativo escuta somente em `127.0.0.1:8777` e exige a chave
  local. A porta `8788` contém apenas os webhooks autenticados.

Isso acompanha a [política do WhatsApp Business](https://www.whatsapp.com/legal/business-policy/),
que exige número fornecido pelo destinatário, permissão para contato e respeito
à revogação. Uma lista pública não vira opt-in por alquimia jurídica. Para bases
tratadas sob legítimo interesse, faça a análise indicada no
[guia da ANPD](https://www.gov.br/anpd/pt-br/documentos-e-publicacoes/guia_legitimo_interesse.pdf)
antes de importar ou enviar.

## Rodar no Linux Mint

Pré-requisitos: Python 3, Docker e `curl`.

```bash
./RODAR.sh
```

O script reconcilia a rede privada do Evolution, inicia painel e webhook e abre
`http://127.0.0.1:8777`. É o que o atalho da área de trabalho roda. Se o
painel já está no ar com código mais antigo que o da pasta, o script o
reinicia (SIGINT, o motor termina a mensagem em voo); com campanha rodando ele
só avisa, para não parar o disparo. A aba aberta no navegador se recarrega
sozinha quando o painel muda — exceto se houver texto digitado e não enviado,
caso em que aparece "painel atualizado — clique para carregar" no topo. Nada
disso toca na sessão dos chips, que vive no gateway. No primeiro uso ele cria duas chaves locais com modo
`0600`:

```bash
cat .painel-chave
```

Use esse valor na tela de login. `.painel-chave`, `.webhook-chave`, credenciais
de provedores e o banco não devem ir para Git, mensagem ou screenshot.

Comandos úteis:

```bash
./RODAR.sh status
./RODAR.sh parar
python3 disparar.py status
python3 disparar.py chips sincronizar
python3 disparar.py bloquear 5517999999999
```

## Importar contatos

Pelo painel, o arquivo deve estar dentro de `importacoes/` (configurável por
`importacao.diretorios`) e respeitar o limite de tamanho. A CLI aceita um
caminho explícito porque é uma ação local do operador:

```bash
python3 disparar.py importar importacoes/leads.csv
```

Colunas reconhecidas incluem `nome`, `telefone`, `cidade`, `bairro`, `segmento`,
`email`, `cnpj`, `uf`, `consentimento_em` e `consentimento_origem`. Exemplo:

```csv
nome,telefone,cidade,consentimento_em,consentimento_origem
Ana,17999999999,Votuporanga,2026-08-20T14:30:00-03:00,formulario_site
```

Sem os dois campos de consentimento o contato pode permanecer na base, mas não
é elegível para WhatsApp.

## Fluxo seguro

1. Configure o provedor na aba **config**; prefira variáveis de ambiente para
   segredos.
2. Faça a prévia com o provedor `simulado`. O botão de teste não envia por um
   provedor real.
3. Crie a campanha e monte o público. O servidor aplica consentimento e
   blocklist novamente.
4. Para um ensaio real, crie uma campanha de um único contato consentido.
5. Confirme o início. Reservas transacionais impedem duas threads de exceder a
   cota do chip ou da campanha.
6. Se aparecer `indeterminado`, confira o painel do provedor antes de qualquer
   reenvio manual.

## Contas e rampa

O limite estrutural é de 25 contas/instâncias. A mesma regra existe na API, na
sincronização e no banco SQLite. Cada conta possui cota diária, mensagens por
minuto e dia de rampa próprios.

A rampa reduz o teto de envio progressivamente. Ela **não** envia mensagens
entre chips, não simula conversa e não substitui reputação, consentimento ou
qualidade do conteúdo. Na aba **chips**, o botão **ativar rampa automática**
habilita todos os chips ativos: os conectados começam no dia 1, os demais
aguardam o QR e começam automaticamente na primeira conexão. A cota avança por
chip sem clique diário e clicar novamente não reinicia o progresso.

## Conversas (ver e responder, chip por chip)

Aba **conversas** do painel, no formato do WhatsApp: chips à esquerda (bolinha
verde = conectado, número = mensagens não lidas), clientes no meio, conversa à
direita com caixa de resposta. Enter envia; Shift+Enter quebra linha.

- O gateway não guarda histórico (`DATABASE_SAVE_*` desligados), então o painel
  grava tudo na tabela `mensagem` do SQLite **a partir do momento em que chega**:
  o que a campanha mandou, o que o cliente respondeu e o que foi respondido —
  pelo painel ou pelo próprio celular do chip. Mensagens anteriores não existem
  em lugar nenhum para serem importadas.
- Foto, áudio e documento aparecem como tipo + legenda; o arquivo não vem no
  webhook.
- Responder **não é disparo**: não passa pela fila, não consome cota, ignora a
  janela de horário. Em troca, só é possível dentro de uma conversa que já
  existe naquele chip, e nunca para número na blocklist.
- Grupos, status e canais não entram.
- Nada nesta aba mexe na conexão dos chips.

## Quedas e reconexão

Conectar um chip custa um QR lido no celular, então o sistema faz o possível
para não perder sessão:

- **Vigia**: enquanto o painel está no ar, uma thread espelha o gateway a cada
  60 s e, para todo chip pareado que aparece `close`/`connecting` sem ser
  banimento, pede `instance/connect` ao Evolution. Com a sessão ainda válida
  isso reconecta **sem QR**. Há freio de 2 min por chip.
- **Webhook `connection.update`**: a queda chega em segundos e já dispara o
  mesmo pedido, sem esperar o vigia.
- **Backup das sessões**: `./RODAR.sh` roda `deploy/backup-sessoes.sh` a cada
  subida (dump do Postgres do Evolution, onde as credenciais vivem). Perdeu o
  container ou o volume: `deploy/restaurar-sessoes.sh` devolve tudo sem QR.
- **Pausar ≠ desconectar**: pausar só tira do rodízio; a sessão fica viva.
  Desconectar exige digitar o nome do chip.

O que **não** tem conserto por software, e o painel mostra no cartão do chip:

- `401` — o WhatsApp encerrou a sessão. É banimento do número ou alguém tocou em
  *desconectar* no celular. O gateway apaga a credencial; só QR novo (e, se
  foi ban, o número não volta).
- `403` — número bloqueado.
- Celular do chip **desligado por mais de 14 dias**: o WhatsApp desvincula os
  aparelhos conectados. Mantenha os aparelhos ligados e com internet.

## Provedores e webhooks

- `simulado`: teste determinístico sem saída de rede.
- `evolution`: gateway WhatsApp local; URL privada permitida somente para esse
  adaptador e webhook protegido por segredo.
- `whatsapp_cloud`: exige token, `phone_number_id`, `app_secret`,
  `verify_token` e webhook HTTPS público.
- `twilio`: exige SID, token, remetente e URL exata do webhook para validar a
  assinatura.
- `sms_http`: só habilita quando o provedor confirma opt-out externo; bloqueia
  destinos privados para evitar SSRF.

Para expor webhook, publique **somente** `8788`. Mantenha `8777` e `8080` em
loopback. Em servidor remoto, acesse o painel com túnel SSH:

```bash
ssh -L 8777:127.0.0.1:8777 usuario@servidor
```

## Estados de envio

`fila -> enviando -> enviado -> entregue -> lido -> respondido`

Saídas laterais: `falha`, `cancelado`, `bloqueado` e `indeterminado`. Eventos de
webhook só avançam monotonicamente; callback atrasado não rebaixa uma mensagem
já entregue ou lida.

## Verificação

```bash
python3 -m compileall -q disparador disparar.py
python3 -m unittest -v testes.invariantes
python3 testes/carga.py --rapido
bash -n RODAR.sh deploy/*.sh
python3 -m json.tool config.exemplo.json >/dev/null
```

Os testes cobrem autenticação, origem/CSRF, tamanho de corpo, HMAC, SSRF,
segredos aninhados, importação restrita, consentimento, opt-out concorrente,
idempotência, cota atômica, limite de 25 contas, reconexão automática e
recuperação após crash.

## Dados persistentes

```text
dados/disparador.db       banco SQLite
dados/logs/               logs locais
config.json               configuração sem segredos publicados
.painel-chave             autenticação administrativa (0600)
.webhook-chave            assinatura local do Evolution (0600)
deploy/.chave             API key local do Evolution (0600)
```

Parar containers não apaga os volumes. Excluir `dados/`, volumes Docker ou
arquivos de chave é operação destrutiva e não faz parte da manutenção normal.
