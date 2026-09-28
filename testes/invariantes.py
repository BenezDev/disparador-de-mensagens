#!/usr/bin/env python3
"""
Testes das invariantes que impedem o disparador de causar dano.

Nao testam "o codigo roda" -- testam as promessas que o sistema faz ao operador
e ao destinatario: ninguem recebe duas vezes, quem pediu para sair nunca mais
recebe, e nada sai fora da janela ou acima da cota.

    python3 testes.py
"""
import json, os, sys, tempfile, threading, unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import disparador.db as db
import disparador.config as config
from disparador import (aquecimento, campanhas, fone, importar, instancias, modelos,
                        provedores, regras, seguranca)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.admin_token = "token-de-teste-local-com-mais-de-32-caracteres"
        os.environ["DISPARADOR_ADMIN_TOKEN"] = self.admin_token
        db.CAMINHO = os.path.join(self.tmp, "t.db")
        db._local = threading.local()
        self.cfg_arq = os.path.join(self.tmp, "config.json")
        with open(self.cfg_arq, "w", encoding="utf-8") as f:
            json.dump({"janela": {"inicio": "00:00", "fim": "23:59",
                                  "sabado_inicio": "00:00", "sabado_fim": "23:59",
                                  "domingo": True},
                       "cooldown_dias": 45,
                       "provedores": {"simulado": {"falha_pct": 0}}}, f)
        config.CAMINHO, config._cache = self.cfg_arq, None
        db.conn()

    def contatos(self, n, cidade="RIO PRETO", consentimento=True):
        for i in range(n):
            db.exec_("INSERT INTO contato(telefone,tipo,nome,cidade,segmento,"
                     "consentimento_em,consentimento_origem,criado_em)"
                     " VALUES(?,?,?,?,?,?,?,?)",
                     f"5517990{i:06d}", "MOVEL", f"EMPRESA {i} LTDA", cidade,
                     "Comercio varejista", db.agora() if consentimento else None,
                     "formulario:testes" if consentimento else None, db.agora())

    def motor(self):
        """Motor com o sono desligado -- o teste nao espera o ritmo real."""
        from disparador.motor import Motor
        m = Motor()
        m._dormir = lambda seg: None
        return m

    def campanha(self, **kw):
        d = dict(nome="t", canal="sms", provedor="simulado",
                 modelo="Ola, {nome}!", por_minuto=6000, limite_dia=1000)
        d.update(kw)
        return campanhas.criar(**d)


class TelefoneTest(Base):
    def test_formas_equivalentes_viram_o_mesmo_numero(self):
        formas = ["5517991234567", "17991234567", "017991234567",
                  "+55 (17) 99123-4567", "55 17 99123 4567"]
        self.assertEqual({fone.normalizar(f)[0] for f in formas}, {"5517991234567"})

    def test_movel_antigo_ganha_o_nono_digito(self):
        self.assertEqual(fone.normalizar("1791234567")[0], "5517991234567")

    def test_fixo_e_identificado_e_nao_vira_movel(self):
        self.assertEqual(fone.normalizar("1732345678"), ("551732345678", "FIXO"))

    def test_lixo_da_base_publica_e_recusado(self):
        for ruim in ("", "11111111111", "0800123456", "179912345", "5500991234567"):
            self.assertIsNone(fone.normalizar(ruim)[0], ruim)


class OptOutTest(Base):
    def test_pedido_de_saida_e_reconhecido(self):
        c = config.carregar()
        for t in ("SAIR", "sair", "Pare!", "nao quero receber", "me tire da lista",
                  "PARAR", "descadastrar", "nao tenho interesse"):
            self.assertTrue(regras.detectar_optout(t, c), t)

    def test_resposta_positiva_nao_e_confundida_com_saida(self):
        c = config.carregar()
        for t in ("quero sim, manda a proposta para amanha",
                  "pode mandar para o meu socio",
                  "obrigado, vou parar para pensar e te retorno",
                  "qual o valor para 100 unidades?"):
            self.assertFalse(regras.detectar_optout(t, c), t)

    def test_saida_cancela_o_que_ja_estava_na_fila(self):
        self.contatos(3)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        tel = db.um("SELECT telefone FROM envio LIMIT 1")["telefone"]
        regras.registrar_optout(tel, origem="teste")
        self.assertEqual(db.um("SELECT estado FROM envio WHERE telefone=?", tel)["estado"],
                         "cancelado")

    def test_bloqueado_nunca_mais_entra_em_publico(self):
        self.contatos(3)
        tel = "5517990000000"
        regras.registrar_optout(tel, origem="teste")
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        self.assertIsNone(db.um("SELECT 1 FROM envio WHERE telefone=?", tel))


class FilaTest(Base):
    def test_ninguem_recebe_duas_vezes_na_mesma_campanha(self):
        self.contatos(5)
        c = self.campanha()
        self.assertEqual(campanhas.montar_publico(c["id"]), 5)
        self.assertEqual(campanhas.montar_publico(c["id"]), 0)   # rodar de novo nao duplica
        self.assertEqual(db.um("SELECT COUNT(*) n FROM envio")["n"], 5)

    def test_quem_esta_na_fila_de_uma_campanha_nao_entra_em_outra(self):
        self.contatos(4)
        a, b = self.campanha(nome="a"), self.campanha(nome="b")
        self.assertEqual(campanhas.montar_publico(a["id"]), 4)
        self.assertEqual(campanhas.montar_publico(b["id"]), 0)

    def test_cooldown_segura_quem_recebeu_ha_pouco(self):
        self.contatos(2)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE envio SET estado='enviado', enviado_em=?", db.agora())
        c2 = self.campanha(nome="depois")
        self.assertEqual(campanhas.montar_publico(c2["id"]), 0)
        # passado o cooldown, volta a ser publico
        velho = (datetime.now().astimezone() - timedelta(days=60)).isoformat(timespec="seconds")
        db.exec_("UPDATE envio SET enviado_em=? WHERE campanha_id=?", velho, c["id"])
        self.assertEqual(campanhas.montar_publico(c2["id"]), 2)

    def test_claim_e_atomico_entre_threads(self):
        """Dois motores em paralelo nao podem pegar a mesma linha."""
        self.contatos(40)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        from disparador.motor import Motor
        pegos, trava = [], threading.Lock()

        def worker():
            db._local = threading.local()      # conexao propria, como no motor real
            m = Motor()
            while True:
                e = m._reivindicar(c["id"])
                if not e:
                    return
                with trava:
                    pegos.append(e["id"])

        ts = [threading.Thread(target=worker) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(pegos), 40)
        self.assertEqual(len(set(pegos)), 40)   # nenhum id saiu duas vezes


class JanelaTest(Base):
    def test_fora_do_horario_a_janela_fecha(self):
        cfg = {"janela": {"inicio": "09:00", "fim": "19:00", "sabado_inicio": "09:00",
                          "sabado_fim": "13:00", "domingo": False}}
        seg10 = datetime(2026, 8, 17, 10, 0)      # segunda
        seg21 = datetime(2026, 8, 17, 21, 0)
        sab15 = datetime(2026, 8, 22, 15, 0)      # sabado a tarde
        dom11 = datetime(2026, 8, 23, 11, 0)      # domingo
        self.assertTrue(regras.janela_aberta(cfg, seg10)[0])
        self.assertFalse(regras.janela_aberta(cfg, seg21)[0])
        self.assertFalse(regras.janela_aberta(cfg, sab15)[0])
        self.assertFalse(regras.janela_aberta(cfg, dom11)[0])
        # e a proxima abertura pula o domingo inteiro
        self.assertEqual(regras.proxima_abertura(cfg, dom11), datetime(2026, 8, 24, 9, 0))


class CotaTest(Base):
    def test_cota_diaria_limita_o_envio(self):
        self.contatos(10)
        c = self.campanha(limite_dia=4)
        campanhas.montar_publico(c["id"])
        self.assertEqual(regras.cota_restante(c), 4)
        db.exec_("UPDATE envio SET estado='enviado', enviado_em=? WHERE id IN"
                 " (SELECT id FROM envio LIMIT 4)", db.agora())
        self.assertEqual(regras.cota_restante(c), 0)

    def test_aquecimento_limita_mais_que_a_cota_no_primeiro_dia(self):
        c = self.campanha(limite_dia=5000, meta={"aquecimento": True})
        db.exec_("UPDATE campanha SET iniciado_em=? WHERE id=?", db.agora(), c["id"])
        c = db.um("SELECT * FROM campanha WHERE id=?", c["id"])
        self.assertEqual(regras.limite_efetivo(c), regras.RAMPA[0])
        antigo = (datetime.now().astimezone() - timedelta(days=4)).isoformat(timespec="seconds")
        db.exec_("UPDATE campanha SET iniciado_em=? WHERE id=?", antigo, c["id"])
        c = db.um("SELECT * FROM campanha WHERE id=?", c["id"])
        self.assertEqual(regras.limite_efetivo(c), regras.RAMPA[4])


class MotorTest(Base):
    def test_ciclo_completo_respeita_cota_e_marca_enviado(self):
        self.contatos(12)
        c = self.campanha(limite_dia=5)
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando', iniciado_em=? WHERE id=?",
                 db.agora(), c["id"])
        from disparador.motor import Motor
        m = Motor()
        for _ in range(12):
            m._ciclo()
            if regras.enviados_hoje(c["id"]) >= 5:
                break
        r = campanhas.resumo(c["id"])
        self.assertEqual(r.get("enviado"), 5)
        self.assertEqual(r.get("fila"), 7)

    def test_falha_do_provedor_reagenda_e_desiste_no_limite(self):
        self.contatos(1)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])
        cfg = config.carregar()
        cfg["provedores"]["simulado"]["falha_pct"] = 100
        from disparador.motor import Motor, MAX_TENTATIVAS
        m = Motor()
        for i in range(MAX_TENTATIVAS):
            e = m._reivindicar(c["id"])
            self.assertIsNotNone(e, f"tentativa {i}")
            camp = db.um("SELECT * FROM campanha WHERE id=?", c["id"])
            rem = instancias.remetentes(camp, cfg)[0]
            m._processar(e, camp, rem, cfg)
            db.exec_("UPDATE envio SET agendado_para=NULL")   # encurta o backoff
        self.assertEqual(db.um("SELECT estado FROM envio")["estado"], "falha")

    def test_provedor_sem_credencial_pausa_a_campanha_sem_perder_o_lead(self):
        self.contatos(1)
        c = self.campanha(provedor="twilio")
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])
        from disparador.motor import Motor
        m = Motor()
        e = m._reivindicar(c["id"])
        camp = db.um("SELECT * FROM campanha WHERE id=?", c["id"])
        cfg = config.carregar()
        rem = instancias.remetentes(camp, cfg)[0]
        m._processar(e, camp, rem, cfg)
        self.assertEqual(db.um("SELECT estado FROM envio")["estado"], "fila")
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "pausada")

    def test_queda_do_processo_isola_o_resultado_ambiguo(self):
        self.contatos(3)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])
        ids = [r["id"] for r in db.todos("SELECT id FROM envio ORDER BY id")]
        antigo = (datetime.now().astimezone() - db.IDADE_ORFAO
                  - timedelta(minutes=1)).isoformat(timespec="seconds")
        db.exec_("UPDATE envio SET estado='enviando', atualizado_em=? WHERE id=?",
                 antigo, ids[0])
        db.exec_("UPDATE envio SET estado='enviando', id_externo='x1', atualizado_em=?"
                 " WHERE id=?", antigo, ids[1])
        # o terceiro esta saindo AGORA, em outro processo: nao pode ser tocado
        db.exec_("UPDATE envio SET estado='enviando', atualizado_em=? WHERE id=?",
                 db.agora(), ids[2])
        db.recuperar_orfaos()
        self.assertEqual(db.um("SELECT estado FROM envio WHERE id=?", ids[0])["estado"],
                         "indeterminado")
        self.assertEqual(db.um("SELECT estado FROM envio WHERE id=?", ids[1])["estado"], "enviado")
        self.assertEqual(db.um("SELECT estado FROM envio WHERE id=?", ids[2])["estado"],
                         "enviando", "recuperacao roubou a mensagem em voo")
        self.assertEqual(db.um("SELECT estado FROM campanha WHERE id=?", c["id"])["estado"],
                         "pausada")


