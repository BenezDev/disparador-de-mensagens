#!/usr/bin/env python3
"""
Bateria pesada: carga, concorrencia, queda de processo e entrada hostil.

Diferente de testes.py (invariantes, rapido), aqui o objetivo e quebrar o
sistema de proposito e ver se as promessas continuam de pe sob estresse.

    python3 testes_carga.py [--rapido]
"""
import json, os, random, shutil, signal, subprocess, sys, tempfile, threading, time
from datetime import datetime, timedelta

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

import disparador.db as db
import disparador.config as config

AREA = None


def montar_area(falha_pct=0, janela_24h=True):
    """Banco e config isolados para a bateria."""
    global AREA
    AREA = tempfile.mkdtemp(prefix="carga_")
    db.CAMINHO = os.path.join(AREA, "carga.db")
    db._local = threading.local()
    cfg = os.path.join(AREA, "config.json")
    janela = ({"inicio": "00:00", "fim": "23:59", "sabado_inicio": "00:00",
               "sabado_fim": "23:59", "domingo": True} if janela_24h else
              {"inicio": "09:00", "fim": "19:00", "sabado_inicio": "09:00",
               "sabado_fim": "13:00", "domingo": False})
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"janela": janela, "cooldown_dias": 45,
                   "provedores": {"simulado": {"falha_pct": falha_pct}}}, f)
    config.CAMINHO, config._cache = cfg, None
    db.conn()
    return AREA


def popular(n, cidades=("RIO PRETO", "MIRASSOL", "CATANDUVA")):
    c = db.conn()
    c.execute("BEGIN")
    c.executemany(
        "INSERT INTO contato(telefone,tipo,nome,cidade,segmento,email,"
        "consentimento_em,consentimento_origem,criado_em)"
        " VALUES(?,'MOVEL',?,?,?,?,?,?,?)",
        [(f"5517{9_0000_0000 + i}", f"EMPRESA {i} LTDA", cidades[i % len(cidades)],
          "Comercio varejista de artigos", f"e{i}@x.com", db.agora(),
          "fixture:carga", db.agora())
         for i in range(n)])
    c.execute("COMMIT")


def integridade(rotulo):
    """As contas que precisam fechar depois de qualquer estresse."""
    total = db.um("SELECT COUNT(*) n FROM envio")["n"]
    soma = db.um("SELECT COUNT(*) n FROM envio WHERE estado IN"
                 " ('fila','enviando','enviado','entregue','lido','respondido',"
                 "  'falha','bloqueado','cancelado','indeterminado')")["n"]
    dup = db.um("SELECT COUNT(*) n FROM (SELECT campanha_id,contato_id FROM envio"
                " GROUP BY 1,2 HAVING COUNT(*)>1)")["n"]
    ids = db.um("SELECT COUNT(*) n, COUNT(DISTINCT id_externo) d FROM envio"
                " WHERE id_externo IS NOT NULL")
    presos = db.um("SELECT COUNT(*) n FROM envio WHERE estado='enviando'"
                   " AND atualizado_em < ?",
                   (datetime.now().astimezone() - timedelta(minutes=5)).isoformat())["n"]
    sem_texto = db.um("SELECT COUNT(*) n FROM envio WHERE estado='enviado'"
                      " AND (texto IS NULL OR texto='')")["n"]
    sem_data = db.um("SELECT COUNT(*) n FROM envio WHERE estado='enviado'"
                     " AND enviado_em IS NULL")["n"]
    bloq_ativo = db.um("SELECT COUNT(*) n FROM envio e JOIN bloqueio b"
                       " ON b.telefone=e.telefone WHERE e.estado='fila'")["n"]
    problemas = []
    if soma != total:
        problemas.append(f"estado invalido em {total - soma} linhas")
    if dup:
        problemas.append(f"{dup} pares (campanha,contato) duplicados")
    if ids["n"] != ids["d"]:
        problemas.append(f"{ids['n'] - ids['d']} id_externo repetido (mensagem em dobro)")
    if presos:
        problemas.append(f"{presos} presos em 'enviando'")
    if sem_texto:
        problemas.append(f"{sem_texto} enviados sem texto gravado")
    if sem_data:
        problemas.append(f"{sem_data} enviados sem data")
    if bloq_ativo:
        problemas.append(f"{bloq_ativo} bloqueados ainda na fila")
    ok = not problemas
    print(f"   {'PASSOU' if ok else 'FALHOU'}  {rotulo}")
    for p in problemas:
        print(f"           !! {p}")
    return ok


