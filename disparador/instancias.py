#!/usr/bin/env python3
"""
Chips (instancias do gateway) como entidade de primeira classe.

A regra que organiza este modulo: quem toma ban e o CHIP, nao a campanha.
Entao cota diaria, ritmo e rampa de aquecimento moram aqui. Duas campanhas
usando o mesmo chip somam no MESMO limite; um chip que estourou a cota para
de enviar por todas elas.

O gateway e a fonte da verdade sobre conexao -- `sincronizar` espelha o que
ele diz. O banco e a fonte da verdade sobre cota e rampa, porque isso o
gateway nao sabe.
"""
from collections import namedtuple
from datetime import datetime, timedelta

from . import db, provedores
from .regras import ENTREGUES

# Rampa local por CHIP. Ela limita o volume inicial e nunca substitui os
# limites, a elegibilidade e as politicas do provedor.
RAMPA = [20, 40, 80, 150, 250, 400, 600, 900]
MAX_INSTANCIAS = db.MAX_INSTANCIAS       # o trigger do banco usa o mesmo numero

# Instancias do gateway recusadas por falta de vaga, ja avisadas neste processo.
_RECUSADOS_AVISADOS = set()

# O que o motor precisa saber para mandar uma mensagem por um chip.
Remetente = namedtuple("Remetente", "id nome provedor por_minuto extra rotulo")


# --- sincronizacao com o gateway --------------------------------------------
def sincronizar(cfg, provedor="evolution"):
    """Espelha o estado das instancias do gateway no banco.

    Devolve (encontradas, novas). Instancia que existe no gateway e nao no
    banco entra automaticamente -- assim quem criou pelo painel do Evolution
    aparece aqui sem cadastro manual.
    """
    conf = cfg["provedores"].get(provedor, {})
    mod = provedores.REGISTRO.get(provedor)
    if not mod or not hasattr(mod, "listar_instancias"):
        return 0, 0
    achadas = mod.listar_instancias(conf)
    if achadas is None:
        return 0, 0

    novas = 0
    for i in achadas:
        linha = db.um("SELECT * FROM instancia WHERE nome=?", i["nome"])
        if linha is None:
            if db.um("SELECT COUNT(*) n FROM instancia")["n"] >= MAX_INSTANCIAS:
                # o vigia sincroniza a cada minuto: sem este filtro a mesma
                # instancia excedente gerava 1440 avisos por dia
                if i["nome"] not in _RECUSADOS_AVISADOS:
                    _RECUSADOS_AVISADOS.add(i["nome"])
                    db.log("chip_ignorado_limite", {"nome": i["nome"],
                                                     "limite": MAX_INSTANCIAS})
                continue
            db.exec_(
                "INSERT INTO instancia(nome,rotulo,provedor,estado,numero,visto_em,criado_em)"
                " VALUES(?,?,?,?,?,?,?)",
                i["nome"], i.get("perfil") or i["nome"], provedor,
                i.get("estado"), i.get("numero"), db.agora(), db.agora())
            novas += 1
            linha = db.um("SELECT * FROM instancia WHERE nome=?", i["nome"])
        # A rampa so comeca quando o chip aparece conectado E esta habilitada.
        # Antes, um chip com a rampa desligada ganhava dias sem operar e podia
        # pular direto para uma cota alta ao ser habilitado depois.
        inicio = linha["iniciado_em"]
        if i.get("estado") == "open" and linha["aquecer"] and not inicio:
            inicio = db.agora()
            db.log("chip_conectado", {"nome": i["nome"], "numero": i.get("numero")})
        conectado = i.get("estado") == "open"
        db.exec_("UPDATE instancia SET estado=?, numero=COALESCE(?,numero),"
                 " iniciado_em=?, visto_em=?, motivo_queda=?,"
                 " visto_ok_em=CASE WHEN ? THEN ? ELSE visto_ok_em END WHERE id=?",
                 i.get("estado"), i.get("numero"), inicio, db.agora(),
                 (None if conectado else str(i.get("motivo_queda") or "")),
                 1 if conectado else 0, db.agora(), linha["id"])
    reconectar_pendentes(cfg, provedor)
    return len(achadas), novas


