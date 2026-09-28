#!/usr/bin/env python3
"""Painel autenticado em loopback e webhook isolado em outra porta."""
import hashlib
import hmac
import ipaddress
import json
import os
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (campanhas, config, conversas, db, importar, instancias, modelos,
               motor, provedores, regras, seguranca)

WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_SUBIU_EM = str(int(time.time()))


def versao_painel():
    """Muda quando o processo reinicia ou o painel.html muda em disco.

    A aba aberta no navegador compara com o que viu ao carregar e se recarrega
    sozinha: atualizar o sistema nao depende de alguem lembrar do F5.
    """
    try:
        html = int(os.path.getmtime(os.path.join(WEB, "painel.html")))
    except OSError:
        html = 0
    return f"{_SUBIU_EM}-{html}"
ESTADOS_CAMPANHA = {"rascunho", "rodando", "pausada", "concluida"}
ESTADOS_STATUS = {"enviado", "entregue", "lido", "falha"}
ORDEM_STATUS = {"enviado": 1, "entregue": 2, "lido": 3}
_LOGIN_TENTATIVAS = {}
_WEBHOOK_TODAS = {}
_TESTES = {}   # rate-limit do envio de teste
_RESPOSTAS = {}  # rate-limit da resposta manual: conversa, nao disparo
_WEBHOOK_INVALIDAS = {}
_RATE_LOCK = threading.Lock()


def _rate_permitido(bucket, ip, limite, registrar=True):
    agora = time.time()
    with _RATE_LOCK:
        recentes = [t for t in bucket.get(ip, []) if agora - t < 60]
        bucket[ip] = recentes
        if len(recentes) >= limite:
            return False
        if registrar:
            recentes.append(agora)
        if len(bucket) > 2048:
            for chave in list(bucket):
                if not bucket[chave] or agora - bucket[chave][-1] >= 60:
                    bucket.pop(chave, None)
        return True


def _registrar_invalido(ip):
    _rate_permitido(_WEBHOOK_INVALIDAS, ip, 30, registrar=True)


class ErroHTTP(Exception):
    def __init__(self, status, mensagem):
        super().__init__(mensagem)
        self.status, self.mensagem = status, mensagem


class ServidorLimitado(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, endereco, handler, max_workers=32):
        self._vagas = threading.BoundedSemaphore(max_workers)
        super().__init__(endereco, handler)

    def process_request(self, request, client_address):
        if not self._vagas.acquire(blocking=False):
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._vagas.release()


def permitido(ip):
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _inteiro(valor, padrao, minimo=1, maximo=seguranca.MAX_PAGINA):
    try:
        n = int(valor if valor not in (None, "") else padrao)
    except (TypeError, ValueError):
        raise ErroHTTP(400, "numero inteiro invalido")
    if not minimo <= n <= maximo:
        raise ErroHTTP(400, f"valor precisa ficar entre {minimo} e {maximo}")
    return n