def titulo(t):
    print(f"\n\033[1m{t}\033[0m\n" + "-" * 68)


# ===========================================================================
class Instantaneo:
    """Provedor sem latencia: isola o custo do MOTOR do custo da rede.

    O 'simulado' dorme 50-250ms de proposito (imita gateway real), entao medir
    vazao com ele mede o sono, nao o codigo.
    """
    ROTULO, CANAL = "instantaneo", ("sms", "whatsapp")
    _n = [0]
    pronto = staticmethod(lambda cfg: (True, ""))

    @staticmethod
    def enviar(destino, texto, cfg, meta=None):
        from disparador.provedores import OK, ERRO
        Instantaneo._n[0] += 1
        if float(cfg.get("falha_pct", 0)) and Instantaneo._n[0] % int(100 / float(cfg["falha_pct"])) == 0:
            return ERRO("falha sintetica")
        return OK(f"ins_{Instantaneo._n[0]}")


def motor_acelerado():
    """Mesmo motor e banco, mas sem o relógio de produção limitar o teste."""
    from disparador.motor import Motor
    m = Motor()
    m._pode_agora = lambda rem: True
    m._agendar = lambda rem: None
    m._dormir = lambda seg: m.parar.wait(min(float(seg), 0.001))
    return m


def teste_soak(minutos=1.0, contatos=20000, falha_pct=8):
    """Motor a todo vapor com gateway instavel. As contas tem que fechar."""
    titulo(f"1. SOAK — {minutos*60:.0f}s de disparo contínuo, {falha_pct}% de falha no gateway")
    montar_area(falha_pct=falha_pct)
    popular(contatos)
    from disparador import campanhas
    c = campanhas.criar("soak", "sms", "simulado",
                        "{Ola|Oi|Opa}, {nome}! {segmento_curto} em {cidade}?",
                        por_minuto=100000, limite_dia=10**9)
    n = campanhas.montar_publico(c["id"])
    db.exec_("UPDATE campanha SET estado='rodando', iniciado_em=? WHERE id=?",
             db.agora(), c["id"])
    print(f"   fila inicial: {n:,}")

    from disparador import provedores
    original = provedores.REGISTRO["simulado"]
    provedores.REGISTRO["simulado"] = Instantaneo    # mede o motor, nao a rede
    try:
        m = motor_acelerado()
        m.start()
        t0, fim = time.time(), time.time() + minutos * 60
        while time.time() < fim:
            time.sleep(2)
            r = campanhas.resumo(c["id"])
            if not r.get("fila"):
                break
            print(f"   \033[2m{m.estado['situacao'][:22]:<22} enviado={r.get('enviado',0):>6,}"
                  f" falha={r.get('falha',0):>4,} fila={r.get('fila',0):>6,}\033[0m")
        seg = time.time() - t0
        m.encerrar()
    finally:
        provedores.REGISTRO["simulado"] = original

    r = campanhas.resumo(c["id"])
    vazao = r.get("enviado", 0) / seg
    print(f"\n   vazão do motor: {vazao:,.0f} msg/s  ({vazao*3600:,.0f}/h)")
    print(f"   -> 250 mil levariam {250000/vazao/3600:.1f}h se o provedor fosse instantâneo")
    print(f"   resultado: {r}")
    return integridade("contas fecham depois do soak")