class ImportarTest(Base):
    """Importar o CSV que o Dr. Hery Leads exporta, do jeito que ele exporta."""

    CAB = ("telefone;tipo;nome;cnpj;cidade;bairro;regiao_ddd17;segmento;cnae;"
           "situacao;porte;mei;email;logradouro;cep;abertura;status;fonte")

    def _csv(self, linhas):
        import tempfile
        fd, caminho = tempfile.mkstemp(suffix=".csv", dir=self.tmp)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(self.CAB + "\r\n")
            for l in linhas:
                f.write(";".join(f'"{c}"' for c in l) + "\r\n")
        return caminho

    def linha(self, tel, tipo="MOVEL", nome="LOJA", status="", **kw):
        return [tel, tipo, nome, "1"*14, "RIO PRETO", "CENTRO", "SIM",
                "Comercio", "4781400", "ATIVA", "MICRO", "NAO",
                "l@x.com", "RUA A", "15000000", "20200101", status, "RF"]

    def test_formato_drhery_importa_sem_ajuste(self):
        arq = self._csv([
            self.linha("5517991234567", "MOVEL"),
            self.linha("551732345678", "FIXO"),            # fixo fora
            self.linha("5517988887777", "MOVEL", status="DESCARTADO"),  # blocklist
        ])
        st = importar.importar(arq)
        self.assertEqual(st["novos"], 1)
        self.assertEqual(st["fixo"], 1)
        self.assertEqual(st["descartado"], 1)
        self.assertTrue(db.um("SELECT 1 FROM bloqueio WHERE telefone='5517988887777'"))
        self.assertIsNone(db.um("SELECT 1 FROM contato WHERE telefone='551732345678'"))

    def test_arquivo_grande_dispara_flush_sem_quebrar(self):
        """Acima de 5000 linhas, _varrer faz flush intermediario do lote. Esse
        caminho somava int += tupla (o retorno novo de _gravar) e so estourava
        em base grande -- por isso os testes pequenos passavam e a base real de
        354 mil quebrava com 'requisicao invalida'. 10.000 e multiplo exato de
        5000, entao o lote final fica vazio: exercita tambem _gravar([])."""
        linhas = [self.linha(f"551799{i:07d}") for i in range(10000)]
        st = importar.importar(self._csv(linhas))
        self.assertEqual(st["novos"], 10000)
        self.assertEqual(st["lidos"], 10000)
        self.assertEqual(db.um("SELECT COUNT(*) n FROM contato")["n"], 10000)

    def test_export_de_emails_do_drhery_avisa_em_vez_de_travar(self):
        """O export de e-mails (email,nome,cidade,segmento) nao tem telefone.

        Era o arquivo que o operador vinha tentando importar. Em vez de um erro
        seco, precisa dizer que e o arquivo errado e qual usar."""
        import tempfile
        fd, arq = tempfile.mkstemp(suffix=".csv", dir=self.tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("email,nome,cidade,segmento\n")
            f.write("a@b.com,LOJA,RIO PRETO,Comercio\n")
        with self.assertRaises(ValueError) as ctx:
            importar.importar(arq)
        self.assertIn("LEADS", str(ctx.exception))
        self.assertIn("telefone", str(ctx.exception).lower())

    def test_sem_optin_o_contato_nao_recebe_consentimento(self):
        importar.importar(self._csv([self.linha("5517991234567")]))
        r = db.um("SELECT consentimento_origem FROM contato WHERE telefone='5517991234567'")
        self.assertIn(r["consentimento_origem"], (None, ""))

    def test_optin_declarado_grava_origem_em_cada_contato(self):
        importar.importar(self._csv([self.linha("5517991234567"),
                                     self.linha("5517988886666")]),
                          consentimento=("2026-01-01T00:00:00", "formulario site"))
        for r in db.todos("SELECT consentimento_origem o FROM contato"):
            self.assertEqual(r["o"], "formulario site")

    def test_reimportar_com_optin_preenche_quem_ja_existia(self):
        """O caso que travava: importou sem opt-in, quer corrigir reimportando."""
        arq = self._csv([self.linha("5517991234567")])
        importar.importar(arq)                                    # sem opt-in
        self.assertEqual(db.um("SELECT COUNT(*) n FROM contato WHERE "
                               "consentimento_origem<>''")["n"], 0)
        st = importar.importar(arq, consentimento=("2026-01-01T00:00:00", "loja"))
        self.assertEqual(st["novos"], 0)
        self.assertEqual(st["atualizados"], 1)
        self.assertEqual(db.um("SELECT consentimento_origem o FROM contato")["o"], "loja")

    def test_declarar_optin_da_base_desbloqueia_o_publico(self):
        """A base sem opt-in dava fila 0. Declarar a origem libera, sem
        sobrescrever quem ja tinha, e fica registrado no log."""
        self.contatos(5, consentimento=False)     # 5 contatos MOVEL, sem opt-in
        db.exec_("UPDATE contato SET consentimento_em=?, consentimento_origem='ORIG'"
                 " WHERE id=(SELECT MIN(id) FROM contato)", db.agora())
        c = self.campanha(canal="whatsapp", provedor="simulado")
        self.assertEqual(campanhas.contar_publico({}, campanha_id=c["id"]), 1)  # so o que ja tinha
        n = importar.declarar_optin("prospeccao B2B - dado publico")
        self.assertEqual(n, 4)                    # os 4 que faltavam
        self.assertEqual(campanhas.contar_publico({}, campanha_id=c["id"]), 5)
        # nao sobrescreveu o opt-in original
        self.assertEqual(db.um("SELECT consentimento_origem o FROM contato "
                               "WHERE id=(SELECT MIN(id) FROM contato)")["o"], "ORIG")
        self.assertTrue(db.um("SELECT 1 FROM evento WHERE tipo='optin_declarado'"))

    def test_declarar_optin_sem_origem_recusa(self):
        self.contatos(2)
        with self.assertRaises(ValueError):
            importar.declarar_optin("")
        with self.assertRaises(ValueError):
            importar.declarar_optin("   ")

    def test_declarar_optin_com_limite_marca_so_um_lote(self):
        self.contatos(10, consentimento=False)
        self.assertEqual(importar.declarar_optin("teste", limite=3), 3)
        self.assertEqual(db.um("SELECT COUNT(*) n FROM contato WHERE "
                               "consentimento_origem<>''")["n"], 3)

    def test_optin_existente_nao_e_sobrescrito(self):
        arq = self._csv([self.linha("5517991234567")])
        importar.importar(arq, consentimento=("2026-01-01T00:00:00", "PRIMEIRA"))
        importar.importar(arq, consentimento=("2026-01-01T00:00:00", "SEGUNDA"))
        self.assertEqual(db.um("SELECT consentimento_origem o FROM contato")["o"], "PRIMEIRA")

class ImagemTest(Base):
    """Foto no disparo: validada pelo conteudo, personalizada na legenda."""

    JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100

    def uri(self, dados, mime="image/jpeg"):
        import base64
        return f"data:{mime};base64," + base64.b64encode(dados).decode()

    def test_so_aceita_imagem_de_verdade(self):
        from disparador import midia
        i, mime, erro = midia.salvar(self.uri(self.JPG))
        self.assertTrue(i and not erro)
        self.assertEqual(mime, "image/jpeg")
        # executavel disfarcado de jpg: recusado pelos bytes, nao pela extensao
        _i, _m, e = midia.salvar(self.uri(b"MZ" + b"x" * 100))
        self.assertTrue(e)
        # base64 quebrado
        self.assertTrue(midia.salvar("data:image/png;base64,@@@")[2])

    def test_id_e_o_hash_entao_dedup(self):
        from disparador import midia
        a, _, _ = midia.salvar(self.uri(self.PNG, "image/png"))
        b, _, _ = midia.salvar(self.uri(self.PNG, "image/png"))
        self.assertEqual(a, b)

    def test_id_nao_escapa_da_pasta(self):
        from disparador import midia
        for veneno in ("../config.json", "..%2f..%2fetc", "/etc/passwd", "x.txt",
                       "a" * 63 + ".jpg", "z" * 64 + ".jpg"):
            self.assertIsNone(midia.caminho(veneno), veneno)

    def test_imagem_so_em_whatsapp(self):
        from disparador import midia
        i, _, _ = midia.salvar(self.uri(self.JPG))
        with self.assertRaises(ValueError):
            self.campanha(canal="sms", provedor="simulado", meta={"imagem": i})

    def test_provedor_sem_imagem_recusa(self):
        from disparador import midia, provedores
        i, _, _ = midia.salvar(self.uri(self.JPG))
        original = provedores.pegar("simulado").IMAGEM
        provedores.pegar("simulado").IMAGEM = False
        try:
            with self.assertRaises(ValueError):
                self.campanha(canal="whatsapp", provedor="simulado", meta={"imagem": i})
        finally:
            provedores.pegar("simulado").IMAGEM = original

    def test_imagem_inexistente_recusa(self):
        with self.assertRaises(ValueError):
            self.campanha(canal="whatsapp", provedor="simulado",
                          meta={"imagem": "0" * 64 + ".jpg"})

    def test_foto_vai_com_legenda_personalizada(self):
        from disparador import midia, provedores
        i, _, _ = midia.salvar(self.uri(self.JPG))
        self.contatos(1)
        db.exec_("UPDATE contato SET nome='Loja Alfa'")
        c = self.campanha(canal="whatsapp", provedor="simulado",
                          modelo="Oi {nome}!", meta={"imagem": i})
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])
        capt = {}
        prov = provedores.pegar("simulado")
        orig = prov.enviar
        prov.enviar = lambda d, t, cf, m=None: (
            capt.update(texto=t, img=bool((m or {}).get("imagem_b64")),
                        mime=(m or {}).get("imagem_mime")) or orig(d, t, cf, m))
        try:
            m = self.motor()
            for _ in range(4):
                m._ciclo()
        finally:
            prov.enviar = orig
        self.assertIn("Loja Alfa", capt["texto"])   # legenda tem a variavel resolvida
        self.assertTrue(capt["img"])                # a foto foi junto
        self.assertEqual(capt["mime"], "image/jpeg")

    def test_imagem_sumida_no_meio_pausa_em_vez_de_mandar_so_texto(self):
        from disparador import midia, provedores
        i, _, _ = midia.salvar(self.uri(self.JPG))
        self.contatos(2)
        c = self.campanha(canal="whatsapp", provedor="simulado",
                          modelo="Oi", meta={"imagem": i})
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])
        os.remove(midia.caminho(i))                 # some do disco no meio
        m = self.motor()
        for _ in range(3):
            m._ciclo()
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "pausada")
        self.assertEqual(
            db.um("SELECT COUNT(*) n FROM envio WHERE estado='enviado'")["n"], 0)


class TextoTest(Base):
    def test_spintax_varia_e_variaveis_sao_substituidas(self):
        self.contatos(1)
        ct = db.um("SELECT * FROM contato LIMIT 1")
        vistos = {modelos.montar("{Ola|Oi|Opa}, {nome}!", ct) for _ in range(60)}
        self.assertEqual(len(vistos), 3)
        self.assertIn("Empresa 0", vistos.pop())          # LTDA e caixa alta sumiram

    def test_rodape_de_saida_entra_uma_vez_so(self):
        self.contatos(1)
        ct = db.um("SELECT * FROM contato LIMIT 1")
        t = modelos.montar("Oi! Responda SAIR p/ nao receber.", ct,
                           "Responda SAIR p/ nao receber.")
        self.assertEqual(t.lower().count("sair"), 1)

    def test_sms_com_acento_custa_mais_e_ascii_resolve(self):
        com = "Promoção especial de manutenção para você não perder"
        self.assertEqual(modelos.segmentos_sms(com)[1], "UCS-2")
        self.assertEqual(modelos.segmentos_sms(modelos.sem_acento(com))[1], "GSM-7")

    def test_cnae_quilometrico_e_encurtado_sem_terminar_em_preposicao(self):
        curto = modelos.encurtar("Comércio varejista de artigos do vestuário e acessórios")
        self.assertLessEqual(len(curto), 34)
        self.assertFalse(curto.split()[-1].lower() in modelos.MINUSCULAS)


