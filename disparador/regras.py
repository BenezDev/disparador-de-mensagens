#!/usr/bin/env python3
"""
Portao de compliance. Nenhuma mensagem passa daqui sem responder 5 perguntas:

  1. o numero e movel e valido?
  2. o numero pediu para sair, ou foi bloqueado?
  3. ja falamos com ele ha pouco tempo (cooldown global entre campanhas)?
  4. estamos dentro da janela de horario permitida?
  5. a campanha ja estourou a cota do dia?

1-3 sao por mensagem, 4-5 sao por lote. Um 'nao' em qualquer uma segura o envio.
"""
import re, unicodedata
from datetime import datetime, time, timedelta

from . import db, fone

PONTUACAO = re.compile(r"[^\w\s]+", re.UNICODE)


def _achatar(t):
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = t.encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", PONTUACAO.sub(" ", t)).strip()


def detectar_optout(texto, cfg):
    """True se a resposta do lead e um pedido de descadastro.

    Errar aqui custa nos dois sentidos, e os dois sao caros: deixar passar um
    'SAIR' vira denuncia; bloquear por engano queima um lead quente para
    sempre. Por isso a palavra solta so conta conforme o quanto ela e ambigua:

      frases    -> casam em qualquer lugar ("nao quero receber")
      comandos  -> podem abrir uma resposta curta ("sair por favor")
      isoladas  -> so quando sao a mensagem inteira ("nao", "para")

    "para qual email?" comeca com 'para' e NAO e opt-out -- e um lead pedindo
    o contato. Por isso 'para' vive em isoladas, nunca em comandos.
    """
    t = _achatar(texto)
    if not t:
        return False
    o = cfg["optout"]
    for f in o.get("frases", []):
        if _achatar(f) in t:
            return True
    comandos = {_achatar(p) for p in o.get("comandos", [])}
    isoladas = {_achatar(p) for p in o.get("isoladas", [])}
    palavras = t.split()
    if t in comandos or t in isoladas:          # a resposta inteira e o comando
        return True
    if len(palavras) <= 3 and palavras[0] in comandos:
        return True
    return False


def _hhmm(s):
    h, m = str(s).split(":")
    return time(int(h), int(m))


def janela_aberta(cfg, quando=None):
    """(bool, motivo). Sabado tem janela curta; domingo, por padrao, fechado."""
    q = quando or datetime.now()
    j = cfg["janela"]
    dia = q.weekday()                            # 0=seg ... 6=dom
    if dia == 6 and not j.get("domingo"):
        return False, "domingo"
    if dia == 5:
        ini, fim = _hhmm(j["sabado_inicio"]), _hhmm(j["sabado_fim"])
    else:
        ini, fim = _hhmm(j["inicio"]), _hhmm(j["fim"])
    if not (ini <= q.time() < fim):
        return False, f"fora da janela ({ini:%H:%M}-{fim:%H:%M})"
    return True, ""


