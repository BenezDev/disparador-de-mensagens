#!/usr/bin/env python3
"""
Banco do disparador (SQLite).

Invariante central: uma mensagem sai NO MAXIMO uma vez por tentativa idempotente.
Uma queda depois da chamada remota e antes do ACK local e ambigua; ela vai para
`indeterminado` e nunca volta automaticamente para a fila.
"""
import json, os, sqlite3, threading
from datetime import datetime, timedelta, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAMINHO = os.path.join(RAIZ, "dados", "disparador.db")

# Teto de chips. E invariante do banco (trigger abaixo) e da API (instancias.py):
# os dois leem daqui para nunca discordarem.
MAX_INSTANCIAS = 25

_local = threading.local()

ESQUEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS contato (
  id        INTEGER PRIMARY KEY,
  telefone  TEXT NOT NULL UNIQUE,       -- E.164 sem '+': 5517991234567
  tipo      TEXT NOT NULL,              -- MOVEL | FIXO
  nome      TEXT, cnpj TEXT, cidade TEXT, bairro TEXT,
  segmento  TEXT, email TEXT, uf TEXT,
  consentimento_em     TEXT,
  consentimento_origem TEXT,
  extra     TEXT,                       -- json com o resto das colunas do CSV
  criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campanha (
  id          INTEGER PRIMARY KEY,
  nome        TEXT NOT NULL,
  canal       TEXT NOT NULL,            -- sms | whatsapp
  provedor    TEXT NOT NULL,
  modelo      TEXT NOT NULL,            -- corpo com {nome} e spintax {a|b}
  meta        TEXT,                     -- json: template/idioma/params do provedor
  estado      TEXT NOT NULL DEFAULT 'rascunho',  -- rascunho|rodando|pausada|concluida
  por_minuto  REAL    NOT NULL DEFAULT 10,
  limite_dia  INTEGER NOT NULL DEFAULT 300,
  criado_em   TEXT NOT NULL,
  iniciado_em TEXT
);

-- Um chip. Quem toma ban e o chip, nao a campanha -- por isso cota, ritmo e
-- rampa de aquecimento moram AQUI, e nao na campanha. Varias campanhas podem
-- usar o mesmo chip, e um chip que estourou a cota para de enviar por todas.
CREATE TABLE IF NOT EXISTS instancia (
  id          INTEGER PRIMARY KEY,
  nome        TEXT NOT NULL UNIQUE,     -- nome da instancia no gateway
  rotulo      TEXT,                     -- apelido humano ("chip loja", "chip 2")
  provedor    TEXT NOT NULL DEFAULT 'evolution',
  ativa       INTEGER NOT NULL DEFAULT 1,
  por_minuto  REAL    NOT NULL DEFAULT 6,
  limite_dia  INTEGER NOT NULL DEFAULT 900,
  aquecer     INTEGER NOT NULL DEFAULT 1,   -- usa a rampa de dias
  conversar   INTEGER NOT NULL DEFAULT 0,   -- legado; conversas artificiais desativadas
  iniciado_em TEXT,                     -- 1o dia conectado: base da rampa
  estado      TEXT,                     -- espelho do connectionState
  numero      TEXT,                     -- proprio numero (vem do ownerJid)
  visto_em    TEXT,
  criado_em   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS envio (
  id            INTEGER PRIMARY KEY,
  campanha_id   INTEGER NOT NULL REFERENCES campanha(id) ON DELETE CASCADE,
  contato_id    INTEGER NOT NULL REFERENCES contato(id),
  instancia_id  INTEGER REFERENCES instancia(id),   -- qual chip mandou
  telefone      TEXT NOT NULL,
  texto         TEXT,
  estado        TEXT NOT NULL DEFAULT 'fila',
  -- fila|enviando|enviado|entregue|lido|respondido|falha|bloqueado|cancelado
  tentativas    INTEGER NOT NULL DEFAULT 0,
  id_externo    TEXT,
  erro          TEXT,
  agendado_para TEXT,
  enviado_em    TEXT,
  tentativa_em  TEXT,
  chave_idempotencia TEXT,
  provedor_externo TEXT,
  conta_externa TEXT,
  atualizado_em TEXT,
  UNIQUE(campanha_id, contato_id)
);
-- o painel conta os enviados de hoje a cada 3s; sem este indice era SCAN

-- Historico legado de uma versao que fabricava conversa entre chips.
-- A tabela permanece apenas para abrir bancos antigos; novas linhas sao proibidas
-- pela aplicacao e a rampa atual vive em instancia.iniciado_em.
CREATE TABLE IF NOT EXISTS aquecimento (
  id          INTEGER PRIMARY KEY,
  de_id       INTEGER NOT NULL REFERENCES instancia(id) ON DELETE CASCADE,
  para_id     INTEGER NOT NULL REFERENCES instancia(id) ON DELETE CASCADE,
  texto       TEXT,
  estado      TEXT NOT NULL DEFAULT 'ok',
  erro        TEXT,
  criado_em   TEXT NOT NULL
);

-- Conversa de verdade, por chip. O gateway nao guarda nada (DATABASE_SAVE_*
-- estao desligados de proposito), entao esta tabela e a unica memoria que
-- existe do que foi dito. Entrada vem do webhook; saida vem do motor (campanha)
-- e da resposta manual do operador.
CREATE TABLE IF NOT EXISTS mensagem (
  id           INTEGER PRIMARY KEY,
  chip         TEXT NOT NULL,             -- nome da instancia no gateway
  instancia_id INTEGER REFERENCES instancia(id) ON DELETE SET NULL,
  telefone     TEXT NOT NULL,             -- do cliente, E.164 sem '+'
  direcao      TEXT NOT NULL,             -- entrada | saida
  texto        TEXT,
  tipo         TEXT NOT NULL DEFAULT 'texto',  -- texto|imagem|audio|video|documento|outro
  id_externo   TEXT,                      -- id da mensagem no WhatsApp
  envio_id     INTEGER REFERENCES envio(id) ON DELETE SET NULL,
  origem       TEXT NOT NULL DEFAULT 'webhook',  -- webhook|campanha|manual
  criado_em    TEXT NOT NULL,
  lida_em      TEXT                       -- quando o operador abriu a conversa
);

CREATE TABLE IF NOT EXISTS bloqueio (
  telefone  TEXT PRIMARY KEY,
  motivo    TEXT NOT NULL,              -- optout|manual|invalido|denuncia|erro_fatal
  origem    TEXT,
  criado_em TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evento (
  id          INTEGER PRIMARY KEY,
  ts          TEXT NOT NULL,
  tipo        TEXT NOT NULL,
  telefone    TEXT,
  campanha_id INTEGER,
  detalhe     TEXT
);

CREATE TABLE IF NOT EXISTS webhook_recebido (
  provedor TEXT NOT NULL,
  evento_id TEXT NOT NULL,
  recebido_em TEXT NOT NULL,
  PRIMARY KEY(provedor, evento_id)
);

CREATE TABLE IF NOT EXISTS acao_idempotente (
  chave TEXT NOT NULL,
  tipo TEXT NOT NULL,
  criado_em TEXT NOT NULL,
  PRIMARY KEY(tipo, chave)
);
"""

# Teto de chips como invariante do banco. Fica fora do ESQUEMA porque precisa
# ser TROCADO quando o teto muda (CREATE IF NOT EXISTS manteria o antigo), e a
# troca tem de ser serializada: cada thread abre a propria conexao e roda o
# esquema, e um DROP+CREATE solto corria entre elas ("trigger already exists").
TRIGGER_MAXIMO = f"""
CREATE TRIGGER trg_instancia_maximo
BEFORE INSERT ON instancia
WHEN (SELECT COUNT(*) FROM instancia) >= {MAX_INSTANCIAS}
BEGIN
  SELECT RAISE(ABORT, 'limite de {MAX_INSTANCIAS} chips atingido');
END;
"""


def agora():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# Colunas acrescentadas depois que o banco ja existia. CREATE TABLE IF NOT
# EXISTS nao mexe em tabela existente, entao coluna nova entra por aqui.
MIGRACOES = [
    ("envio", "instancia_id", "INTEGER REFERENCES instancia(id)"),
    # ultima vez que o chip foi visto CONECTADO, e por que caiu. Sao os dois
    # dados que separam "oscilou a rede" de "foi banido" na hora de decidir se
    # a faixa dele vai para outro chip.
    ("instancia", "visto_ok_em", "TEXT"),
    ("instancia", "motivo_queda", "TEXT"),
    # ultima vez que o vigia mandou o gateway reconectar este chip. Sem o
    # freio, cada sincronizacao (a cada 20s) bateria de novo no gateway.
    ("instancia", "reconexao_em", "TEXT"),
    ("envio", "tentativa_em", "TEXT"),
    ("envio", "chave_idempotencia", "TEXT"),
    ("envio", "provedor_externo", "TEXT"),
    ("envio", "conta_externa", "TEXT"),
    ("contato", "consentimento_em", "TEXT"),
    ("contato", "consentimento_origem", "TEXT"),
]

INDICES = """
""" + "\n".join(['CREATE INDEX IF NOT EXISTS ix_contato_cidade  ON contato(cidade);', 'CREATE INDEX IF NOT EXISTS ix_contato_tipo    ON contato(tipo);', 'CREATE INDEX IF NOT EXISTS ix_envio_fila  ON envio(campanha_id, estado, agendado_para);', 'CREATE INDEX IF NOT EXISTS ix_envio_fone  ON envio(telefone, estado);', 'CREATE INDEX IF NOT EXISTS ix_envio_ext_escopo ON envio(provedor_externo,conta_externa,id_externo);', "CREATE UNIQUE INDEX IF NOT EXISTS ux_envio_ext_escopo ON envio(provedor_externo,COALESCE(conta_externa,''),id_externo) WHERE id_externo IS NOT NULL AND provedor_externo IS NOT NULL;", 'CREATE INDEX IF NOT EXISTS ix_envio_dia   ON envio(enviado_em) WHERE enviado_em IS NOT NULL;', 'CREATE INDEX IF NOT EXISTS ix_envio_inst  ON envio(instancia_id, enviado_em);', 'CREATE INDEX IF NOT EXISTS ix_aquec_dia ON aquecimento(criado_em);', 'CREATE INDEX IF NOT EXISTS ix_evento_ts ON evento(id DESC);', 'CREATE INDEX IF NOT EXISTS ix_msg_conversa ON mensagem(chip, telefone, id);', 'CREATE INDEX IF NOT EXISTS ix_msg_chip ON mensagem(chip, id DESC);', "CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_externo ON mensagem(chip, id_externo) WHERE id_externo IS NOT NULL;"]) + """
"""


def migrar(c):
    """Colunas novas e o trigger do teto, numa transacao so.

    Cada thread abre a propria conexao e passa por aqui. Sem o BEGIN IMMEDIATE
    duas delas viam a coluna faltando ao mesmo tempo e a segunda estourava
    "duplicate column name"; o mesmo valia para DROP+CREATE do trigger.
    """
    c.execute("BEGIN IMMEDIATE")
    try:
        for tabela, coluna, tipo in MIGRACOES:
            existentes = {r[1] for r in c.execute(f"PRAGMA table_info({tabela})")}
            if not existentes:
                continue                   # tabela ainda nao existe: o script cria
            if coluna not in existentes:
                c.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {tipo}")
        # Conversas fabricadas entre chips foram removidas; a coluna fica apenas
        # para abrir bancos antigos sem uma migracao destrutiva.
        c.execute("UPDATE instancia SET conversar=0 WHERE conversar<>0")
        # trigger do teto: cria, ou troca o de um banco criado com teto antigo
        atual = c.execute("SELECT sql FROM sqlite_master WHERE type='trigger'"
                          " AND name='trg_instancia_maximo'").fetchone()
        if atual is None or f">= {MAX_INSTANCIAS}" not in atual[0]:
            c.execute("DROP TRIGGER IF EXISTS trg_instancia_maximo")
            c.execute(TRIGGER_MAXIMO)
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise


def desde(iso):
    """Timestamp gravado por agora() -> datetime com fuso.

    Tolera valor ingenuo (banco antigo, ou datetime() do SQLite): sem isso a
    subtracao 'aware - naive' estoura TypeError dentro do calculo de cota e a
    campanha inteira trava sem explicacao.
    """
    try:
        d = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.astimezone()


def meta(linha, campo="meta"):
    """Coluna json -> dict, tolerando nulo, vazio ou conteudo corrompido."""
    try:
        v = json.loads(linha[campo] or "{}")
    except (TypeError, ValueError, IndexError, KeyError):
        return {}
    return v if isinstance(v, dict) else {}


def conn():
    """Conexao por thread. O motor roda em thread separada do servidor HTTP."""
    c = getattr(_local, "c", None)
    if c is None:
        pasta = os.path.dirname(CAMINHO)
        os.makedirs(pasta, mode=0o700, exist_ok=True)
        os.chmod(pasta, 0o700)
        c = sqlite3.connect(CAMINHO, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.executescript(ESQUEMA)
        migrar(c)
        c.executescript(INDICES)
        os.chmod(CAMINHO, 0o600)
        for sufixo in ("-wal", "-shm"):
            if os.path.exists(CAMINHO + sufixo):
                os.chmod(CAMINHO + sufixo, 0o600)
        _local.c = c
    return c


def log(tipo, detalhe=None, telefone=None, campanha_id=None):
    conn().execute(
        "INSERT INTO evento(ts,tipo,telefone,campanha_id,detalhe) VALUES(?,?,?,?,?)",
        (agora(), tipo, telefone, campanha_id,
         detalhe if isinstance(detalhe, str) else json.dumps(detalhe, ensure_ascii=False)),
    )


def um(sql, *args):
    r = conn().execute(sql, args).fetchone()
    return r


def todos(sql, *args):
    return conn().execute(sql, args).fetchall()


def exec_(sql, *args):
    return conn().execute(sql, args)


# Uma mensagem nunca demora isso para ser aceita pelo provedor (o timeout de
# HTTP e 30s). Passou disso em 'enviando', o processo que a segurava morreu.
IDADE_ORFAO = timedelta(minutes=5)


def recuperar_orfaos():
    """Queda durante chamada remota vira estado indeterminado, nunca reenvio.

    `id_externo IS NULL` nao prova que o provedor recusou: o processo pode ter
    morrido depois do aceite remoto e antes do UPDATE local.
    """
    corte = (datetime.now(timezone.utc).astimezone()
             - IDADE_ORFAO).isoformat(timespec="seconds")
    cur = exec_(
        "UPDATE envio SET estado='indeterminado', erro=?, atualizado_em=? "
        "WHERE estado='enviando' AND id_externo IS NULL AND atualizado_em<?",
        "processo caiu durante confirmacao do provedor; revisao manual necessaria",
        agora(), corte)
    if cur.rowcount:
        exec_("UPDATE campanha SET estado='pausada' WHERE estado='rodando' AND id IN ("
              "SELECT campanha_id FROM envio WHERE estado='indeterminado')")
        log("recuperacao_indeterminada", f"{cur.rowcount} envios isolados")
    # os que tem id_externo sairam mesmo: marca como enviado
    exec_("UPDATE envio SET estado='enviado', "
          "enviado_em=COALESCE(enviado_em,tentativa_em,atualizado_em,?), atualizado_em=? "
          "WHERE estado='enviando' AND id_externo IS NOT NULL AND atualizado_em<?",
          agora(), agora(), corte)
    return cur.rowcount
