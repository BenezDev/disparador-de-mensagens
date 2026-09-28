"""
Evolution API / WPPConnect / Baileys -- gateway nao-oficial rodando local.

Gateway local nao-oficial. Volume nao solicitado e conversas artificiais nao
sao mecanismo de seguranca; a rampa apenas limita volume de contatos consentidos.
"""
ROTULO = "Evolution API / WPPConnect (nao-oficial)"
OBRIGATORIOS = ("url", "instancia", "apikey")
AJUDA = ("Gateway que roda na sua maquina. Suba com deploy/subir.sh: ele "
         "escreve a apikey em deploy/.chave e cria a instancia.")
DOC = "https://doc.evolution-api.com"
CANAL = ("whatsapp",)
# Aqui o numero e SEU e nao ha rede de protecao: o gateway nao tem fila, nem
# limite de vazao, nem retentativa. A rampa e obrigatoria, sem trafego fabricado.
AQUECE = True
IMAGEM = True   # sendMedia com a foto em base64 e a legenda personalizada
CAMPOS = {"url": "URL do gateway", "instancia": "Nome da instancia",
          "apikey": "API key", "webhook_secret": "Segredo do webhook",
          "versao": "1 ou 2"}


# O painel consulta o gateway a cada 3s; o cache curto evita que ele fique
# refem de um gateway lento. A CHAVE inclui a apikey de proposito: sem isso,
# uma tentativa com credencial errada devolvia resultado da credencial certa
# (e vice-versa), fazendo o painel mentir sobre o estado da conexao.
_CACHE = {}
_TTL = 8.0


def sondar(cfg, timeout=4):
    """(status_http, estado_da_instancia). status 0 = nao alcancou o gateway.

    Guardar o status separado do estado e o que permite dizer 'a chave foi
    recusada' em vez de 'o gateway nao respondeu' -- sao problemas diferentes,
    com solucoes diferentes.
    """
    from . import http, cavar
    import time
    chave = (cfg.get("url"), cfg.get("instancia"), cfg.get("apikey"))
    achado = _CACHE.get(chave)
    if achado and time.time() - achado[0] < _TTL:
        return achado[1]
    st, js, _t = http(
        f"{str(cfg['url']).rstrip('/')}/instance/connectionState/{cfg['instancia']}",
        metodo="GET", cabecalhos={"apikey": str(cfg.get("apikey", ""))},
        timeout=timeout, permitir_rede_privada=True)
    estado = cavar(js or {}, ("instance", "state"), "state") if st == 200 else None
    _CACHE[chave] = (time.time(), (st, estado))
    return st, estado


ESTADOS = {
    "open": None,                                    # conectado: nada a dizer
    "connecting": ("chip ainda não conectou",
                   "Leia o QR: cd deploy && ./qr.sh"),
    "close": ("chip desconectado",
              "Leia o QR de novo: cd deploy && ./qr.sh"),
}


def diagnosticar(cfg):
    """O que impede este gateway de enviar agora."""
    from . import diag, faltantes, url_valida
    faltam = faltantes(cfg, OBRIGATORIOS)
    if faltam:
        return diag(faltando=faltam,
                    mensagem="falta preencher: " + ", ".join(faltam),
                    acao=AJUDA)
    ok, motivo = url_valida(cfg["url"])
    if not ok:
        return diag(faltando=["url"], mensagem=f"endereço inválido — {motivo}",
                    acao="Exemplo: http://127.0.0.1:8080")

    st, estado = sondar(cfg)
    if st == 0:
        return diag(mensagem="o gateway não respondeu",
                    acao="O container está de pé? Rode: docker ps | grep evolution",
                    detalhe=f"sem resposta de {cfg['url']}")
    if st in (401, 403):
        return diag(faltando=["apikey"], mensagem="a apikey foi recusada",
                    acao="Confira a chave em deploy/.chave", detalhe=f"HTTP {st}")
    if st == 404:
        return diag(faltando=["instancia"],
                    mensagem=f"a instância '{cfg['instancia']}' não existe nesse gateway",
                    acao="Crie com: cd deploy && ./subir.sh " + str(cfg["instancia"]),
                    detalhe="HTTP 404")
    if st != 200:
        return diag(mensagem=f"o gateway respondeu HTTP {st}",
                    acao="Veja: docker logs --tail 30 evolution", detalhe=f"HTTP {st}")

    aviso = ESTADOS.get(estado, ("estado desconhecido: " + str(estado),
                                 "Veja: docker logs --tail 30 evolution"))
    if aviso:
        return diag(mensagem=aviso[0], acao=aviso[1], detalhe=f"estado '{estado}'")
    return diag(ok=True, mensagem="conectado e pronto")