class ProvedorTest(Base):
    def test_cloud_api_recusa_contato_frio_sem_template(self):
        """Falta de template e erro DA CAMPANHA: pausa, nunca bloqueia o lead."""
        r = provedores.pegar("whatsapp_cloud").enviar(
            "5517991234567", "oi", {"phone_id": "1", "token": "t"}, {})
        self.assertFalse(r.ok)
        self.assertTrue(r.pausar)
        self.assertFalse(r.fatal)
        self.assertIn("template", r.erro)

    def test_erro_de_template_nao_pode_ser_confundido_com_numero_morto(self):
        """Template com nome errado devolve 400 em TODO envio.

        Tratar isso como 'numero morto' mandava lead bom para a blocklist, um
        por um, ate acabar a fila -- sem ninguem perceber.
        """
        wa = provedores.pegar("whatsapp_cloud")
        for codigo in (131047, 131051, 132000, 132001, 100, 190):
            self.assertIn(codigo, wa.CAMPANHA_ERRADA, codigo)
            self.assertNotIn(codigo, wa.NUMERO_MORTO, codigo)
        self.assertIn(131026, wa.NUMERO_MORTO)      # esse sim: nao tem WhatsApp
        tw = provedores.pegar("twilio")
        self.assertTrue(tw.NUMERO_MORTO.isdisjoint(tw.CAMPANHA_ERRADA))

    def test_so_o_erro_do_destinatario_queima_o_lead(self):
        """Conferido contra a tabela oficial de erros da Cloud API.

        131021 e 'remetente = destinatario' e 131052 e midia que o USUARIO
        mandou -- nenhum dos dois diz que o numero esta morto, mas os dois
        estavam na lista que manda para a blocklist.
        """
        wa = provedores.pegar("whatsapp_cloud")
        self.assertEqual(wa.NUMERO_MORTO, {131026})
        for grupo in (wa.CAMPANHA_ERRADA, wa.TRANSITORIO, wa.ALARME_QUALIDADE):
            self.assertTrue(wa.NUMERO_MORTO.isdisjoint(grupo - {131026}))
        self.assertTrue(wa.CAMPANHA_ERRADA.isdisjoint(wa.TRANSITORIO))
        # limite de vazao e passageiro: reagenda, nao pausa a campanha
        for codigo in (130429, 80007, 131016, 131049, 131056):
            self.assertIn(codigo, wa.TRANSITORIO, codigo)

    def test_alarme_de_spam_para_tudo_em_vez_de_insistir(self):
        """131048 = o WhatsApp ja marcou nossas mensagens como spam.

        Reagendar isso e continuar cavando: o proximo passo da Meta e derrubar
        a conta. Tem que parar e chamar o operador.
        """
        wa = provedores.pegar("whatsapp_cloud")
        self.assertIn(131048, wa.ALARME_QUALIDADE)
        r = wa.enviar("5517991234567", "oi", {"phone_id": "1", "token": "t"},
                      {"template": "x"})
        self.assertFalse(r.ok)      # sem rede, cai em erro de conexao mesmo

    def test_palavras_de_saida_do_padrao_internacional(self):
        """A Twilio so filtra STOP em ingles -- o 'SAIR' chega ate nos.

        E o contrario tambem: o lead pode responder STOP/CANCEL/QUIT, entao a
        lista padrao CTIA/FCC precisa valer aqui dentro.
        """
        c = config.carregar()
        for t in ("STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT",
                  "REVOKE", "OPTOUT"):
            self.assertTrue(regras.detectar_optout(t, c), t)
        self.assertFalse(regras.detectar_optout("end of the day tomorrow", c))

    def test_chip_desconectado_pausa_em_vez_de_moer_a_fila(self):
        """Descoberto rodando contra um Evolution API de verdade.

        Com o chip fora do ar o gateway nao devolve erro -- ele PENDURA a
        requisicao ate o timeout. Sem conferir a conexao antes, cada mensagem
        gastava 30s pendurada, era retentada 3x, e a fila inteira virava
        'falha' em silencio enquanto o painel dizia "enviando".
        """
        ev = provedores.pegar("evolution")
        self.assertTrue(hasattr(ev, "sondar"))
        # tudo preenchido, mas o gateway nao existe: precisa dizer isso, e rapido
        import time
        t = time.time()
        ok, falta = ev.pronto({"url": "http://127.0.0.1:9", "instancia": "x",
                               "apikey": "k"})
        self.assertFalse(ok)
        self.assertIn("não respondeu", falta)
        self.assertLess(time.time() - t, 10, "checagem de conexao lenta demais")

    def test_aquecimento_e_decisao_do_provedor(self):
        """Rampa protege chip proprio, mas na Cloud API impede a conta de subir
        de tier (a Meta exige usar metade do limite num dia)."""
        self.assertTrue(provedores.pegar("evolution").AQUECE)
        self.assertFalse(provedores.pegar("whatsapp_cloud").AQUECE)
        self.assertFalse(provedores.pegar("sms_http").AQUECE)

    def test_todo_provedor_declara_canal_e_prontidao(self):
        for nome, m in provedores.REGISTRO.items():
            self.assertTrue(set(m.CANAL) <= {"sms", "whatsapp"}, nome)
            self.assertIsInstance(m.pronto({}), tuple, nome)


# ---------------------------------------------------------------------------
# Regressao: cada teste abaixo nasceu de um bug que existiu de verdade aqui.
# ---------------------------------------------------------------------------
class RegressaoTest(Base):
    def test_pergunta_do_lead_nao_vira_pedido_de_saida(self):
        """'para qual email?' comeca com 'para' -- e um lead pedindo contato.

        A regra antiga casava qualquer palavra-chave no INICIO de resposta curta
        e bloqueava o lead para sempre, silenciosamente.
        """
        c = config.carregar()
        for t in ("para qual email?", "para que horas?", "para mim serve",
                  "nao entendi bem", "nao consigo agora", "nao sei ainda",
                  "chega amanha?", "quanto custa para 100?"):
            self.assertFalse(regras.detectar_optout(t, c), t)
        for t in ("sair", "SAIR", "pare", "parar", "nao", "chega",
                  "sair por favor", "pare com isso"):
            self.assertTrue(regras.detectar_optout(t, c), t)

    def test_numero_sem_o_nono_digito_e_o_mesmo_lead(self):
        self.assertIn("551791234567", fone.variantes("5517991234567"))
        self.assertIn("5517991234567", fone.variantes("551791234567"))

    def test_saida_vinda_sem_o_nono_digito_cancela_a_fila_certa(self):
        """Gateway devolve 551791234567; a fila esta em 5517991234567.

        Antes: bloqueava um numero fantasma e cancelava zero mensagens -- o lead
        pedia para sair e continuava recebendo.
        """
        from disparador import servidor
        db.exec_("INSERT INTO contato(telefone,tipo,nome,consentimento_em,"
                 "consentimento_origem,criado_em)"
                 " VALUES('5517991234567','MOVEL','ALVO',?,?,?)",
                 db.agora(), "formulario:teste", db.agora())
        c = self.campanha(canal="whatsapp", provedor="whatsapp_cloud")
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE envio SET conta_externa='PHONE-A', provedor_externo='whatsapp_cloud'")
        servidor._webhook("whatsapp_cloud", {"entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "PHONE-A"},
            "messages": [{"from": "551791234567", "text": {"body": "SAIR"}}]}}]}]})
        self.assertEqual(db.um("SELECT telefone FROM bloqueio")["telefone"], "5517991234567")
        self.assertEqual(db.um("SELECT estado FROM envio")["estado"], "cancelado")

    def test_bloqueio_tecnico_nao_apaga_o_pedido_de_saida(self):
        """O 'optout' e a prova de que o pedido do titular foi atendido."""
        tel = "5517990000001"
        regras.registrar_optout(tel, origem="lead", motivo="optout")
        regras.registrar_optout(tel, origem="twilio", motivo="erro_fatal")
        self.assertEqual(db.um("SELECT motivo FROM bloqueio")["motivo"], "optout")
        # o caminho inverso vale: pedido do titular promove bloqueio tecnico
        db.exec_("DELETE FROM bloqueio")
        regras.registrar_optout(tel, origem="twilio", motivo="erro_fatal")
        regras.registrar_optout(tel, origem="lead", motivo="optout")
        self.assertEqual(db.um("SELECT motivo FROM bloqueio")["motivo"], "optout")

    def test_provedor_que_explode_isola_a_linha_e_pausa_a_campanha(self):
        """Excecao pode ocorrer depois do aceite remoto: nunca reenvia no escuro."""
        self.contatos(5)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])

        class Bomba:
            ROTULO, CANAL = "bomba", ("sms",)
            pronto = staticmethod(lambda cfg: (True, ""))

            @staticmethod
            def enviar(*a, **k):
                raise RuntimeError("gateway caiu no meio")

        original = provedores.REGISTRO["simulado"]
        provedores.REGISTRO["simulado"] = Bomba
        try:
            m = self.motor()
            for _ in range(3):
                m._ciclo()
        finally:
            provedores.REGISTRO["simulado"] = original
        self.assertEqual(db.um("SELECT COUNT(*) n FROM envio WHERE estado='enviando'")["n"], 0)
        self.assertEqual(campanhas.resumo(c["id"]).get("fila"), 4)
        self.assertEqual(campanhas.resumo(c["id"]).get("indeterminado"), 1)
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "pausada")

    def test_erro_de_campanha_pausa_sem_bloquear_o_lead(self):
        self.contatos(4)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])

        class Chato:
            ROTULO, CANAL = "chato", ("sms",)
            pronto = staticmethod(lambda cfg: (True, ""))
            enviar = staticmethod(lambda *a, **k: provedores.PAUSA("template invalido"))

        original = provedores.REGISTRO["simulado"]
        provedores.REGISTRO["simulado"] = Chato
        try:
            m = self.motor()
            for _ in range(3):
                m._ciclo()
        finally:
            provedores.REGISTRO["simulado"] = original
        self.assertEqual(campanhas.resumo(c["id"]).get("fila"), 4)   # leads intactos
        self.assertEqual(db.um("SELECT COUNT(*) n FROM bloqueio")["n"], 0)
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "pausada")

    def test_provedor_inexistente_nao_consome_a_fila(self):
        self.contatos(5)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando', provedor='nao_existe' WHERE id=?",
                 c["id"])
        m = self.motor()
        for _ in range(3):
            m._ciclo()
        self.assertEqual(campanhas.resumo(c["id"]).get("fila"), 5)
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "pausada")

    def test_janela_que_fecha_no_meio_do_ciclo_interrompe_o_envio(self):
        """Um ciclo com varias campanhas em ritmo lento leva minutos.

        Antes a janela era conferida so no topo: se ela fechasse durante o
        ciclo, as mensagens seguintes saiam depois do horario permitido. Aqui a
        janela abre na checagem do topo e fecha na de dentro do laco.
        """
        self.contatos(5)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])
        import disparador.motor as motor_mod
        original, chamadas = motor_mod.regras.janela_aberta, []

        def fecha_depois_do_topo(cfg, quando=None):
            chamadas.append(1)
            return (True, "") if len(chamadas) == 1 else (False, "fechou agora")

        motor_mod.regras.janela_aberta = fecha_depois_do_topo
        try:
            self.motor()._ciclo()
        finally:
            motor_mod.regras.janela_aberta = original
        self.assertGreater(len(chamadas), 1, "a janela nao foi reconferida no laco")
        self.assertIsNone(campanhas.resumo(c["id"]).get("enviado"))
        self.assertEqual(campanhas.resumo(c["id"]).get("fila"), 5)

    def test_cadastro_sem_nome_nao_vira_saudacao_constrangedora(self):
        """A base real tem 336 contatos chamados 'NAO DETECTADO'.

        Sem tratar isso a mensagem saia como "Oi, Nao Detectado!", que denuncia
        o disparo automatico na primeira linha.
        """
        for lixo in ("NÃO DETECTADO", "NAO DETECTADO", "---", "*", "3152", "."):
            self.assertEqual(modelos.titulo(lixo), "", lixo)
        self.assertEqual(modelos.titulo("ABN COMERCIAL HOSPITALAR LTDA"),
                         "Abn Comercial Hospitalar")
        db.exec_("INSERT INTO contato(telefone,tipo,nome,cidade,criado_em)"
                 " VALUES('5517990000009','MOVEL','NÃO DETECTADO','ADOLFO',?)", db.agora())
        t = modelos.montar("{Ola|Oi}, {nome}! Falo de {cidade}.",
                           db.um("SELECT * FROM contato WHERE telefone='5517990000009'"))
        self.assertNotIn("Detectado", t)
        self.assertIn("Adolfo", t)

    def test_teste_de_envio_funciona_com_a_base_ainda_vazia(self):
        """Primeira coisa que um usuario novo faz, antes de importar nada."""
        from disparador import servidor
        r = servidor._teste({"telefone": "17 99123-4567", "provedor": "simulado",
                             "canal": "sms", "modelo": "{Ola|Oi}, {nome}!"})
        self.assertTrue(r["ok"], r)

    def test_cota_sobrevive_a_data_ingenua_e_meta_corrompida(self):
        """cota_restante roda no motor E na listagem do painel: uma linha ruim
        derrubava os dois."""
        db.exec_("INSERT INTO campanha(nome,canal,provedor,modelo,meta,limite_dia,criado_em)"
                 " VALUES('x','sms','simulado','oi','{\"aquecimento\":true}',5000,datetime())")
        c = db.um("SELECT * FROM campanha")
        self.assertEqual(regras.limite_efetivo(c), regras.RAMPA[0])
        db.exec_("UPDATE campanha SET meta='isto nao e json'")
        self.assertEqual(regras.limite_efetivo(db.um("SELECT * FROM campanha")), 5000)


