#!/usr/bin/env python3
"""
O motor: le a fila, aplica as regras e entrega ao provedor -- uma mensagem
por vez, no ritmo da campanha.

Por que serial e nao paralelo: o gargalo aqui nunca e CPU. O ritmo configurado
por remetente precisa ser previsivel, auditavel e respeitar a cota diaria.

O claim e atomico (UPDATE ... RETURNING): mesmo com dois motores rodando por
engano, ninguem pega a mesma linha duas vezes.
"""
import random, threading, time, uuid
from datetime import datetime, timedelta

from . import campanhas, config, conversas, db, instancias, modelos, provedores, regras

ESPERA_OCIOSO = 3.0          # sem fila: nao queimar CPU
TICK = 0.5                   # com chips esperando o proprio ritmo
SINCRONIZAR_A_CADA = 20.0    # espelhar o estado dos chips do gateway
BACKOFF = [60, 300, 1800]    # 1min, 5min, 30min entre as tentativas
MAX_TENTATIVAS = 3


class Motor(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="motor")
        self.parar = threading.Event()
        self.rnd = random.Random()
        # ritmo por REMETENTE (chip), nao por campanha: e o chip que toma ban,
        # e dois chips podem mandar ao mesmo tempo sem se atrapalhar
        self.proximo = {}
        self._cache_img = {}          # id -> (base64, mime, nome), lido uma vez
        self.sincronizado_em = 0.0
        self.estado = {"situacao": "parado", "campanha": None, "ultimo": None,
                       "proximo_em": None, "enviados_sessao": 0, "erros_sessao": 0,
                       "chips": 0}

    # -- ciclo de vida --------------------------------------------------------
    def run(self):
        db.recuperar_orfaos()
        self._marcar("ocioso")
        while not self.parar.is_set():
            try:
                self._ciclo()
            except Exception as e:                     # nunca derrubar a thread
                db.log("erro_motor", f"{type(e).__name__}: {e}")
                self._marcar(f"erro: {e}")
                self._dormir(5)

    def encerrar(self, espera=15):
        """Sinaliza a parada e espera a mensagem em voo terminar.

        Sem o join, um 'ligar' logo apos o 'parar' podia criar um segundo motor
        enquanto o primeiro ainda gravava o envio -- e a linha saia duas vezes.
        """
        self.parar.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=espera)
        self._marcar("parado")

    def _marcar(self, situacao, **kw):
        self.estado["situacao"] = situacao
        self.estado.update(kw)

    def _dormir(self, seg):
        """Sono interrompivel: parar() responde na hora."""
        self.estado["proximo_em"] = (datetime.now() + timedelta(seconds=seg)).isoformat(timespec="seconds")
        self.parar.wait(seg)

    # -- um ciclo -------------------------------------------------------------
    def _ciclo(self):
        cfg = config.carregar(recarregar=True)

        aberta, motivo = regras.janela_aberta(cfg)
        if not aberta:
            reabre = regras.proxima_abertura(cfg)
            self._marcar(f"aguardando janela ({motivo})",
                         proximo_em=reabre.isoformat(timespec="seconds"))
            # acorda de minuto em minuto: a janela pode mudar no config
            self._dormir(min(60, max(5, (reabre - datetime.now()).total_seconds())))
            return

        # espelhar o estado dos chips de tempos em tempos (nao a cada tique)
        if time.time() - self.sincronizado_em > SINCRONIZAR_A_CADA:
            try:
                instancias.sincronizar(cfg)
            except Exception as e:
                db.log("erro_sincronizar", f"{type(e).__name__}: {e}")
            self.sincronizado_em = time.time()

        ativas = db.todos("SELECT * FROM campanha WHERE estado='rodando' ORDER BY id")
        if not ativas:
            self._marcar("ocioso", chips=len(instancias.prontas()))
            self._dormir(ESPERA_OCIOSO)
            return

        trabalhou, cotadas, sem_chip = False, [], False
        for camp in ativas:
            if self.parar.is_set():
                return
            # Um ciclo com varias campanhas em ritmo lento leva minutos: a
            # janela conferida la em cima pode ja ter fechado. Reconferir antes
            # de cada envio e o que impede a mensagem de sair 19h05.
            if not regras.janela_aberta(cfg)[0]:
                break
            if camp["provedor"] not in provedores.REGISTRO:
                # provedor apagado ou digitado errado: sem isso a campanha
                # ficava girando em "nenhum chip disponivel", que e outra coisa
                db.exec_("UPDATE campanha SET estado='pausada' WHERE id=?", camp["id"])
                db.log("campanha_pausada",
                       f"provedor '{camp['provedor']}' nao existe", campanha_id=camp["id"])
                continue
            if regras.cota_restante(camp) <= 0:
                cotadas.append(f"{camp['nome']} ({regras.limite_efetivo(camp)}/dia)")
                continue

            # Faixa de chip que saiu do ar nao pode ficar parada para sempre.
            # A carencia de 10 min (ou ban confirmado) decide a hora.
            for inst, quantos, ja in instancias.orfas(camp["id"]):
                if ja and inst is not None:
                    movidas = instancias.redistribuir(
                        camp["id"], inst["id"],
                        "banido" if instancias.banido(inst) else "fora do ar")
                    if movidas:
                        self._marcar(f"faixa de {inst['rotulo'] or inst['nome']}"
                                     f" redistribuída ({movidas})")
                elif ja:
                    instancias.redistribuir(camp["id"], None, "chip removido")

            remetentes = instancias.remetentes(camp, cfg)
            if not remetentes:
                sem_chip = True
                self._concluir_se_vazia(camp)
                continue

            for rem in remetentes:
                if self.parar.is_set():
                    return
                if not self._pode_agora(rem):
                    continue                 # este chip ainda esta no intervalo
                envio = self._reivindicar(camp, rem)
                if envio is None:
                    continue                 # a faixa DELE acabou; outros seguem
                trabalhou = True
                self._marcar("enviando", campanha=camp["nome"],
                             chips=len(remetentes))
                if self._processar(envio, camp, rem, cfg):
                    self._agendar(rem)       # so o envio que saiu paga o ritmo
            self._concluir_se_vazia(camp)

        if not trabalhou:
            if cotadas:
                self._marcar(f"cota do dia atingida: {', '.join(cotadas)}")
            elif sem_chip:
                self._marcar("nenhum chip disponível (conectado e com cota)")
            else:
                self._marcar("aguardando o ritmo dos chips")
            self._dormir(30 if (cotadas or sem_chip) else TICK)

    def _imagem(self, ident):
        """Base64 da imagem, lido do disco uma vez e guardado em memoria.

        Sem o cache, uma campanha de 900 disparos releria e recodificaria o
        mesmo arquivo 900 vezes. Com ele, o disco e tocado uma vez por foto.
        """
        if ident not in self._cache_img:
            from . import midia
            self._cache_img[ident] = midia.base64_de(ident)
        return self._cache_img[ident]

    def _pode_agora(self, rem):
        return time.time() >= self.proximo.get(rem.id, 0.0)

    def _agendar(self, rem):
        """Proximo envio deste chip: 60/por_minuto com +-35% de jitter.

        Cadencia identica e assinatura de bot; o jitter e o que faz o ritmo
        parecer humano sem depender de sorte.
        """
        base = 60.0 / max(0.1, float(rem.por_minuto))
        self.proximo[rem.id] = time.time() + base * self.rnd.uniform(0.65, 1.35)

    def _reivindicar(self, campanha, rem=None):
        """Reserva fila e cotas no mesmo BEGIN IMMEDIATE.

        Aceita o id por compatibilidade com ferramentas locais; o motor passa
        a linha da campanha e o remetente para reservar tambem a cota do chip.
        """
        camp = (db.um("SELECT * FROM campanha WHERE id=?", int(campanha))
                if isinstance(campanha, (int, str)) else campanha)
        if not camp:
            return None
        c, agora = db.conn(), db.agora()
        c.execute("BEGIN IMMEDIATE")
        try:
            estados = regras.ENTREGUES + ("enviando", "indeterminado")
            lugares = ",".join("?" * len(estados))
            usados = c.execute(
                f"SELECT COUNT(*) n FROM envio WHERE campanha_id=? "
                f"AND estado IN ({lugares}) AND COALESCE(enviado_em,tentativa_em)>=?",
                (camp["id"], *estados, regras.inicio_do_dia())).fetchone()["n"]
            if usados >= regras.limite_efetivo(camp):
                c.execute("ROLLBACK")
                return None
            instancia_id = getattr(rem, "id", None) if rem is not None else None
            if instancia_id is not None:
                inst = c.execute("SELECT * FROM instancia WHERE id=?", (instancia_id,)).fetchone()
                usados_chip = c.execute(
                    f"SELECT COUNT(*) n FROM envio WHERE instancia_id=? "
                    f"AND estado IN ({lugares}) AND COALESCE(enviado_em,tentativa_em)>=?",
                    (instancia_id, *estados, regras.inicio_do_dia())).fetchone()["n"]
                if not inst or usados_chip >= instancias.limite_efetivo(inst):
                    c.execute("ROLLBACK")
                    return None
            # Com faixas, a linha ja nasce reservada: o chip so pode pegar o
            # que e dele (ou o que nao tem dono, no modo rodizio).
            if instancia_id is None:
                dono, dono_par = "", ()
            else:
                dono, dono_par = " AND (instancia_id IS NULL OR instancia_id=?)", (instancia_id,)
            cur = c.execute(
                "UPDATE envio SET estado='enviando', instancia_id=?, tentativa_em=?,"
                " chave_idempotencia=COALESCE(chave_idempotencia,?), atualizado_em=?"
                " WHERE id=(SELECT id FROM envio WHERE campanha_id=? AND estado='fila'"
                f"           AND (agendado_para IS NULL OR agendado_para<=?){dono}"
                "           ORDER BY id LIMIT 1) RETURNING *",
                (instancia_id, agora, uuid.uuid4().hex, agora, camp["id"], agora,
                 *dono_par))
            envio = cur.fetchone()
            c.execute("COMMIT")
            return envio
        except BaseException:
            if c.in_transaction:
                c.execute("ROLLBACK")
            raise

    def _concluir_se_vazia(self, camp):
        r = db.um("SELECT COUNT(*) n FROM envio WHERE campanha_id=?"
                  " AND estado IN ('fila','enviando')", camp["id"])
        if r and r["n"] == 0:
            db.exec_("UPDATE campanha SET estado='concluida' WHERE id=?", camp["id"])
            db.log("campanha_concluida", campanhas.resumo(camp["id"]), campanha_id=camp["id"])

    # -- uma mensagem ---------------------------------------------------------
    def _processar(self, envio, camp, rem, cfg):
        """Devolve True se a mensagem chegou a ser entregue ao provedor."""
        ok, motivo = regras.liberar(envio, cfg)
        if not ok:
            self._fechar(envio, "bloqueado", erro=motivo)
            db.log("envio_bloqueado", motivo, telefone=envio["telefone"],
                   campanha_id=camp["id"])
            return False

        contato = db.um("SELECT * FROM contato WHERE id=?", envio["contato_id"])
        texto = modelos.montar(camp["modelo"], contato, campanhas.rodape(camp["canal"]),
                               self.rnd, campanhas.sem_acento(camp))
        meta = db.meta(camp)
        meta["canal"] = camp["canal"]
        meta["idempotency_key"] = envio["chave_idempotencia"]
        if meta.get("imagem"):
            b64, mime, nome = self._imagem(meta["imagem"])
            if b64 is None:
                # imagem sumiu do disco no meio da campanha: nao manda so o
                # texto no lugar (mudaria a mensagem sem avisar) -- pausa.
                return self._pausar(envio, camp, "a imagem da campanha nao esta mais disponivel")
            meta["imagem_b64"], meta["imagem_mime"], meta["imagem_nome"] = b64, mime, nome
        if meta.get("params"):    # Cloud API: parametros do template tambem sao modelo
            meta = dict(meta, params=[modelos.montar(p, contato, "", self.rnd)
                                      for p in meta["params"]])

        # a credencial do provedor + o que identifica ESTE chip
        cred = dict(cfg["provedores"].get(camp["provedor"], {}), **rem.extra)
        try:
            prov = provedores.pegar(camp["provedor"])
            pronto, falta = prov.pronto(cred)
        except KeyError as e:
            return self._pausar(envio, camp, str(e))
        if not pronto:
            # com varios chips, um fora do ar nao pode pausar a campanha
            # inteira: desativa so ele e segue pelos outros
            if rem.id is not None:
                db.exec_("UPDATE instancia SET estado='close' WHERE id=?", rem.id)
                db.exec_("UPDATE envio SET estado='fila', atualizado_em=? WHERE id=?",
                         db.agora(), envio["id"])
                db.log("chip_indisponivel", f"{rem.nome}: {falta}")
                return False
            return self._pausar(envio, camp, f"provedor indisponível: {falta}")

        try:
            r = prov.enviar(envio["telefone"], texto, cred, meta)
        except Exception as e:
            return self._indeterminado(envio, camp, f"{type(e).__name__}: {e}")

        if r.ok:
            conta_externa = rem.nome
            if camp["provedor"] == "whatsapp_cloud":
                conta_externa = cred.get("phone_id")
            elif camp["provedor"] == "twilio":
                conta_externa = cred.get("sid")
            fechado = self._fechar(
                envio, "enviado", texto=texto, id_externo=r.id_externo,
                enviado_em=db.agora(), instancia_id=rem.id,
                provedor_externo=camp["provedor"], conta_externa=conta_externa)
            if not fechado:
                db.log("aceite_apos_cancelamento",
                       {"id_externo": r.id_externo, "chave": envio["chave_idempotencia"]},
                       telefone=envio["telefone"], campanha_id=camp["id"])
                return True
            if rem.id is not None:
                # a campanha abre a conversa: e o que o cliente vai responder
                conversas.registrar(rem.nome, envio["telefone"], "saida", texto,
                                    "imagem" if meta.get("imagem_b64") else "texto",
                                    r.id_externo, envio_id=envio["id"],
                                    origem="campanha", instancia_id=rem.id)
            self.estado["enviados_sessao"] += 1
            self.estado["ultimo"] = {"telefone": envio["telefone"], "quando": db.agora(),
                                     "ok": True, "campanha": camp["nome"],
                                     "chip": rem.rotulo}
            return True

        self.estado["erros_sessao"] += 1
        self.estado["ultimo"] = {"telefone": envio["telefone"], "quando": db.agora(),
                                 "ok": False, "erro": r.erro, "campanha": camp["nome"],
                                 "chip": rem.rotulo}
        tent = envio["tentativas"] + 1

        if getattr(r, "indeterminado", False):
            return self._indeterminado(envio, camp, r.erro)

        if r.pausar:
            # erro da campanha: devolve o lead intacto e chama o operador
            return self._pausar(envio, camp, r.erro)

        if r.fatal:
            # numero morto: para de tentar e tira da base para nao voltar
            self._fechar(envio, "falha", texto=texto, erro=r.erro, tentativas=tent)
            regras.registrar_optout(envio["telefone"], origem=camp["provedor"],
                                    motivo="erro_fatal")
            return True

        if tent >= MAX_TENTATIVAS:
            self._fechar(envio, "falha", texto=texto, erro=r.erro, tentativas=tent)
            return True

        volta = (datetime.now().astimezone()
                 + timedelta(seconds=BACKOFF[min(tent - 1, len(BACKOFF) - 1)]))
        db.exec_("UPDATE envio SET estado='fila', tentativas=?, erro=?,"
                 " agendado_para=?, atualizado_em=? WHERE id=? AND estado='enviando'",
                 tent, r.erro, volta.isoformat(timespec="seconds"), db.agora(), envio["id"])
        return True

    def _pausar(self, envio, camp, motivo):
        """Problema de configuracao, nao do lead: devolve a linha e para a campanha.

        Sem pausar, o motor reivindica a proxima linha no ciclo seguinte e repete
        o erro ate esvaziar a fila -- gastando os leads sem enviar nada.
        """
        db.exec_("UPDATE envio SET estado='fila', tentativas=?, erro=?, atualizado_em=?"
                 " WHERE id=? AND estado='enviando'", envio["tentativas"], motivo[:500],
                 db.agora(), envio["id"])
        db.exec_("UPDATE campanha SET estado='pausada' WHERE id=?", camp["id"])
        db.log("campanha_pausada", f"{camp['provedor']}: {motivo}", campanha_id=camp["id"])
        self._marcar(f"pausada: {camp['nome']}")
        return False

    def _indeterminado(self, envio, camp, motivo):
        db.exec_("UPDATE envio SET estado='indeterminado', erro=?, atualizado_em=? "
                 "WHERE id=? AND estado='enviando'", str(motivo)[:500], db.agora(),
                 envio["id"])
        db.exec_("UPDATE campanha SET estado='pausada' WHERE id=?", camp["id"])
        db.log("envio_indeterminado",
               {"erro": str(motivo)[:500], "chave": envio["chave_idempotencia"]},
               telefone=envio["telefone"], campanha_id=camp["id"])
        self._marcar(f"revisao manual: {camp['nome']}")
        return False

    def _fechar(self, envio, estado, **campos):
        campos["estado"] = estado
        campos["atualizado_em"] = db.agora()
        cols = ", ".join(f"{k}=?" for k in campos)
        cur = db.exec_(f"UPDATE envio SET {cols} WHERE id=? AND estado='enviando'",
                       *campos.values(), envio["id"])
        return cur.rowcount == 1


# --- instancia unica ---------------------------------------------------------
MOTOR = None


def ligar():
    global MOTOR
    if MOTOR is None or not MOTOR.is_alive():
        MOTOR = Motor()
        MOTOR.start()
    return MOTOR


def situacao():
    if MOTOR is None or not MOTOR.is_alive():
        return {"situacao": "parado"}
    return dict(MOTOR.estado)


if __name__ == "__main__":
    m = ligar()
    print("motor rodando (ctrl+c para parar)")
    try:
        while m.is_alive():
            s = situacao()
            print(f"\r  {s['situacao']:<40} enviados={s.get('enviados_sessao',0)} "
                  f"erros={s.get('erros_sessao',0)}   ", end="")
            time.sleep(1)
    except KeyboardInterrupt:
        m.encerrar()
        print("\nparado.")