# ===========================================================================
def teste_optout_durante_disparo(contatos=4000):
    """Webhook de SAIR chegando enquanto o motor dispara para os mesmos numeros.

    E a corrida mais perigosa do sistema: se o cancelamento perder para o envio,
    alguem que pediu para sair recebe mensagem.
    """
    titulo("2. CORRIDA — opt-out chegando no meio do disparo")
    montar_area()
    popular(contatos)
    from disparador import campanhas, regras
    c = campanhas.criar("corrida", "whatsapp", "simulado", "Ola {nome}",
                        por_minuto=100000, limite_dia=10**9)
    campanhas.montar_publico(c["id"])
    db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])

    alvos = [r["telefone"] for r in db.todos(
        "SELECT telefone FROM envio ORDER BY RANDOM() LIMIT 600")]
    parar = threading.Event()

    def bombardeio():
        db._local = threading.local()          # conexao propria, como o servidor
        for t in alvos:
            if parar.is_set():
                break
            regras.registrar_optout(t, origem="webhook:teste")
            time.sleep(0.001)

    m = motor_acelerado()
    m.start()
    time.sleep(0.4)                            # deixa o motor pegar ritmo
    th = threading.Thread(target=bombardeio)
    th.start()
    th.join()
    time.sleep(1.5)
    parar.set()
    m.encerrar()

    # a pergunta que importa: alguem que pediu para sair recebeu DEPOIS disso?
    vazou = db.um(
        "SELECT COUNT(*) n FROM envio e JOIN bloqueio b ON b.telefone=e.telefone"
        " WHERE e.estado IN ('enviado','entregue','lido') AND e.enviado_em > b.criado_em")["n"]
    r = campanhas.resumo(c["id"])
    print(f"   {len(alvos)} opt-outs durante o disparo")
    print(f"   resultado: {r}")
    print(f"   {'PASSOU' if vazou == 0 else 'FALHOU'}  mensagens enviadas DEPOIS do opt-out: {vazou}")
    return integridade("contas fecham depois da corrida") and vazou == 0


# ===========================================================================
def teste_queda_do_processo(contatos=1200):
    """kill -9 no meio do disparo. Ao voltar, nada pode sair duas vezes."""
    titulo("3. QUEDA — kill -9 durante o disparo e religa")
    area = montar_area()
    popular(contatos)
    from disparador import campanhas
    c = campanhas.criar("queda", "sms", "simulado", "Ola {nome}",
                        por_minuto=100000, limite_dia=10**9)
    campanhas.montar_publico(c["id"])
    db.exec_("UPDATE campanha SET estado='rodando', iniciado_em=? WHERE id=?",
             db.agora(), c["id"])
    db.conn().close()
    db._local = threading.local()

    roteiro = f'''
import sys, threading, time, uuid
sys.path.insert(0, {RAIZ!r})
import disparador.db as db, disparador.config as config
db.CAMINHO = {os.path.join(area, "carga.db")!r}
db._local = threading.local()
config.CAMINHO, config._cache = {os.path.join(area, "config.json")!r}, None
db.conn()
from disparador import provedores
class Rapido:
    SIMULA = True
    pronto = staticmethod(lambda cfg: (True, ""))
    @staticmethod
    def enviar(*args, **kwargs):
        # janela larga de proposito: sem ela o kill -9 quase sempre cai ENTRE
        # dois envios, e o caminho que o teste existe para exercitar (linha em
        # voo sem recibo) nunca acontece
        time.sleep(0.012)
        return provedores.OK("proc_" + uuid.uuid4().hex)
provedores.REGISTRO["simulado"] = Rapido
from disparador.motor import Motor
m = Motor()
m._pode_agora = lambda rem: True
m._agendar = lambda rem: None
m._dormir = lambda seg: m.parar.wait(min(float(seg), 0.001))
m.start()
time.sleep(60)
'''
    def rodar_ate(alvo, incluir_indeterminado=False):
        p = subprocess.Popen([sys.executable, "-c", roteiro])
        db._local = threading.local()
        for _ in range(300):
            time.sleep(0.1)
            resumo = campanhas.resumo(c["id"])
            resolvidos = resumo.get("enviado", 0)
            if incluir_indeterminado:
                resolvidos += resumo.get("indeterminado", 0)
            if resolvidos >= alvo:
                break
        return p

    p = rodar_ate(400)
    enviados_antes = campanhas.resumo(c["id"]).get("enviado", 0)
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    print(f"   morto com {enviados_antes:,} enviados")
    print(f"   deixou em 'enviando': "
          f"{db.um(chr(83)+'ELECT COUNT(*) n FROM envio WHERE estado=' + chr(39) + 'enviando' + chr(39))['n']}")

    # Sem recibo remoto, o boot isola a tentativa: nunca devolve ao reenvio.
    db.exec_("UPDATE envio SET atualizado_em=? WHERE estado='enviando'",
             (datetime.now().astimezone() - timedelta(minutes=10)).isoformat(timespec="seconds"))
    isolados = db.recuperar_orfaos()
    print(f"   isolados como indeterminados apos o boot: {isolados}")
    pausada = db.um("SELECT estado FROM campanha WHERE id=?", c["id"])["estado"]
    print(f"   campanha apos recuperacao: {pausada}")
    # Simula a revisao do recibo no provedor e a retomada explicita do operador.
    db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])

    p = rodar_ate(contatos, incluir_indeterminado=True)
    time.sleep(0.1)
    os.kill(p.pid, signal.SIGKILL)
    p.wait()
    db._local = threading.local()
    r = campanhas.resumo(c["id"])
    print(f"   depois de religar: {r}")
    # `and` curto-circuitava: quando o kill caia entre dois envios, isolados=0
    # devolvia False sem nunca rodar a verificacao de integridade -- o teste
    # falhava sem dizer nada, e o invariante de verdade nao era conferido.
    em_voo = isolados >= 1
    ok = integridade(
        "nenhuma duplicata e tentativa ambigua sem reenvio depois do kill -9")
    if em_voo and pausada != "pausada":
        print("   !! havia linha em voo e a campanha nao foi pausada")
        ok = False
    if not em_voo:
        print("   (o kill caiu entre envios: nada estava em voo para isolar)")
    return ok


