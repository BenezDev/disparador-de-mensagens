#!/usr/bin/env python3
"""
Imagens dos disparos: guardadas em disco, referenciadas pela campanha.

A campanha guarda so o ID da imagem (`meta.imagem`); os bytes ficam em
dados/midia/<sha256>.<ext>. O ID e o proprio hash do conteudo, entao a mesma
foto enviada em dez campanhas ocupa espaco uma vez so, e nao ha como um ID
apontar para fora da pasta (path traversal): o formato e fixo e validado.

So imagem, e so os tres formatos que o WhatsApp aceita sem reclamar. O tipo e
decidido pelos bytes (magic number), nao pelo que o navegador declarou -- um
.exe renomeado para .jpg nao passa daqui.
"""
import hashlib
import os
import re
import base64 as _b64

from . import config

PASTA = os.path.join(config.RAIZ, "dados", "midia")
LIMITE_BYTES = 5 * 1024 * 1024        # 5 MiB: acima disso o WhatsApp recomprime feio
ID_VALIDO = re.compile(r"^[0-9a-f]{64}\.(jpg|png|webp)$")

# assinatura nos primeiros bytes -> (mime, extensao)
ASSINATURAS = [
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
]


def _sniff(dados):
    """(mime, ext) pelo conteudo real, ou (None, None) se nao for imagem aceita."""
    for assinatura, mime, ext in ASSINATURAS:
        if dados.startswith(assinatura):
            return mime, ext
    # webp = "RIFF....WEBP"
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None, None


def salvar(data_uri):
    """data URI (data:image/...;base64,....) -> (id, mime, erro).

    Aceita tambem base64 puro sem o prefixo. Nunca confia no mime declarado.
    """
    if not isinstance(data_uri, str) or not data_uri.strip():
        return None, None, "imagem vazia"
    corpo = data_uri.split(",", 1)[1] if data_uri.startswith("data:") else data_uri
    try:
        dados = _b64.b64decode(corpo, validate=True)
    except Exception:
        return None, None, "imagem nao e base64 valido"
    if len(dados) > LIMITE_BYTES:
        return None, None, f"imagem maior que {LIMITE_BYTES // (1024 * 1024)} MiB"
    if len(dados) < 64:
        return None, None, "imagem pequena demais para ser real"
    mime, ext = _sniff(dados)
    if not mime:
        return None, None, "so aceito JPG, PNG ou WEBP (o arquivo nao parece imagem)"

    os.makedirs(PASTA, exist_ok=True)
    ident = f"{hashlib.sha256(dados).hexdigest()}.{ext}"
    destino = os.path.join(PASTA, ident)
    if not os.path.exists(destino):
        tmp = destino + ".parcial"
        with open(tmp, "wb") as f:
            f.write(dados)
        os.replace(tmp, destino)          # so aparece inteiro
    return ident, mime, ""


def caminho(ident):
    if not isinstance(ident, str) or not ID_VALIDO.match(ident):
        return None
    p = os.path.join(PASTA, ident)
    # defesa em profundidade: garante que continua dentro da pasta
    if os.path.commonpath([os.path.realpath(p), os.path.realpath(PASTA)]) != os.path.realpath(PASTA):
        return None
    return p if os.path.exists(p) else None


def ler(ident):
    """(bytes, mime) ou (None, None)."""
    p = caminho(ident)
    if not p:
        return None, None
    with open(p, "rb") as f:
        dados = f.read(LIMITE_BYTES + 1)
    mime, _ext = _sniff(dados)
    return dados, mime


def existe(ident):
    return caminho(ident) is not None


def base64_de(ident):
    """(base64_sem_prefixo, mime, nome) para o provedor, ou (None, None, None)."""
    dados, mime = ler(ident)
    if dados is None:
        return None, None, None
    return _b64.b64encode(dados).decode("ascii"), mime, ident
