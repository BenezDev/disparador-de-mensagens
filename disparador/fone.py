#!/usr/bin/env python3
"""
Normalizacao de telefone brasileiro para E.164 (sem o '+').

A base da Receita traz o telefone ja como DDD+numero, mas o CSV exportado
pelo HTML de leads vem com o "55" na frente. Aqui tudo converge para a mesma
forma canonica: 55 + DDD(2) + assinante(8 ou 9). E essa forma que vira chave
unica no banco -- sem isso o mesmo lead entra duas vezes e leva duas mensagens.
"""
import re

SO_DIGITO = re.compile(r"\D+")

# DDDs validos (Anatel). Serve de peneira: a base publica tem lixo tipo "00".
DDDS = {
    11, 12, 13, 14, 15, 16, 17, 18, 19,
    21, 22, 24, 27, 28,
    31, 32, 33, 34, 35, 37, 38,
    41, 42, 43, 44, 45, 46, 47, 48, 49,
    51, 53, 54, 55,
    61, 62, 63, 64, 65, 66, 67, 68, 69,
    71, 73, 74, 75, 77, 79,
    81, 82, 83, 84, 85, 86, 87, 88, 89,
    91, 92, 93, 94, 95, 96, 97, 98, 99,
}

REPETIDO = re.compile(r"^(\d)\1+$")        # 999999999, 000000000
SEQUENCIA = {"12345678", "123456789", "987654321", "23456789", "12345679"}


def normalizar(bruto):
    """Devolve (e164, tipo) ou (None, motivo_da_recusa).

    tipo e "MOVEL" ou "FIXO"; so MOVEL recebe SMS/WhatsApp.
    """
    d = SO_DIGITO.sub("", str(bruto or ""))
    if not d:
        return None, "vazio"

    # 0800/0300 e afins nao recebem mensagem
    if d.startswith("0800") or d.startswith("0300") or d.startswith("4004"):
        return None, "nao_geografico"

    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    elif d.startswith("0") and len(d) in (11, 12):   # 0XX gravado com o zero
        d = d[1:]

    if len(d) not in (10, 11):
        return None, "tamanho"

    ddd, ass = int(d[:2]), d[2:]
    if ddd not in DDDS:
        return None, "ddd"
    if REPETIDO.match(ass) or ass in SEQUENCIA:
        return None, "placeholder"

    if len(ass) == 9:
        if ass[0] != "9":
            return None, "movel_sem_9"
        tipo = "MOVEL"
    else:                        # 8 digitos
        if ass[0] in "6789":
            # movel antigo: a Anatel ja migrou tudo para 9 digitos, entao
            # promovemos inserindo o 9. Sem isso o numero nao existe mais.
            ass, tipo = "9" + ass, "MOVEL"
        elif ass[0] in "2345":
            tipo = "FIXO"
        else:
            return None, "prefixo"

    return f"55{ddd:02d}{ass}", tipo


def bonito(e164):
    """5517991234567 -> (17) 99123-4567"""
    d = SO_DIGITO.sub("", e164 or "")
    if len(d) == 13 and d.startswith("55"):
        return f"({d[2:4]}) {d[4:9]}-{d[9:]}"
    if len(d) == 12 and d.startswith("55"):
        return f"({d[2:4]}) {d[4:8]}-{d[8:]}"
    return e164


def variantes(e164):
    """Formas em que o MESMO celular aparece no mundo real.

    Gateways de WhatsApp devolvem numero brasileiro sem o 9o digito (heranca de
    antes da migracao da Anatel): mandamos para 5517991234567 e o webhook
    responde 551791234567. Sao o mesmo aparelho -- se nao reconhecermos isso, o
    'SAIR' do lead bloqueia um numero fantasma e nao cancela a fila dele.
    """
    d = SO_DIGITO.sub("", e164 or "")
    if len(d) not in (12, 13) or not d.startswith("55"):
        return [d] if d else []
    ddd, ass = d[2:4], d[4:]
    if len(ass) == 9 and ass[0] == "9":
        return [d, f"55{ddd}{ass[1:]}"]        # com e sem o 9
    if len(ass) == 8 and ass[0] in "6789":
        return [d, f"55{ddd}9{ass}"]
    return [d]