# ===========================================================================
def teste_entrada_hostil():
    """CSV torto, telefone estranho, emoji, injecao de SQL nos filtros."""
    titulo("4. ENTRADA HOSTIL — csv torto, unicode, injeção de SQL")
    montar_area()
    from disparador import campanhas, importar, modelos, regras, fone
    ok = True

    # -- csv com lixo de verdade
    csv = os.path.join(AREA, "torto.csv")
    with open(csv, "w", encoding="utf-8") as f:
        f.write("telefone;nome;cidade;segmento;status\n")
        f.write("5517991111111;EMPRESA A;RIO PRETO;Comercio;\n")
        f.write("17 99222-2222;EMPRESA \"B\" LTDA;MIRASSOL;Servicos;\n")
        f.write(";SEM TELEFONE;X;Y;\n")
        f.write("abc;NAO E NUMERO;X;Y;\n")
        f.write("5517993333333;EMPRESA C;RIO PRETO;Comercio;DESCARTADO\n")
        f.write("5517991111111;DUPLICADO;RIO PRETO;Comercio;\n")
        f.write("551799444444;MOVEL ANTIGO SEM O 9;X;Y;\n")
        f.write("5517995555555;EMOJI 🚀 LTDA;RIO PRETO;Café & Cia;\n")
        f.write("5517996666666;LINHA;SEM;TODAS;AS;COLUNAS\n")
    st = importar.importar(csv)
    print(f"   csv torto -> {dict(st)}")
    # A, B, MOVEL ANTIGO (ganha o 9), EMOJI, LINHA. C virou bloqueio; sem
    # telefone e 'abc' recusados; a repetida de A deduplicada.
    esperado = 5
    if st["novos"] != esperado:
        print(f"   !! esperava {esperado} novos, veio {st['novos']}")
        ok = False
    if st["descartado"] != 1 or st["duplicados"] != 1 or st["recusado_vazio"] != 2:
        print(f"   !! classificação errada das linhas ruins")
        ok = False
    if not db.um("SELECT 1 FROM contato WHERE telefone='5517999444444'"):
        print("   !! móvel antigo de 8 dígitos não ganhou o 9")
        ok = False
    if not db.um("SELECT 1 FROM bloqueio WHERE telefone='5517993333333'"):
        print("   !! DESCARTADO não virou bloqueio")
        ok = False
    print("   linhas ruins classificadas certo; DESCARTADO virou bloqueio;"
          " móvel de 8 dígitos promovido")

    # -- injecao de SQL nos filtros do publico
    c = campanhas.criar("hostil", "sms", "simulado", "Ola {nome}")
    venenos = ["' OR '1'='1", "'; DROP TABLE contato; --", "%", "_", "\\", "х" * 200]
    for v in venenos:
        try:
            campanhas.contar_publico({"cidade": v})
            campanhas.contar_publico({"segmento": v, "busca": v})
        except Exception as e:
            print(f"   !! filtro {v!r} explodiu: {e}")
            ok = False
    vivo = db.um("SELECT COUNT(*) n FROM contato")["n"]
    print(f"   {len(venenos)} tentativas de injeção -> tabela contato intacta ({vivo} linhas)")
    if vivo != st["novos"]:
        ok = False

    # -- template hostil
    ct = db.um("SELECT * FROM contato LIMIT 1")
    hostis = ["{nao_existe}", "{{aninhado}}", "{a|{b|c}|d}", "🚀" * 500,
              "{" * 50, "}" * 50, "", "{nome}" * 200]
    for h in hostis:
        try:
            t = modelos.montar(h, ct, "rodape")
            modelos.segmentos_sms(t)
        except Exception as e:
            print(f"   !! modelo {h[:18]!r} explodiu: {type(e).__name__}: {e}")
            ok = False
    print(f"   {len(hostis)} modelos hostis renderizados sem exceção")

    # -- telefones hostis
    hostis_fone = [None, "", "0", "+++", "55" * 20, "🚀", "5517991234567@s.whatsapp.net",
                   "  17 9 9123 4567  ", "005517991234567", "17991234567\n"]
    for h in hostis_fone:
        try:
            fone.normalizar(h)
            regras.canonizar(h)
        except Exception as e:
            print(f"   !! telefone {h!r} explodiu: {type(e).__name__}: {e}")
            ok = False
    print(f"   {len(hostis_fone)} telefones hostis normalizados sem exceção")
    print(f"   {'PASSOU' if ok else 'FALHOU'}  entrada hostil")
    return ok


