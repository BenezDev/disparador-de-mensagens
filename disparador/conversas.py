#!/usr/bin/env python3
"""
Conversa por chip: o que entrou, o que saiu e a resposta manual do operador.

O gateway nao guarda mensagem nenhuma (DATABASE_SAVE_DATA_NEW_MESSAGE=false e
irmaos, desligados de proposito para o banco dele nao crescer). Entao a tabela
`mensagem` e a unica memoria da conversa, e ela so tem o que passou por aqui:
entrada pelo webhook, saida pelo motor (campanha) e pela resposta manual.

A regra que organiza este modulo: responder NAO e disparar. A resposta manual
sai fora da fila, fora da cota e fora da janela -- porque o cliente esta do
outro lado esperando -- mas so para quem JA falou com aquele chip. Sem essa
amarra o painel viraria um segundo caminho de disparo, sem consentimento.
"""
from . import db, fone, provedores, regras

LIMITE_TEXTO = 4096            # o WhatsApp corta bem antes disso


def _instancia_id(chip):
    linha = db.um("SELECT id FROM instancia WHERE nome=?", chip)
    return linha["id"] if linha else None


def registrar(chip, telefone, direcao, texto, tipo="texto", id_externo=None,
              envio_id=None, origem="webhook", instancia_id=None):
    """Grava uma mensagem. Devolve o id, ou None se ja existia.

    A mesma mensagem chega mais de uma vez quando o gateway repete o webhook;
    o indice unico (chip, id_externo) resolve isso sem consulta previa.
    """
    if not chip or not telefone:
        return None
    if instancia_id is None:
        instancia_id = _instancia_id(chip)
    cur = db.exec_(
        "INSERT OR IGNORE INTO mensagem(chip,instancia_id,telefone,direcao,texto,"
        "tipo,id_externo,envio_id,origem,criado_em) VALUES(?,?,?,?,?,?,?,?,?,?)",
        chip, instancia_id, telefone, direcao, texto, tipo, id_externo, envio_id,
        origem, db.agora())
    if cur.rowcount == 1:
        return cur.lastrowid
    if id_externo and origem != "webhook":
        # o eco do webhook (fromMe) chegou antes de quem mandou gravar: a linha
        # e a mesma mensagem, so falta dizer de onde ela saiu
        db.exec_("UPDATE mensagem SET origem=?, envio_id=COALESCE(envio_id,?)"
                 " WHERE chip=? AND id_externo=? AND origem='webhook'",
                 origem, envio_id, chip, id_externo)
    return None


def chips_com_conversa():
    """Um item por chip cadastrado, com o que o painel precisa para a lista."""
    naolidas = {r["chip"]: r["n"] for r in db.todos(
        "SELECT chip, COUNT(*) n FROM mensagem WHERE direcao='entrada'"
        " AND lida_em IS NULL GROUP BY chip")}
    ultimas = {r["chip"]: r["ts"] for r in db.todos(
        "SELECT chip, MAX(criado_em) ts FROM mensagem GROUP BY chip")}
    saida = []
    for i in db.todos("SELECT * FROM instancia ORDER BY id"):
        saida.append({
            "chip": i["nome"], "rotulo": i["rotulo"] or i["nome"],
            "numero": i["numero"], "conectada": i["estado"] == "open",
            "ativa": bool(i["ativa"]),
            "nao_lidas": naolidas.get(i["nome"], 0),
            "ultima_em": ultimas.get(i["nome"]),
        })
    return saida


def conversas(chip, limite=200):
    """Uma linha por cliente que falou com este chip, mais recente primeiro."""
    linhas = db.todos(
        "SELECT m.telefone,"
        "       MAX(m.id) ultimo_id,"
        "       COUNT(*) total,"
        "       SUM(CASE WHEN m.direcao='entrada' AND m.lida_em IS NULL THEN 1 ELSE 0 END) nao_lidas"
        "  FROM mensagem m WHERE m.chip=? GROUP BY m.telefone"
        "  ORDER BY ultimo_id DESC LIMIT ?", chip, int(limite))
    saida = []
    for r in linhas:
        ult = db.um("SELECT texto, tipo, direcao, criado_em FROM mensagem WHERE id=?",
                    r["ultimo_id"])
        contato = db.um("SELECT nome, cidade FROM contato WHERE telefone=?", r["telefone"])
        saida.append({
            "telefone": r["telefone"],
            "nome": (contato["nome"] if contato else None),
            "cidade": (contato["cidade"] if contato else None),
            "total": r["total"], "nao_lidas": r["nao_lidas"] or 0,
            "ultimo_texto": ult["texto"] if ult else "",
            "ultimo_tipo": ult["tipo"] if ult else "texto",
            "ultima_direcao": ult["direcao"] if ult else None,
            "ultima_em": ult["criado_em"] if ult else None,
            "bloqueado": regras.bloqueado(r["telefone"]),
        })
    return saida