# --- reconexao automatica ---------------------------------------------------
# O gateway (Baileys) reconecta sozinho na maioria das quedas, mas nao em todas:
# depois de um restart do container, de um 'connecting' que nunca termina ou de
# um close por timeout ele as vezes fica parado esperando alguem chamar
# /instance/connect. Com a sessao ainda valida essa chamada volta SEM QR. E o
# que o vigia faz aqui, com freio por chip para nao martelar o gateway.
#
# O que NAO e reconectavel: 401 (o WhatsApp encerrou a sessao -- banimento ou
# "desconectar" feito no celular) e 403 (numero bloqueado). Ai a sessao ja nao
# existe no gateway e so um QR novo resolve; insistir seria inutil.
RECONEXAO_A_CADA = timedelta(minutes=2)


def reconectar_pendentes(cfg, provedor="evolution"):
    """Pede ao gateway para reerguer os chips pareados que estao fora do ar.

    Devolve os nomes em que a reconexao foi pedida. Chip sem numero nunca
    pareou (esta esperando QR); chip banido nao tem sessao para reerguer.
    """
    mod = provedores.REGISTRO.get(provedor)
    if not mod or not hasattr(mod, "reconectar_instancia"):
        return []
    conf = cfg["provedores"].get(provedor, {})
    agora = datetime.now().astimezone()
    pedidos = []
    for i in db.todos("SELECT * FROM instancia WHERE provedor=? ORDER BY id", provedor):
        if not reconectavel(i):
            continue
        ultima = db.desde(i["reconexao_em"])
        if ultima and agora - ultima < RECONEXAO_A_CADA:
            continue
        db.exec_("UPDATE instancia SET reconexao_em=? WHERE id=?", db.agora(), i["id"])
        resultado, detalhe = mod.reconectar_instancia(i["nome"], conf)
        if resultado == "qr":
            # o gateway nao tem mais a sessao: para de insistir e avisa no
            # painel que este chip precisa de QR
            db.exec_("UPDATE instancia SET motivo_queda='sem_sessao' WHERE id=?", i["id"])
        db.log("chip_reconectando", {"nome": i["nome"], "estado": i["estado"],
                                     "motivo_queda": i["motivo_queda"],
                                     "resultado": resultado, "detalhe": detalhe})
        pedidos.append(i["nome"])
    return pedidos


def registrar_conexao(nome, estado, motivo=None):
    """Espelha um connection.update do webhook na hora, sem esperar o vigia.

    Devolve a linha do chip ou None se ele nao esta cadastrado.
    """
    linha = db.um("SELECT * FROM instancia WHERE nome=?", nome)
    if not linha:
        return None
    conectado = estado == "open"
    inicio = linha["iniciado_em"]
    if conectado and linha["aquecer"] and not inicio:
        inicio = db.agora()
    # o motivo so faz sentido em 'close'; 'connecting' vem com 200 e nao e queda
    motivo_queda = None if conectado else (
        str(motivo) if estado == "close" and motivo is not None else "")
    db.exec_("UPDATE instancia SET estado=?, iniciado_em=?, visto_em=?, motivo_queda=?,"
             " visto_ok_em=CASE WHEN ? THEN ? ELSE visto_ok_em END WHERE id=?",
             estado, inicio, db.agora(), motivo_queda,
             1 if conectado else 0, db.agora(), linha["id"])
    db.log("chip_conectado" if conectado else
           ("chip_caiu" if estado == "close" else "chip_conectando"),
           {"nome": nome, "estado": estado, "motivo": motivo})
    return db.um("SELECT * FROM instancia WHERE id=?", linha["id"])


def criar(nome, cfg, rotulo=None, provedor="evolution"):
    """Cria a instancia no gateway e registra aqui."""
    if db.um("SELECT COUNT(*) n FROM instancia")["n"] >= MAX_INSTANCIAS:
        return None, f"limite de {MAX_INSTANCIAS} chips atingido"
    mod = provedores.pegar(provedor)
    ok, erro = mod.criar_instancia(nome, cfg["provedores"].get(provedor, {}))
    if not ok:
        return None, erro
    db.exec_("INSERT OR IGNORE INTO instancia(nome,rotulo,provedor,criado_em)"
             " VALUES(?,?,?,?)", nome, rotulo or nome, provedor, db.agora())
    db.log("chip_criado", {"nome": nome})
    return db.um("SELECT * FROM instancia WHERE nome=?", nome), ""