def proxima_abertura(cfg, quando=None):
    """Quando a janela reabre -- o motor dorme ate la em vez de girar em vao."""
    q = quando or datetime.now()
    for _ in range(8):
        ok, _m = janela_aberta(cfg, q)
        if ok:
            return q
        j = cfg["janela"]
        ini = _hhmm(j["sabado_inicio"] if q.weekday() == 5 else j["inicio"])
        cand = q.replace(hour=ini.hour, minute=ini.minute, second=0, microsecond=0)
        q = cand if cand > q else (q + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
    return q


def bloqueado(telefone):
    r = db.um("SELECT motivo FROM bloqueio WHERE telefone=?", telefone)
    return r["motivo"] if r else None


ENTREGUES = ("enviado", "entregue", "lido", "respondido")


def em_cooldown(telefone, dias, ignorar_envio=None):
    if not dias:
        return False
    limite = (datetime.now().astimezone() - timedelta(days=dias)).isoformat(timespec="seconds")
    r = db.um(
        f"SELECT 1 FROM envio WHERE telefone=? AND id IS NOT ? "
        f"AND estado IN ({','.join('?' * len(ENTREGUES))}) AND enviado_em > ? LIMIT 1",
        telefone, ignorar_envio, *ENTREGUES, limite)
    return bool(r)


def inicio_do_dia():
    """ISO local do 00:00 de hoje. Comparar string com string evita o date() do
    SQLite converter o offset para UTC e virar o dia."""
    return datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def enviados_hoje(campanha_id=None):
    inicio = inicio_do_dia()
    if campanha_id is None:
        r = db.um(f"SELECT COUNT(*) n FROM envio WHERE enviado_em>=? "
                  f"AND estado IN ({','.join('?' * len(ENTREGUES))})", inicio, *ENTREGUES)
        return r["n"] if r else 0
    r = db.um(
        f"SELECT COUNT(*) n FROM envio WHERE campanha_id=? AND enviado_em>=? "
        f"AND estado IN ({','.join('?' * len(ENTREGUES))})",
        campanha_id, inicio, *ENTREGUES)
    return r["n"] if r else 0


def cota_restante(campanha):
    return max(0, limite_efetivo(campanha) - enviados_hoje(campanha["id"]))


def liberar(envio, cfg):
    """Portao por mensagem. Devolve (True,'') ou (False, motivo)."""
    tel = envio["telefone"]
    m = bloqueado(tel)
    if m:
        return False, f"bloqueado:{m}"
    if em_cooldown(tel, cfg.get("cooldown_dias", 0), envio["id"]):
        return False, f"cooldown:{cfg['cooldown_dias']}d"
    return True, ""


def canonizar(bruto):
    """Numero cru (webhook, painel, CLI) -> a forma exata que existe no banco.

    O gateway pode devolver o numero sem o 9o digito. Bloquear a string que ele
    mandou, sem procurar o equivalente, cria um bloqueio fantasma: o lead pediu
    para sair e a fila DELE continua intacta. Aqui o numero e resolvido contra o
    que realmente foi enviado antes de virar bloqueio.
    """
    e164, _tipo = fone.normalizar(bruto)
    formas = fone.variantes(e164 or bruto)
    if not formas:
        return None, []
    for f in formas:                            # o que ja existe manda
        if db.um("SELECT 1 FROM envio WHERE telefone=? LIMIT 1", f) or \
           db.um("SELECT 1 FROM contato WHERE telefone=? LIMIT 1", f):
            return f, formas
    return e164 or formas[0], formas


def registrar_optout(telefone, origem="resposta", motivo="optout"):
    """Bloqueia e cancela tudo que ainda estava na fila para esse numero.

    Cancelar a fila e o ponto critico: sem isso o lead pede para sair e recebe
    mais 3 mensagens ja agendadas -- que e exatamente o que gera denuncia.
    """
    alvo, formas = canonizar(telefone)
    if not alvo:
        return 0
    anterior = db.um("SELECT motivo FROM bloqueio WHERE telefone=?", alvo)
    # 'optout' e o pedido do titular: pode substituir um bloqueio tecnico, mas
    # nunca ser substituido por um. Perder esse registro e perder a prova de
    # que o pedido foi atendido.
    if anterior is None or (motivo == "optout" and anterior["motivo"] != "optout"):
        db.exec_("INSERT OR REPLACE INTO bloqueio(telefone,motivo,origem,criado_em) "
                 "VALUES(?,?,?,?)", alvo, motivo, origem, db.agora())
    cancelados = 0
    for f in formas or [alvo]:                  # a fila pode ter as duas formas
        cur = db.exec_("UPDATE envio SET estado='cancelado', erro=?, atualizado_em=? "
                       "WHERE telefone=? AND estado IN ('fila','enviando','indeterminado')",
                       f"cancelado por {motivo}", db.agora(), f)
        cancelados += cur.rowcount
    db.log("optout", {"origem": origem, "motivo": motivo, "recebido": telefone,
                      "bloqueado": alvo, "cancelados": cancelados}, telefone=alvo)
    return cancelados


# --- aquecimento ------------------------------------------------------------
# Chip novo que dispara 500 mensagens no primeiro dia e chip banido no segundo.
# A rampa vale para WhatsApp nao-oficial; em SMS/Cloud API o limite e so a cota.
RAMPA = [20, 40, 80, 150, 250, 400, 600, 900]


def limite_efetivo(campanha):
    """Cota do dia considerando a rampa de aquecimento, se a campanha usar.

    Nada aqui pode levantar excecao: esta funcao roda dentro do ciclo do motor e
    na listagem do painel -- uma linha com meta corrompida derrubaria as duas.
    """
    teto = int(campanha["limite_dia"])
    rampa = db.meta(campanha).get("aquecimento")
    if not rampa:
        return teto
    if rampa is True or not isinstance(rampa, list):
        rampa = RAMPA
    inicio = db.desde(campanha["iniciado_em"] or campanha["criado_em"])
    if inicio is None:
        return min(teto, int(rampa[0]))         # sem data confiavel, comeca devagar
    dias = max(0, (datetime.now().astimezone() - inicio).days)
    return min(teto, int(rampa[min(dias, len(rampa) - 1)]))
