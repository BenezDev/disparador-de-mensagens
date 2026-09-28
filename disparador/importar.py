#!/usr/bin/env python3
"""
Importa o CSV de leads para a tabela `contato`.

Le tanto o CSV bruto do pipeline (leads_ddd17.csv) quanto o exportado pelo
botao [EXPORTAR CSV] do HTML -- os cabecalhos sao diferentes e o mapeamento
por apelido resolve os dois. Streaming: nunca carrega o arquivo na memoria.

O telefone e normalizado para E.164 e vira chave unica. Um mesmo numero
cadastrado em 30 CNPJs (contador, escritorio) entra UMA vez -- quem recebe a
mensagem e a pessoa do outro lado, nao o CNPJ.
"""
import csv, json, os, sys
from collections import Counter

from . import db, fone

# cabecalho do csv -> coluna do banco
APELIDOS = {
    "telefone": ("telefone", "fone", "celular", "whatsapp", "numero", "tel"),
    "tipo": ("tipo", "tipo_fone"),
    "nome": ("nome", "fantasia", "razao_social", "empresa"),
    "cnpj": ("cnpj", "documento"),
    "cidade": ("cidade", "municipio"),
    "bairro": ("bairro",),
    "uf": ("uf", "estado"),
    "segmento": ("segmento", "cnae_desc", "atividade", "ramo"),
    "email": ("email", "e_mail", "e-mail"),
    "consentimento_em": ("consentimento_em", "optin_em", "opt_in_em", "aceite_em"),
    "consentimento_origem": ("consentimento_origem", "optin_origem",
                              "opt_in_origem", "origem_consentimento"),
    "status": ("status",),
}
COLUNAS = ("telefone", "tipo", "nome", "cnpj", "cidade", "bairro", "segmento",
           "email", "uf", "consentimento_em", "consentimento_origem", "extra",
           "criado_em")


def caminho_painel(caminho, cfg):
    """Resolve um CSV somente dentro dos diretorios autorizados no config."""
    bruto = os.path.expanduser(str(caminho or "").strip())
    if not bruto:
        raise ValueError("arquivo nao informado")
    alvo = os.path.realpath(bruto if os.path.isabs(bruto)
                            else os.path.join(db.RAIZ, bruto))
    imp = cfg.get("importacao") or {}
    raizes = imp.get("diretorios") or ["importacoes"]
    permitidas = [os.path.realpath(os.path.expanduser(
        r if os.path.isabs(r) else os.path.join(db.RAIZ, r))) for r in raizes]
    if not any(os.path.commonpath([alvo, raiz]) == raiz for raiz in permitidas):
        raise PermissionError("arquivo fora dos diretorios de importacao autorizados")
    if not os.path.isfile(alvo) or os.path.islink(alvo):
        raise ValueError("o caminho precisa apontar para um CSV regular")
    maximo = max(1, min(int(imp.get("max_bytes", 536870912)), 536870912))
    if os.path.getsize(alvo) > maximo:
        raise ValueError(f"arquivo excede o limite de {maximo} bytes")
    return alvo


def _mapear(cabecalho):
    """{coluna_do_banco: indice_no_csv} a partir da primeira linha."""
    limpo = [(c or "").strip().lstrip("﻿").lower() for c in cabecalho]
    mapa = {}
    for destino, apelidos in APELIDOS.items():
        for a in apelidos:
            if a in limpo:
                mapa[destino] = limpo.index(a)
                break
    return mapa, limpo


