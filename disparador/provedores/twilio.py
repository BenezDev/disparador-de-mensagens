"""Twilio -- SMS (e WhatsApp, se a conta tiver sender aprovado)."""
ROTULO = "Twilio"
IMAGEM = False
CANAL = ("sms", "whatsapp")
AQUECE = False
CAMPOS = {"sid": "Account SID", "token": "Auth Token",
          "de": "Numero remetente (+55...)", "webhook": "URL de status (opcional)"}


OBRIGATORIOS = ("sid", "token", "de", "webhook")
AJUDA = ("Account SID e Auth Token ficam no painel da Twilio. Configure a URL "
         "publica da porta 8788 para receber status e opt-out. Conta de teste "
         "so envia para numeros verificados.")
DOC = "https://www.twilio.com/console"


def diagnosticar(cfg):
    from . import diag, faltantes
    faltam = faltantes(cfg, OBRIGATORIOS)
    if faltam:
        return diag(faltando=faltam,
                    mensagem="falta preencher: " + ", ".join(faltam), acao=AJUDA)
    if not str(cfg["sid"]).startswith("AC"):
        return diag(faltando=["sid"], mensagem="o Account SID começa com 'AC'",
                    acao="Copie o SID do painel da Twilio.")
    if not str(cfg["de"]).startswith("+"):
        return diag(faltando=["de"], mensagem="o remetente precisa do formato +55...",
                    acao="Use o número no padrão E.164, com o + na frente.")
    return diag(ok=True, mensagem="configurado",
                detalhe="só o primeiro envio confirma a credencial")


def pronto(cfg):
    d = diagnosticar(cfg)
    return d.ok, (d.mensagem if d.ok else f"{d.mensagem} — {d.acao}")


# numero morto: invalido, nao recebe SMS, ou deu opt-out no proprio Twilio
NUMERO_MORTO = {21211, 21214, 21217, 21610, 21612, 21614}
# campanha errada: remetente invalido, corpo invalido, credencial ruim
CAMPANHA_ERRADA = {20003, 21606, 21611, 21659, 21660, 63007, 63024, 21617}


def enviar(destino, texto, cfg, meta=None):
    from . import OK, ERRO, PAUSA, AMBIGUO, http, cavar
    canal = (meta or {}).get("canal", "sms")
    pre = "whatsapp:" if canal == "whatsapp" else ""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{cfg['sid']}/Messages.json"
    forma = {"To": f"{pre}+{destino}", "From": f"{pre}{cfg['de']}", "Body": texto}
    if cfg.get("webhook"):
        forma["StatusCallback"] = cfg["webhook"]
    cab = {}
    if (meta or {}).get("idempotency_key"):
        cab["Idempotency-Key"] = meta["idempotency_key"]
    st, js, txt = http(url, forma=forma, basico=(cfg["sid"], cfg["token"]),
                       cabecalhos=cab)
    if st in (200, 201) and js:
        return OK(js.get("sid"))
    codigo = cavar(js or {}, "code")
    detalhe = f"[{st}/{codigo}] {cavar(js or {}, 'message') or txt}"
    if st == 0:
        return AMBIGUO(detalhe)
    if codigo in NUMERO_MORTO:
        return ERRO(detalhe, fatal=True)
    if codigo in CAMPANHA_ERRADA or st == 401:
        return PAUSA(detalhe)
    return ERRO(detalhe)


def ler_webhook(form):
    estados = {"delivered": "entregue", "read": "lido", "sent": "enviado",
               "undelivered": "falha", "failed": "falha"}
    if form.get("MessageStatus"):
        return [{"tipo": "status", "id_externo": form.get("MessageSid"),
                 "conta": form.get("AccountSid"),
                 "telefone": str(form.get("To", "")).lstrip("+").replace("whatsapp:", ""),
                 "estado": estados.get(form.get("MessageStatus")),
                 "erro": form.get("ErrorCode")}]
    if form.get("Body"):
        return [{"tipo": "resposta",
                 "conta": form.get("AccountSid"),
                 "telefone": str(form.get("From", "")).lstrip("+").replace("whatsapp:", ""),
                 "texto": form.get("Body")}]
    return []
