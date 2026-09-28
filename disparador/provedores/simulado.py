"""Provedor de ensaio: nao manda nada, so simula latencia e falha.

E o padrao de fabrica. Toda campanha nova nasce apontando para ca, para o
primeiro erro do operador nunca custar dinheiro nem reputacao de numero.
"""
import random, time, uuid

ROTULO = "Simulado (nao envia nada)"
CANAL = ("sms", "whatsapp")
AQUECE = False
SIMULA = True
IMAGEM = True          # nada sai daqui; o painel precisa dizer isso na tela
CAMPOS = {"falha_pct": "% de falhas simuladas"}
OBRIGATORIOS = ()
AJUDA = "Nao envia nada. Serve para conferir texto, filtros e ritmo sem risco."
DOC = ""


def enviar(destino, texto, cfg, meta=None):
    from . import OK, ERRO
    time.sleep(random.uniform(0.05, 0.25))
    if random.random() * 100 < float(cfg.get("falha_pct", 3)):
        return ERRO("falha simulada de gateway")
    return OK("sim_" + uuid.uuid4().hex[:16])


def diagnosticar(cfg):
    from . import diag
    return diag(ok=True, mensagem="modo de ensaio — nenhuma mensagem sai daqui",
                acao=AJUDA)


def pronto(cfg):
    return True, "modo de ensaio"
