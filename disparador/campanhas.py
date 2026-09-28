#!/usr/bin/env python3
"""
Criacao de campanha e montagem do publico.

A fila inteira e materializada em UM insert-select: para 250 mil leads isso e
a diferenca entre 2 segundos e meia hora. E, no mesmo SQL, ja saem de fora:
  - quem esta na blocklist / pediu para sair
  - quem ja esta na fila de outra campanha (ninguem recebe dois disparos)
  - quem recebeu mensagem nossa dentro do cooldown
"""
import json, random
from datetime import datetime, timedelta

from . import config, db, modelos, provedores


def criar(nome, canal, provedor, modelo, meta=None, por_minuto=10, limite_dia=300):
    if canal not in provedores.CANAIS:
        raise ValueError(f"canal invalido: {canal}")
    if provedor not in provedores.CANAIS[canal]:
        raise ValueError(f"{provedor} nao atende o canal {canal}")
    img = (meta or {}).get("imagem")
    if img:
        from . import midia
        if canal != "whatsapp":
            raise ValueError("imagem so vale para WhatsApp; SMS nao carrega foto")
        if not getattr(provedores.pegar(provedor), "IMAGEM", False):
            raise ValueError(f"{provedor} nao envia imagem (use WhatsApp via Evolution)")
        if not midia.existe(img):
            raise ValueError("a imagem informada nao existe mais; envie de novo")
    cur = db.exec_(
        "INSERT INTO campanha(nome,canal,provedor,modelo,meta,por_minuto,limite_dia,criado_em)"
        " VALUES(?,?,?,?,?,?,?,?) RETURNING *",
        nome, canal, provedor, modelo,
        json.dumps(meta or {}, ensure_ascii=False), por_minuto, limite_dia, db.agora())
    c = cur.fetchone()
    db.log("campanha_criada", {"nome": nome, "canal": canal, "provedor": provedor},
           campanha_id=c["id"])
    return c


def _filtro(f):
    """Traduz o dict de filtros em (sql, params) -- tudo parametrizado."""
    sql, par = [], []
    if f.get("cidade"):
        cids = f["cidade"] if isinstance(f["cidade"], list) else [f["cidade"]]
        sql.append(f"UPPER(c.cidade) IN ({','.join('?' * len(cids))})")
        par += [x.upper() for x in cids]
    if f.get("uf"):
        sql.append("UPPER(c.uf)=?")
        par.append(f["uf"].upper())
    if f.get("segmento"):
        sql.append("c.segmento LIKE ?")
        par.append(f"%{f['segmento']}%")
    if f.get("busca"):
        sql.append("(c.nome LIKE ? OR c.segmento LIKE ?)")
        par += [f"%{f['busca']}%"] * 2
    if f.get("com_email"):
        sql.append("c.email IS NOT NULL AND c.email<>''")
    if f.get("ddd"):
        sql.append("substr(c.telefone,3,2)=?")
        par.append(str(f["ddd"]))
    return sql, par


def contar_publico(filtros=None, campanha_id=None):
    sql, par = _montar_select("COUNT(*) n", filtros or {}, campanha_id)
    return db.conn().execute(sql, par).fetchone()["n"]


def _montar_select(colunas, filtros, campanha_id=None, limite=None, aleatorio=False):
    if limite is not None:
        limite = int(limite)
        if limite <= 0:
            raise ValueError("limite precisa ser positivo")
    cfg = config.carregar()
    corte = (datetime.now().astimezone()
             - timedelta(days=cfg.get("cooldown_dias", 0))).isoformat(timespec="seconds")
    onde, par = _filtro(filtros)
    par_ini = []
    base = f"""
      SELECT {colunas} FROM contato c
       WHERE c.tipo='MOVEL'
         AND NOT EXISTS (SELECT 1 FROM bloqueio b WHERE b.telefone=c.telefone)
         AND NOT EXISTS (SELECT 1 FROM envio e WHERE e.telefone=c.telefone
                           AND (e.estado IN ('fila','enviando','indeterminado')
                                OR (e.enviado_em IS NOT NULL AND e.enviado_em > ?)))
    """
    par_ini.append(corte)
    if campanha_id:
        base += " AND NOT EXISTS (SELECT 1 FROM envio e2 WHERE e2.campanha_id=? AND e2.contato_id=c.id)"
        par_ini.append(campanha_id)
        camp = db.um("SELECT canal FROM campanha WHERE id=?", campanha_id)
        if camp and camp["canal"] == "whatsapp":
            base += (" AND c.consentimento_em IS NOT NULL AND c.consentimento_em<>''"
                     " AND c.consentimento_origem IS NOT NULL"
                     " AND c.consentimento_origem<>''")
    if onde:
        base += " AND " + " AND ".join(onde)
    if aleatorio and limite:
        # sortear so importa quando se pega um pedaco; sem limite, ordenar
        # 354 mil linhas por RANDOM() e trabalho jogado fora
        base += " ORDER BY RANDOM()"
    if limite:
        base += f" LIMIT {int(limite)}"
    return base, par_ini + par