def importar(caminho, somente_movel=True, limite=None, progresso=None,
             consentimento=None):
    """consentimento = (quando, origem) declarado pelo operador para a lista toda.

    O export do DrHery nao traz coluna de opt-in. Se o operador afirma que
    aquela lista tem consentimento (ex.: formulario do site, cadastro na loja),
    ele informa a origem aqui e ela e gravada em cada contato -- e o registro
    auditavel que o portao de WhatsApp exige. Coluna de opt-in na propria
    planilha, quando existe, tem prioridade sobre o declarado.
    """
    if not os.path.exists(caminho):
        raise FileNotFoundError(caminho)
    c = db.conn()
    stats = Counter()
    lote, extras_idx = [], None

    with open(caminho, encoding="utf-8-sig", newline="") as f:
        amostra = f.readline()
        f.seek(0)
        sep = ";" if amostra.count(";") >= amostra.count(",") else ","
        leitor = csv.reader(f, delimiter=sep)
        cab = next(leitor, None)
        if not cab:
            raise ValueError("csv vazio")
        mapa, limpo = _mapear(cab)
        if "telefone" not in mapa:
            # o operador quase sempre pegou o export de E-MAILS do Dr. Hery,
            # que nao tem telefone. Dizer isso, em vez de despejar a lista de
            # colunas, e a diferenca entre resolver sozinho ou travar.
            tem_email = any(c in ("email", "e_mail", "e-mail") for c in limpo)
            if tem_email:
                raise ValueError(
                    "este arquivo é o export de E-MAILS do Dr. Hery (não tem "
                    "telefone). Para disparar WhatsApp/SMS, exporte na aba de "
                    "LEADS — o arquivo drhery_leads_...csv, que tem a coluna telefone.")
            raise ValueError("o arquivo não tem coluna de telefone. "
                             f"Colunas encontradas: {', '.join(limpo[:8])}")
        extras_idx = [i for i, n in enumerate(limpo)
                      if n in ("situacao", "porte", "mei", "cnae", "abertura",
                               "logradouro", "cep", "regiao_ddd17", "regiao")]

        c.execute("BEGIN")
        n = 0
        try:
            n = _varrer(c, leitor, mapa, limpo, extras_idx, lote, stats,
                        somente_movel, limite, progresso, consentimento)
            nov, atu = _gravar(c, lote)
            stats["novos"] += nov
            stats["atualizados"] += atu
            stats["lidos"] = n
            c.execute("COMMIT")
        except BaseException:
            # csv com linha corrompida no meio nao pode deixar transacao aberta
            c.execute("ROLLBACK")
            raise

    stats["duplicados"] = (stats["lidos"] - stats["novos"] - stats["atualizados"]
        - sum(v for k, v in stats.items() if k.startswith("recusado_"))
        - stats["fixo"] - stats["descartado"])
    db.log("importacao", dict(stats))
    return stats


def _varrer(c, leitor, mapa, limpo, extras_idx, lote, stats,
            somente_movel, limite, progresso, consentimento=None):
    """Le o csv linha a linha e enche `lote`. Devolve quantas linhas leu."""
    n = 0
    for n, linha in enumerate(leitor, 1):
        if limite and stats["novos"] >= limite:
            break
        pega = lambda k: (linha[mapa[k]].strip()
                          if k in mapa and mapa[k] < len(linha) else "")
        e164, tipo = fone.normalizar(pega("telefone"))
        if not e164:
            stats["recusado_" + (tipo or "?")] += 1
            continue
        if somente_movel and tipo != "MOVEL":
            stats["fixo"] += 1
            continue

        # o HTML marca leads como DESCARTADO; isso e um 'nao' explicito
        # do operador e vira bloqueio, nao contato.
        if pega("status").upper().startswith("DESCART"):
            db.exec_("INSERT OR IGNORE INTO bloqueio(telefone,motivo,origem,criado_em)"
                     " VALUES(?,?,?,?)", e164, "manual", "csv:descartado", db.agora())
            stats["descartado"] += 1
            continue

        extra = {limpo[i]: linha[i] for i in extras_idx if i < len(linha) and linha[i]}
        cons_em, cons_origem = pega("consentimento_em"), pega("consentimento_origem")
        if consentimento and not (cons_em and cons_origem):
            cons_em = cons_em or consentimento[0]
            cons_origem = cons_origem or consentimento[1]
        if cons_em and cons_origem:
            stats["com_optin"] += 1
        lote.append((e164, tipo, pega("nome"), pega("cnpj"), pega("cidade"),
                     pega("bairro"), pega("segmento"), pega("email"),
                     pega("uf"), cons_em, cons_origem,
                     json.dumps(extra, ensure_ascii=False) if extra else None,
                     db.agora()))
        if len(lote) >= 5000:
            nov, atu = _gravar(c, lote)
            stats["novos"] += nov
            stats["atualizados"] += atu
            lote.clear()
            if progresso:
                progresso(n, stats)
    return n