def pausar(nome_ou_id, ativa=False):
    """Tira o chip do rodizio SEM tocar na sessao do WhatsApp.

    E esta a acao do dia a dia. Conectar um chip custa um QR lido no celular;
    desconectar por engano custa refazer isso. Aqui so muda uma coluna do nosso
    banco -- a sessao continua viva no gateway e volta com um clique.
    """
    coluna = "id" if isinstance(nome_ou_id, int) else "nome"
    linha = db.um(f"SELECT * FROM instancia WHERE {coluna}=?", nome_ou_id)
    if not linha:
        return None
    db.exec_("UPDATE instancia SET ativa=? WHERE id=?", 1 if ativa else 0, linha["id"])
    db.log("chip_pausado" if not ativa else "chip_religado", {"nome": linha["nome"]})
    return db.um("SELECT * FROM instancia WHERE id=?", linha["id"])


def remover(nome, cfg, confirmar_nome=None):
    """DESCONECTA a sessao no gateway e apaga o chip daqui.

    Destrutivo e irreversivel: faz logout no WhatsApp, e o numero so volta
    lendo um QR novo. Por isso exige repetir o nome do chip -- um clique
    errado no painel nao pode custar uma sessao.

    Para so tirar da operacao, use pausar().
    """
    linha = db.um("SELECT * FROM instancia WHERE nome=?", nome)
    if not linha:
        return False, "chip nao encontrado"
    if str(confirmar_nome or "").strip() != linha["nome"]:
        return False, ("digite o nome do chip para confirmar: a sessao do WhatsApp "
                       "sera encerrada e so volta lendo um QR novo")
    mod = provedores.pegar(linha["provedor"])
    if hasattr(mod, "remover_instancia"):
        mod.remover_instancia(nome, cfg["provedores"].get(linha["provedor"], {}))
    # A faixa reservada tem de sair da frente ANTES do DELETE: envio.instancia_id
    # aponta para instancia(id), e apagar o chip com fila reservada estourava
    # FOREIGN KEY -- deixando o operador presis com um chip banido que nao sai.
    soltos = 0
    for camp in db.todos("SELECT DISTINCT campanha_id FROM envio WHERE instancia_id=?",
                         linha["id"]):
        soltos += redistribuir(camp["campanha_id"], linha["id"], "chip desconectado")
    # o que nao pode ser redistribuido (nenhum chip vivo, ou ja saiu) perde
    # so o dono; o historico do envio continua inteiro
    db.exec_("UPDATE envio SET instancia_id=NULL WHERE instancia_id=?", linha["id"])
    db.exec_("DELETE FROM instancia WHERE id=?", linha["id"])
    db.log("chip_desconectado",
           {"nome": nome, "sessao": "encerrada", "fila_solta": soltos})
    return True, ""


# --- cota e rampa, por chip --------------------------------------------------
def _dias_de_rampa(inst):
    inicio = db.desde(inst["iniciado_em"])
    if inicio is None:
        return None
    return max(0, (datetime.now().astimezone() - inicio).days)


def _indice_rampa(inst):
    dias = _dias_de_rampa(inst)
    return 0 if dias is None else min(dias, len(RAMPA) - 1)


def limite_efetivo(inst):
    """Cota de hoje deste chip, considerando a rampa."""
    teto = int(inst["limite_dia"])
    if not inst["aquecer"]:
        return teto
    return min(teto, RAMPA[_indice_rampa(inst)])


def enviados_hoje(instancia_id):
    from .regras import inicio_do_dia
    r = db.um(f"SELECT COUNT(*) n FROM envio WHERE instancia_id=? AND enviado_em>=?"
              f" AND estado IN ({','.join('?' * len(ENTREGUES))})",
              instancia_id, inicio_do_dia(), *ENTREGUES)
    return r["n"] if r else 0


def cota_restante(inst):
    return max(0, limite_efetivo(inst) - enviados_hoje(inst["id"]))


def dia_da_rampa(inst):
    return _indice_rampa(inst) + 1


def estado_da_rampa(inst):
    if not inst["aquecer"]:
        return "desativada"
    if not inst["iniciado_em"]:
        return "aguardando_conexao"
    if _indice_rampa(inst) == len(RAMPA) - 1:
        return "estabilizada"
    return "em_andamento"


def proximo_limite(inst):
    """Teto da proxima etapa, sem ultrapassar o limite manual do chip."""
    teto = int(inst["limite_dia"])
    if not inst["aquecer"]:
        return teto
    proximo = min(_indice_rampa(inst) + 1, len(RAMPA) - 1)
    return min(teto, RAMPA[proximo])