def montar_publico(campanha_id, filtros=None, limite=None, aleatorio=False,
                   faixas=False):
    """Enfileira os contatos que passam nos filtros. Devolve quantos entraram.

    Com `faixas`, a lista e cortada em fatias iguais entre os chips conectados,
    na ordem em que os contatos foram importados (contato.id e sequencial), e
    cada linha ja nasce reservada para um chip. Sem `faixas`, ninguem e dono de
    nada e o primeiro chip livre pega a proxima linha -- o comportamento antigo.
    """
    sel, par = _montar_select("c.id, c.telefone", filtros or {}, campanha_id,
                              limite, aleatorio)
    chips = []
    if faixas:
        from . import instancias
        camp = db.um("SELECT provedor FROM campanha WHERE id=?", campanha_id)
        chips = [i["id"] for i in instancias.prontas(camp["provedor"] if camp else "evolution")]
        if not chips:
            raise ValueError("nenhum chip conectado e com cota para receber faixa")

    # A ordem dos parametros muda entre os dois SQLs: com faixas o CTE (que
    # carrega o filtro) vem ANTES do INSERT, entao os '?' do filtro sao lidos
    # primeiro. Trocar isso liga campanha_id ao corte de cooldown e estoura
    # em FOREIGN KEY -- longe do lugar onde o erro de verdade esta.
    if chips:
        # (pos * N) / total espalha o resto por igual: com 100 contatos e 15
        # chips ninguem fica com 1 e ninguem com 7 de diferenca.
        ramos = " ".join(f"WHEN {i} THEN {cid}" for i, cid in enumerate(chips))
        sql = (f"WITH numerado AS (SELECT s.id, s.telefone,"
               f" ROW_NUMBER() OVER (ORDER BY s.id) - 1 AS pos,"
               f" COUNT(*) OVER () AS total FROM ({sel}) s)"
               f" INSERT OR IGNORE INTO envio(campanha_id,contato_id,telefone,"
               f"instancia_id,estado,atualizado_em)"
               f" SELECT ?, id, telefone,"
               f" CASE (pos * {len(chips)}) / total {ramos} ELSE {chips[-1]} END,"
               f" 'fila', ? FROM numerado")
        args = par + [campanha_id, db.agora()]
    else:
        sql = (f"INSERT OR IGNORE INTO envio(campanha_id,contato_id,telefone,estado,"
               f"atualizado_em) SELECT ?, s.id, s.telefone, 'fila', ? FROM ({sel}) s")
        args = [campanha_id, db.agora()] + par

    c = db.conn()
    c.execute("BEGIN")
    try:
        antes = c.total_changes
        c.execute(sql, args)
        n = c.total_changes - antes
        c.execute("COMMIT")
    except BaseException:
        # sem o rollback a transacao fica aberta nesta conexao e o motor trava
        # em "database is locked" ate a thread do servidor morrer
        c.execute("ROLLBACK")
        raise
    db.log("publico_montado",
           {"filtros": filtros, "enfileirados": n, "faixas": bool(chips),
            "chips": chips}, campanha_id=campanha_id)
    return n


def mapa_faixas(campanha_id):
    """Como a fila ficou repartida: um item por chip, com o intervalo."""
    linhas = db.todos(
        "SELECT e.instancia_id, i.nome, i.rotulo, COUNT(*) n,"
        " MIN(e.contato_id) primeiro, MAX(e.contato_id) ultimo,"
        " SUM(CASE WHEN e.estado='fila' THEN 1 ELSE 0 END) restam"
        " FROM envio e LEFT JOIN instancia i ON i.id=e.instancia_id"
        " WHERE e.campanha_id=? GROUP BY e.instancia_id ORDER BY primeiro",
        campanha_id)
    return [{"instancia_id": r["instancia_id"],
             "chip": r["rotulo"] or r["nome"] or "sem reserva",
             "contatos": r["n"], "restam": r["restam"],
             "primeiro": r["primeiro"], "ultimo": r["ultimo"]} for r in linhas]