class DiagnosticoTest(Base):
    """Configuração ausente/inválida precisa virar instrução, não 'erro de API'."""

    def test_cada_falha_do_gateway_tem_mensagem_propria(self):
        """Antes, quatro problemas diferentes davam a mesma frase inútil."""
        ev = provedores.pegar("evolution")
        vistos = {}
        for nome, cfg in (
            ("vazio", {}),
            ("url_torta", {"url": "nao-e-url", "instancia": "i", "apikey": "k"}),
            ("sem_gateway", {"url": "http://127.0.0.1:9", "instancia": "i", "apikey": "k"}),
        ):
            d = ev.diagnosticar(cfg)
            self.assertFalse(d.ok, nome)
            self.assertTrue(d.mensagem and d.acao, nome)
            vistos[nome] = d.mensagem
        self.assertEqual(len(set(vistos.values())), len(vistos), vistos)

    def test_url_invalida_nao_estoura_excecao(self):
        """'nao-e-url' fazia o urllib levantar ValueError e virar erro 500."""
        for u in ("nao-e-url", "", "http://", "ftp://x", None):
            d = provedores.diagnosticar("evolution",
                                        {"url": u, "instancia": "i", "apikey": "k"})
            self.assertFalse(d.ok)
            self.assertIsInstance(d.mensagem, str)

    def test_diagnostico_nunca_carrega_o_valor_da_credencial(self):
        cfg = {"url": "http://127.0.0.1:9", "instancia": "i", "apikey": "SEGREDO123"}
        d = provedores.diagnosticar("evolution", cfg)
        inteiro = " ".join([d.mensagem, d.acao, d.detalhe] + d.faltando)
        self.assertNotIn("SEGREDO123", inteiro)

    def test_todo_provedor_diagnostica_sem_explodir(self):
        for nome in provedores.REGISTRO:
            for cfg in ({}, {"url": None}, {"token": ""}, {"corpo": "nao e dict"}):
                d = provedores.diagnosticar(nome, cfg)
                self.assertIsInstance(d.ok, bool)
                self.assertIsInstance(d.faltando, list)

    def test_provedor_de_ensaio_se_declara(self):
        """O painel precisa deste sinal para nunca dizer 'enviado' num ensaio."""
        self.assertTrue(getattr(provedores.pegar("simulado"), "SIMULA", False))
        for outro in ("evolution", "whatsapp_cloud", "sms_http", "twilio"):
            self.assertFalse(getattr(provedores.pegar(outro), "SIMULA", False), outro)

    def test_sonda_nao_mistura_resultado_de_credenciais_diferentes(self):
        """A chave do cache precisa incluir a apikey.

        Sem isso, uma tentativa com credencial errada devolvia o resultado da
        credencial certa -- e o painel mentia sobre o estado da conexão.
        """
        ev = provedores.pegar("evolution")
        ev._CACHE.clear()
        base = {"url": "http://127.0.0.1:9", "instancia": "i"}
        ev.sondar(dict(base, apikey="A"))
        chaves = {c[2] for c in ev._CACHE}
        ev.sondar(dict(base, apikey="B"))
        self.assertEqual({c[2] for c in ev._CACHE}, chaves | {"B"})


class ChipTest(Base):
    """Cota e aquecimento pertencem ao CHIP -- quem toma ban e o numero."""

    def chip(self, nome, **kw):
        d = dict(estado="open", numero="5517" + nome.rjust(9, "0")[:9],
                 por_minuto=600, limite_dia=900, aquecer=1, conversar=1,
                 iniciado_em=db.agora())
        d.update(kw)
        cols = ",".join(d)
        db.exec_(f"INSERT INTO instancia(nome,rotulo,provedor,criado_em,{cols})"
                 f" VALUES(?,?,'evolution',?,{','.join('?' * len(d))})",
                 nome, nome, db.agora(), *d.values())
        return db.um("SELECT * FROM instancia WHERE nome=?", nome)

    def test_rampa_e_por_chip_e_conta_do_primeiro_dia_conectado(self):
        c = self.chip("a", limite_dia=99999)
        self.assertEqual(instancias.limite_efetivo(c), instancias.RAMPA[0])
        antigo = (datetime.now().astimezone() - timedelta(days=4)).isoformat(timespec="seconds")
        db.exec_("UPDATE instancia SET iniciado_em=? WHERE id=?", antigo, c["id"])
        c = db.um("SELECT * FROM instancia WHERE id=?", c["id"])
        self.assertEqual(instancias.limite_efetivo(c), instancias.RAMPA[4])

    def test_duas_campanhas_no_mesmo_chip_somam_na_mesma_cota(self):
        """O chip e o recurso escasso: cada campanha nao ganha uma cota nova."""
        c = self.chip("a", limite_dia=10, aquecer=0)
        self.contatos(20)
        for nome in ("x", "y"):
            camp = self.campanha(nome=nome, canal="whatsapp", provedor="evolution")
            campanhas.montar_publico(camp["id"], limite=5)
        db.exec_("UPDATE envio SET estado='enviado', enviado_em=?, instancia_id=?",
                 db.agora(), c["id"])
        c = db.um("SELECT * FROM instancia WHERE id=?", c["id"])
        self.assertEqual(instancias.enviados_hoje(c["id"]), 10)
        self.assertEqual(instancias.cota_restante(c), 0)

    def test_chip_desconectado_ou_sem_cota_nao_e_remetente(self):
        self.chip("conectado")
        self.chip("caido", estado="close")
        self.chip("desligado", ativa=0)
        zerado = self.chip("sem_cota", limite_dia=0, aquecer=0)
        nomes = {i["nome"] for i in instancias.prontas()}
        self.assertEqual(nomes, {"conectado"})
        self.assertEqual(instancias.cota_restante(zerado), 0)

    def test_capacidade_do_conjunto_soma_os_chips(self):
        for i in range(15):
            self.chip(f"c{i:02d}", limite_dia=99999)
        self.assertEqual(instancias.capacidade_diaria(), 15 * instancias.RAMPA[0])

    def test_provedor_sem_instancias_vira_um_remetente_so(self):
        """SMS e Cloud API nao tem chips; o motor precisa de um caminho unico."""
        c = self.campanha(provedor="simulado", por_minuto=7)
        rs = instancias.remetentes(c, config.carregar())
        self.assertEqual(len(rs), 1)
        self.assertIsNone(rs[0].id)
        self.assertEqual(rs[0].por_minuto, 7)

    def test_campanha_pode_restringir_quais_chips_usa(self):
        self.chip("a"), self.chip("b"), self.chip("c")
        c = self.campanha(canal="whatsapp", provedor="evolution",
                          meta={"instancias": ["a", "c"]})
        nomes = {r.nome for r in instancias.remetentes(c, config.carregar())}
        self.assertEqual(nomes, {"a", "c"})


class FaixaTest(Base):
    """Cada conta com uma fatia definida da lista, previsivel e conferivel."""

    def chip(self, nome, **kw):
        return ChipTest.chip(self, nome, **kw)

    def montar(self, n_chips=5, n_contatos=100):
        for i in range(n_chips):
            self.chip(f"c{i}", limite_dia=99999, aquecer=0)
        self.contatos(n_contatos)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        total = campanhas.montar_publico(c["id"], faixas=True)
        return c, total

    def test_faixas_cobrem_a_lista_sem_buraco_nem_sobreposicao(self):
        c, total = self.montar(5, 100)
        faixas = campanhas.mapa_faixas(c["id"])
        self.assertEqual(len(faixas), 5)
        self.assertEqual(sum(f["contatos"] for f in faixas), total)
        for a, b in zip(faixas, faixas[1:]):
            self.assertEqual(a["ultimo"] + 1, b["primeiro"], "buraco ou sobreposicao")

    def test_fatias_ficam_do_mesmo_tamanho(self):
        # 15 chips e um total que nao divide redondo: e onde o resto apareceria
        c, _ = self.montar(15, 4001)
        tam = [f["contatos"] for f in campanhas.mapa_faixas(c["id"])]
        self.assertLessEqual(max(tam) - min(tam), 1)

    def test_ordem_do_arquivo_e_preservada(self):
        """Faixa 1 = as primeiras linhas do CSV. E o que torna conferivel."""
        c, _ = self.montar(4, 40)
        faixas = campanhas.mapa_faixas(c["id"])
        menor = db.um("SELECT MIN(id) m FROM contato")["m"]
        self.assertEqual(faixas[0]["primeiro"], menor)

    def test_chip_nao_invade_faixa_alheia(self):
        c, _ = self.montar(4, 40)
        cfg = config.carregar()
        m = self.motor()
        for rem in instancias.remetentes(c, cfg):
            envio = m._reivindicar(c, rem)
            self.assertIsNotNone(envio)
            reservado = db.um("SELECT instancia_id FROM envio WHERE id=?",
                              envio["id"])["instancia_id"]
            self.assertEqual(reservado, rem.id)

    def test_sem_chip_conectado_a_montagem_recusa(self):
        self.chip("desligado", estado="close")
        self.contatos(10)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        with self.assertRaises(ValueError):
            campanhas.montar_publico(c["id"], faixas=True)

    def test_modo_rodizio_continua_sem_dono(self):
        """Sem faixas ninguem e dono: o comportamento antigo nao muda."""
        for i in range(3):
            self.chip(f"c{i}", limite_dia=99999, aquecer=0)
        self.contatos(30)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        campanhas.montar_publico(c["id"])
        sem_dono = db.um("SELECT COUNT(*) n FROM envio WHERE instancia_id IS NULL")["n"]
        self.assertEqual(sem_dono, 30)