# --- quem pode enviar agora --------------------------------------------------
def prontas(provedor="evolution"):
    """Chips ativos, conectados e com cota sobrando."""
    saida = []
    for i in db.todos("SELECT * FROM instancia WHERE provedor=? AND ativa=1"
                      " ORDER BY id", provedor):
        if i["estado"] != "open":
            continue
        if cota_restante(i) <= 0:
            continue
        saida.append(i)
    return saida


# Quanto tempo um chip pode ficar fora antes de perder a faixa. Sem carencia,
# uma oscilacao de rede de 30s embaralharia a divisao inteira.
CARENCIA_QUEDA = timedelta(minutes=10)


def banido(inst):
    """True quando a queda tem cara de bloqueio, nao de oscilacao."""
    return str(inst["motivo_queda"] or "") in ("401", "403", "banned", "forbidden")


# Quedas em que a sessao ja nao existe no gateway: 401/403 (WhatsApp encerrou),
# 500 (sessao corrompida) e 'sem_sessao' (o gateway respondeu com QR quando
# pedimos para reconectar). Insistir e inutil -- so um QR novo resolve.
SEM_SESSAO = ("401", "403", "500", "banned", "forbidden", "sem_sessao")


def reconectavel(inst):
    """Pareou, esta fora do ar e a sessao ainda pode existir no gateway."""
    return (bool(inst["numero"]) and inst["estado"] != "open" and bool(inst["ativa"])
            and str(inst["motivo_queda"] or "") not in SEM_SESSAO)


def redistribuir(campanha_id, de_id, motivo="chip indisponivel"):
    """Passa a fila reservada de um chip para os que continuam de pe.

    Mexe SO em linhas 'fila'. Nao encosta em 'enviando'/'enviado', nao muda o
    estado da instancia e nao desconecta nada -- a sessao do chip que saiu
    continua intacta, esperando voltar.
    """
    destinos = [i["id"] for i in prontas() if i["id"] != de_id]
    if not destinos:
        return 0
    linhas = db.todos("SELECT id FROM envio WHERE campanha_id=? AND instancia_id=?"
                      " AND estado='fila' ORDER BY id", campanha_id, de_id)
    for pos, linha in enumerate(linhas):
        db.exec_("UPDATE envio SET instancia_id=?, atualizado_em=? WHERE id=?",
                 destinos[pos % len(destinos)], db.agora(), linha["id"])
    if linhas:
        db.log("faixa_redistribuida",
               {"de": de_id, "para": destinos, "linhas": len(linhas), "motivo": motivo},
               campanha_id=campanha_id)
    return len(linhas)


def orfas(campanha_id):
    """Chips com fila reservada que nao podem enviar agora.

    Devolve [(instancia, quantidade, pode_redistribuir_ja)].
    """
    vivos = {i["id"] for i in prontas()}
    saida = []
    for r in db.todos(
            "SELECT instancia_id, COUNT(*) n FROM envio WHERE campanha_id=?"
            " AND estado='fila' AND instancia_id IS NOT NULL GROUP BY instancia_id",
            campanha_id):
        if r["instancia_id"] in vivos:
            continue
        inst = db.um("SELECT * FROM instancia WHERE id=?", r["instancia_id"])
        if not inst:
            saida.append((None, r["n"], True))
            continue
        visto = db.desde(inst["visto_ok_em"] or inst["visto_em"])
        fora_ha = (datetime.now().astimezone() - visto) if visto else CARENCIA_QUEDA
        saida.append((inst, r["n"], banido(inst) or fora_ha >= CARENCIA_QUEDA))
    return saida