def pronto(cfg):
    """Interface curta usada pelo motor.

    A checagem de conexao nao e zelo: com o chip fora do ar o Evolution nao
    responde erro -- ele simplesmente PENDURA a requisicao ate o timeout. Sem
    conferir antes, cada mensagem gastaria 30s pendurada, seria retentada 3x e
    a fila inteira viraria 'falha' em silencio enquanto o operador acha que
    esta disparando.
    """
    d = diagnosticar(cfg)
    return d.ok, (d.mensagem if d.ok else f"{d.mensagem} — {d.acao}")


def enviar(destino, texto, cfg, meta=None):
    from . import OK, ERRO, AMBIGUO, http, cavar
    meta = meta or {}
    base = cfg["url"].rstrip("/")
    cab = {"apikey": cfg.get("apikey", "")}
    if meta.get("idempotency_key"):
        cab["Idempotency-Key"] = meta["idempotency_key"]

    if meta.get("imagem_b64"):
        # foto + legenda numa mensagem so. A legenda ja vem montada (texto do
        # contato + rodape), entao a imagem nao vira um segundo disparo.
        url = f"{base}/message/sendMedia/{cfg['instancia']}"
        corpo = {"number": destino, "delay": 1200, "mediatype": "image",
                 "mimetype": meta.get("imagem_mime", "image/jpeg"),
                 "media": meta["imagem_b64"], "caption": texto,
                 "fileName": meta.get("imagem_nome", "foto.jpg")}
        if str(cfg.get("versao", "2")) == "1":
            corpo = {"number": destino, "options": {"delay": 1200},
                     "mediaMessage": {"mediatype": "image", "caption": texto,
                                      "media": meta["imagem_b64"]}}
    elif str(cfg.get("versao", "2")) == "1":
        url = f"{base}/message/sendText/{cfg['instancia']}"
        corpo = {"number": destino, "options": {"delay": 1200, "presence": "composing"},
                 "textMessage": {"text": texto}}
    else:
        url = f"{base}/message/sendText/{cfg['instancia']}"
        corpo = {"number": destino, "delay": 1200, "text": texto}

    st, js, txt = http(url, corpo=corpo, cabecalhos=cab, timeout=45,
                       permitir_rede_privada=True)
    if st in (200, 201) and js:
        mid = cavar(js, ("key", "id"), "id", ("message", "key", "id"))
        if mid:
            return OK(mid)
    erro = cavar(js or {}, "message", "error", ("response", "message")) or txt or f"HTTP {st}"
    if st == 0:
        return AMBIGUO(erro)
    fatal = st == 400 and any(s in str(erro).lower() for s in (
        "not exists", "does not exist", "nao existe", "não existe"))
    return ERRO(f"[{st}] {erro}", fatal=fatal)


# tipo da mensagem do WhatsApp -> (tipo nosso, campo da legenda). Midia chega
# sem o arquivo (WEBHOOK_BASE64 desligado): guardamos o tipo e a legenda.
_MIDIA = {"imageMessage": ("imagem", "caption"), "videoMessage": ("video", "caption"),
          "audioMessage": ("audio", None), "pttMessage": ("audio", None),
          "documentMessage": ("documento", "caption"),
          "documentWithCaptionMessage": ("documento", None),
          "stickerMessage": ("figurinha", None), "locationMessage": ("localizacao", None),
          "contactMessage": ("contato", None)}