class BaseApp(BaseHTTPRequestHandler):
    server_version = "disparador"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def version_string(self):
        return "disparador"

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def responder(self, dados, status=200, tipo="application/json; charset=utf-8",
                  cabecalhos=None):
        corpo = (dados if isinstance(dados, bytes)
                 else json.dumps(dados, ensure_ascii=False, default=str).encode("utf-8"))
        try:
            self.send_response(status)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                             "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                             "frame-ancestors 'none'; base-uri 'none'")
            for k, v in (cabecalhos or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(corpo)
        except (BrokenPipeError, ConnectionResetError):
            # O navegador cancela `fetch` antigo ao trocar de aba ou recarregar.
            # Isso nao e falha da aplicacao e nao deve gerar outro write no socket.
            return

    def _bruto(self, limite=None):
        limite = limite or seguranca.MAX_CORPO_HTTP
        valor = self.headers.get("Content-Length")
        if valor is None:
            raise ErroHTTP(411, "Content-Length obrigatorio")
        try:
            n = int(valor)
        except ValueError:
            raise ErroHTTP(400, "Content-Length invalido")
        if n < 0 or n > limite:
            raise ErroHTTP(413, "corpo grande demais")
        return self.rfile.read(n) if n else b""

    def log_message(self, *args):
        pass

    def _falha(self, e, contexto):
        if isinstance(e, ErroHTTP):
            return self.responder({"erro": e.mensagem}, e.status)
        db.log("erro_http", f"{contexto}: {type(e).__name__}: {e}")
        return self.responder({"erro": "requisicao invalida"}, 400)


# Eventos que o operador precisa VER sem cacar no log. Ban entra como
# chip_indisponivel com motivo 401/403 (o motor grava assim). categoria pinta a
# linha no painel; titulo e o texto curto.
AVISO_TIPOS = {
    "chip_indisponivel":    ("chip",      "chip fora do ar"),
    "chip_caiu":            ("chip",      "chip caiu"),
    "chip_reconectando":    ("chip",      "reconectando chip"),
    "resposta_manual_falhou": ("erro",    "resposta nao enviada"),
    "chip_desconectado":    ("chip",      "chip desconectou"),
    "chip_pausado":         ("chip",      "chip pausado"),
    "chip_religado":        ("chip",      "chip religado"),
    "chip_ignorado_limite": ("chip",      "chip ignorado (limite)"),
    "faixa_redistribuida":  ("faixa",     "fila redistribuida"),
    "acesso_negado":        ("seguranca", "acesso negado"),
    "erro_http":            ("erro",      "erro na requisicao"),
    "campanha_concluida":   ("campanha",  "campanha concluida"),
}


def avisos_recentes(limite=100):
    """Feed unico de avisos: eventos-problema + bloqueios, mais recente primeiro.

    Junta as duas fontes numa lista so, ordenada por data, para o operador nao
    precisar abrir o log cru nem a aba de bloqueios para saber o que caiu.
    """
    tipos = tuple(AVISO_TIPOS)
    marc = ",".join("?" * len(tipos))
    saida = []
    for r in db.todos(f"SELECT ts, tipo, telefone, detalhe FROM evento "
                      f"WHERE tipo IN ({marc}) ORDER BY id DESC LIMIT ?",
                      *tipos, limite):
        cat, rot = AVISO_TIPOS[r["tipo"]]
        saida.append({"quando": r["ts"], "categoria": cat, "titulo": rot,
                      "telefone": r["telefone"], "detalhe": r["detalhe"] or ""})
    for b in db.todos("SELECT telefone, motivo, origem, criado_em FROM bloqueio "
                      "ORDER BY criado_em DESC LIMIT ?", limite):
        saida.append({"quando": b["criado_em"], "categoria": "bloqueio",
                      "titulo": "contato bloqueado", "telefone": b["telefone"],
                      "detalhe": f"{b['origem'] or ''} · {b['motivo'] or ''}"})
    saida.sort(key=lambda x: x["quando"] or "", reverse=True)
    return saida[:limite]


class App(BaseApp):
    """Somente painel. O socket deve ficar em loopback."""

    def _autenticado(self):
        return permitido(self.client_address[0]) and seguranca.autenticado(self.headers)

    def _pagina_login(self):
        with open(os.path.join(WEB, "login.html"), "rb") as f:
            return self.responder(f.read(), 401, "text/html; charset=utf-8")

    def do_GET(self):
        rota = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(rota.query).items()}
        try:
            if rota.path == "/api/saude":
                return self.responder({"ok": True})
            if not self._autenticado():
                return self._pagina_login() if rota.path in ("/", "/index.html") else \
                    self.responder({"erro": "autenticacao necessaria"}, 401)
            self._get(rota.path, q)
        except Exception as e:
            self._falha(e, rota.path)

    def do_POST(self):
        rota = urllib.parse.urlsplit(self.path)
        try:
            if not permitido(self.client_address[0]):
                raise ErroHTTP(403, "origem negada")
            if not seguranca.origem_permitida(self.headers, self.server.server_port):
                raise ErroHTTP(403, "origem do navegador negada")
            if rota.path == "/api/importar/arquivo":
                if not self._autenticado():
                    raise ErroHTTP(401, "autenticacao necessaria")
                return self._importar_arquivo(rota)
            if "application/json" not in self.headers.get("Content-Type", "").lower():
                raise ErroHTTP(415, "use Content-Type application/json")
            limite = seguranca.MAX_MIDIA_HTTP if rota.path == "/api/midia" else None
            bruto = self._bruto(limite)
            try:
                dados = json.loads(bruto.decode("utf-8")) if bruto else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ErroHTTP(400, "JSON invalido")
            if not isinstance(dados, dict):
                raise ErroHTTP(400, "o corpo precisa ser um objeto JSON")
            if rota.path == "/api/login":
                return self._login(dados)
            if not self._autenticado():
                raise ErroHTTP(401, "autenticacao necessaria")
            self._post(rota.path, dados)
        except Exception as e:
            self._falha(e, rota.path)

    def do_OPTIONS(self):
        self.responder({"erro": "CORS desabilitado"}, 405)

    def _login(self, dados):
        ip = self.client_address[0]
        agora = time.time()
        recentes = [t for t in _LOGIN_TENTATIVAS.get(ip, []) if agora - t < 60]
        if len(recentes) >= 8:
            raise ErroHTTP(429, "muitas tentativas; aguarde um minuto")
        recentes.append(agora)
        _LOGIN_TENTATIVAS[ip] = recentes
        token = str(dados.get("token") or "")
        if not hmac.compare_digest(token, seguranca.segredo_painel()):
            raise ErroHTTP(401, "chave invalida")
        _LOGIN_TENTATIVAS.pop(ip, None)
        return self.responder({"ok": True}, cabecalhos={"Set-Cookie": seguranca.cookie_sessao()})

    def _get(self, caminho, q):
        if caminho in ("/", "/index.html"):
            with open(os.path.join(WEB, "painel.html"), "rb") as f:
                return self.responder(f.read(), tipo="text/html; charset=utf-8")
        if caminho == "/api/estado":
            return self.responder(estado_geral())
        if caminho.startswith("/midia/"):
            from . import midia
            dados, mime = midia.ler(caminho[len("/midia/"):])
            if dados is None:
                raise ErroHTTP(404, "imagem nao encontrada")
            return self.responder(dados, tipo=mime)
        if caminho == "/api/campanhas":
            cs = [dict(c) for c in db.todos("SELECT * FROM campanha ORDER BY id DESC")]
            for c in cs:
                c["resumo"] = campanhas.resumo(c["id"])
                c["cota_restante"] = regras.cota_restante(c)
            return self.responder(cs)
        if caminho == "/api/envios":
            filtro, par = "", []
            if q.get("campanha"):
                filtro, par = " AND campanha_id=?", [_inteiro(q["campanha"], 1, 1, 10**9)]
            if q.get("estado"):
                filtro += " AND estado=?"
                par.append(str(q["estado"])[:30])
            limite = _inteiro(q.get("limite"), 60)
            return self.responder([dict(r) for r in db.todos(
                f"SELECT * FROM envio WHERE 1=1{filtro} ORDER BY id DESC LIMIT ?",
                *par, limite)])
        if caminho == "/api/eventos":
            return self.responder([dict(r) for r in db.todos(
                "SELECT * FROM evento ORDER BY id DESC LIMIT ?",
                _inteiro(q.get("limite"), 60))])
        if caminho == "/api/bloqueios":
            return self.responder([dict(r) for r in db.todos(
                "SELECT * FROM bloqueio ORDER BY criado_em DESC LIMIT ?",
                _inteiro(q.get("limite"), 200))])
        if caminho == "/api/avisos":
            return self.responder(avisos_recentes(_inteiro(q.get("limite"), 100)))
        if caminho == "/api/publico":
            try:
                filtros = json.loads(q.get("filtros") or "{}")
            except json.JSONDecodeError:
                raise ErroHTTP(400, "filtros invalidos")
            if not isinstance(filtros, dict):
                raise ErroHTTP(400, "filtros precisam ser um objeto")
            cid = _inteiro(q["campanha"], 1, 1, 10**9) if q.get("campanha") else None
            return self.responder({"total": campanhas.contar_publico(filtros, cid)})
        if caminho == "/api/cidades":
            return self.responder([dict(r) for r in db.todos(
                "SELECT cidade, COUNT(*) n FROM contato WHERE tipo='MOVEL' "
                "GROUP BY cidade ORDER BY n DESC LIMIT 400")])
        if caminho == "/api/indeterminados":
            return self.responder(campanhas.indeterminados(
                int(q["campanha"]) if q.get("campanha") else None))

        if caminho == "/api/instancias":
            cfg = config.carregar()
            if q.get("sincronizar") == "1":
                instancias.sincronizar(cfg)
            optins = db.um(
                "SELECT COUNT(*) n FROM contato c WHERE c.tipo='MOVEL'"
                " AND c.consentimento_em IS NOT NULL AND c.consentimento_em<>''"
                " AND c.consentimento_origem IS NOT NULL AND c.consentimento_origem<>''"
                " AND NOT EXISTS (SELECT 1 FROM bloqueio b WHERE b.telefone=c.telefone)"
            )["n"]
            return self.responder({"chips": instancias.resumo(),
                "aquecimento": instancias.estado_rampa(), "aquecidas_hoje": 0,
                "capacidade_hoje": instancias.capacidade_diaria(),
                "contatos_com_optin": optins,
                # o teto vem do servidor: o painel nunca deve adivinhar quantas
                # vagas sobraram a partir do numero de chips cadastrados
                "max_chips": instancias.MAX_INSTANCIAS,
                "rampa": instancias.RAMPA, "conversas_por_dia": []})
        if caminho == "/api/conversas":
            chip = str(q.get("chip") or "")[:80]
            if not chip:
                return self.responder({"chips": conversas.chips_com_conversa(),
                                       "nao_lidas": conversas.nao_lidas()})
            if not db.um("SELECT 1 FROM instancia WHERE nome=?", chip):
                raise ErroHTTP(404, "chip nao encontrado")
            return self.responder({"chip": chip, "conversas": conversas.conversas(chip)})
        if caminho == "/api/conversa":
            chip, tel = str(q.get("chip") or "")[:80], str(q.get("telefone") or "")[:20]
            if not chip or not tel.isdigit():
                raise ErroHTTP(400, "informe chip e telefone")
            # abrir a conversa e ler: zera o contador dela
            if q.get("ler") == "1":
                conversas.marcar_lida(chip, tel)
            contato = db.um("SELECT nome, cidade FROM contato WHERE telefone=?", tel)
            return self.responder({
                "chip": chip, "telefone": tel,
                "nome": contato["nome"] if contato else None,
                "cidade": contato["cidade"] if contato else None,
                "bloqueado": regras.bloqueado(tel),
                "mensagens": conversas.thread(chip, tel)})
        if caminho == "/api/config":
            return self.responder(config_seguro())
        raise ErroHTTP(404, "rota nao encontrada")

    def _post(self, caminho, d):
        if caminho == "/api/importar":
            cfg = config.carregar(True)
            arquivo = importar.caminho_painel(d.get("arquivo"), cfg)
            limite = d.get("limite")
            if limite is not None:
                limite = _inteiro(limite, 1, 1, 10**7)
            st = importar.importar(arquivo, somente_movel=bool(d.get("somente_movel", True)),
                                   limite=limite)
            return self.responder(dict(st))
        if caminho == "/api/midia":
            from . import midia
            ident, mime, erro = midia.salvar(d.get("dados"))
            if erro:
                return self.responder({"erro": erro}, 400)
            return self.responder({"id": ident, "mime": mime, "url": f"/midia/{ident}"})

        if caminho == "/api/consentimento/declarar":
            origem = str(d.get("origem") or "").strip()
            if not origem:
                return self.responder({"erro": "informe de onde veio o opt-in"}, 400)
            limite = d.get("limite")
            if limite is not None:
                limite = _inteiro(limite, 1, 1, 10**7)
            n = importar.declarar_optin(origem, limite=limite)
            return self.responder({"ok": True, "marcados": n,
                                   "consentidos": estado_geral()["consentidos"]})

        if caminho == "/api/campanha":
            c = campanhas.criar(str(d["nome"])[:120], d["canal"], d["provedor"],
                                str(d["modelo"])[:8000], d.get("meta"),
                                max(0.1, min(float(d.get("por_minuto", 10)), 60.0)),
                                _inteiro(d.get("limite_dia"), 300, 1, 100000))
            return self.responder(dict(c))
        if caminho == "/api/envio/indeterminado":
            n = campanhas.resolver_indeterminado(
                str(d.get("decisao", "")), d.get("envio_id"), d.get("campanha_id"))
            return self.responder({"ok": True, "resolvidos": n,
                                   "restantes": campanhas.indeterminados()})

        if caminho == "/api/instancias/rampa":
            try:
                resultado = instancias.ativar_rampa(d.get("ids"))
            except ValueError as e:
                raise ErroHTTP(400, str(e)) from e
            return self.responder({"ok": True, **resultado,
                                   "aquecimento": instancias.estado_rampa(),
                                   "chips": instancias.resumo()})

        if caminho == "/api/campanha/redistribuir":
            cid = int(d["id"])
            total = 0
            for inst, _q, _ja in instancias.orfas(cid):
                if inst is not None:
                    total += instancias.redistribuir(cid, inst["id"], "manual")
            return self.responder({"ok": True, "movidas": total,
                                   "faixas": campanhas.mapa_faixas(cid)})

        if caminho == "/api/campanha/publico":
            limite = d.get("limite")
            if limite is not None:
                limite = _inteiro(limite, 1, 1, 10**7)
            cid = _inteiro(d["id"], 1, 1, 10**9)
            try:
                n = campanhas.montar_publico(cid, d.get("filtros") or {}, limite,
                                             bool(d.get("aleatorio", False)),
                                             faixas=bool(d.get("faixas", False)))
            except ValueError as e:
                return self.responder(
                    {"erro": str(e),
                     "acao": "Conecte ao menos um chip antes de dividir em faixas."}, 400)
            return self.responder({"enfileirados": n,
                                   "resumo": campanhas.resumo(cid),
                                   "faixas": campanhas.mapa_faixas(cid)})
        if caminho == "/api/campanha/estado":
            return self._estado_campanha(d)
        if caminho == "/api/campanha/previa":
            c = db.um("SELECT * FROM campanha WHERE id=?", int(d["id"])) if d.get("id") else None
            if d.get("modelo"):
                c = dict(c or {"canal": d.get("canal", "sms"), "meta": "{}", "id": 0},
                         modelo=str(d["modelo"])[:8000])
            return self.responder(campanhas.previa(c, _inteiro(d.get("quantos"), 3, 1, 20)))
        if caminho == "/api/bloqueio":
            fones = d["telefones"] if isinstance(d.get("telefones"), list) else [d.get("telefone")]
            n = 0
            from .fone import normalizar
            for t in fones[:1000]:
                e164, _ = normalizar(t)
                if e164:
                    regras.registrar_optout(e164, origem="painel",
                                            motivo=str(d.get("motivo", "manual"))[:40])
                    n += 1
            return self.responder({"bloqueados": n})
        if caminho == "/api/instancia":
            nome = str(d.get("nome", "")).strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]{2,40}", nome):
                raise ErroHTTP(400, "nome invalido; use letras, numeros, - e _")
            linha, erro = instancias.criar(nome, config.carregar(True), d.get("rotulo"))
            if erro:
                raise ErroHTTP(409 if "limite" in erro else 400, erro)
            instancias.sincronizar(config.carregar(True))
            return self.responder({"ok": True, "chips": instancias.resumo()})
        if caminho == "/api/instancia/qr":
            linha = db.um("SELECT * FROM instancia WHERE nome=?", d.get("nome"))
            if not linha:
                raise ErroHTTP(404, "chip nao encontrado")
            mod = provedores.pegar(linha["provedor"])
            png, codigo, erro = mod.qr_instancia(
                linha["nome"], config.carregar(True)["provedores"].get(linha["provedor"], {}))
            if erro:
                raise ErroHTTP(400, erro)
            import base64
            return self.responder({"png": "data:image/png;base64," +
                base64.b64encode(png).decode() if png else "", "codigo": codigo,
                "nome": linha["nome"]})
        if caminho == "/api/instancia/config":
            chip_id = _inteiro(d["id"], 1, 1, 10**9)
            if not db.um("SELECT 1 FROM instancia WHERE id=?", chip_id):
                raise ErroHTTP(404, "chip nao encontrado")
            campos = {k: d[k] for k in ("ativa", "por_minuto", "limite_dia", "aquecer",
                                         "rotulo") if k in d}
            if not campos:
                raise ErroHTTP(400, "nada a alterar")
            if "por_minuto" in campos:
                campos["por_minuto"] = max(0.1, min(float(campos["por_minuto"]), 60.0))
            if "limite_dia" in campos:
                campos["limite_dia"] = _inteiro(campos["limite_dia"], 1, 0, 100000)
            for k in ("ativa", "aquecer"):
                if k in campos:
                    campos[k] = int(bool(campos[k]))
            sets = ", ".join(f"{k}=?" for k in campos)
            db.exec_(f"UPDATE instancia SET {sets} WHERE id=?", *campos.values(),
                     chip_id)
            atualizado = db.um("SELECT * FROM instancia WHERE id=?", chip_id)
            if atualizado["ativa"] and atualizado["aquecer"]:
                instancias.ativar_rampa([chip_id])
            db.log("chip_ajustado", {"id": chip_id, **campos})
            return self.responder({"ok": True, "chips": instancias.resumo()})
        if caminho == "/api/instancia/pausar":
            # tira do rodizio; a sessao do WhatsApp continua conectada
            linha = instancias.pausar(str(d["nome"]), ativa=bool(d.get("ativa")))
            if linha is None:
                raise ErroHTTP(404, "chip nao encontrado")
            return self.responder({"ok": True, "chips": instancias.resumo()})

        if caminho == "/api/instancia/remover":
            ok, erro = instancias.remover(str(d["nome"]), config.carregar(True),
                                          d.get("confirmar_nome"))
            if not ok:
                return self.responder(
                    {"erro": erro, "confirmar_nome": True, "nome": d.get("nome"),
                     "acao": "Para apenas tirar da operação, use Pausar — "
                             "a sessão continua conectada."}, 409)
            return self.responder({"ok": True, "chips": instancias.resumo()})
        if caminho == "/api/motor":
            acao = d.get("acao")
            if acao == "parar":
                if motor.MOTOR:
                    motor.MOTOR.encerrar()
            elif acao == "ligar":
                motor.ligar()
            else:
                raise ErroHTTP(400, "acao precisa ser ligar ou parar")
            return self.responder(motor.situacao())
        if caminho == "/api/responder":
            if not _rate_permitido(_RESPOSTAS, self.client_address[0], 30):
                return self.responder({"erro": "muitas respostas seguidas; espere um minuto"}, 429)
            ok, erro = conversas.responder(str(d.get("chip") or "")[:80],
                                           str(d.get("telefone") or "")[:20],
                                           d.get("texto"), config.carregar(True))
            if not ok:
                return self.responder({"erro": erro}, 409)
            return self.responder({"ok": True})
        if caminho == "/api/teste":
            r = _teste(d, self.client_address[0])
            return self.responder(r, r.pop("_status", 200))
        if caminho == "/api/config":
            config.salvar(config.fundir_segredos(d))
            for m in provedores.REGISTRO.values():
                getattr(m, "_CACHE", {}).clear()
            return self.responder(config_seguro())
        raise ErroHTTP(404, "rota nao encontrada")

    def _importar_arquivo(self, rota):
        """Recebe o CSV cru do navegador, grava em dados/importacoes e importa.

        Sem caminho de servidor: o operador so escolhe o arquivo no computador.
        Os parametros (consentimento, so-movel) vem na query, para o corpo
        carregar apenas os bytes do arquivo.
        """
        import tempfile
        bruto = self._bruto(seguranca.MAX_IMPORT_HTTP)
        if not bruto:
            raise ErroHTTP(400, "arquivo vazio")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(rota.query).items()}
        cons = None
        if q.get("optin") == "1":
            origem = str(q.get("optin_origem") or "").strip()[:120]
            if not origem:
                raise ErroHTTP(400, "informe a origem do opt-in")
            cons = (db.agora(), origem)
        pasta = os.path.join(config.RAIZ, "dados", "importacoes")
        os.makedirs(pasta, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".csv", dir=pasta)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(bruto)
            try:
                st = importar.importar(tmp, somente_movel=(q.get("com_fixos") != "1"),
                                       consentimento=cons)
            except (ValueError, UnicodeDecodeError) as e:
                # arquivo errado (ex.: export de e-mails) devolve a explicacao,
                # nao um "requisicao invalida" generico
                raise ErroHTTP(400, str(e))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return self.responder(dict(st))

    def _estado_campanha(self, d):
        novo, cid = d.get("estado"), _inteiro(d.get("id"), 1, 1, 10**9)
        if novo not in ESTADOS_CAMPANHA:
            raise ErroHTTP(400, f"estado invalido: {novo}")
        if novo == "rodando":
            c = db.um("SELECT * FROM campanha WHERE id=?", cid)
            if not c:
                raise ErroHTTP(404, "campanha nao encontrada")
            cfg = config.carregar(True)
            modulo = provedores.REGISTRO.get(c["provedor"])
            if modulo is None:
                raise ErroHTTP(400, f"provedor '{c['provedor']}' nao existe")
            # Prontidao para enviar. Um provedor multi-chip (chips na tabela
            # instancia) esta pronto se AO MENOS UM chip ativo esta conectado --
            # e assim que o motor envia (instancias.remetentes, um chip por vez).
            # Checar so a instancia unica do config ('principal') barrava a
            # campanha inteira quando a 'principal' caia e outros chips estavam
            # no ar. Sem chips cadastrados, cai no diagnostico da instancia unica.
            ativos = db.todos("SELECT * FROM instancia WHERE provedor=? AND ativa=1",
                              c["provedor"])
            if ativos:
                try:
                    instancias.sincronizar(cfg, c["provedor"])   # estado real do gateway
                    ativos = db.todos("SELECT * FROM instancia WHERE provedor=? "
                                      "AND ativa=1", c["provedor"])
                except Exception:
                    pass   # gateway mudo: decide pelo ultimo estado conhecido
                if not any(i["estado"] == "open" for i in ativos):
                    return self.responder({"erro": "nenhum chip conectado",
                        "acao": "Conecte ou religue um chip na aba chips antes de rodar.",
                        "detalhe": f"{len(ativos)} chip(s) ativo(s), nenhum com estado 'open'",
                        "provedor": c["provedor"]}, 409)
            else:
                diag = provedores.diagnosticar(c["provedor"],
                                               cfg["provedores"].get(c["provedor"], {}))
                if not diag.ok:
                    return self.responder({"erro": diag.mensagem, "acao": diag.acao,
                        "detalhe": diag.detalhe, "faltando": diag.faltando,
                        "provedor": c["provedor"]}, 400)
            # fila vazia era o que dava "concluido" na hora, sem explicar: o
            # motor liga, nao acha nada para enviar e encerra. Barrar aqui e
            # dizer o passo que faltou.
            if not campanhas.resumo(cid).get("fila"):
                return self.responder({"erro": "a fila está vazia",
                    "acao": 'clique em "montar público" nesta campanha antes de rodar',
                    "fila_vazia": True}, 409)
            if not getattr(modulo, "SIMULA", False):
                if c["canal"] == "whatsapp":
                    sem = campanhas.sem_consentimento_na_fila(cid)
                    if sem:
                        return self.responder({"erro": "fila sem opt-in comprovado",
                            "sem_consentimento": sem,
                            "acao": "Importe consentimento_em e consentimento_origem."}, 409)
                if not d.get("confirmar"):
                    r = campanhas.resumo(cid)
                    return self.responder({"erro": "confirmacao necessaria", "confirmar": True,
                        "provedor": c["provedor"], "canal": c["canal"],
                        "na_fila": r.get("fila", 0)}, 409)
            db.exec_("UPDATE campanha SET estado='rodando', iniciado_em=COALESCE(iniciado_em,?) "
                     "WHERE id=?", db.agora(), cid)
            motor.ligar()
        else:
            db.exec_("UPDATE campanha SET estado=? WHERE id=?", novo, cid)
        db.log("campanha_estado", novo, campanha_id=cid)
        return self.responder({"ok": True, "estado": novo})