class RedistribuicaoTest(Base):
    """Faixa de chip que saiu nao pode ficar parada, nem sumir."""

    def chip(self, nome, **kw):
        return ChipTest.chip(self, nome, **kw)

    def preparar(self):
        for i in range(4):
            self.chip(f"c{i}", limite_dia=99999, aquecer=0, visto_ok_em=db.agora())
        self.contatos(80)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        campanhas.montar_publico(c["id"], faixas=True)
        return c, db.um("SELECT * FROM instancia WHERE nome='c1'")

    def test_oscilacao_curta_segura_a_faixa(self):
        c, alvo = self.preparar()
        db.exec_("UPDATE instancia SET estado='close', visto_ok_em=? WHERE id=?",
                 db.agora(), alvo["id"])
        pendentes = [q for i, q, ja in instancias.orfas(c["id"]) if not ja]
        self.assertTrue(pendentes, "queda recente nao deveria redistribuir ainda")

    def test_fora_do_ar_alem_da_carencia_redistribui(self):
        c, alvo = self.preparar()
        antes = db.um("SELECT COUNT(*) n FROM envio WHERE instancia_id=?", alvo["id"])["n"]
        velho = (datetime.now().astimezone() - instancias.CARENCIA_QUEDA
                 - timedelta(minutes=1)).isoformat(timespec="seconds")
        db.exec_("UPDATE instancia SET estado='close', visto_ok_em=? WHERE id=?",
                 velho, alvo["id"])
        movidas = instancias.redistribuir(c["id"], alvo["id"])
        self.assertEqual(movidas, antes)
        self.assertEqual(
            db.um("SELECT COUNT(*) n FROM envio WHERE instancia_id=? AND estado='fila'",
                  alvo["id"])["n"], 0)

    def test_ban_redistribui_sem_esperar(self):
        c, alvo = self.preparar()
        db.exec_("UPDATE instancia SET estado='close', motivo_queda='401',"
                 " visto_ok_em=? WHERE id=?", db.agora(), alvo["id"])
        self.assertTrue(instancias.banido(db.um("SELECT * FROM instancia WHERE id=?",
                                                alvo["id"])))
        self.assertTrue(all(ja for _i, _q, ja in instancias.orfas(c["id"])))

    def test_redistribuir_nao_perde_nem_duplica(self):
        c, alvo = self.preparar()
        total = db.um("SELECT COUNT(*) n FROM envio WHERE campanha_id=?", c["id"])["n"]
        db.exec_("UPDATE instancia SET estado='close' WHERE id=?", alvo["id"])
        instancias.redistribuir(c["id"], alvo["id"])
        self.assertEqual(
            db.um("SELECT COUNT(*) n FROM envio WHERE campanha_id=?", c["id"])["n"], total)
        self.assertEqual(
            db.um("SELECT COUNT(*) n FROM (SELECT contato_id FROM envio"
                  " WHERE campanha_id=? GROUP BY contato_id HAVING COUNT(*)>1)",
                  c["id"])["n"], 0)

    def test_redistribuir_nao_encosta_na_sessao(self):
        """Mover fila e uma coisa; desconectar o chip e outra."""
        c, alvo = self.preparar()
        db.exec_("UPDATE instancia SET estado='close' WHERE id=?", alvo["id"])
        instancias.redistribuir(c["id"], alvo["id"])
        depois = db.um("SELECT * FROM instancia WHERE id=?", alvo["id"])
        self.assertEqual(depois["ativa"], 1)
        self.assertTrue(db.um("SELECT 1 FROM instancia WHERE id=?", alvo["id"]))

    def test_nao_mexe_no_que_ja_saiu(self):
        c, alvo = self.preparar()
        db.exec_("UPDATE envio SET estado='enviado', enviado_em=? WHERE instancia_id=?"
                 " AND id IN (SELECT id FROM envio WHERE instancia_id=? LIMIT 3)",
                 db.agora(), alvo["id"], alvo["id"])
        db.exec_("UPDATE instancia SET estado='close' WHERE id=?", alvo["id"])
        instancias.redistribuir(c["id"], alvo["id"])
        self.assertEqual(
            db.um("SELECT COUNT(*) n FROM envio WHERE instancia_id=? AND estado='enviado'",
                  alvo["id"])["n"], 3)


class LimboTest(Base):
    """Envio em 'indeterminado' nao pode virar buraco sem saida."""

    def preso(self, quantos=3):
        self.contatos(quantos + 2)
        c = self.campanha(canal="whatsapp", provedor="simulado")
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE campanha SET estado='rodando' WHERE id=?", c["id"])
        velho = (datetime.now().astimezone() - db.IDADE_ORFAO
                 - timedelta(minutes=1)).isoformat(timespec="seconds")
        db.exec_("UPDATE envio SET estado='enviando', tentativa_em=?, atualizado_em=?"
                 " WHERE id IN (SELECT id FROM envio LIMIT ?)", velho, velho, quantos)
        db.recuperar_orfaos()
        return c

    def test_queda_isola_e_pausa(self):
        c = self.preso(2)
        self.assertEqual(len(campanhas.indeterminados()), 2)
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "pausada")

    def test_operador_consegue_tirar_do_limbo(self):
        """Antes nao havia rota, botao nem comando: ficava preso para sempre."""
        c = self.preso(3)
        self.assertEqual(campanhas.resolver_indeterminado("nao_saiu", campanha_id=c["id"]), 3)
        self.assertEqual(len(campanhas.indeterminados()), 0)
        self.assertEqual(
            db.um("SELECT COUNT(*) n FROM envio WHERE estado='fila'")["n"], 5)

    def test_voltar_pra_fila_limpa_a_marca_da_tentativa(self):
        """Sem limpar, a chave de idempotencia velha barraria o reenvio."""
        c = self.preso(1)
        db.exec_("UPDATE envio SET id_externo='x', chave_idempotencia='y'"
                 " WHERE estado='indeterminado'")
        campanhas.resolver_indeterminado("nao_saiu", campanha_id=c["id"])
        r = db.um("SELECT id_externo, chave_idempotencia FROM envio WHERE estado='fila'"
                  " AND erro IS NOT NULL")
        self.assertIsNone(r["id_externo"])
        self.assertIsNone(r["chave_idempotencia"])

    def test_confirmar_que_saiu_marca_enviado_com_data(self):
        c = self.preso(2)
        campanhas.resolver_indeterminado("saiu", campanha_id=c["id"])
        for r in db.todos("SELECT estado, enviado_em FROM envio WHERE estado='enviado'"):
            self.assertIsNotNone(r["enviado_em"], "enviado sem data quebra cota e cooldown")

    def test_decisao_invalida_nao_mexe_em_nada(self):
        c = self.preso(2)
        with self.assertRaises(ValueError):
            campanhas.resolver_indeterminado("qualquer", campanha_id=c["id"])
        self.assertEqual(len(campanhas.indeterminados()), 2)

    def test_recibo_do_provedor_resgata_sozinho(self):
        """Se a entrega chegou, a mensagem saiu -- e a prova que faltava."""
        from disparador import servidor
        self.assertTrue(servidor._transicao_status("indeterminado", "entregue"))
        self.assertTrue(servidor._transicao_status("indeterminado", "falha"))
        # e o que ja foi resolvido nao regride
        self.assertFalse(servidor._transicao_status("cancelado", "entregue"))
        self.assertFalse(servidor._transicao_status("respondido", "enviado"))

    def test_limbo_aparece_no_estado_do_painel(self):
        """So no log ninguem ve; e o que mantem a campanha pausada."""
        from disparador import servidor
        self.preso(2)
        self.assertEqual(servidor.estado_geral()["indeterminados"], 2)