# Um envio vira 'indeterminado' quando o processo cai no meio da chamada ao
# provedor: nao da para saber se a mensagem saiu. Reenviar as cegas duplicaria;
# deixar parado perde o lead. Quem desempata e o operador, olhando o recibo no
# provedor -- ou o proprio provedor, se o webhook de entrega chegar depois.
DECISOES = {
    "saiu": ("enviado", "operador confirmou o envio no provedor"),
    "nao_saiu": ("fila", "operador confirmou que nao saiu; volta para a fila"),
    "descartar": ("cancelado", "operador descartou"),
}


def resolver_indeterminado(decisao, envio_id=None, campanha_id=None):
    """Tira envios do limbo. Devolve quantos foram resolvidos."""
    if decisao not in DECISOES:
        raise ValueError(f"decisao invalida: {decisao}")
    novo, motivo = DECISOES[decisao]
    onde, par = "estado='indeterminado'", []
    if envio_id is not None:
        onde += " AND id=?"
        par.append(int(envio_id))
    elif campanha_id is not None:
        onde += " AND campanha_id=?"
        par.append(int(campanha_id))
    else:
        raise ValueError("informe envio_id ou campanha_id")

    extra = ""
    if novo == "enviado":
        # sem recibo local, a data do envio e a da tentativa
        extra = ", enviado_em=COALESCE(enviado_em,tentativa_em,atualizado_em,?)"
        par.insert(0, db.agora())
    elif novo == "fila":
        # volta a ser disputavel: limpa a marca da tentativa que ficou no ar
        extra = ", id_externo=NULL, tentativa_em=NULL, chave_idempotencia=NULL"
    cur = db.exec_(f"UPDATE envio SET estado=?, erro=?, atualizado_em=?{extra} WHERE {onde}",
                   novo, motivo, db.agora(), *par)
    if cur.rowcount:
        db.log("indeterminado_resolvido",
               {"decisao": decisao, "novo": novo, "envios": cur.rowcount},
               campanha_id=campanha_id)
    return cur.rowcount


def indeterminados(campanha_id=None):
    """Lista o que esta no limbo, para o operador decidir."""
    onde = " WHERE e.estado='indeterminado'"
    par = []
    if campanha_id is not None:
        onde += " AND e.campanha_id=?"
        par.append(int(campanha_id))
    return [dict(r) for r in db.todos(
        "SELECT e.id, e.campanha_id, e.telefone, e.erro, e.tentativa_em,"
        " e.conta_externa, c.nome campanha FROM envio e"
        " JOIN campanha c ON c.id=e.campanha_id" + onde + " ORDER BY e.id", *par)]


def sem_consentimento_na_fila(campanha_id):
    r = db.um(
        "SELECT COUNT(*) n FROM envio e JOIN contato c ON c.id=e.contato_id "
        "WHERE e.campanha_id=? AND e.estado IN ('fila','enviando') AND "
        "(c.consentimento_em IS NULL OR c.consentimento_em='' OR "
        " c.consentimento_origem IS NULL OR c.consentimento_origem='')",
        campanha_id)
    return r["n"] if r else 0


def rodape(canal):
    o = config.carregar()["optout"]
    return o["rodape_sms"] if canal == "sms" else o["rodape_whatsapp"]


def sem_acento(campanha):
    """SMS sem acento cabe em 160 caracteres por segmento; padrao ligado."""
    return bool(db.meta(campanha).get("sem_acento", campanha["canal"] == "sms"))


def previa(campanha, quantos=3, rnd=None):
    """Amostra do texto real que vai sair, com custo de SMS calculado."""
    rnd = rnd or random.Random()
    linhas = db.todos(
        "SELECT c.* FROM envio e JOIN contato c ON c.id=e.contato_id"
        " WHERE e.campanha_id=? AND e.estado='fila' LIMIT ?", campanha["id"], quantos)
    if not linhas:
        linhas = db.todos("SELECT * FROM contato ORDER BY id LIMIT ?", quantos)
    saida = []
    for ct in linhas:
        txt = modelos.montar(campanha["modelo"], ct, rodape(campanha["canal"]),
                             rnd, sem_acento(campanha))
        seg, alfa, tam = modelos.segmentos_sms(txt)
        saida.append({"telefone": ct["telefone"], "nome": ct["nome"], "texto": txt,
                      "segmentos": seg, "alfabeto": alfa, "caracteres": tam})
    return saida


def resumo(campanha_id):
    r = {x["estado"]: x["n"] for x in db.todos(
        "SELECT estado, COUNT(*) n FROM envio WHERE campanha_id=? GROUP BY estado",
        campanha_id)}
    r["total"] = sum(r.values())
    return r