class WebhookApp(BaseApp):
    """Listener sem painel: aceita somente callbacks autenticados."""

    def do_GET(self):
        rota = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(rota.query).items()}
        try:
            if not _rate_permitido(_WEBHOOK_TODAS, self.client_address[0], 2400):
                raise ErroHTTP(429, "limite de webhook atingido")
            if rota.path != "/webhook/whatsapp_cloud":
                raise ErroHTTP(404, "rota nao encontrada")
            cfg = config.carregar(True)["provedores"].get("whatsapp_cloud", {})
            esperado = str(cfg.get("verify_token") or "")
            if (q.get("hub.mode") != "subscribe" or not esperado or
                    not hmac.compare_digest(q.get("hub.verify_token", ""), esperado)):
                raise ErroHTTP(403, "verificacao negada")
            return self.responder(str(q.get("hub.challenge", "")).encode(),
                                  tipo="text/plain; charset=utf-8")
        except Exception as e:
            self._falha(e, rota.path)

    def do_POST(self):
        rota = urllib.parse.urlsplit(self.path)
        try:
            ip = self.client_address[0]
            if not _rate_permitido(_WEBHOOK_INVALIDAS, ip, 30, registrar=False):
                raise ErroHTTP(429, "muitas assinaturas invalidas")
            if not _rate_permitido(_WEBHOOK_TODAS, ip, 2400):
                raise ErroHTTP(429, "limite de webhook atingido")
            partes = [p for p in rota.path.split("/") if p]
            if len(partes) != 2 or partes[0] != "webhook":
                raise ErroHTTP(404, "rota nao encontrada")
            nome = partes[1]
            if nome not in provedores.REGISTRO:
                raise ErroHTTP(404, "provedor desconhecido")
            bruto = self._bruto()
            tipo = self.headers.get("Content-Type", "").lower()
            if "application/x-www-form-urlencoded" in tipo:
                dados = {k: v[0] for k, v in urllib.parse.parse_qs(
                    bruto.decode("utf-8", "replace")).items()}
            elif "application/json" in tipo:
                try:
                    dados = json.loads(bruto.decode("utf-8")) if bruto else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise ErroHTTP(400, "JSON invalido")
            else:
                raise ErroHTTP(415, "tipo de webhook nao suportado")
            if not isinstance(dados, dict):
                raise ErroHTTP(400, "evento invalido")
            q = {k: v[0] for k, v in urllib.parse.parse_qs(rota.query).items()}
            url_publica = f"http://{self.headers.get('Host', '')}{rota.path}"
            if not seguranca.validar_webhook(nome, bruto, dados, self.headers, q, url_publica):
                _registrar_invalido(ip)
                raise ErroHTTP(401, "assinatura do webhook invalida")
            chave = hashlib.sha256(nome.encode() + b"\0" + bruto).hexdigest()
            return self.responder(_webhook(nome, dados, chave))
        except Exception as e:
            self._falha(e, rota.path)