class SessaoTest(Base):
    """Conectar um chip custa um QR lido no celular. Nao se perde por engano."""

    def chip(self, nome, **kw):
        return ChipTest.chip(self, nome, **kw)

    def test_pausar_nao_desconecta(self):
        c = self.chip("a")
        instancias.pausar("a")
        depois = db.um("SELECT * FROM instancia WHERE nome='a'")
        self.assertEqual(depois["ativa"], 0)
        self.assertEqual(depois["estado"], "open", "a sessao nao pode cair ao pausar")
        instancias.pausar("a", ativa=True)
        self.assertEqual(db.um("SELECT ativa FROM instancia WHERE nome='a'")["ativa"], 1)

    def test_desconectar_exige_o_nome_digitado(self):
        self.chip("a")
        chamou = []
        mod = provedores.pegar("evolution")
        original = mod.remover_instancia
        mod.remover_instancia = lambda n, cfg: chamou.append(n)
        try:
            for tentativa in (None, "", "outro", "A"):
                ok, erro = instancias.remover("a", config.carregar(), tentativa)
                self.assertFalse(ok, tentativa)
                self.assertTrue(erro)
            self.assertEqual(chamou, [], "logout nao pode ser chamado sem confirmacao")
            self.assertTrue(db.um("SELECT 1 FROM instancia WHERE nome='a'"))
            ok, _e = instancias.remover("a", config.carregar(), "a")
            self.assertTrue(ok)
            self.assertEqual(chamou, ["a"])
        finally:
            mod.remover_instancia = original

    def test_desconectar_chip_com_faixa_nao_estoura_nem_perde_fila(self):
        """envio.instancia_id aponta para instancia(id): apagar o chip com fila
        reservada estourava FOREIGN KEY e prendia o operador com um chip banido."""
        self.chip("a"), self.chip("b")
        self.contatos(20)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        campanhas.montar_publico(c["id"], faixas=True)
        antes = db.um("SELECT COUNT(*) n FROM envio")["n"]
        mod = provedores.pegar("evolution")
        original = mod.remover_instancia
        mod.remover_instancia = lambda n, cfg: True
        try:
            ok, erro = instancias.remover("a", config.carregar(), "a")
        finally:
            mod.remover_instancia = original
        self.assertTrue(ok, erro)
        self.assertEqual(db.um("SELECT COUNT(*) n FROM envio")["n"], antes)
        self.assertEqual(
            db.um("SELECT COUNT(*) n FROM envio WHERE instancia_id NOT IN"
                  " (SELECT id FROM instancia)")["n"], 0)

    def test_vigia_reconecta_so_chip_pareado_que_caiu_sem_ban(self):
        """Queda de rede volta sem QR; ban (401/403) e chip sem numero nao."""
        self.chip("caiu", estado="close", motivo_queda="428")
        self.chip("sem_motivo", estado="connecting", motivo_queda="")
        self.chip("banido", estado="close", motivo_queda="401")
        self.chip("bloqueado", estado="close", motivo_queda="403")
        self.chip("sem_qr", estado="close", numero=None)
        self.chip("vivo", estado="open")
        self.chip("pausado", estado="close", ativa=0)
        chamou = []
        mod = provedores.pegar("evolution")
        original = mod.reconectar_instancia
        resposta = {"caiu": ("ok", "estado connecting"), "sem_motivo": ("qr", "precisa de QR")}
        mod.reconectar_instancia = lambda n, cfg: (chamou.append(n), resposta[n])[1]
        try:
            pedidos = instancias.reconectar_pendentes(config.carregar())
            self.assertEqual(sorted(pedidos), ["caiu", "sem_motivo"])
            self.assertEqual(sorted(chamou), ["caiu", "sem_motivo"])
            # freio: dentro de 2 min nao pede de novo
            self.assertEqual(instancias.reconectar_pendentes(config.carregar()), [])
            self.assertEqual(len(chamou), 2)
            antigo = (datetime.now().astimezone() - timedelta(minutes=3)).isoformat(timespec="seconds")
            db.exec_("UPDATE instancia SET reconexao_em=?", antigo)
            # o gateway respondeu QR para 'sem_motivo': a sessao nao existe mais,
            # entao ele sai do vigia e o painel passa a pedir QR
            self.assertEqual(instancias.reconectar_pendentes(config.carregar()), ["caiu"])
            self.assertEqual(db.um("SELECT motivo_queda FROM instancia WHERE nome='sem_motivo'")
                             ["motivo_queda"], "sem_sessao")
            self.assertFalse(instancias.banido(db.um("SELECT * FROM instancia WHERE nome='sem_motivo'")))
        finally:
            mod.reconectar_instancia = original

    def test_webhook_de_conexao_espelha_estado_e_pede_reconexao(self):
        from disparador import servidor
        self.chip("a", estado="open")
        chamou = []
        mod = provedores.pegar("evolution")
        original = mod.reconectar_instancia
        mod.reconectar_instancia = lambda n, cfg: (chamou.append(n), ("ok", "ok"))[1]
        try:
            r = servidor._webhook("evolution", {"event": "connection.update", "instance": "a",
                                                "data": {"instance": "a", "state": "close",
                                                         "statusReason": 428}})
            self.assertEqual(r["eventos"], 1)
            linha = db.um("SELECT * FROM instancia WHERE nome='a'")
            self.assertEqual(linha["estado"], "close")
            self.assertEqual(linha["motivo_queda"], "428")
            self.assertEqual(chamou, ["a"])

            servidor._webhook("evolution", {"event": "connection.update", "instance": "a",
                                            "data": {"instance": "a", "state": "open",
                                                     "statusReason": 200}})
            linha = db.um("SELECT * FROM instancia WHERE nome='a'")
            self.assertEqual(linha["estado"], "open")
            self.assertIsNone(linha["motivo_queda"])

            # ban avisado pelo gateway: espelha, mas NAO insiste
            servidor._webhook("evolution", {"event": "connection.update", "instance": "a",
                                            "data": {"instance": "a", "state": "close",
                                                     "statusReason": 401}})
            self.assertTrue(instancias.banido(db.um("SELECT * FROM instancia WHERE nome='a'")))
            self.assertEqual(chamou, ["a"])
            # chip desconhecido nao cria linha nem quebra
            r = servidor._webhook("evolution", {"event": "connection.update", "instance": "zz",
                                                "data": {"instance": "zz", "state": "close"}})
            self.assertEqual(r["eventos"], 0)
        finally:
            mod.reconectar_instancia = original

    def test_iniciar_rampa_nao_reinicia_quem_ja_comecou(self):
        antigo = (datetime.now().astimezone() - timedelta(days=5)).isoformat(timespec="seconds")
        self.chip("velho", iniciado_em=antigo)
        self.chip("novo", iniciado_em=None)
        mexidos = instancias.iniciar_rampa()
        self.assertEqual(mexidos, ["novo"])
        self.assertEqual(db.um("SELECT iniciado_em FROM instancia WHERE nome='velho'")
                         ["iniciado_em"], antigo)
        self.assertIsNotNone(db.um("SELECT iniciado_em FROM instancia WHERE nome='novo'")
                             ["iniciado_em"])

    def test_botao_habilita_rampa_e_inicia_so_quem_esta_conectado(self):
        self.chip("aberto", aquecer=0, iniciado_em=None)
        self.chip("fechado", estado="close", aquecer=0, iniciado_em=None)
        self.chip("inativo", ativa=0, aquecer=0, iniciado_em=None)

        r = instancias.ativar_rampa()

        self.assertEqual(r["habilitados"], ["aberto", "fechado"])
        self.assertEqual(r["iniciados"], ["aberto"])
        self.assertEqual(r["aguardando_conexao"], ["fechado"])
        aberto = db.um("SELECT aquecer,iniciado_em FROM instancia WHERE nome='aberto'")
        fechado = db.um("SELECT aquecer,iniciado_em FROM instancia WHERE nome='fechado'")
        inativo = db.um("SELECT aquecer,iniciado_em FROM instancia WHERE nome='inativo'")
        self.assertEqual(aberto["aquecer"], 1)
        self.assertIsNotNone(aberto["iniciado_em"])
        self.assertEqual(fechado["aquecer"], 1)
        self.assertIsNone(fechado["iniciado_em"])
        self.assertEqual(inativo["aquecer"], 0)
        self.assertIsNone(inativo["iniciado_em"])

    def test_botao_e_idempotente(self):
        inicio = db.agora()
        self.chip("a", aquecer=1, iniciado_em=inicio)
        r = instancias.ativar_rampa()
        self.assertEqual(r["habilitados"], [])
        self.assertEqual(r["iniciados"], [])
        self.assertEqual(r["ja_ativos"], ["a"])
        self.assertEqual(db.um("SELECT iniciado_em FROM instancia WHERE nome='a'")
                         ["iniciado_em"], inicio)


class ConversaTest(Base):
    """Conversa por chip: grava tudo, responde so dentro de conversa aberta."""

    def chip(self, nome, **kw):
        return ChipTest.chip(self, nome, **kw)

    def webhook(self, chip, tel, texto, de_mim=False, mid=None, extra=None):
        from disparador import servidor
        chave = {"remoteJid": f"{tel}@s.whatsapp.net", "fromMe": de_mim,
                 "id": mid or f"M{abs(hash((chip, tel, texto, de_mim)))}"}
        chave.update(extra or {})
        return servidor._webhook("evolution", {"event": "messages.upsert", "instance": chip,
                                               "data": {"key": chave,
                                                        "message": {"conversation": texto}}})

    def test_entrada_e_saida_do_celular_viram_uma_conversa(self):
        from disparador import conversas
        self.chip("c1")
        self.webhook("c1", "5517999990001", "oi, quero saber mais")
        self.webhook("c1", "5517999990001", "claro! te explico", de_mim=True)
        ms = conversas.thread("c1", "5517999990001")
        self.assertEqual([(m["direcao"], m["texto"]) for m in ms],
                         [("entrada", "oi, quero saber mais"), ("saida", "claro! te explico")])
        self.assertEqual(conversas.nao_lidas(), 1)
        conversas.marcar_lida("c1", "5517999990001")
        self.assertEqual(conversas.nao_lidas(), 0)

    def test_webhook_repetido_nao_duplica(self):
        from disparador import conversas
        self.chip("c1")
        for _ in range(3):
            self.webhook("c1", "5517999990001", "oi", mid="IGUAL")
        self.assertEqual(len(conversas.thread("c1", "5517999990001")), 1)

    def test_grupo_e_status_nao_viram_conversa(self):
        from disparador import servidor
        self.chip("c1")
        for jid in ("120363@g.us", "status@broadcast"):
            servidor._webhook("evolution", {"event": "messages.upsert", "instance": "c1",
                "data": {"key": {"remoteJid": jid, "id": jid}, "message": {"conversation": "x"}}})
        self.assertEqual(db.um("SELECT COUNT(*) n FROM mensagem")["n"], 0)

    def test_conversa_e_separada_por_chip(self):
        from disparador import conversas
        self.chip("c1"), self.chip("c2")
        self.webhook("c1", "5517999990001", "pro chip 1")
        self.webhook("c2", "5517999990001", "pro chip 2")
        self.assertEqual([m["texto"] for m in conversas.thread("c1", "5517999990001")],
                         ["pro chip 1"])
        self.assertEqual(len(conversas.conversas("c2")), 1)

    def enviar_falso(self):
        mod = provedores.pegar("evolution")
        enviados = []
        orig = (mod.enviar, mod.pronto)
        mod.enviar = lambda d, t, c, m=None: (enviados.append((c["instancia"], d, t)),
                                              provedores.OK(f"R{len(enviados)}"))[1]
        mod.pronto = lambda c: (True, "")
        self.addCleanup(lambda: (setattr(mod, "enviar", orig[0]), setattr(mod, "pronto", orig[1])))
        return enviados

    def test_responder_sai_pelo_chip_da_conversa(self):
        from disparador import conversas
        self.chip("c1")
        enviados = self.enviar_falso()
        self.webhook("c1", "5517999990001", "oi")
        ok, erro = conversas.responder("c1", "5517999990001", "Olá!", config.carregar())
        self.assertTrue(ok, erro)
        self.assertEqual(enviados, [("c1", "5517999990001", "Olá!")])
        ultima = conversas.thread("c1", "5517999990001")[-1]
        self.assertEqual((ultima["direcao"], ultima["origem"]), ("saida", "manual"))

    def test_responder_so_dentro_de_conversa_aberta(self):
        """O painel nao pode virar um segundo caminho de disparo."""
        from disparador import conversas
        self.chip("c1"), self.chip("c2")
        enviados = self.enviar_falso()
        self.webhook("c2", "5517999990001", "falei com o outro chip")
        ok, erro = conversas.responder("c1", "5517999990001", "oi", config.carregar())
        self.assertFalse(ok)
        ok, erro = conversas.responder("c1", "5517988887777", "oi", config.carregar())
        self.assertFalse(ok)
        self.assertEqual(enviados, [])

    def test_responder_respeita_blocklist(self):
        from disparador import conversas
        self.chip("c1")
        enviados = self.enviar_falso()
        self.webhook("c1", "5517999990001", "SAIR")     # opt-out pelo proprio webhook
        self.assertEqual(regras.bloqueado("5517999990001"), "optout")
        ok, erro = conversas.responder("c1", "5517999990001", "mas espera", config.carregar())
        self.assertFalse(ok)
        self.assertIn("blocklist", erro)
        self.assertEqual(enviados, [])

    def test_responder_exige_chip_conectado(self):
        from disparador import conversas
        self.chip("c1", estado="close")
        enviados = self.enviar_falso()
        self.webhook("c1", "5517999990001", "oi")
        ok, erro = conversas.responder("c1", "5517999990001", "Olá", config.carregar())
        self.assertFalse(ok)
        self.assertEqual(enviados, [])

    def test_campanha_abre_a_conversa_e_eco_do_webhook_nao_duplica(self):
        from disparador import conversas
        self.chip("c1", limite_dia=99999, aquecer=0)
        enviados = self.enviar_falso()
        self.contatos(1)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        campanhas.montar_publico(c["id"])
        camp = db.um("SELECT * FROM campanha WHERE id=?", c["id"])
        rem = instancias.remetentes(camp, config.carregar())[0]
        m = self.motor()
        envio = m._reivindicar(camp, rem)
        self.assertTrue(m._processar(envio, camp, rem, config.carregar()))
        tel = envio["telefone"]
        # o gateway ecoa a propria mensagem como fromMe, com o mesmo id
        self.webhook("c1", tel, "eco", de_mim=True, mid="R1")
        ms = conversas.thread("c1", tel)
        self.assertEqual(len(ms), 1)
        self.assertEqual((ms[0]["origem"], ms[0]["envio_id"]), ("campanha", envio["id"]))
        # o cliente responde e ja da para responder de volta
        self.webhook("c1", tel, "tenho interesse")
        ok, erro = conversas.responder("c1", tel, "Que bom!", config.carregar())
        self.assertTrue(ok, erro)
        self.assertEqual(len(enviados), 2)


class AquecimentoTest(Base):
    """A rampa existe, mas trafego artificial nao."""

    def chip(self, nome, **kw):
        return ChipTest.chip(self, nome, **kw)

    def test_nunca_fabrica_conversa_mesmo_com_dois_chips(self):
        self.chip("a"), self.chip("b")
        self.contatos(5)
        self.assertFalse(aquecimento.rodar(config.carregar()))
        self.assertEqual(db.um("SELECT COUNT(*) n FROM aquecimento")["n"], 0)
        self.assertEqual(db.um("SELECT COUNT(*) n FROM envio")["n"], 0)

    def test_rampa_legitima_continua_por_chip(self):
        c = self.chip("a", limite_dia=99999)
        self.assertEqual(instancias.limite_efetivo(c), instancias.RAMPA[0])
        self.assertEqual(aquecimento.POR_DIA, [])

    def test_chip_sem_primeira_conexao_nao_envelhece_a_rampa(self):
        antigo = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
        c = self.chip("a", estado="close", iniciado_em=None, limite_dia=99999)
        db.exec_("UPDATE instancia SET criado_em=? WHERE id=?", antigo, c["id"])
        c = db.um("SELECT * FROM instancia WHERE id=?", c["id"])
        self.assertEqual(instancias.limite_efetivo(c), instancias.RAMPA[0])
        self.assertEqual(instancias.dia_da_rampa(c), 1)
        self.assertEqual(instancias.estado_da_rampa(c), "aguardando_conexao")