# ===========================================================================
def teste_escala_real(contatos=354000):
    """As operacoes do painel no tamanho real da base."""
    titulo(f"5. ESCALA — {contatos:,} contatos, operações do painel cronometradas")
    montar_area()
    t = time.time()
    popular(contatos)
    print(f"   popular:                {time.time()-t:6.2f}s")
    from disparador import campanhas, servidor
    ok = True

    def crono(rotulo, fn, teto):
        t = time.time()
        r = fn()
        dt = (time.time() - t) * 1000
        marca = "ok" if dt < teto else f"LENTO (>{teto:.0f}ms)"
        print(f"   {rotulo:<24}{dt:7.1f} ms   {marca}")
        return r, dt < teto

    c = campanhas.criar("escala", "sms", "simulado", "Ola {nome}", limite_dia=10**9)
    _, a = crono("contar público", lambda: campanhas.contar_publico(), 1500)
    _, b = crono("contar filtrado", lambda: campanhas.contar_publico({"cidade": "RIO PRETO"}), 1500)
    n, cc = crono("montar fila 50k", lambda: campanhas.montar_publico(c["id"], limite=50000), 5000)
    _, d = crono("/api/estado", lambda: servidor.estado_geral(), 500)
    _, e = crono("/api/estado (2ª)", lambda: servidor.estado_geral(), 500)
    _, f = crono("resumo da campanha", lambda: campanhas.resumo(c["id"]), 500)
    _, g = crono("últimos 60 envios", lambda: db.todos(
        "SELECT * FROM envio ORDER BY id DESC LIMIT 60"), 100)
    print(f"   fila montada: {n:,}")
    tam = os.path.getsize(db.CAMINHO) / 1e6
    print(f"   banco: {tam:.0f} MB  ({tam*1024/contatos:.1f} KB por contato)")
    return all([a, b, cc, d, e, f, g])