def _teste(d, ip="local"):
    """Envia UMA mensagem para o numero digitado, para o operador ver como fica.

    E o operador mandando para si mesmo: quem digita o numero autoriza aquele
    unico envio, entao o portao de consentimento (que vale para a base) nao se
    aplica aqui. Nao toca na fila, nao consome cota, e e limitado a poucos por
    minuto para nao virar um atalho de spam.
    """
    from .fone import normalizar
    e164, tipo = normalizar(d.get("telefone"))
    if not e164:
        return {"erro": f"telefone invalido ({tipo})", "_status": 400}
    if not _rate_permitido(_TESTES, ip, 5):
        return {"erro": "muitos testes seguidos; espere um minuto", "_status": 429}
    cfg = config.carregar(True)
    nome = d.get("provedor")
    if nome not in provedores.REGISTRO:
        return {"erro": f"provedor '{nome}' nao existe", "_status": 400}
    prov = provedores.pegar(nome)
    cred = dict(cfg["provedores"].get(nome, {}))
    canal = d.get("canal", "sms")
    simula = getattr(prov, "SIMULA", False)

    # provedor com chips (Evolution): escolhe um conectado para mandar o teste
    if not simula and hasattr(prov, "listar_instancias"):
        chip = db.um("SELECT * FROM instancia WHERE provedor=? AND estado='open'"
                     " AND ativa=1 ORDER BY id LIMIT 1", nome)
        if not chip:
            return {"erro": "nenhum chip conectado para enviar o teste",
                    "acao": "conecte um chip na aba Chips (ler QR)", "_status": 409}
        cred["instancia"] = chip["nome"]

    diag = provedores.diagnosticar(nome, cred)
    if not diag.ok:
        return {"erro": diag.mensagem, "acao": diag.acao, "detalhe": diag.detalhe,
                "faltando": diag.faltando, "provedor": nome, "_status": 400}

    # legenda/texto com as variaveis resolvidas a partir do proprio numero,
    # ou de um contato de exemplo se ele nao estiver na base
    contato = db.um("SELECT * FROM contato WHERE telefone=?", e164)
    contato = contato or db.um("SELECT * FROM contato LIMIT 1") or {
        "nome": "voce", "cidade": "", "bairro": "", "segmento": "", "email": "",
        "cnpj": "", "telefone": e164}
    texto = modelos.montar(str(d.get("modelo") or "")[:8000], contato,
                           campanhas.rodape(canal), ascii_=(canal == "sms"))
    meta = dict(d.get("meta") or {}, canal=canal)
    if meta.get("imagem"):
        from . import midia
        b64, mime, mnome = midia.base64_de(meta["imagem"])
        if b64 is None:
            return {"erro": "a imagem do teste nao esta mais no disco",
                    "_status": 400}
        meta["imagem_b64"], meta["imagem_mime"], meta["imagem_nome"] = b64, mime, mnome

    try:
        r = prov.enviar(e164, texto, cred, meta)
    except Exception as e:
        return {"erro": f"falha ao enviar: {type(e).__name__}: {e}", "_status": 502}
    db.log("teste", {"telefone": e164, "ok": r.ok, "erro": r.erro,
            "provedor": nome, "simulado": simula})
    return {"ok": r.ok, "id_externo": r.id_externo, "erro": r.erro, "texto": texto,
            "simulado": simula, "provedor": nome, "com_imagem": bool(meta.get("imagem"))}