class SegurancaNovaTest(Base):
    def test_whatsapp_sem_optin_nao_entra_na_fila(self):
        self.contatos(3, consentimento=False)
        c = self.campanha(canal="whatsapp")
        self.assertEqual(campanhas.montar_publico(c["id"]), 0)
        sms = self.campanha(nome="sms")
        self.assertEqual(campanhas.montar_publico(sms["id"]), 3)

    def test_optout_em_voo_nao_e_sobrescrito_como_enviado(self):
        self.contatos(1)
        c = self.campanha()
        campanhas.montar_publico(c["id"])
        m = self.motor()
        envio = m._reivindicar(c["id"])
        camp = db.um("SELECT * FROM campanha WHERE id=?", c["id"])
        rem = instancias.remetentes(camp, config.carregar())[0]
        iniciou, liberar = threading.Event(), threading.Event()

        class Lento:
            pronto = staticmethod(lambda cfg: (True, ""))

            @staticmethod
            def enviar(*args, **kwargs):
                iniciou.set()
                liberar.wait(3)
                return provedores.OK("aceito-remoto")

        original = provedores.REGISTRO["simulado"]
        provedores.REGISTRO["simulado"] = Lento
        try:
            t = threading.Thread(target=m._processar,
                                 args=(envio, camp, rem, config.carregar()))
            t.start()
            self.assertTrue(iniciou.wait(2))
            regras.registrar_optout(envio["telefone"], origem="teste:concorrente")
            liberar.set()
            t.join(3)
        finally:
            provedores.REGISTRO["simulado"] = original
        self.assertEqual(db.um("SELECT estado FROM envio")["estado"], "cancelado")

    def test_reserva_atomica_nao_estoura_ultima_vaga_da_cota(self):
        self.contatos(3)
        c = self.campanha(limite_dia=1)
        campanhas.montar_publico(c["id"])
        m = self.motor()
        self.assertIsNotNone(m._reivindicar(c["id"]))
        self.assertIsNone(m._reivindicar(c["id"]))
        self.assertEqual(db.um("SELECT COUNT(*) n FROM envio WHERE estado='enviando'")["n"], 1)

    def test_teto_de_chips_e_invariante_do_banco(self):
        self.assertEqual(instancias.MAX_INSTANCIAS, 25)
        for i in range(instancias.MAX_INSTANCIAS):
            db.exec_("INSERT INTO instancia(nome,criado_em) VALUES(?,?)", f"i{i}", db.agora())
        with self.assertRaises(Exception):
            db.exec_("INSERT INTO instancia(nome,criado_em) VALUES('extra',?)", db.agora())

    def test_instancia_excedente_e_avisada_uma_vez_so(self):
        """O vigia sincroniza a cada minuto; o aviso do excedente nao pode repetir."""
        for i in range(instancias.MAX_INSTANCIAS):
            db.exec_("INSERT INTO instancia(nome,criado_em) VALUES(?,?)", f"i{i}", db.agora())
        mod = provedores.pegar("evolution")
        original = mod.listar_instancias
        mod.listar_instancias = lambda cfg: [{"nome": "sobra", "estado": "connecting"}]
        instancias._RECUSADOS_AVISADOS.discard("sobra")
        try:
            for _ in range(5):
                instancias.sincronizar({"provedores": {"evolution": {}}})
        finally:
            mod.listar_instancias = original
        self.assertEqual(db.um("SELECT COUNT(*) n FROM evento WHERE tipo='chip_ignorado_limite'")["n"], 1)
        self.assertIsNone(db.um("SELECT 1 FROM instancia WHERE nome='sobra'"))

    def test_estado_publica_versao_do_painel(self):
        from disparador import servidor
        v = servidor.estado_geral()["versao"]
        self.assertRegex(v, r"^\d+-\d+$")
        self.assertEqual(v, servidor.versao_painel())

    def test_banco_antigo_com_teto_de_15_sobe_para_o_novo(self):
        """O trigger e recriado a cada abertura: banco criado com 15 aceita 25."""
        c = db.conn()
        c.executescript("DROP TRIGGER IF EXISTS trg_instancia_maximo;"
                        "CREATE TRIGGER trg_instancia_maximo BEFORE INSERT ON instancia"
                        " WHEN (SELECT COUNT(*) FROM instancia) >= 15"
                        " BEGIN SELECT RAISE(ABORT, 'limite de 15 chips atingido'); END;")
        db._local = threading.local()          # reabre: o esquema roda de novo
        for i in range(20):
            db.exec_("INSERT INTO instancia(nome,criado_em) VALUES(?,?)", f"i{i}", db.agora())
        self.assertEqual(db.um("SELECT COUNT(*) n FROM instancia")["n"], 20)

    def test_importacao_do_painel_fica_no_diretorio_autorizado(self):
        dentro = os.path.join(self.tmp, "permitido.csv")
        fora = os.path.join(tempfile.gettempdir(), "fora-do-disparador.csv")
        for caminho in (dentro, fora):
            with open(caminho, "w", encoding="utf-8") as f:
                f.write("telefone\n17999999999\n")
        cfg = {"importacao": {"diretorios": [self.tmp], "max_bytes": 1024}}
        self.assertEqual(importar.caminho_painel(dentro, cfg), os.path.realpath(dentro))
        with self.assertRaises(PermissionError):
            importar.caminho_painel(fora, cfg)

    def test_url_generica_bloqueia_rede_privada(self):
        self.assertFalse(seguranca.validar_url_saida("http://127.0.0.1:8080")[0])
        self.assertTrue(seguranca.validar_url_saida(
            "http://127.0.0.1:8080", permitir_rede_privada=True)[0])

    def test_cliente_http_nao_segue_redirect_com_credencial(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        atingiu_destino = []

        class Redireciona(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/origem":
                    self.send_response(302)
                    self.send_header("Location", "/destino")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    atingiu_destino.append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Redireciona)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            st, _, _ = provedores.http(
                f"http://127.0.0.1:{srv.server_address[1]}/origem", metodo="GET",
                cabecalhos={"Authorization": "Bearer SEGREDO"},
                permitir_rede_privada=True)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(st, 302)
        self.assertEqual(atingiu_destino, [])

    def test_trocar_origem_nao_reaproveita_credencial_antiga(self):
        cfg = config.carregar()
        cfg["provedores"]["evolution"].update(url="http://127.0.0.1:8080",
                                               apikey="SEGREDO-ANTIGO")
        config._cache = cfg
        novo = json.loads(json.dumps(cfg))
        novo["provedores"]["evolution"].update(url="http://127.0.0.1:9090", apikey=None)
        fundido = config.fundir_segredos(novo)
        self.assertIsNone(fundido["provedores"]["evolution"]["apikey"])

    def test_meta_webhook_exige_hmac_do_corpo_exato(self):
        import hashlib, hmac
        cfg = config.carregar()
        cfg["provedores"]["whatsapp_cloud"]["app_secret"] = "segredo-meta"
        config.salvar(cfg)
        bruto = b'{"entry":[]}'
        assinatura = "sha256=" + hmac.new(b"segredo-meta", bruto, hashlib.sha256).hexdigest()
        self.assertTrue(seguranca.validar_webhook(
            "whatsapp_cloud", bruto, {"entry": []},
            {"X-Hub-Signature-256": assinatura}, {}, "https://x/webhook"))
        self.assertFalse(seguranca.validar_webhook(
            "whatsapp_cloud", bruto + b" ", {"entry": []},
            {"X-Hub-Signature-256": assinatura}, {}, "https://x/webhook"))

    def test_resposta_so_altera_envio_da_conta_que_recebeu(self):
        from disparador import servidor
        self.contatos(1)
        contato = db.um("SELECT * FROM contato")
        a = self.campanha(nome="conta-a", provedor="twilio")
        b = self.campanha(nome="conta-b", provedor="twilio")
        legado = self.campanha(nome="sem-conta", provedor="twilio")
        for camp, conta in ((a, "AC-CONTA-A"), (b, "AC-CONTA-B"), (legado, None)):
            db.exec_("INSERT INTO envio(campanha_id,contato_id,telefone,estado,"
                     "id_externo,provedor_externo,conta_externa,atualizado_em) "
                     "VALUES(?,?,?,'enviado',?,?,?,?)", camp["id"], contato["id"],
                     contato["telefone"], "SM-" + (conta or "LEGADO"),
                     "twilio", conta, db.agora())
        servidor._webhook("twilio", {"Body": "Tenho interesse",
                          "From": "+" + contato["telefone"],
                          "AccountSid": "AC-CONTA-A"})
        estados = {r["id_externo"]: r["estado"] for r in db.todos(
            "SELECT id_externo,estado FROM envio")}
        self.assertEqual(estados, {"SM-AC-CONTA-A": "respondido",
                                   "SM-AC-CONTA-B": "enviado",
                                   "SM-LEGADO": "enviado"})


class ServidorTest(Base):
    """Sobe o servidor de verdade numa porta livre e fala HTTP com ele."""

    def setUp(self):
        super().setUp()
        from http.server import ThreadingHTTPServer
        from disparador import servidor
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), servidor.App)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, rota, corpo, autenticado=True, tipo="application/json", extras=None):
        import urllib.error, urllib.request
        cab = {"Content-Type": tipo}
        if autenticado:
            cab["Authorization"] = "Bearer " + self.admin_token
        cab.update(extras or {})
        req = urllib.request.Request(self.url + rota, json.dumps(corpo).encode(), cab)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def _post_bruto(self, rota, corpo_bytes, tipo="text/csv"):
        import urllib.error, urllib.request
        req = urllib.request.Request(self.url + rota, corpo_bytes,
            {"Content-Type": tipo, "Authorization": "Bearer " + self.admin_token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_upload_de_csv_importa_e_nao_deixa_temporario(self):
        csv = (b"telefone;tipo;nome;status\r\n"
               b'"5517991234567";"MOVEL";"LOJA";""\r\n')
        st, r = self._post_bruto("/api/importar/arquivo?optin=1&optin_origem=loja", csv)
        self.assertEqual(st, 200)
        self.assertEqual(r["novos"], 1)
        self.assertEqual(r["com_optin"], 1)
        self.assertEqual(db.um("SELECT consentimento_origem o FROM contato "
                               "WHERE telefone='5517991234567'")["o"], "loja")
        import glob
        from disparador import config
        sobra = glob.glob(os.path.join(config.RAIZ, "dados", "importacoes", "tmp*"))
        self.assertEqual(sobra, [])

    def test_upload_optin_sem_origem_recusa(self):
        st, r = self._post_bruto("/api/importar/arquivo?optin=1",
                                 b"telefone\r\n5517991234567\r\n")
        self.assertEqual(st, 400)

    def test_api_sem_autenticacao_e_recusada(self):
        st, r = self.post("/api/motor", {"acao": "ligar"}, autenticado=False)
        self.assertEqual(st, 401)
        self.assertEqual(db.um("SELECT COUNT(*) n FROM evento")["n"], 0)

    def test_api_ativa_rampa_sem_fabricar_trafego(self):
        ChipTest.chip(self, "aberto", aquecer=0, iniciado_em=None)
        ChipTest.chip(self, "fechado", estado="close", aquecer=0, iniciado_em=None)
        st, r = self.post("/api/instancias/rampa", {})
        self.assertEqual(st, 200)
        self.assertEqual(r["habilitados"], ["aberto", "fechado"])
        self.assertEqual(r["iniciados"], ["aberto"])
        self.assertEqual(r["aguardando_conexao"], ["fechado"])
        self.assertEqual(db.um("SELECT COUNT(*) n FROM aquecimento")["n"], 0)
        self.assertEqual(db.um("SELECT COUNT(*) n FROM envio")["n"], 0)

    def test_formulario_cross_site_nao_e_aceito(self):
        st, _ = self.post("/api/motor", {"acao": "ligar"},
                          tipo="application/x-www-form-urlencoded")
        self.assertEqual(st, 415)

    def test_origin_cross_site_e_recusada(self):
        st, _ = self.post("/api/motor", {"acao": "ligar"},
                          extras={"Origin": "https://malicioso.example"})
        self.assertEqual(st, 403)

    def test_corpo_maior_que_1_mib_e_recusado(self):
        st, _ = self.post("/api/motor", {"acao": "ligar", "lixo": "x" * (1024 * 1024)})
        self.assertEqual(st, 413)

    def test_api_limita_ritmo_de_campanha(self):
        st, r = self.post("/api/campanha", {"nome": "ritmo", "canal": "sms",
                          "provedor": "simulado", "modelo": "oi",
                          "por_minuto": 100000, "limite_dia": 10})
        self.assertEqual(st, 200)
        self.assertEqual(r["por_minuto"], 60.0)

    def test_estado_de_campanha_inventado_e_recusado(self):
        c = self.campanha()
        st, r = self.post("/api/campanha/estado", {"id": c["id"], "estado": "batata"})
        self.assertEqual(st, 400)
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "rascunho")

    def test_cli_status_le_o_diagnostico_sem_quebrar(self):
        """O `disparar.py status` consome o mesmo /api/estado do painel."""
        from disparador import servidor
        e = servidor.estado_geral()
        for n, p in e["provedores"].items():
            for campo in ("pronto", "simula", "mensagem", "acao", "faltando", "rotulo"):
                self.assertIn(campo, p, f"{n}.{campo}")

    def test_config_nao_devolve_o_valor_de_nenhum_segredo(self):
        from disparador import servidor
        cfg = config.carregar()
        cfg["provedores"]["evolution"]["apikey"] = "NAO-PODE-SAIR-DAQUI"
        cfg["provedores"]["whatsapp_cloud"]["token"] = "TOKEN-SECRETO"
        config._cache = cfg
        saida = json.dumps(servidor.config_seguro(), ensure_ascii=False)
        self.assertNotIn("NAO-PODE-SAIR-DAQUI", saida)
        self.assertNotIn("TOKEN-SECRETO", saida)
        # mas a tela precisa saber que existe credencial guardada
        visao = servidor.config_seguro()["provedores"]["evolution"]
        self.assertIn("apikey", visao["_preenchidos"])
        self.assertIsNone(visao["apikey"])

    def test_config_redige_segredo_aninhado(self):
        from disparador import servidor
        cfg = config.carregar()
        cfg["provedores"]["sms_http"]["cabecalhos"] = {
            "Authorization": "Bearer NAO-VAZA"}
        config._cache = cfg
        saida = servidor.config_seguro()
        self.assertNotIn("NAO-VAZA", json.dumps(saida))
        self.assertIsNone(saida["provedores"]["sms_http"]["cabecalhos"]["Authorization"])

    def test_salvar_config_nao_apaga_o_segredo_guardado(self):
        """A tela nunca recebe o token; devolver o formulário o apagaria."""
        cfg = config.carregar()
        cfg["provedores"]["evolution"]["apikey"] = "GUARDADA"
        config._cache = cfg
        from disparador import servidor
        devolvido = servidor.config_seguro()
        fundido = config.fundir_segredos(devolvido)
        self.assertEqual(fundido["provedores"]["evolution"]["apikey"], "GUARDADA")
        self.assertNotIn("_preenchidos", fundido["provedores"]["evolution"])

    def test_campanha_com_provedor_incompleto_nao_comeca(self):
        c = self.campanha(canal="whatsapp", provedor="evolution")
        st, r = self.post("/api/campanha/estado", {"id": c["id"], "estado": "rodando"})
        self.assertEqual(st, 400)
        self.assertTrue(r["erro"] and r["acao"])
        self.assertIn("url", r["faltando"])
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "rascunho")

    def test_whatsapp_roda_com_um_chip_aberto_mesmo_principal_close(self):
        """O gate de 'rodar' checava so a instancia unica do config ('principal').
        Com a principal caida mas outros chips conectados, barrava a campanha
        inteira com 'chip desconectado'. Basta UM chip ativo e aberto."""
        cfg = config.carregar()
        cfg["provedores"]["evolution"].update(
            url="http://127.0.0.1:9", instancia="principal", apikey="x")
        config.salvar(cfg)
        ChipTest.chip(self, "principal", estado="close", ativa=1)   # a do config, caida
        ChipTest.chip(self, "chip03", estado="open", ativa=1)       # conectada de verdade
        self.contatos(2)                                            # com opt-in (padrao)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        campanhas.montar_publico(c["id"])
        st, r = self.post("/api/campanha/estado", {"id": c["id"], "estado": "rodando"})
        # passou do gate de conexao -> agora pede confirmacao (nao 'chip desconectado')
        self.assertEqual(st, 409)
        self.assertTrue(r.get("confirmar"))
        self.assertNotIn("desconectado", (r.get("erro") or ""))

    def test_whatsapp_barra_quando_nenhum_chip_conectado(self):
        cfg = config.carregar()
        cfg["provedores"]["evolution"].update(
            url="http://127.0.0.1:9", instancia="principal", apikey="x")
        config.salvar(cfg)
        ChipTest.chip(self, "principal", estado="close", ativa=1)
        ChipTest.chip(self, "chip03", estado="close", ativa=1)
        self.contatos(2)
        c = self.campanha(canal="whatsapp", provedor="evolution")
        campanhas.montar_publico(c["id"])
        st, r = self.post("/api/campanha/estado", {"id": c["id"], "estado": "rodando"})
        self.assertEqual(st, 409)
        self.assertEqual(r["erro"], "nenhum chip conectado")
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "rascunho")

    def test_avisos_junta_eventos_problema_e_bloqueios(self):
        """A aba avisos precisa mostrar chip fora do ar, redistribuicao e bloqueio
        numa lista so, ordenada, sem o operador cacar no log cru."""
        from disparador import servidor
        db.log("chip_indisponivel", "chip03: 401")
        db.log("faixa_redistribuida", {"de": "chip03"})
        db.log("importacao", {"novos": 1})          # NAO e aviso: deve ficar de fora
        db.exec_("INSERT INTO bloqueio(telefone,motivo,origem,criado_em)"
                 " VALUES(?,?,?,?)", "5517999999999", "optout", "stop", db.agora())
        av = servidor.avisos_recentes(50)
        titulos = {a["titulo"] for a in av}
        self.assertIn("chip fora do ar", titulos)
        self.assertIn("fila redistribuida", titulos)
        self.assertIn("contato bloqueado", titulos)
        self.assertNotIn("importacao", {a["categoria"] for a in av})
        self.assertTrue(all(a.get("quando") and a.get("categoria") for a in av))

    def test_contador_disparadas_soma_offset_sem_afetar_cota(self):
        """Offset de envios feitos por fora entra so no contador do painel.
        Nao vira envio nem mexe na cota do dia."""
        from disparador import servidor
        self.contatos(3)
        c = self.campanha(provedor="simulado")
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE envio SET estado='enviado', enviado_em=?", db.agora())
        est = servidor.estado_geral()
        self.assertEqual(est["disparadas"], 3 + 12000)   # reais + offset padrao
        self.assertEqual(est["hoje"], 3)                 # cota do dia ignora offset

    def test_disparo_real_exige_confirmacao_explicita(self):
        """Aba velha ou clique errado nao pode comecar a mandar de verdade."""
        cfg = config.carregar()
        cfg["provedores"]["sms_http"].update(
            url="https://exemplo.com/send",
            corpo={"number": "{destino}", "msg": "{texto}"},
            optout_externo=True)
        config.salvar(cfg)          # a rota relê do disco a cada chamada
        self.contatos(3)
        c = self.campanha(provedor="sms_http")
        campanhas.montar_publico(c["id"])
        st, r = self.post("/api/campanha/estado", {"id": c["id"], "estado": "rodando"})
        self.assertEqual(st, 409)
        self.assertTrue(r["confirmar"])
        self.assertEqual(r["na_fila"], 3)
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "rascunho")
        st, r = self.post("/api/campanha/estado",
                          {"id": c["id"], "estado": "rodando", "confirmar": True})
        self.assertEqual(st, 200)
        self.assertEqual(db.um("SELECT estado FROM campanha")["estado"], "rodando")

    def test_ensaio_nao_pede_confirmacao_mas_se_declara(self):
        self.contatos(2)
        c = self.campanha(provedor="simulado")
        campanhas.montar_publico(c["id"])
        st, r = self.post("/api/campanha/estado", {"id": c["id"], "estado": "rodando"})
        self.assertEqual(st, 200)
        st, r = self.post("/api/teste", {"telefone": "17 99123-4567",
                                         "provedor": "simulado", "canal": "sms",
                                         "modelo": "Oi {nome}"})
        self.assertTrue(r["simulado"], "o painel precisa saber que foi ensaio")

    def test_teste_real_manda_para_o_numero_mas_e_limitado(self):
        """O operador testa no proprio numero: o envio chega ao adaptador,
        mas e limitado a poucos por minuto para nao virar atalho de spam."""
        enviados = []

        class RealFalso:
            SIMULA = False
            CANAL = ("sms",)

            @staticmethod
            def diagnosticar(cfg):
                return provedores.diag(ok=True, mensagem="configurado")

            @staticmethod
            def enviar(destino, texto, cfg, meta=None):
                enviados.append(destino)
                return provedores.OK("id_" + str(len(enviados)))

        provedores.REGISTRO["real_falso"] = RealFalso
        try:
            oks = 0
            for _ in range(7):
                st, r = self.post("/api/teste", {"telefone": "17 99123-4567",
                                  "provedor": "real_falso", "canal": "sms",
                                  "modelo": "oi"})
                if st == 200 and r.get("ok"):
                    oks += 1
                else:
                    self.assertEqual(st, 429)      # barrado pelo rate-limit
        finally:
            provedores.REGISTRO.pop("real_falso", None)
        self.assertGreaterEqual(oks, 1)            # o primeiro chega ao adaptador
        self.assertLessEqual(oks, 5)               # mas nao ilimitado
        self.assertTrue(enviados)

    def test_teste_com_chips_exige_um_conectado(self):
        """Provedor tipo Evolution precisa de um chip aberto para o teste."""
        st, r = self.post("/api/teste", {"telefone": "17 99123-4567",
                          "provedor": "evolution", "canal": "whatsapp", "modelo": "oi"})
        self.assertEqual(st, 409)
        self.assertIn("chip", r["erro"])

    def test_status_desconhecido_do_provedor_nao_suja_o_banco(self):
        self.contatos(1)
        c = self.campanha(canal="whatsapp", provedor="whatsapp_cloud")
        campanhas.montar_publico(c["id"])
        db.exec_("UPDATE envio SET estado='enviado', id_externo='wamid.X', enviado_em=?, "
                 "provedor_externo='whatsapp_cloud', conta_externa='PHONE-A'", db.agora())
        for meta_status, esperado in (("delivered", "entregue"), ("read", "lido"),
                                      ("inventado", "lido")):
            from disparador import servidor
            servidor._webhook("whatsapp_cloud", {"entry": [{"changes": [{"value": {
                "metadata": {"phone_number_id": "PHONE-A"},
                "statuses": [{"id": "wamid.X", "recipient_id": "5517990000000",
                              "status": meta_status}]}}]}]})
            self.assertEqual(db.um("SELECT estado FROM envio")["estado"], esperado,
                             meta_status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