def ativar_rampa(ids=None):
    """Habilita a rampa nos chips ativos e inicia os que estao conectados.

    Chips ainda sem QR ficam habilitados e entram automaticamente no dia 1
    assim que `sincronizar` os enxergar como `open`. Quem ja comecou nunca e
    reiniciado, portanto o botao e idempotente e pode ser clicado sem medo.
    """
    selecionados = None
    if ids is not None:
        if not isinstance(ids, (list, tuple, set)):
            raise ValueError("ids precisa ser uma lista")
        try:
            selecionados = {int(i) for i in ids}
        except (TypeError, ValueError):
            raise ValueError("id de chip invalido") from None

    habilitados, iniciados, aguardando, ja_ativos = [], [], [], []
    for i in db.todos("SELECT * FROM instancia WHERE ativa=1 ORDER BY id"):
        if selecionados is not None and i["id"] not in selecionados:
            continue
        if not i["aquecer"]:
            db.exec_("UPDATE instancia SET aquecer=1 WHERE id=?", i["id"])
            habilitados.append(i["nome"])
        if i["estado"] == "open" and not i["iniciado_em"]:
            db.exec_("UPDATE instancia SET iniciado_em=? WHERE id=?", db.agora(), i["id"])
            iniciados.append(i["nome"])
        elif not i["iniciado_em"]:
            aguardando.append(i["nome"])
        else:
            ja_ativos.append(i["nome"])

    if habilitados or iniciados:
        db.log("rampa_automatica_ativada", {
            "habilitados": habilitados, "iniciados": iniciados,
            "aguardando_conexao": aguardando,
        })
    return {"habilitados": habilitados, "iniciados": iniciados,
            "aguardando_conexao": aguardando, "ja_ativos": ja_ativos}


def iniciar_rampa(ids=None):
    """Compatibilidade: devolve somente os chips iniciados agora."""
    return ativar_rampa(ids)["iniciados"]


def remetentes(campanha, cfg):
    """Por onde esta campanha pode mandar agora.

    Provedor com instancias (Evolution) devolve um remetente por chip, cada um
    com o proprio ritmo. Provedor sem instancias (SMS, Cloud API) devolve um
    remetente unico, com o ritmo e a cota da propria campanha -- assim o motor
    tem um caminho so.
    """
    prov = campanha["provedor"]
    mod = provedores.REGISTRO.get(prov)
    if mod is None:
        return []
    if not hasattr(mod, "listar_instancias"):
        from .regras import cota_restante as cota_campanha
        if cota_campanha(campanha) <= 0:
            return []
        return [Remetente(None, prov, prov, float(campanha["por_minuto"]), {}, prov)]

    escolhidos = db.meta(campanha).get("instancias")     # opcional: restringir
    saida = []
    for i in prontas(prov):
        if escolhidos and i["nome"] not in escolhidos:
            continue
        saida.append(Remetente(i["id"], i["nome"], prov, float(i["por_minuto"]),
                               {"instancia": i["nome"]}, i["rotulo"] or i["nome"]))
    return saida


def resumo():
    """Uma linha por chip, para o painel e o CLI."""
    saida = []
    for i in db.todos("SELECT * FROM instancia ORDER BY id"):
        saida.append({
            "id": i["id"], "nome": i["nome"], "rotulo": i["rotulo"],
            "provedor": i["provedor"], "ativa": bool(i["ativa"]),
            "estado": i["estado"], "numero": i["numero"],
            "conectada": i["estado"] == "open",
            "por_minuto": i["por_minuto"], "limite_dia": i["limite_dia"],
            "aquecer": bool(i["aquecer"]), "conversar": False,
            "dia_rampa": dia_da_rampa(i) if i["aquecer"] else None,
            "rampa_estado": estado_da_rampa(i),
            "rampa_iniciada_em": i["iniciado_em"],
            "cota_hoje": limite_efetivo(i),
            "proxima_cota": proximo_limite(i),
            "enviados_hoje": enviados_hoje(i["id"]),
            "cota_restante": cota_restante(i),
            "motivo_queda": i["motivo_queda"] or None,
            "banido": banido(i),
            "reconectando": reconectavel(i),    # o vigia esta reerguendo
            "reconexao_em": i["reconexao_em"],
        })
    return saida


def estado_rampa():
    """Resumo operacional para o painel, sem dados do legado artificial."""
    chips = resumo()
    return {
        "habilitados": sum(1 for i in chips if i["aquecer"]),
        "em_andamento": sum(1 for i in chips if i["rampa_estado"] == "em_andamento"),
        "estabilizados": sum(1 for i in chips if i["rampa_estado"] == "estabilizada"),
        "aguardando_conexao": sum(
            1 for i in chips if i["rampa_estado"] == "aguardando_conexao"),
        "desativados": sum(1 for i in chips if i["rampa_estado"] == "desativada"),
    }


def capacidade_diaria():
    """Quantas mensagens o conjunto de chips aguenta HOJE, somando as rampas."""
    return sum(limite_efetivo(i) for i in
               db.todos("SELECT * FROM instancia WHERE ativa=1")
               if i["estado"] == "open")