def _transicao_status(atual, novo):
    # 'indeterminado' significa "nao sei se saiu". Um recibo do provedor e
    # exatamente a prova que faltava: deixar entrar tira o envio do limbo sem
    # ninguem precisar conferir a mao.
    if atual == "indeterminado":
        return novo in ("enviado", "entregue", "lido", "falha")
    if novo not in ESTADOS_STATUS or atual in ("respondido", "cancelado", "bloqueado"):
        return False
    if novo == "falha":
        return atual in ("enviando", "enviado")
    if atual not in ORDEM_STATUS:
        return novo in ORDEM_STATUS
    return ORDEM_STATUS[novo] > ORDEM_STATUS[atual]


def _webhook(nome, dados, evento_id=None):
    """Aplica evento verificado, idempotente, escopado e monotonicamente."""
    prov = provedores.REGISTRO.get(nome)
    if not prov or not hasattr(prov, "ler_webhook"):
        return {"erro": f"webhook nao suportado para {nome}"}
    if evento_id:
        cur = db.exec_("INSERT OR IGNORE INTO webhook_recebido(provedor,evento_id,recebido_em) "
                       "VALUES(?,?,?)", nome, evento_id, db.agora())
        if cur.rowcount != 1:
            return {"ok": True, "eventos": 0, "duplicado": True}
    cfg, tratados = config.carregar(), 0
    for ev in prov.ler_webhook(dados) or []:
        if ev.get("tipo") == "status" and ev.get("id_externo"):
            linhas = db.todos(
                "SELECT e.*, i.nome instancia_nome FROM envio e "
                "JOIN campanha c ON c.id=e.campanha_id "
                "LEFT JOIN instancia i ON i.id=e.instancia_id "
                "WHERE c.provedor=? AND e.id_externo=?",
                nome, ev["id_externo"])
            conta = str(ev.get("conta") or "")
            if not conta:
                db.log("webhook_sem_conta", {"provedor": nome,
                                              "id_externo": ev.get("id_externo")})
                continue
            linhas = [e for e in linhas if e["conta_externa"] and
                      hmac.compare_digest(str(e["conta_externa"]), conta)]
            if len(linhas) != 1:
                db.log("webhook_sem_vinculo", {"provedor": nome,
                                                "id_externo": ev.get("id_externo")})
                continue
            envio = linhas[0]
            if _transicao_status(envio["estado"], ev.get("estado")):
                db.exec_("UPDATE envio SET estado=?, erro=COALESCE(?,erro), atualizado_em=? "
                         "WHERE id=? AND estado=?", ev["estado"], ev.get("erro"),
                         db.agora(), envio["id"], envio["estado"])
                tratados += 1
        elif ev.get("tipo") == "resposta":
            tel, formas = regras.canonizar(ev.get("telefone") or "")
            if not tel:
                continue
            conta = str(ev.get("conta") or "")
            if not conta:
                db.log("webhook_sem_conta", {"provedor": nome, "telefone": tel})
                continue
            for f in formas:
                sql = (
                    "UPDATE envio SET estado='respondido', atualizado_em=? WHERE id IN ("
                    "SELECT e.id FROM envio e JOIN campanha c ON c.id=e.campanha_id "
                    "WHERE c.provedor=? AND e.telefone=? "
                    "AND e.estado IN ('enviado','entregue','lido')"
                )
                args = [db.agora(), nome, f]
                sql += " AND e.conta_externa=?"
                args.append(conta)
                cur = db.exec_(sql + ")", *args)
                tratados += cur.rowcount
            db.log("resposta", str(ev.get("texto", ""))[:400], telefone=tel)
            if regras.detectar_optout(ev.get("texto", ""), cfg):
                regras.registrar_optout(tel, origem=f"webhook:{nome}")
        elif ev.get("tipo") == "mensagem" and ev.get("conta"):
            # a mesma forma de numero que a campanha usou: sem isso o cliente
            # que respondeu sem o 9o digito abria uma segunda conversa
            tel, _formas = regras.canonizar(ev.get("telefone") or "")
            if tel and conversas.registrar(
                    str(ev["conta"]), tel, ev.get("direcao") or "entrada",
                    ev.get("texto"), ev.get("tipo_msg") or "texto",
                    ev.get("id_externo")) is not None:
                tratados += 1
        elif ev.get("tipo") == "conexao" and ev.get("conta"):
            # queda avisada pelo gateway: espelha na hora e, se nao for ban,
            # ja pede a reconexao -- sem esperar a proxima sincronizacao
            linha = instancias.registrar_conexao(str(ev["conta"]), ev.get("estado"),
                                                 ev.get("motivo"))
            if linha is not None:
                tratados += 1
                if ev.get("estado") != "open":
                    instancias.reconectar_pendentes(cfg, nome)
    return {"ok": True, "eventos": tratados}


