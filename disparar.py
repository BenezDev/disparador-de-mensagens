#!/usr/bin/env python3
"""
Disparador de mensagens (SMS / WhatsApp) — ponto de entrada.

  python3 disparar.py painel               abre o painel em http://127.0.0.1:8777
  python3 disparar.py importar <arquivo>   carrega o csv de leads
  python3 disparar.py rodar                so o motor, sem painel (servidor/cron)
  python3 disparar.py status               resumo no terminal
  python3 disparar.py chips                lista os chips e a capacidade do dia
  python3 disparar.py bloquear <num...>    bloqueia numeros (aceita arquivo)
"""
import sys, time

from disparador import (campanhas, config, db, importar, instancias,
                        motor, regras, servidor)


def cmd_painel(args):
    porta = int(args[0]) if args else 8777
    servidor.servir(porta)


def cmd_importar(args):
    if not args:
        sys.exit("uso: disparar.py importar <arquivo.csv> [--com-fixos] [--limite N]")
    limite = int(args[args.index("--limite") + 1]) if "--limite" in args else None
    st = importar.importar(args[0], somente_movel="--com-fixos" not in args,
                           limite=limite,
                           progresso=lambda n, s: print(f"\r  {n:>9,} linhas lidas...", end=""))
    print("\r" + " " * 40)
    for k, v in sorted(st.items()):
        print(f"  {k:<22} {v:>9,}")


def cmd_rodar(args):
    m = motor.ligar()
    print("motor rodando — ctrl+c para parar")
    try:
        while m.is_alive():
            s = motor.situacao()
            print(f"\r  {s['situacao'][:52]:<54} enviados={s.get('enviados_sessao', 0)} "
                  f"erros={s.get('erros_sessao', 0)}   ", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        m.encerrar()
        print("\nparado.")


def cmd_status(args):
    e = servidor.estado_geral()
    print(f"\n  contatos {e['contatos']:,}  (celulares {e['moveis']:,})"
          f"  bloqueios {e['bloqueios']:,}  enviados hoje {e['hoje']:,}")
    j = e["janela"]
    print(f"  janela: {'ABERTA' if j['aberta'] else j['motivo'] + ' — reabre ' + j['reabre']}")
    print(f"  motor:  {e['motor'].get('situacao')}")
    ch = e.get("chips") or {}
    if ch.get("total"):
        print(f"  chips:  {ch['conectados']}/{ch['total']} conectados · "
              f"{ch['capacidade_hoje']:,}/dia · rampa sem trafego artificial")
    if e["envios"]:
        print("\n  fila: " + "  ".join(f"{k}={v:,}" for k, v in sorted(e["envios"].items())))
    print("\n  campanhas:")
    for c in db.todos("SELECT * FROM campanha ORDER BY id"):
        r = campanhas.resumo(c["id"])
        print(f"   #{c['id']:<3} {c['nome'][:26]:<28} {c['canal']:<9} {c['estado']:<10}"
              f" fila={r.get('fila', 0):<7,} enviado={r.get('enviado', 0):<7,}"
              f" falha={r.get('falha', 0):,}")
    print("\n  provedores:")
    for n, p in e["provedores"].items():
        marca = "ensaio" if p["simula"] else ("ok" if p["pronto"] else "--")
        print(f"   {marca:<7}{n:<16} {p['mensagem']}")
        if not p["pronto"] and p.get("acao"):
            print(f"          -> {p['acao']}")
    print()


def cmd_chips(args):
    """Lista os chips; `chips sincronizar` espelha o gateway antes."""
    if args and args[0] == "sincronizar":
        achadas, novas = instancias.sincronizar(config.carregar(True))
        print(f"  {achadas} instancia(s) no gateway, {novas} nova(s)")
    chips = instancias.resumo()
    if not chips:
        return print("  nenhum chip. Crie pelo painel, aba 'chips'.")
    conectados = sum(1 for c in chips if c["conectada"])
    cap = instancias.capacidade_diaria()
    base = db.um("SELECT COUNT(*) n FROM contato WHERE tipo='MOVEL'")["n"]
    print(f"\n  {conectados} de {len(chips)} conectados  |  capacidade hoje "
          f"{cap:,}/dia" + (f"  |  base escoa em {-(-base // cap)} dias" if cap else ""))
    print("  rampa: limite progressivo; conversas artificiais desativadas\n")
    for c in chips:
        marca = "on " if c["conectada"] else ("-- " if c["ativa"] else "off")
        rampa = f"dia {c['dia_rampa']}" if c["aquecer"] else "sem rampa"
        print(f"   {marca} {(c['rotulo'] or c['nome'])[:18]:<20} {str(c['estado'] or '-'):<11}"
              f" {c['enviados_hoje']:>4}/{c['cota_hoje']:<5} {rampa:<9}"
              f" {c['por_minuto']}/min  {c['numero'] or ''}")
    print()


def cmd_bloquear(args):
    from disparador.fone import normalizar
    alvos = []
    for a in args:
        try:
            with open(a, encoding="utf-8") as f:
                alvos += [l.strip() for l in f if l.strip()]
        except OSError:
            alvos.append(a)
    n = 0
    for t in alvos:
        e164, _ = normalizar(t)
        if e164:
            regras.registrar_optout(e164, origem="cli", motivo="manual")
            n += 1
    print(f"  {n} de {len(alvos)} numero(s) bloqueado(s)")


COMANDOS = {"painel": cmd_painel, "importar": cmd_importar, "rodar": cmd_rodar,
            "status": cmd_status, "bloquear": cmd_bloquear, "chips": cmd_chips}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "painel"
    if cmd not in COMANDOS:
        sys.exit(__doc__)
    db.conn()
    COMANDOS[cmd](sys.argv[2:])