def _conteudo(msg):
    """(texto, tipo) de uma mensagem do Baileys."""
    texto = (msg.get("conversation")
             or (msg.get("extendedTextMessage") or {}).get("text", "")
             or (msg.get("buttonsResponseMessage") or {}).get("selectedDisplayText", "")
             or (msg.get("listResponseMessage") or {}).get("title", ""))
    if texto:
        return texto, "texto"
    for campo, (tipo, legenda) in _MIDIA.items():
        if campo in msg:
            return (str((msg[campo] or {}).get(legenda) or "") if legenda else ""), tipo
    return "", "outro"


def _telefone(chave, d):
    """Numero do cliente, ou None se a conversa nao e com uma pessoa.

    Grupo (@g.us), status (status@broadcast) e canal (@newsletter) nao sao
    conversa com cliente. O WhatsApp novo pode mandar o remetente como @lid
    (id interno, nao e telefone); o numero real vem em remoteJidAlt/senderPn.
    """
    jid = str(chave.get("remoteJid") or "")
    if jid.endswith(("@g.us", "@broadcast", "@newsletter")) or jid == "status@broadcast":
        return None
    if jid.endswith("@lid"):
        jid = str(chave.get("remoteJidAlt") or chave.get("senderPn")
                  or d.get("senderPn") or "")
        if not jid:
            return None
    numero = jid.split("@")[0].split(":")[0]
    return numero if numero.isdigit() else None


def ler_webhook(js):
    ev = js.get("event", "")
    d = js.get("data", {}) or {}
    conta = js.get("instance") or d.get("instance") or d.get("instanceName")
    if ev in ("messages.upsert", "messages_upsert"):
        chave = d.get("key", {}) or {}
        tel = _telefone(chave, d)
        if not tel:
            return []
        texto, tipo = _conteudo(d.get("message", {}) or {})
        # fromMe = saiu deste chip: pela API (campanha/resposta do painel, que ja
        # gravamos) ou pelo proprio celular -- que so aparece por aqui. Grava para
        # a conversa ficar inteira; o indice por id_externo descarta a repeticao.
        mensagem = {"tipo": "mensagem", "conta": conta, "telefone": tel,
                    "direcao": "saida" if chave.get("fromMe") else "entrada",
                    "texto": texto, "tipo_msg": tipo, "id_externo": chave.get("id")}
        if chave.get("fromMe"):
            return [mensagem]
        return [mensagem, {"tipo": "resposta", "telefone": tel, "texto": texto,
                           "conta": conta}]
    if ev in ("connection.update", "connection_update"):
        # {"state": "open"|"close"|"connecting", "statusReason": 401|408|...}
        # E o aviso mais rapido de queda que existe: chega antes de qualquer
        # sincronizacao, e e o que permite reerguer o chip em segundos.
        return [{"tipo": "conexao", "conta": conta, "estado": d.get("state"),
                 "motivo": d.get("statusReason")}]
    if ev in ("messages.update", "messages_update", "send.message"):
        chave = d.get("key", {}) or {}
        estados = {"DELIVERY_ACK": "entregue", "READ": "lido", "PLAYED": "lido",
                   "SERVER_ACK": "enviado", "ERROR": "falha"}
        return [{"tipo": "status", "id_externo": chave.get("id"),
                 "telefone": str(chave.get("remoteJid", "")).split("@")[0],
                 "estado": estados.get(str(d.get("status", "")).upper()),
                 "conta": conta}]
    return []