def estado_geral():
    tot = db.um("SELECT COUNT(*) n FROM contato")["n"]
    mov = db.um("SELECT COUNT(*) n FROM contato WHERE tipo='MOVEL'")["n"]
    consentidos = db.um("SELECT COUNT(*) n FROM contato WHERE consentimento_em IS NOT NULL "
                         "AND consentimento_em<>'' AND consentimento_origem IS NOT NULL "
                         "AND consentimento_origem<>''")["n"]
    blo = db.um("SELECT COUNT(*) n FROM bloqueio")["n"]
    por_estado = {r["estado"]: r["n"] for r in db.todos(
        "SELECT estado, COUNT(*) n FROM envio GROUP BY estado")}
    cfg = config.carregar()
    aberta, motivo = regras.janela_aberta(cfg)
    presos = db.um("SELECT COUNT(*) n FROM envio WHERE estado='indeterminado'")["n"]
    # webhook descartado por falta de escopo era so linha de log: ninguem olha.
    # Se isso passar de zero, opt-out pode estar se perdendo -- e opt-out
    # perdido vira denuncia, que vira chip banido.
    descartados = db.um(
        "SELECT COUNT(*) n FROM evento WHERE tipo IN ('webhook_sem_conta',"
        "'webhook_sem_vinculo') AND ts>=?", regras.inicio_do_dia())["n"]
    chips = instancias.resumo()
    # disparadas = enviadas de verdade + o que ja foi disparado por fora (offset).
    # Offset e so de exibicao; nao entra em cota, fila nem rampa.
    reais = sum(por_estado.get(e, 0) for e in ("enviado", "entregue", "lido", "respondido"))
    offset = int((cfg.get("painel") or {}).get("envios_offset", 0) or 0)
    return {"contatos": tot, "moveis": mov, "consentidos": consentidos,
            "bloqueios": blo, "envios": por_estado, "hoje": regras.enviados_hoje(),
            "disparadas": reais + offset,
            "motor": motor.situacao(),
            "indeterminados": presos, "webhooks_descartados": descartados,
            "nao_lidas": conversas.nao_lidas(),
            "versao": versao_painel(),
            "chips": {"total": len(chips),
            "conectados": sum(1 for c in chips if c["conectada"]),
            "capacidade_hoje": instancias.capacidade_diaria(), "aquecidas_hoje": 0},
            "janela": {"aberta": aberta, "motivo": motivo,
            "reabre": regras.proxima_abertura(cfg).isoformat(timespec="minutes")},
            "provedores": {n: diagnostico_publico(n, m, cfg)
                            for n, m in provedores.REGISTRO.items()}}


