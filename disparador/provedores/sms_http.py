"""
SMS por HTTP generico -- cobre SMSDev, Comtele, Facilita Movel, Zenvia e
qualquer gateway com "manda um POST com o numero e o texto".

A URL, os cabecalhos e o corpo vem do config e aceitam {destino} e {texto},
entao trocar de fornecedor e mexer no config.json, nao no codigo.

Exemplo (SMSDev):
  "url": "https://api.smsdev.com.br/v1/send",
  "corpo": {"key":"SUA_KEY","type":"9","number":"{destino}","msg":"{texto}"},
  "campo_id": "id"
"""
import json

ROTULO = "SMS via HTTP (generico)"
IMAGEM = False
CANAL = ("sms",)
# SMS nao bane por volume -- cobra por mensagem. Rampa aqui so atrasa.
AQUECE = False
CAMPOS = {"url": "URL do endpoint", "metodo": "POST/GET", "tipo": "json/form",
          "cabecalhos": "Headers (json)", "corpo": "Corpo com {destino}/{texto}",
          "campo_id": "Campo do id na resposta",
          "optout_externo": "Provedor bloqueia opt-out (true/false)"}


OBRIGATORIOS = ("url",)
AJUDA = ("Cole a URL do endpoint do seu gateway e ajuste 'corpo' com {destino} "
         "e {texto}. O config.exemplo.json ja traz o formato do SMSDev.")
DOC = ""


def diagnosticar(cfg):
    from . import diag, faltantes, url_valida
    from ..seguranca import validar_url_saida
    faltam = faltantes(cfg, OBRIGATORIOS)
    if faltam:
        return diag(faltando=faltam, mensagem="falta a URL do gateway", acao=AJUDA)
    ok, motivo = url_valida(cfg["url"])
    if not ok:
        return diag(faltando=["url"], mensagem=f"URL inválida — {motivo}",
                    acao="Exemplo: https://api.smsdev.com.br/v1/send")
    ok, motivo = validar_url_saida(cfg["url"])
    if not ok:
        return diag(faltando=["url"], mensagem=f"destino bloqueado — {motivo}",
                    acao="Use um endpoint publico HTTPS do provedor.")
    if str(cfg.get("metodo") or "POST").upper() not in ("GET", "POST"):
        return diag(faltando=["metodo"], mensagem="metodo precisa ser GET ou POST",
                    acao="Nao use o gateway generico para alterar recursos remotos.")
    if not (cfg.get("corpo") or {}):
        return diag(faltando=["corpo"],
                    mensagem="falta o corpo da requisição",
                    acao="Precisa conter {destino} e {texto}.")
    if cfg.get("optout_externo") is not True:
        return diag(faltando=["optout_externo"],
                    mensagem="o gateway nao confirma bloqueio de opt-out",
                    acao="Ative somente se o provedor bloquear respostas de saida na origem.")
    corpo = json.dumps(cfg.get("corpo") or {}, ensure_ascii=False)
    if "{destino}" not in corpo or "{texto}" not in corpo:
        return diag(faltando=["corpo"],
                    mensagem="o corpo não usa {destino} e {texto}",
                    acao="Sem esses dois marcadores o gateway recebe a mensagem vazia.")
    return diag(ok=True, mensagem="configurado",
                detalhe="só o primeiro envio confirma a credencial")


def pronto(cfg):
    d = diagnosticar(cfg)
    return d.ok, (d.mensagem if d.ok else f"{d.mensagem} — {d.acao}")


def _preencher(valor, destino, texto):
    if isinstance(valor, str):
        return valor.replace("{destino}", destino).replace("{texto}", texto)
    if isinstance(valor, dict):
        return {k: _preencher(v, destino, texto) for k, v in valor.items()}
    if isinstance(valor, list):
        return [_preencher(v, destino, texto) for v in valor]
    return valor


def enviar(destino, texto, cfg, meta=None):
    from . import OK, ERRO, AMBIGUO, http, cavar
    import urllib.parse
    url = _preencher(cfg["url"], destino, texto)
    corpo = _preencher(cfg.get("corpo") or {}, destino, texto)
    cab = _preencher(cfg.get("cabecalhos") or {}, destino, texto)
    if (meta or {}).get("idempotency_key"):
        cab.setdefault("Idempotency-Key", meta["idempotency_key"])
    metodo = (cfg.get("metodo") or "POST").upper()
    if metodo not in ("GET", "POST"):
        return ERRO("metodo HTTP bloqueado")

    if metodo == "GET":
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(corpo)
        st, js, txt = http(url, metodo="GET", cabecalhos=cab)
    elif (cfg.get("tipo") or "json").lower() == "form":
        st, js, txt = http(url, metodo=metodo, forma=corpo, cabecalhos=cab)
    else:
        st, js, txt = http(url, metodo=metodo, corpo=corpo, cabecalhos=cab)

    if 200 <= st < 300:
        campo = cfg.get("campo_id") or "id"
        mid = cavar(js or {}, campo, "messageId", "message_id", ("data", "id"))
        # gateway que responde 200 sem id ainda e sucesso: usa o proprio corpo
        return OK(mid or (txt or "ok")[:60])
    if st == 0:
        return AMBIGUO(txt or "resultado da chamada desconhecido")
    return ERRO(f"[{st}] {txt or 'sem corpo'}")
