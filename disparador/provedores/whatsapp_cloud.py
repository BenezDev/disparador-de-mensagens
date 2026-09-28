"""
WhatsApp Cloud API (oficial, Meta).

Ponto que costuma pegar todo mundo de surpresa: fora da janela de 24h desde a
ultima resposta do cliente, a Meta SO aceita TEMPLATE aprovado. Prospeccao fria
e 100% fora dessa janela -- portanto, se a campanha nao trouxer `template` em
meta, o envio e recusado aqui mesmo, antes de gastar chamada de API.
"""
ROTULO = "WhatsApp Cloud API (oficial Meta)"
IMAGEM = False
CANAL = ("whatsapp",)
# Aqui a rampa ATRAPALHA. A Meta sobe o limite (250 -> 1.000 -> 10.000 -> ...)
# so quando a conta usa pelo menos METADE do limite atual em um unico dia nos
# ultimos 7 dias. Mandar 20/dia num limite de 250 e garantir que ele nunca
# suba. Quem regula o volume aqui e o tier da conta, nao a nossa rampa.
AQUECE = False
CAMPOS = {"phone_id": "Phone Number ID", "token": "Token permanente",
          "app_secret": "App Secret para assinar webhooks",
          "verify_token": "Verify Token do webhook",
          "versao": "Versao da Graph API"}
OBRIGATORIOS = ("phone_id", "token", "app_secret", "verify_token")
AJUDA = ("No Meta Business: WhatsApp > Configuracao da API. O 'Phone Number ID' "
         "fica sob o numero; o token permanente sai em Usuarios do sistema. "
         "Configure tambem App Secret e Verify Token do webhook. Contato frio "
         "EXIGE opt-in e template de marketing aprovado.")
DOC = "https://developers.facebook.com/docs/whatsapp/cloud-api/get-started"

_CACHE = {}
_TTL = 30.0


def sondar(cfg, timeout=6):
    """(status, erro) do proprio numero. Valida token e phone_id de uma vez."""
    from . import http, cavar
    import time
    chave = (cfg.get("phone_id"), cfg.get("token"), cfg.get("versao"))
    achado = _CACHE.get(chave)
    if achado and time.time() - achado[0] < _TTL:
        return achado[1]
    st, js, txt = http(
        f"https://graph.facebook.com/{cfg.get('versao', 'v21.0')}/{cfg['phone_id']}"
        "?fields=display_phone_number,verified_name,quality_rating",
        metodo="GET", cabecalhos={"Authorization": f"Bearer {cfg['token']}"},
        timeout=timeout)
    r = (st, cavar(js or {}, ("error", "message")) or (txt or "")[:160], js or {})
    _CACHE[chave] = (time.time(), r)
    return r


def diagnosticar(cfg):
    from . import diag, faltantes
    faltam = faltantes(cfg, OBRIGATORIOS)
    if faltam:
        return diag(faltando=faltam,
                    mensagem="falta preencher: " + ", ".join(faltam), acao=AJUDA)

    st, erro, js = sondar(cfg)
    if st == 0:
        return diag(mensagem="não consegui falar com a Graph API",
                    acao="Sem internet, ou a saída está bloqueada.", detalhe=erro)
    if st in (401, 403) or "expired" in erro.lower() or "session" in erro.lower():
        return diag(faltando=["token"], mensagem="o token foi recusado pela Meta",
                    acao="Gere um token permanente em Usuarios do sistema.",
                    detalhe=f"HTTP {st}: {erro}")
    if st == 404 or "does not exist" in erro.lower():
        return diag(faltando=["phone_id"],
                    mensagem="esse Phone Number ID não existe para este token",
                    acao="Copie o ID que fica sob o numero em WhatsApp > Configuracao da API.",
                    detalhe=f"HTTP {st}: {erro}")
    if st != 200:
        return diag(mensagem=f"a Meta respondeu HTTP {st}", acao=AJUDA,
                    detalhe=erro)

    nome = js.get("verified_name") or js.get("display_phone_number") or ""
    qual = js.get("quality_rating")
    if str(qual).upper() == "RED":
        return diag(mensagem=f"qualidade do numero em VERMELHO ({nome})",
                    acao="Disparar agora arrisca o bloqueio. Melhore a mensagem antes.",
                    detalhe=f"quality_rating={qual}")
    return diag(ok=True, mensagem=f"conectado{f' — {nome}' if nome else ''}",
                detalhe=f"qualidade {qual}" if qual else "")


def pronto(cfg):
    d = diagnosticar(cfg)
    return d.ok, (d.mensagem if d.ok else f"{d.mensagem} — {d.acao}")


# Classificacao conferida contra a tabela oficial de erros da Cloud API.
# Errar de lado aqui e caro: o que cai em NUMERO_MORTO vai para a blocklist e
# nunca mais recebe nada.
#
#   131026  "not a WhatsApp phone number" -- o unico erro que e realmente do
#           destinatario. So ele justifica queimar o lead.
NUMERO_MORTO = {131026}