def diagnostico_publico(nome, modulo, cfg):
    d = provedores.diagnosticar(nome, cfg["provedores"].get(nome, {}))
    return {"rotulo": modulo.ROTULO, "canais": list(modulo.CANAL),
            "aquece": getattr(modulo, "AQUECE", False),
            "simula": getattr(modulo, "SIMULA", False), "doc": getattr(modulo, "DOC", ""),
            "ajuda": getattr(modulo, "AJUDA", ""), "campos": getattr(modulo, "CAMPOS", {}),
            "obrigatorios": list(getattr(modulo, "OBRIGATORIOS", ())), "pronto": d.ok,
            "faltando": d.faltando, "mensagem": d.mensagem, "acao": d.acao,
            "detalhe": d.detalhe}


def _redigir(obj, caminho, preenchidos):
    if isinstance(obj, dict):
        saida = {}
        for k, v in obj.items():
            atual = f"{caminho}.{k}" if caminho else k
            if provedores.e_segredo(k):
                if str(v or "").strip():
                    preenchidos.append(atual)
                saida[k] = None
            else:
                saida[k] = _redigir(v, atual, preenchidos)
        return saida
    if isinstance(obj, list):
        return [_redigir(v, f"{caminho}.{i}", preenchidos) for i, v in enumerate(obj)]
    return obj


