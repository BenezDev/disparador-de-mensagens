#!/usr/bin/env python3
"""Fronteiras de seguranca compartilhadas pelo painel e pelos provedores."""
import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import urllib.parse
from http.cookies import SimpleCookie

from . import config

MAX_CORPO_HTTP = 1024 * 1024
MAX_MIDIA_HTTP = 8 * 1024 * 1024   # upload de imagem (base64 infla ~33%)
MAX_IMPORT_HTTP = 64 * 1024 * 1024   # upload de CSV de contatos
MAX_RESPOSTA_PROVEDOR = 2 * 1024 * 1024
MAX_PAGINA = 500
COOKIE_SESSAO = "disparador_sessao"


def _segredo(env, arquivo, tamanho_minimo=32):
    valor = os.environ.get(env, "").strip()
    if valor:
        return valor
    caminho = os.path.join(config.RAIZ, arquivo)
    try:
        with open(caminho, encoding="utf-8") as f:
            valor = f.read().strip()
    except FileNotFoundError:
        valor = ""
    if len(valor) >= tamanho_minimo:
        os.chmod(caminho, 0o600)
        return valor
    valor = secrets.token_urlsafe(32)
    fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(valor)
    return valor


def segredo_painel():
    return _segredo("DISPARADOR_ADMIN_TOKEN", ".painel-chave", tamanho_minimo=6)


def segredo_webhook_local():
    return _segredo("DISPARADOR_WEBHOOK_TOKEN", ".webhook-chave")


def autenticado(headers):
    esperado = segredo_painel()
    auth = headers.get("Authorization", "")
    recebido = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not recebido:
        cookie = SimpleCookie()
        try:
            cookie.load(headers.get("Cookie", ""))
            recebido = cookie.get(COOKIE_SESSAO).value if cookie.get(COOKIE_SESSAO) else ""
        except Exception:
            recebido = ""
    return bool(recebido) and hmac.compare_digest(recebido, esperado)


def cookie_sessao():
    return (f"{COOKIE_SESSAO}={segredo_painel()}; Path=/; HttpOnly; "
            "SameSite=Strict; Max-Age=43200")


def origem_permitida(headers, porta):
    origem = headers.get("Origin")
    if not origem:
        return True
    try:
        u = urllib.parse.urlsplit(origem)
        host = ipaddress.ip_address(u.hostname) if u.hostname != "localhost" else None
        local = u.hostname == "localhost" or (host is not None and host.is_loopback)
        return u.scheme == "http" and local and (u.port or 80) == int(porta)
    except (TypeError, ValueError):
        return False


def mesma_origem(url_a, url_b):
    def chave(url):
        u = urllib.parse.urlsplit(str(url or ""))
        porta = u.port or (443 if u.scheme == "https" else 80)
        return u.scheme.lower(), (u.hostname or "").lower(), porta
    try:
        return chave(url_a) == chave(url_b)
    except ValueError:
        return False


def validar_url_saida(url, permitir_rede_privada=False):
    """Valida esquema, host e todos os enderecos DNS antes do urllib."""
    try:
        u = urllib.parse.urlsplit(str(url or "").strip())
        if u.scheme not in ("http", "https") or not u.hostname or u.username:
            return False, "URL HTTP(S) sem usuario embutido"
        porta = u.port or (443 if u.scheme == "https" else 80)
        enderecos = {r[4][0] for r in socket.getaddrinfo(u.hostname, porta, type=socket.SOCK_STREAM)}
    except (OSError, TypeError, ValueError) as e:
        return False, f"destino invalido: {type(e).__name__}"
    if not enderecos:
        return False, "destino sem endereco DNS"
    for bruto in enderecos:
        ip = ipaddress.ip_address(bruto)
        privado = (ip.is_private or ip.is_loopback or ip.is_link_local or
                   ip.is_reserved or ip.is_multicast or ip.is_unspecified)
        if privado and not permitir_rede_privada:
            return False, f"rede privada ou reservada bloqueada: {ip}"
    return True, ""


def _meta_assinatura(raw, headers, cfg):
    recebido = headers.get("X-Hub-Signature-256", "")
    segredo = str(cfg.get("app_secret") or "")
    if not recebido or not segredo:
        return False
    esperado = "sha256=" + hmac.new(segredo.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(recebido, esperado)


def _twilio_assinatura(dados, headers, cfg, url_publica):
    recebido = headers.get("X-Twilio-Signature", "")
    segredo = str(cfg.get("token") or "")
    if not recebido or not segredo or not url_publica:
        return False
    base = url_publica + "".join(k + str(dados[k]) for k in sorted(dados))
    esperado = base64.b64encode(hmac.new(
        segredo.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(recebido, esperado)


def validar_webhook(nome, raw, dados, headers, consulta, url_publica):
    cfg = config.carregar(True).get("provedores", {}).get(nome, {})
    if nome == "whatsapp_cloud":
        return _meta_assinatura(raw, headers, cfg)
    if nome == "twilio":
        return _twilio_assinatura(dados, headers, cfg, cfg.get("webhook") or url_publica)
    if nome == "evolution":
        esperado = str(cfg.get("webhook_secret") or segredo_webhook_local())
        recebido = (headers.get("X-Webhook-Secret") or consulta.get("token") or "")
        return bool(recebido) and hmac.compare_digest(str(recebido), esperado)
    return False
