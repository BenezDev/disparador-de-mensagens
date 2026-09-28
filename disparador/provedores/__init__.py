#!/usr/bin/env python3
"""
Adaptadores de provedor.

Contrato unico:
    enviar(destino, texto, cfg, meta) -> Resultado(ok, id_externo, erro, fatal)

Tres desfechos possiveis para um erro, e a diferenca entre eles e cara:

  ERRO()          transitorio -> o motor reagenda (1min, 5min, 30min)
  ERRO(fatal=1)   o NUMERO esta morto -> vai para a blocklist, nunca mais
  PAUSA()         a CAMPANHA esta errada -> para tudo e chama o operador

Confundir os dois ultimos e o erro caro: um nome de template digitado errado
devolve 400 em todo envio, e tratar isso como "numero morto" bloquearia a base
inteira, um lead por vez, sem ninguem perceber.
"""
import base64, json, ssl, urllib.error, urllib.parse, urllib.request
from collections import namedtuple

# Diagnostico do provedor: o que falta, em portugues, sem NUNCA carregar o
# valor da credencial junto. E o que o painel mostra na tela de configuracao.
#   ok        pronto para enviar de verdade
#   faltando  nomes dos campos vazios (so os nomes)
#   mensagem  o que esta errado, em uma frase
#   acao      o que fazer a respeito
#   detalhe   pista tecnica (status HTTP, estado da instancia), pode ser vazio
Diagnostico = namedtuple("Diagnostico", "ok faltando mensagem acao detalhe")


def diag(ok=False, faltando=(), mensagem="", acao="", detalhe=""):
    return Diagnostico(ok, list(faltando), mensagem, acao, detalhe)


def faltantes(cfg, obrigatorios):
    """Campos obrigatorios vazios. So os nomes -- valor nao sai daqui."""
    return [c for c in obrigatorios if not str(cfg.get(c, "") or "").strip()]


# Campos cujo VALOR nunca pode sair do processo: nao vao para a API do painel,
# nao vao para o log, nao vao para a tela.
SEGREDO = ("token", "apikey", "api_key", "senha", "password", "secret", "sid",
           "key", "authorization", "auth", "cookie")


def e_segredo(campo):
    c = campo.lower()
    return any(s in c for s in SEGREDO)


Resultado = namedtuple("Resultado", "ok id_externo erro fatal pausar indeterminado",
                       defaults=(False,))
OK = lambda i: Resultado(True, str(i), None, False, False, False)
ERRO = lambda e, fatal=False: Resultado(False, None, str(e)[:500], fatal, False, False)
# PAUSA: o erro e da CAMPANHA (template errado, remetente invalido), nao do
# numero. Insistir so queima lead bom; bloquear o lead seria pior ainda.
PAUSA = lambda e: Resultado(False, None, str(e)[:500], False, True, False)
AMBIGUO = lambda e: Resultado(False, None, str(e)[:500], False, True, True)

_CTX = ssl.create_default_context()


class _SemRedirecionar(urllib.request.HTTPRedirectHandler):
    """Credenciais nunca acompanham redirect decidido por endpoint remoto."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _abridor():
    return urllib.request.build_opener(
        _SemRedirecionar(), urllib.request.HTTPSHandler(context=_CTX))


def http(url, metodo="POST", corpo=None, cabecalhos=None, forma=None,
         basico=None, timeout=30, permitir_rede_privada=False):
    """POST/GET simples. `corpo`=json, `forma`=x-www-form-urlencoded."""
    from ..seguranca import MAX_RESPOSTA_PROVEDOR, validar_url_saida
    ok, motivo = validar_url_saida(url, permitir_rede_privada)
    if not ok:
        return 0, None, f"destino bloqueado: {motivo}"
    h = dict(cabecalhos or {})
    dados = None
    if forma is not None:
        dados = urllib.parse.urlencode(forma).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif corpo is not None:
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    if basico:
        h["Authorization"] = "Basic " + base64.b64encode(
            f"{basico[0]}:{basico[1]}".encode()).decode()
    req = urllib.request.Request(url, data=dados, headers=h, method=metodo)
    try:
        with _abridor().open(req, timeout=timeout) as r:
            bruto = r.read(MAX_RESPOSTA_PROVEDOR + 1)
            if len(bruto) > MAX_RESPOSTA_PROVEDOR:
                return 0, None, "resposta do provedor excede 2 MiB"
            txt = bruto.decode("utf-8", "replace")
            return r.status, _json(txt), txt
    except urllib.error.HTTPError as e:
        bruto = e.read(MAX_RESPOSTA_PROVEDOR + 1)
        if len(bruto) > MAX_RESPOSTA_PROVEDOR:
            return e.code, None, "resposta de erro do provedor excede 2 MiB"
        txt = bruto.decode("utf-8", "replace")
        return e.code, _json(txt), txt
    except Exception as e:                       # rede, dns, timeout
        return 0, None, f"{type(e).__name__}: {e}"


def _json(txt):
    try:
        return json.loads(txt)
    except Exception:
        return None


def cavar(obj, *chaves):
    """Pega o primeiro caminho que existir: cavar(d, 'id', ('messages',0,'id'))"""
    for c in chaves:
        cur, ok = obj, True
        for p in (c if isinstance(c, tuple) else (c,)):
            try:
                cur = cur[p]
            except Exception:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return None


# --- registro ---------------------------------------------------------------
from . import simulado, whatsapp_cloud, evolution, sms_http, twilio  # noqa: E402

REGISTRO = {
    "simulado": simulado,
    "whatsapp_cloud": whatsapp_cloud,
    "evolution": evolution,
    "sms_http": sms_http,
    "twilio": twilio,
}

CANAIS = {
    "whatsapp": ["whatsapp_cloud", "evolution", "simulado"],
    "sms": ["sms_http", "twilio", "simulado"],
}


def pegar(nome):
    if nome not in REGISTRO:
        raise KeyError(f"provedor desconhecido: {nome}")
    return REGISTRO[nome]


def diagnosticar(nome, cfg):
    """Diagnostico de um provedor, sempre sem levantar excecao.

    O painel chama isto a cada 3s; qualquer erro aqui viraria tela quebrada.
    """
    try:
        m = pegar(nome)
    except KeyError as e:
        return diag(mensagem=str(e), acao="Escolha outro provedor.")
    try:
        return m.diagnosticar(cfg or {})
    except Exception as e:
        return diag(mensagem=f"não consegui verificar {nome}",
                    acao="Veja o log do disparador.",
                    detalhe=f"{type(e).__name__}: {e}")


def url_valida(u):
    """(True,'') ou (False, motivo). Evita ValueError la dentro do urllib."""
    u = str(u or "").strip()
    if not u:
        return False, "endereço vazio"
    if not u.startswith(("http://", "https://")):
        return False, "precisa começar com http:// ou https://"
    resto = u.split("://", 1)[1]
    if not resto or resto.startswith("/"):
        return False, "falta o endereço do servidor depois do http://"
    return True, ""