# ===========================================================================
def teste_cota_e_janela_no_relogio():
    """Cota diaria e janela conferidas contra o relogio, nao contra a fila."""
    titulo("6. LIMITES — cota diária e janela sob disparo real")
    montar_area()
    popular(2000)
    from disparador import campanhas, regras
    ok = True

    c = campanhas.criar("cota", "sms", "simulado", "Ola {nome}",
                        por_minuto=100000, limite_dia=137)
    campanhas.montar_publico(c["id"])
    db.exec_("UPDATE campanha SET estado='rodando', iniciado_em=? WHERE id=?",
             db.agora(), c["id"])
    from disparador import provedores
    original = provedores.REGISTRO["simulado"]
    provedores.REGISTRO["simulado"] = Instantaneo
    try:
        m = motor_acelerado()
        m.start()
        pico = 0
        for _ in range(200):                     # ate 50s
            time.sleep(0.25)
            pico = max(pico, campanhas.resumo(c["id"]).get("enviado", 0))
            if m.estado["situacao"].startswith("cota"):
                break
        time.sleep(1)                            # deixa passar do limite, se for passar
        m.encerrar()
    finally:
        provedores.REGISTRO["simulado"] = original
    env = campanhas.resumo(c["id"]).get("enviado", 0)
    print(f"   limite_dia=137 -> enviados: {env} (motor: {m.estado['situacao'][:34]})")
    if env != 137:
        print(f"   !! cota não parou em 137")
        ok = False

    # rampa de aquecimento por idade da campanha
    c2 = campanhas.criar("rampa", "whatsapp", "simulado", "oi",
                         meta={"aquecimento": True}, limite_dia=10**6)
    for dia, esperado in enumerate(regras.RAMPA):
        inicio = (datetime.now().astimezone() - timedelta(days=dia)).isoformat(timespec="seconds")
        db.exec_("UPDATE campanha SET iniciado_em=? WHERE id=?", inicio, c2["id"])
        got = regras.limite_efetivo(db.um("SELECT * FROM campanha WHERE id=?", c2["id"]))
        if got != esperado:
            print(f"   !! dia {dia}: esperava {esperado}, veio {got}")
            ok = False
    print(f"   rampa de aquecimento dia 0→7: {regras.RAMPA}  conferida")

    # janela: com a janela fechada nada pode sair
    montar_area(janela_24h=False)
    popular(50)
    c3 = campanhas.criar("janela", "sms", "simulado", "oi",
                         por_minuto=100000, limite_dia=10**9)
    campanhas.montar_publico(c3["id"])
    db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c3["id"])
    aberta, motivo = regras.janela_aberta(config.carregar())
    m = motor_acelerado()
    m._dormir = lambda s: None
    for _ in range(5):
        m._ciclo()
    env3 = campanhas.resumo(c3["id"]).get("enviado", 0)
    agora = datetime.now().strftime("%H:%M de %a")
    if aberta:
        print(f"   janela ABERTA agora ({agora}) -> enviou {env3}, esperado")
        if env3 == 0:
            ok = False
    else:
        print(f"   janela FECHADA agora ({agora}, {motivo}) -> enviou {env3}")
        if env3 != 0:
            print("   !! enviou fora da janela")
            ok = False
    print(f"   {'PASSOU' if ok else 'FALHOU'}  limites")
    return ok