def _gravar(c, lote):
    if not lote:
        return 0, 0     # (novos, atualizados) -- contrato de tupla mesmo vazio
    antes = c.total_changes
    # Contato novo entra inteiro. Contato que ja existe NAO e sobrescrito -- mas
    # se ele estava sem opt-in e agora chega com opt-in declarado, preenche so
    # esse campo. Sem isso, reimportar a mesma lista "com opt-in" nao teria
    # efeito (o INSERT OR IGNORE pularia), e o operador ficaria travado sem
    # entender por que a fila continua vazia.
    total0 = c.execute("SELECT COUNT(*) FROM contato").fetchone()[0]
    c.executemany(
        f"INSERT INTO contato({','.join(COLUNAS)}) "
        f"VALUES({','.join('?' * len(COLUNAS))}) "
        f"ON CONFLICT(telefone) DO UPDATE SET "
        f"  consentimento_em=COALESCE(NULLIF(contato.consentimento_em,''), excluded.consentimento_em), "
        f"  consentimento_origem=COALESCE(NULLIF(contato.consentimento_origem,''), excluded.consentimento_origem) "
        f"WHERE excluded.consentimento_origem IS NOT NULL AND excluded.consentimento_origem<>''",
        lote)
    mudancas = c.total_changes - antes
    novos = c.execute("SELECT COUNT(*) FROM contato").fetchone()[0] - total0
    return novos, mudancas - novos     # (novos, atualizados)


def declarar_optin(origem, quando=None, limite=None):
    """Marca como opt-in os contatos que ainda nao tem consentimento.

    E uma declaracao do operador: ele afirma que aquela base tem base legal
    para receber mensagem, e informa a origem. So preenche quem esta sem
    consentimento -- nunca sobrescreve um opt-in ja existente -- e fica
    registrada no log com a origem, quem declarou e quantos foram, para haver
    rastro do que foi afirmado.
    """
    origem = str(origem or "").strip()[:120]
    if not origem:
        raise ValueError("informe a origem do consentimento")
    quando = quando or db.agora()
    onde = ("(consentimento_em IS NULL OR consentimento_em='' "
            "OR consentimento_origem IS NULL OR consentimento_origem='')")
    alvo = db.um(f"SELECT COUNT(*) n FROM contato WHERE {onde}")["n"]
    if limite:
        # so um lote: escolhe os primeiros por id (ordem de importacao)
        cur = db.exec_(
            f"UPDATE contato SET consentimento_em=?, consentimento_origem=? "
            f"WHERE id IN (SELECT id FROM contato WHERE {onde} ORDER BY id LIMIT ?)",
            quando, origem, int(limite))
    else:
        cur = db.exec_(
            f"UPDATE contato SET consentimento_em=?, consentimento_origem=? WHERE {onde}",
            quando, origem)
    db.log("optin_declarado", {"origem": origem, "afetados": cur.rowcount,
                               "sem_optin_antes": alvo, "limite": limite})
    return cur.rowcount


if __name__ == "__main__":
    alvo = sys.argv[1] if len(sys.argv) > 1 else None
    if not alvo:
        sys.exit("uso: python3 -m disparador.importar <arquivo.csv>")
    st = importar(alvo, progresso=lambda n, s: print(f"\r  {n:>9,} linhas...", end=""))
    print("\n", dict(st))