# --- gestao de instancias ---------------------------------------------------
# So este provedor tem varios chips atras da mesma credencial. A presenca de
# `listar_instancias` e o que sinaliza isso ao resto do sistema.
def listar_instancias(cfg):
    """[{nome, estado, numero, perfil}] ou None se o gateway nao respondeu."""
    from . import http, url_valida
    if not cfg.get("url") or not url_valida(cfg["url"])[0]:
        return None
    st, js, _t = http(f"{str(cfg['url']).rstrip('/')}/instance/fetchInstances",
                      metodo="GET", cabecalhos={"apikey": str(cfg.get("apikey", ""))},
                      timeout=8, permitir_rede_privada=True)
    if st != 200 or js is None:
        return None
    linhas = js if isinstance(js, list) else [js]
    saida = []
    for i in linhas:
        if not isinstance(i, dict):
            continue
        dentro = i.get("instance") if isinstance(i.get("instance"), dict) else i
        nome = dentro.get("name") or dentro.get("instanceName")
        if not nome:
            continue
        # ownerJid vem como "5517999999999@s.whatsapp.net"
        jid = dentro.get("ownerJid") or ""
        saida.append({
            "nome": nome,
            "estado": dentro.get("connectionStatus") or dentro.get("state"),
            "numero": (str(jid).split("@")[0] or None) if jid else dentro.get("number"),
            "perfil": dentro.get("profileName"),
            # 401/403 aqui e bloqueio, nao queda de rede -- e o que autoriza
            # redistribuir a faixa do chip na hora, sem esperar a carencia
            "motivo_queda": dentro.get("disconnectionReasonCode"),
        })
    return saida


def criar_instancia(nome, cfg):
    from . import http, cavar
    st, js, txt = http(f"{str(cfg['url']).rstrip('/')}/instance/create",
                       corpo={"instanceName": nome, "integration": "WHATSAPP-BAILEYS",
                              "qrcode": False},
                       cabecalhos={"apikey": str(cfg.get("apikey", ""))}, timeout=20,
                       permitir_rede_privada=True)
    if st in (200, 201):
        return True, ""
    erro = cavar(js or {}, "message", "error", ("response", "message")) or txt
    if st == 403 and "already in use" in str(erro).lower():
        return True, ""                       # ja existia: para nos, sucesso
    return False, f"[{st}] {erro}"


def remover_instancia(nome, cfg):
    from . import http
    base = str(cfg["url"]).rstrip("/")
    cab = {"apikey": str(cfg.get("apikey", ""))}
    http(f"{base}/instance/logout/{nome}", metodo="DELETE", cabecalhos=cab, timeout=15,
         permitir_rede_privada=True)
    st, _js, txt = http(f"{base}/instance/delete/{nome}", metodo="DELETE",
                        cabecalhos=cab, timeout=15, permitir_rede_privada=True)
    return st in (200, 204), "" if st in (200, 204) else txt


def reconectar_instancia(nome, cfg):
    """Pede ao gateway para reerguer o socket do chip. (resultado, detalhe).

    /instance/connect com sessao valida reconecta SEM QR -> ("ok", estado).
    Sem sessao o gateway devolve um QR -> ("qr", ...): ai so o operador,
    lendo pelo painel, resolve. Gateway mudo ou erro -> ("erro", ...).
    """
    from . import http, cavar
    st, js, txt = http(f"{str(cfg['url']).rstrip('/')}/instance/connect/{nome}",
                       metodo="GET", cabecalhos={"apikey": str(cfg.get("apikey", ""))},
                       timeout=20, permitir_rede_privada=True)
    if st != 200 or not js:
        return "erro", f"[{st}] {txt[:200]}"
    estado = cavar(js, ("instance", "state"), "state")
    if estado:
        return "ok", f"estado {estado}"
    if js.get("base64") or js.get("code"):
        return "qr", "sem sessao no gateway: precisa de QR"
    return "ok", "pedido aceito"


def qr_instancia(nome, cfg):
    """(png_bytes, codigo, erro). O QR gira a cada ~20s."""
    import base64
    from . import http, cavar
    st, js, txt = http(f"{str(cfg['url']).rstrip('/')}/instance/connect/{nome}",
                       metodo="GET", cabecalhos={"apikey": str(cfg.get("apikey", ""))},
                       timeout=20, permitir_rede_privada=True)
    if st != 200 or not js:
        return None, "", f"[{st}] {txt[:200]}"
    if cavar(js, ("instance", "state"), "state") == "open":
        return None, "", "ja conectado"
    b64 = js.get("base64") or ""
    png = None
    if b64.startswith("data:image"):
        try:
            png = base64.b64decode(b64.split(",", 1)[1])
        except Exception:
            png = None
    return png, js.get("code") or "", ""