# ===========================================================================
def teste_multichip(chips=25, contatos=8000):
    """25 chips disparando junto: cada um com a propria cota e o proprio ritmo."""
    titulo(f"7. MULTICHIP — {chips} chips, cota e ritmo por chip")
    montar_area()
    popular(contatos)
    from disparador import campanhas, instancias, provedores

    # provedor de mentira que se declara "com instancias", para exercitar o
    # escalonador sem depender de gateway
    class Fake:
        ROTULO, CANAL, AQUECE = "fake", ("whatsapp",), True
        pronto = staticmethod(lambda cfg: (True, ""))
        enviar = staticmethod(lambda d, t, c, m=None: provedores.OK("f_" + d[-6:]))
        listar_instancias = staticmethod(lambda cfg: [])
        diagnosticar = staticmethod(lambda cfg: provedores.diag(ok=True, mensagem="ok"))
    original = provedores.REGISTRO.get("evolution")
    provedores.REGISTRO["evolution"] = Fake
    try:
        COTA = 40
        for i in range(chips):
            db.exec_("INSERT INTO instancia(nome,rotulo,provedor,estado,numero,"
                     "por_minuto,limite_dia,aquecer,conversar,iniciado_em,criado_em)"
                     " VALUES(?,?,'evolution','open',?,?,?,0,0,?,?)",
                     f"chip{i:02d}", f"Chip {i}", f"55179{i:08d}", 6000, COTA,
                     db.agora(), db.agora())
        c = campanhas.criar("multichip", "whatsapp", "evolution", "Oi {nome}",
                            por_minuto=6000, limite_dia=10 ** 9)
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando', iniciado_em=? WHERE id=?",
                 db.agora(), c["id"])
        print(f"   {chips} chips x {COTA}/dia = {chips*COTA} de teto; fila com {contatos:,}")

        m = motor_acelerado()
        m.start()
        for _ in range(120):
            time.sleep(0.5)
            if m.estado["situacao"].startswith(("cota", "nenhum chip")):
                break
        m.encerrar()
    finally:
        if original is not None:
            provedores.REGISTRO["evolution"] = original

    por_chip = {r["nome"]: r["n"] for r in db.todos(
        "SELECT i.nome, COUNT(e.id) n FROM instancia i"
        " LEFT JOIN envio e ON e.instancia_id=i.id AND e.estado='enviado'"
        " GROUP BY i.id ORDER BY i.id")}
    total = sum(por_chip.values())
    estourou = {k: v for k, v in por_chip.items() if v > COTA}
    zerados = [k for k, v in por_chip.items() if v == 0]
    print(f"   enviados: {total} (teto do conjunto: {chips*COTA})")
    print(f"   por chip: min={min(por_chip.values())} max={max(por_chip.values())}")
    ok = True
    if estourou:
        print(f"   !! chips acima da propria cota: {estourou}")
        ok = False
    if zerados:
        print(f"   !! chips que nunca foram usados: {zerados}")
        ok = False
    if total != chips * COTA:
        print(f"   !! esperava {chips*COTA} no total")
        ok = False
    print(f"   {'PASSOU' if ok else 'FALHOU'}  cada chip respeitou a própria cota")
    return integridade("contas fecham com varios chips") and ok