def config_seguro():
    c = json.loads(json.dumps(config.carregar()))
    c.pop("_env", None)
    ambiente = config.PADRAO.get("_env", {})
    for prov, campos in list(c.get("provedores", {}).items()):
        preenchidos = []
        c["provedores"][prov] = _redigir(campos, "", preenchidos)
        c["provedores"][prov]["_preenchidos"] = preenchidos
        c["provedores"][prov]["_do_ambiente"] = [p for p in preenchidos
            if os.environ.get(ambiente.get(f"{prov}.{p}", "\x00"))]
    return c


VIGIA_A_CADA = 60.0


def vigiar(parar):
    """Espelha os chips e reergue os que cairam, enquanto o painel viver.

    O motor so sincroniza quando esta ligado e com a janela aberta; uma queda
    as 22h ficaria esperando o dia seguinte. Este laco roda sempre, e o freio
    por chip em instancias.reconectar_pendentes evita martelar o gateway.
    """
    while not parar.wait(VIGIA_A_CADA):
        try:
            instancias.sincronizar(config.carregar(True))
        except Exception as e:                       # nunca derrubar a thread
            db.log("erro_vigia", f"{type(e).__name__}: {e}")


def servir(porta=8777, host="127.0.0.1", porta_webhook=8788,
           host_webhook="0.0.0.0"):
    if not permitido(host):
        raise ValueError("o painel administrativo so pode escutar em loopback")
    db.conn()
    db.recuperar_orfaos()
    seguranca.segredo_painel()
    seguranca.segredo_webhook_local()
    painel = ServidorLimitado((host, porta), App)
    webhook = ServidorLimitado((host_webhook, porta_webhook), WebhookApp)
    threading.Thread(target=webhook.serve_forever, name="webhook", daemon=True).start()
    parar_vigia = threading.Event()
    threading.Thread(target=vigiar, args=(parar_vigia,), name="vigia", daemon=True).start()
    print(f"painel:  http://127.0.0.1:{porta}  (chave em .painel-chave)")
    print(f"webhook isolado: http://127.0.0.1:{porta_webhook}/webhook/<provedor>")
    try:
        painel.serve_forever()
    except KeyboardInterrupt:
        if motor.MOTOR:
            motor.MOTOR.encerrar()
    finally:
        parar_vigia.set()
        painel.server_close()
        webhook.shutdown()
        webhook.server_close()


if __name__ == "__main__":
    import sys
    servir(int(sys.argv[1]) if len(sys.argv) > 1 else 8777)
