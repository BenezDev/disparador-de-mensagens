#!/usr/bin/env python3
"""
Montagem do texto de cada mensagem.

Duas sintaxes convivem no mesmo modelo, desambiguadas pelo '|':
  {nome}              -> variavel do contato
  {Ola|Oi|Bom dia}    -> spintax, sorteia uma das opcoes

Spintax nao e enfeite: gateway de SMS e antispam de WhatsApp pontuam volume de
mensagens identicas. Variar a abertura reduz o fingerprint do lote.
"""
import random, re, unicodedata
from datetime import datetime

SPIN = re.compile(r"\{([^{}]*\|[^{}]*)\}")     # so casa se tiver '|' dentro
VAR = re.compile(r"\{([a-z_]+)\}")

# sufixos societarios que so poluem a saudacao
SUFIXOS = re.compile(
    r"\s+(LTDA|ME|EPP|EIRELI|S\.?A\.?|SA|MEI|SOCIEDADE\s+INDIVIDUAL|"
    r"EMPRESA\s+INDIVIDUAL|COMERCIO|SERVICOS)\b\.?", re.I)
MINUSCULAS = {"de", "da", "do", "das", "dos", "e", "em", "para", "com", "a", "o",
              "no", "na", "nos", "nas", "por", "sem"}


# A extracao marca com este texto o cadastro sem nome aproveitavel, e a propria
# Receita aceita '*', '-' e numero solto no campo. Tratar como nome de verdade
# produzia "Oi, Nao Detectado!" -- que denuncia o disparo automatico na hora.
SEM_NOME = re.compile(r"^(nao detectado|n[aã]o detectado|sem nome|[\W\d_]+)$", re.I)


def titulo(nome):
    """'ABN COMERCIAL HOSPITALAR LTDA' -> 'Abn Comercial Hospitalar'.

    A base da Receita e toda em caixa alta; mandar assim grita 'mala direta'.
    Cadastro sem nome util devolve vazio, e quem chama decide o que colocar.
    """
    bruto = (nome or "").strip()
    if SEM_NOME.match(unicodedata.normalize("NFKD", bruto)
                      .encode("ascii", "ignore").decode() or "x"):
        return ""
    n = SUFIXOS.sub("", bruto)
    if not n:
        return ""
    palavras = []
    for i, p in enumerate(n.split()):
        b = p.lower()
        palavras.append(b if (i and b in MINUSCULAS) else b.capitalize())
    return " ".join(palavras)


# A descricao de CNAE da Receita e quilometrica ("Fabricacao de artefatos de
# material plastico para uso na construcao, exceto tubos e acessorios"). Colada
# num SMS ela sozinha estoura 2 segmentos. {segmento_curto} corta na primeira
# virgula/ressalva e limita o tamanho.
CORTE_CNAE = re.compile(r"\s*(,|;|\bexceto\b|\bnao especificad|\be outros\b|\binclusive\b).*$", re.I)


def encurtar(txt, limite=34):
    t = CORTE_CNAE.sub("", (txt or "").strip())
    if len(t) > limite:
        t = t[:limite].rsplit(" ", 1)[0]
    # nao terminar em preposicao: "...artigos do" fica torto na frase
    palavras = t.rstrip(" ,.").split()
    while palavras and palavras[-1].lower() in MINUSCULAS:
        palavras.pop()
    return " ".join(palavras)


def saudacao(quando=None):
    h = (quando or datetime.now()).hour
    return "Bom dia" if h < 12 else ("Boa tarde" if h < 18 else "Boa noite")


def girar(texto, rnd=random):
    """Resolve o spintax de dentro para fora (suporta aninhamento)."""
    for _ in range(12):
        novo = SPIN.sub(lambda m: rnd.choice(m.group(1).split("|")).strip(), texto)
        if novo == texto:
            return novo
        texto = novo
    return texto


def variaveis(contato):
    """dict de substituicao a partir de uma linha da tabela contato.

    Aceita sqlite3.Row ou dict simples: o envio de teste roda antes de existir
    qualquer contato importado.
    """
    campos = set(contato.keys())
    pega = lambda k: (contato[k] or "") if k in campos else ""
    nome = titulo(pega("nome"))
    return {
        "nome": nome or "tudo bem",
        "primeiro_nome": nome.split()[0] if nome else "",
        "empresa": nome,
        "cidade": titulo(pega("cidade")),
        "bairro": titulo(pega("bairro")),
        "segmento": pega("segmento"),
        "segmento_curto": encurtar(pega("segmento")),
        "cnpj": pega("cnpj"),
        "email": pega("email"),
        "saudacao": saudacao(),
    }


def montar(modelo, contato, rodape="", rnd=random, ascii_=False):
    """Modelo + contato -> texto final pronto para o provedor.

    `ascii_` tira os acentos: em SMS isso mantem a mensagem em GSM-7 (160
    caracteres por segmento em vez de 70) e costuma cortar o custo em 2-3x.
    """
    txt = girar(modelo, rnd)
    v = variaveis(contato)
    txt = VAR.sub(lambda m: str(v.get(m.group(1), m.group(0))), txt)
    txt = re.sub(r"[ \t]{2,}", " ", txt).strip()
    if rodape and _achatar_cmp(rodape) not in _achatar_cmp(txt):
        txt = f"{txt}\n{rodape.strip()}"
    return sem_acento(txt) if ascii_ else txt


def _achatar_cmp(t):
    return re.sub(r"\s+", " ", sem_acento(t or "").lower()).strip()


def sem_acento(txt):
    """Transliteracao para ASCII: mantem o SMS em 1 segmento (GSM-7)."""
    return unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()


# --- custo de SMS ------------------------------------------------------------
# Alfabeto GSM 03.38 basico. Qualquer caractere fora dele joga a mensagem
# inteira para UCS-2, onde o segmento cai de 160 para 70 caracteres.
GSM = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM_EXT = set("^{}\\[~]|€")     # ocupam 2 posicoes


def segmentos_sms(txt):
    """(qtd_segmentos, alfabeto, tamanho_efetivo) -- cada segmento e cobrado."""
    if all(c in GSM or c in GSM_EXT for c in txt):
        n = sum(2 if c in GSM_EXT else 1 for c in txt)
        limite, unico, alfabeto = 153, 160, "GSM-7"
    else:
        n, limite, unico, alfabeto = len(txt), 67, 70, "UCS-2"
    qtd = 1 if n <= unico else -(-n // limite)
    return qtd, alfabeto, n