# ===========================================================================
def teste_faixas(chips=25, contatos=60000, derrubar=3):
    """Faixas por chip com 3 chips caindo no meio: nada se perde nem duplica."""
    titulo(f"8. FAIXAS — {chips} chips, {contatos:,} contatos, {derrubar} caem no meio")
    montar_area()
    popular(contatos)
    from disparador import campanhas, instancias, provedores
    from disparador.motor import Motor

    class Fake:
        ROTULO, CANAL, AQUECE = "fake", ("whatsapp",), True
        pronto = staticmethod(lambda cfg: (True, ""))
        enviar = staticmethod(lambda d, t, c, m=None: provedores.OK("f_" + d[-7:]))
        listar_instancias = staticmethod(lambda cfg: [])
        diagnosticar = staticmethod(lambda cfg: provedores.diag(ok=True, mensagem="ok"))
    original = provedores.REGISTRO.get("evolution")
    provedores.REGISTRO["evolution"] = Fake
    ok = True
    try:
        for i in range(chips):
            db.exec_("INSERT INTO instancia(nome,rotulo,provedor,estado,numero,por_minuto,"
                     "limite_dia,aquecer,iniciado_em,visto_ok_em,criado_em)"
                     " VALUES(?,?,'evolution','open',?,6000,120,0,?,?,?)",
                     f"chip{i:02d}", f"Chip {i+1}", f"55179{i:08d}",
                     db.agora(), db.agora(), db.agora())
        c = campanhas.criar("faixas", "whatsapp", "evolution", "Oi {nome}",
                            por_minuto=6000, limite_dia=10 ** 9)
        total = campanhas.montar_publico(c["id"], faixas=True)
        db.exec_("UPDATE campanha SET estado='rodando', iniciado_em=? WHERE id=?",
                 db.agora(), c["id"])
        faixas = campanhas.mapa_faixas(c["id"])
        tam = [f["contatos"] for f in faixas]
        print(f"   {total:,} divididos em {len(faixas)} faixas"
              f" (min {min(tam):,} / max {max(tam):,})")
        if max(tam) - min(tam) > 1:
            print("   !! fatias desiguais"); ok = False
        if sum(tam) != total:
            print("   !! soma das faixas nao bate"); ok = False

        m = Motor()
        m.start()
        time.sleep(3)
        velho = (datetime.now().astimezone() - timedelta(minutes=30)).isoformat(timespec="seconds")
        caidos = [r["id"] for r in db.todos(
            "SELECT id FROM instancia ORDER BY id LIMIT ?", derrubar)]
        db.exec_(f"UPDATE instancia SET estado='close', visto_ok_em=? WHERE id IN"
                 f" ({','.join('?' * len(caidos))})", velho, *caidos)
        print(f"   {derrubar} chips fora do ar ha 30 min")
        for _ in range(40):
            time.sleep(0.5)
            if m.estado["situacao"].startswith(("cota", "nenhum chip")):
                break
        m.encerrar()
    finally:
        if original is not None:
            provedores.REGISTRO["evolution"] = original

    parados = db.um("SELECT COUNT(*) n FROM envio WHERE estado='fila' AND instancia_id IN"
                    f" ({','.join('?' * len(caidos))})", *caidos)["n"]
    enviados = db.um("SELECT COUNT(*) n FROM envio WHERE estado='enviado'")["n"]
    print(f"   enviados: {enviados:,} | fila ainda presa nos chips caidos: {parados}")
    if parados:
        print("   !! faixa dos caidos nao foi redistribuida"); ok = False
    vivas = db.um("SELECT COUNT(*) n FROM instancia WHERE ativa=1")["n"]
    if vivas != chips:
        print(f"   !! {chips - vivas} chip(s) foram desativados sozinhos"); ok = False
    print(f"   {'PASSOU' if ok else 'FALHOU'}  faixas sobrevivem a queda de chip")
    return integridade("contas fecham com faixas e redistribuicao") and ok


# ===========================================================================
if __name__ == "__main__":
    rapido = "--rapido" in sys.argv
    random.seed(1)
    baterias = [
        ("soak", lambda: teste_soak(0.3 if rapido else 1.0, 20000, 8)),
        ("corrida de opt-out", teste_optout_durante_disparo),
        ("queda do processo", teste_queda_do_processo),
        ("entrada hostil", teste_entrada_hostil),
        ("escala real", lambda: teste_escala_real(60000 if rapido else 354000)),
        ("limites", teste_cota_e_janela_no_relogio),
        ("multichip", teste_multichip),
        ("faixas", teste_faixas),
    ]
    resultados = []
    t0 = time.time()
    for nome, fn in baterias:
        try:
            resultados.append((nome, fn()))
        except Exception as e:
            import traceback
            traceback.print_exc()
            resultados.append((nome, False))
        finally:
            if AREA and os.path.isdir(AREA):
                shutil.rmtree(AREA, ignore_errors=True)
    print("\n" + "=" * 68)
    for nome, ok in resultados:
        print(f"  {'PASSOU' if ok else 'FALHOU'}   {nome}")
    print("=" * 68)
    print(f"  {sum(1 for _, o in resultados if o)}/{len(resultados)} baterias "
          f"em {time.time()-t0:.0f}s")
    sys.exit(0 if all(o for _, o in resultados) else 1)