# A CAMPANHA ou a CONTA e que estao erradas. O lead volta intacto para a fila.
#   131005 permissao | 131008 falta parametro | 131021 remetente = destinatario
#   131031 conta restrita | 131042 problema de cobranca | 131045 numero nao
#   registrado | 131047 precisa de template (fora das 24h) | 131048 mensagens
#   anteriores marcadas como SPAM | 131051 tipo nao suportado | 131053 midia
#   132000/132001/132005/132007/132012/132015/132016 template
#   133010 numero nao registrado | 100 parametro | 190 token | 368 conta banida
CAMPANHA_ERRADA = {131005, 131008, 131021, 131031, 131042, 131045, 131047,
                   131048, 131051, 131053, 132000, 132001, 132005, 132007,
                   132012, 132015, 132016, 132068, 133010, 100, 190, 368, 470}

# 131048 e o mais serio da lista: significa que o WhatsApp ja esta marcando
# nossas mensagens como spam. Continuar disparando depois disso e o caminho
# direto para o banimento da conta.
ALARME_QUALIDADE = {131048, 131031, 368}

# Passageiros: limite de vazao, indisponibilidade momentanea, limite por
# destinatario. Reagendar resolve; pausar a campanha seria exagero.
#   130429 vazao da app | 80007 limite da conta | 131000 erro desconhecido
#   131016 servico indisponivel | 131049 limite do destinatario (ecossistema)
#   131056 rapido demais para o mesmo numero
TRANSITORIO = {130429, 80007, 131000, 131016, 131049, 131056}


def enviar(destino, texto, cfg, meta=None):
    from . import OK, ERRO, PAUSA, AMBIGUO, http, cavar
    meta = meta or {}
    url = (f"https://graph.facebook.com/{cfg.get('versao', 'v21.0')}"
           f"/{cfg['phone_id']}/messages")
    cab = {"Authorization": f"Bearer {cfg['token']}"}
    if meta.get("idempotency_key"):
        cab["Idempotency-Key"] = meta["idempotency_key"]

    if meta.get("template"):
        comps = []
        params = meta.get("params") or []
        if params:
            comps.append({"type": "body", "parameters":
                          [{"type": "text", "text": str(p)} for p in params]})
        corpo = {"messaging_product": "whatsapp", "to": destino,
                 "type": "template",
                 "template": {"name": meta["template"],
                              "language": {"code": meta.get("idioma", "pt_BR")},
                              "components": comps}}
    elif meta.get("sessao"):
        # so vale se o contato respondeu nas ultimas 24h
        corpo = {"messaging_product": "whatsapp", "to": destino, "type": "text",
                 "text": {"preview_url": False, "body": texto}}
    else:
        return PAUSA("Cloud API exige template aprovado para contato frio "
                     "(defina 'template' no modelo da campanha)")

    st, js, txt = http(url, corpo=corpo, cabecalhos=cab)
    if st == 200 and js:
        mid = cavar(js, ("messages", 0, "id"))
        if mid:
            return OK(mid)
    erro = cavar(js or {}, ("error", "message")) or txt or f"HTTP {st}"
    codigo = cavar(js or {}, ("error", "code"))
    detalhe = f"[{st}/{codigo}] {erro}"
    if st == 0:
        return AMBIGUO(detalhe)
    if codigo in ALARME_QUALIDADE:
        return PAUSA(f"QUALIDADE — {detalhe}. Pare e reveja a mensagem antes "
                     f"de continuar: insistir agora derruba a conta.")
    if codigo in NUMERO_MORTO:
        return ERRO(detalhe, fatal=True)
    if codigo in TRANSITORIO:
        return ERRO(detalhe)
    if codigo in CAMPANHA_ERRADA or st in (401, 403):
        return PAUSA(detalhe)
    return ERRO(detalhe)


# a Meta fala em ingles; o banco fala o vocabulario interno
ESTADOS = {"sent": "enviado", "delivered": "entregue", "read": "lido",
           "failed": "falha", "deleted": "cancelado"}


def ler_webhook(js):
    """Traduz o webhook da Meta para eventos internos.

    Devolve lista de dicts: {tipo: status|resposta, id_externo, telefone, ...}
    """
    saida = []
    for entry in js.get("entry", []):
        for ch in entry.get("changes", []):
            v = ch.get("value", {})
            conta = (v.get("metadata") or {}).get("phone_number_id")
            for s in v.get("statuses", []):
                saida.append({"tipo": "status", "id_externo": s.get("id"),
                              "telefone": s.get("recipient_id"),
                              "conta": conta,
                              "estado": ESTADOS.get(s.get("status")),
                              "erro": (s.get("errors") or [{}])[0].get("title")})
            for m in v.get("messages", []):
                saida.append({"tipo": "resposta", "telefone": m.get("from"),
                              "conta": conta,
                              "texto": (m.get("text") or {}).get("body", "")
                                       or (m.get("button") or {}).get("text", "")})
    return saida