def thread(chip, telefone, limite=300):
    """As mensagens da conversa, da mais antiga para a mais nova."""
    linhas = db.todos(
        "SELECT * FROM (SELECT id, direcao, texto, tipo, criado_em, origem, envio_id"
        "  FROM mensagem WHERE chip=? AND telefone=? ORDER BY id DESC LIMIT ?)"
        " ORDER BY id", chip, telefone, int(limite))
    return [dict(r) for r in linhas]


def marcar_lida(chip, telefone):
    """Some com o contador de nao lidas desta conversa."""
    cur = db.exec_("UPDATE mensagem SET lida_em=? WHERE chip=? AND telefone=?"
                   " AND direcao='entrada' AND lida_em IS NULL",
                   db.agora(), chip, telefone)
    return cur.rowcount


def conhece(chip, telefone):
    """True se este cliente ja trocou mensagem com este chip."""
    return bool(db.um("SELECT 1 FROM mensagem WHERE chip=? AND telefone=? LIMIT 1",
                      chip, telefone))


def responder(chip, telefone, texto, cfg):
    """Manda uma resposta manual por este chip. Devolve (ok, erro_ou_vazio).

    Nao passa pela fila, nao consome cota e ignora a janela de horario: e uma
    conversa, nao um disparo. Em troca exige que o cliente JA tenha falado com
    este chip -- e o que impede o painel de virar um caminho de disparo sem
    consentimento -- e respeita a blocklist, que vale para qualquer saida.
    """
    texto = str(texto or "").strip()
    if not texto:
        return False, "escreva a mensagem"
    if len(texto) > LIMITE_TEXTO:
        return False, f"mensagem longa demais (maximo {LIMITE_TEXTO} caracteres)"

    e164, tipo_num = fone.normalizar(telefone)
    if not e164:
        return False, f"telefone invalido ({tipo_num})"
    alvo, _formas = regras.canonizar(e164)
    alvo = alvo or e164

    if not conhece(chip, alvo):
        return False, ("este numero nunca falou com este chip: a resposta manual"
                       " so existe dentro de uma conversa ja aberta")

    motivo = regras.bloqueado(alvo)
    if motivo:
        # opt-out vale para tudo que sai, inclusive resposta manual: quem pediu
        # para sair pediu para sair.
        return False, f"numero na blocklist ({motivo}) — nao e possivel responder"

    inst = db.um("SELECT * FROM instancia WHERE nome=?", chip)
    if not inst:
        return False, "chip nao encontrado"
    if inst["estado"] != "open":
        return False, "chip desconectado — conecte antes de responder"

    prov = provedores.pegar(inst["provedor"])
    cred = dict(cfg["provedores"].get(inst["provedor"], {}), instancia=chip)
    pronto, falta = prov.pronto(cred)
    if not pronto:
        return False, falta

    r = prov.enviar(alvo, texto, cred, {"canal": "whatsapp"})
    if not r.ok:
        db.log("resposta_manual_falhou", {"chip": chip, "erro": r.erro}, telefone=alvo)
        return False, r.erro

    registrar(chip, alvo, "saida", texto, id_externo=r.id_externo,
              origem="manual", instancia_id=inst["id"])
    db.log("resposta_manual", {"chip": chip, "caracteres": len(texto)}, telefone=alvo)
    return True, ""


def nao_lidas():
    """Total de entradas nao lidas, para o contador do topo do painel."""
    r = db.um("SELECT COUNT(*) n FROM mensagem WHERE direcao='entrada' AND lida_em IS NULL")
    return r["n"] if r else 0
