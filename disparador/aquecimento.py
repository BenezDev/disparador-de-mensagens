#!/usr/bin/env python3
"""Compatibilidade para bancos antigos; trafego artificial foi removido."""
from . import db

POR_DIA = []


def rodar(cfg, rnd=None):
    """Nunca envia mensagens. A rampa legitima vive em instancias.py."""
    return False


def total_hoje():
    """Historico legado, sem criar novas conversas."""
    return 0


def resumo():
    return [{"id": i["id"], "nome": i["nome"], "rotulo": i["rotulo"],
             "conversar": False, "hoje": 0, "meta_hoje": 0}
            for i in db.todos("SELECT * FROM instancia WHERE ativa=1 ORDER BY id")]
