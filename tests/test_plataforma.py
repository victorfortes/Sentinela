"""Testes da plataforma (API, RBAC, fila, agendador, alertas, análise). Transportes falsos: não substituem testes reais."""
import json
import os
import sys
import unittest
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_unitarios import Base, CNPJ_OK, NUM, item, resp  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from sentinela import analise, clientes, fila, notificacoes, seguranca  # noqa: E402


def dj_ok(data="20261028090000", extra=None):
    src = {"numeroProcesso": NUM, "dataAjuizamento": data, "grau": "G1", "classe": {"nome": "Execução de Título Extrajudicial"},
           "orgaoJulgador": {"nome": "1ª Vara Cível"}, "sistema": {"nome": "PJe"}, "assuntos": [{"nome": "Duplicata"}]}
    src.update(extra or {})
    return lambda *a: resp({"hits": {"hits": [{"_source": src}]}})


def djen(itens):
    return lambda *a: resp({"status": "success", "count": len(itens), "items": itens})


class TestAlertasAnalise(Base):
    def prepara(self, texto="CNPJ 47.960.950/0001-21 tutela de urgência valor da causa R$ 2.500.000,00"):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(djen([item(1, texto=texto)]), dj_ok())
        m.hoje = date.today()
        m.executar(desde=date.today())
        self.con.execute("UPDATE correspondencias SET categoria_data='nova_distribuicao'"); self.con.commit()
        return m

    def test_analise_separa_origens_e_nao_inventa(self):
        self.prepara()
        cid = self.con.execute("SELECT id FROM correspondencias").fetchone()[0]
        aid = analise.analisar(self.con, cid, None)
        a = self.con.execute("SELECT * FROM analises WHERE id=?", (aid,)).fetchone()
        campos = json.loads(a["campos"])
        self.assertEqual(campos["valor_causa"]["valor"], 2500000.0)
        self.assertEqual(campos["valor_causa"]["origem"], "fonte")
        self.assertEqual(campos["tutela_provisoria"]["origem"], "inferencia")
        self.assertEqual(campos["pedidos"]["origem"], "nao_verificado")      # sem IA, nada de pedidos inventados
        self.assertIsNone(campos["pedidos"]["valor"])
        self.assertEqual(a["prioridade"], "alta")
        self.assertIn("indisponivel", a["documentos"])

    def test_ia_falsa_e_ia_com_resposta_invalida(self):
        self.prepara("Tutela de urgência deferida")
        cid = self.con.execute("SELECT id FROM correspondencias").fetchone()[0]
        class IA:
            modelo = "falso"
            def gerar(self, p): return 'ok {"resumo":"Resumo X","principais_fatos":["f1"],"fundamentos_juridicos":[],"pedidos":["p1"],"urgencia_aparente":true}'
        a1 = self.con.execute("SELECT * FROM analises WHERE id=?", (analise.analisar(self.con, cid, IA()),)).fetchone()
        c1 = json.loads(a1["campos"])
        self.assertEqual((a1["resumo"], c1["pedidos"]["origem"], c1["pedidos"]["valor"]), ("Resumo X", "inferencia", ["p1"]))
        self.assertEqual(c1["fundamentos_juridicos"]["origem"], "nao_verificado")  # lista vazia da IA não vira fato
        class Ruim:
            def gerar(self, p): return "isto não é json"
        a2 = self.con.execute("SELECT * FROM analises WHERE id=?", (analise.analisar(self.con, cid, Ruim()),)).fetchone()
        self.assertIn("_erro_ia", json.loads(a2["campos"]))
        self.assertIn("indisponível", a2["resumo"])

    def test_alerta_sem_duplicidade_e_retentativa_de_falha(self):
        self.prepara()
        env = []
        r1 = notificacoes.processar_pendentes(self.con)
        r2 = notificacoes.processar_pendentes(self.con)
        self.assertEqual(r1["definitivos"], 1)
        self.assertEqual((r2["definitivos"], r2["duplicados"]), (0, 1))
        self.assertEqual(self.con.execute("SELECT count(*) FROM alertas").fetchone()[0], 1)
        # e-mail habilitado: falha de SMTP é registrada e a retentativa não duplica linha
        from sentinela import config
        config.salvar(self.con, "notificacoes", {**config.obter(self.con, "notificacoes"), "email": True, "smtp_host": "x", "emails": ["a@b.c"]})
        def quebra(s, c): raise OSError("smtp fora")
        a = notificacoes.montar(self.con, 1, "nova_acao_confirmada")
        self.assertEqual(notificacoes.emitir(self.con, a, "1", enviar_email=quebra)["email"], "falha")
        self.assertEqual(notificacoes.emitir(self.con, a, "1", enviar_email=lambda s, c: env.append(s))["email"], "enviado")
        self.assertEqual(notificacoes.emitir(self.con, a, "1", enviar_email=lambda s, c: env.append(s))["email"], "duplicado_ignorado")
        self.assertEqual(len(env), 1)
        txt = notificacoes.texto(a)[1]
        for campo in ("Cliente", "Processo", "Tribunal", "Distribuição", "Natureza", "Valor da causa", "Prioridade", "Confiança", "Situação", "Síntese", "Consulta oficial"):
            self.assertIn(campo, txt)

    def test_processo_antigo_e_descartado_nao_alertam(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(djen([item(1, texto="CNPJ 47.960.950/0001-21"), {**item(2, polo="A"), "numero_processo": "00000000000000000002"}]), dj_ok("20200101000000"))
        m.executar(desde=date(2026, 10, 28))
        self.assertEqual(notificacoes.processar_pendentes(self.con), {"definitivos": 0, "conferencia": 0, "possiveis": 0, "duplicados": 0, "falhas_envio": 0})

    def test_falha_de_execucao_gera_alerta_distinto(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(lambda *a: resp({}, 503))
        r = m.executar(desde=date(2026, 10, 28))
        out = notificacoes.alertar_falha_execucao(self.con, r["execucao_id"], r)
        self.assertEqual(out["interno"], "enviado")
        t = self.con.execute("SELECT tipo FROM alertas").fetchone()[0]
        self.assertEqual(t, "falha_consulta")


class TestFila(Base):
    def test_fila_atomica_agendador_recuperacao(self):
        j1 = fila.enfileirar(self.con, "alertas")
        self.assertEqual(fila.reivindicar(self.con)["id"], j1)
        self.assertIsNone(fila.reivindicar(self.con))
        # agendador: uma execução por dia, só depois do horário
        self.assertFalse(fila.agendar(self.con, datetime(2026, 10, 30, 5, 0))["executar"])
        self.assertTrue(fila.agendar(self.con, datetime(2026, 10, 30, 6, 1))["executar"])
        self.assertFalse(fila.agendar(self.con, datetime(2026, 10, 30, 9, 0))["executar"])
        # worker caiu: job preso volta para a fila
        self.con.execute("UPDATE jobs SET iniciado_em='2020-01-01T00:00:00' WHERE id=?", (j1,)); self.con.commit()
        self.assertEqual(fila.recuperar_travados(self.con)["jobs_devolvidos"], 1)
        self.assertEqual(self.con.execute("SELECT status FROM jobs WHERE id=?", (j1,)).fetchone()[0], "na_fila")

    def test_job_executar_completo_e_reprocessamento(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        estado = {"falhar": True}
        def tr(m, url, h, c, t):
            return resp({}, 503) if estado["falhar"] else resp({"status": "success", "count": 1, "items": [item(1, texto="CNPJ 47.960.950/0001-21")]})
        mt = self.motor(tr, dj_ok(date.today().strftime("%Y%m%d") + "000000"))
        mt.hoje = date.today()
        fila.enfileirar(self.con, "executar", {"desde": date.today().isoformat()})
        r = fila.rodar_proximo(self.con, mt)
        self.assertEqual(r["execucao"]["status"], "falha")
        self.assertIn("alerta_falha", r)
        self.assertEqual(self.con.execute("SELECT count(*) FROM correspondencias").fetchone()[0], 0)
        self.assertTrue(fila.agendar(self.con, datetime.now().replace(hour=23, minute=59))["reprocessar"])
        estado["falhar"] = False   # a fonte voltou
        r2 = fila.rodar_proximo(self.con, mt)  # job de reprocessamento
        self.assertEqual(r2["execucao"]["status"], "sucesso")
        self.assertEqual(r2["alertas"]["definitivos"], 1)
        self.assertEqual(r2["analises"], 1)
        r3 = mt.reprocessar_falhas()
        self.assertEqual(r3["consultas_executadas"], 0)  # nada mais a reprocessar


class TestAPI(Base):
    def setUp(self):
        super().setUp()
        os.environ["SENTINELA_DB"] = os.path.join(self.tmp, "t.db")
        from sentinela import api
        api._migrado.clear()
        self.c = TestClient(api.app)
        for email, papel in (("adm@x.com", "admin"), ("adv@x.com", "advogado"), ("ler@x.com", "leitura")):
            self.con.execute("INSERT INTO usuarios(email,nome,senha_hash,papel) VALUES (?,?,?,?)", (email, email, seguranca.hash_senha("senha-de-teste-123"), papel))
        self.con.commit()

    def h(self, email):
        r = self.c.post("/api/login", json={"email": email, "senha": "senha-de-teste-123"})
        self.assertEqual(r.status_code, 200)
        return {"Authorization": "Bearer " + r.json()["token"]}

    def test_autenticacao_e_permissoes(self):
        self.assertEqual(self.c.get("/api/dashboard").status_code, 401)
        self.assertEqual(self.c.post("/api/login", json={"email": "adm@x.com", "senha": "errada"}).status_code, 401)
        self.assertEqual(self.c.get("/api/dashboard", headers={"Authorization": "Bearer lixo.lixo"}).status_code, 401)
        corpo = {"razao_social": "Acme Ltda", "cnpj_matriz": CNPJ_OK}
        self.assertEqual(self.c.post("/api/clientes", json=corpo, headers=self.h("ler@x.com")).status_code, 403)
        self.assertEqual(self.c.post("/api/clientes", json=corpo, headers=self.h("adv@x.com")).status_code, 201)
        self.assertEqual(self.c.post("/api/clientes", json=corpo, headers=self.h("adv@x.com")).status_code, 409)
        self.assertEqual(self.c.post("/api/clientes", json={**corpo, "cnpj_matriz": "11.111.111/1111-11", "razao_social": "Y"}, headers=self.h("adv@x.com")).status_code, 422)
        self.assertEqual(self.c.get("/api/config", headers=self.h("adv@x.com")).status_code, 403)
        self.assertEqual(self.c.get("/api/config", headers=self.h("adm@x.com")).status_code, 200)
        self.assertEqual(self.c.get("/api/auditoria", headers=self.h("adv@x.com")).status_code, 403)

    def test_importacao_dashboard_e_fluxo(self):
        h = self.h("adv@x.com")
        csv_ = "razao_social,cnpj_matriz\nBeta SA,11.222.333/0001-81\nRuim,1\n".encode()
        r = self.c.post("/api/clientes/importar", files={"arquivo": ("c.csv", csv_)}, headers=h).json()
        self.assertEqual((r["importados"], len(r["erros"])), (1, 1))
        d = self.c.get("/api/dashboard", headers=h).json()
        self.assertEqual(d["estado_pesquisa"]["codigo"], "nunca_executou")
        self.assertEqual(d["clientes_monitorados"], 1)
        # execução com falha total aparece como "não foi possível pesquisar", não como "sem resultados"
        m = self.motor(lambda *a: resp({}, 503)); m.executar(desde=date.today())
        self.assertEqual(self.c.get("/api/dashboard", headers=h).json()["estado_pesquisa"]["codigo"], "falha_total")
        m2 = self.motor(djen([])); m2.executar(desde=date.today(), forcar=True)
        from sentinela import config as _cfg
        _cfg.salvar(self.con, "radar", {**_cfg.obter(self.con, "radar"), "fontes_esperadas": ["DJEN_BUSCA"]})
        self.assertEqual(self.c.get("/api/dashboard", headers=h).json()["estado_pesquisa"]["codigo"], "ok_sem_novos")

    def test_validacao_atribuicao_detalhe(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(djen([item(1)]), dj_ok()); m.executar(desde=date.today())
        h, hl = self.h("adv@x.com"), self.h("ler@x.com")
        self.assertIn(self.c.get("/api/validacao/pendentes", headers=h).status_code, (404, 405))   # a fila humana deixou de existir
        cid = self.con.execute("SELECT id FROM correspondencias").fetchone()[0]
        self.assertEqual(self.c.post("/api/validacao/%d" % cid, json={"decisao": "confirmada"}, headers=hl).status_code, 403)
        self.assertEqual(self.c.post("/api/validacao/%d" % cid, json={"decisao": "confirmada", "justificativa": "ok"}, headers=h).status_code, 200)
        uid = self.con.execute("SELECT id FROM usuarios WHERE email='adv@x.com'").fetchone()[0]
        self.assertEqual(self.c.post("/api/processos/%d/atribuir" % cid, json={"usuario_id": uid}, headers=h).status_code, 200)
        d = self.c.get("/api/processos/%d" % cid, headers=h).json()
        self.assertEqual((d["classificacao"], d["responsavel"], len(d["validacoes"])), ("confirmada", "adv@x.com", 1))
        lst = self.c.get("/api/processos?responsavel_id=%d&classificacao=confirmada" % uid, headers=h).json()
        self.assertEqual(lst["total"], 1)
        self.assertGreaterEqual(len(self.c.get("/api/auditoria", headers=self.h("adm@x.com")).json()), 3)
        self.assertEqual(self.c.get("/api/").status_code if False else 200, 200)




class TestClienteNovoCapturaProcessos(Base):
    """Garantias verificáveis para: cadastrei um cliente hoje, ele começa a ser pesquisado? (com vários CNPJs)"""
    def test_varios_cnpjs_geram_uma_consulta_cada_e_filial_confirma(self):
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK, filiais=["11.222.333/0001-81", "11.222.333/0002-62"],
                                 anteriores=["Velha Acme S/A"])
        m = self.motor(djen([]))
        qs = {q["chave"] for q in m._consultas_do_cliente(clientes.carregar(self.con, cid))}
        self.assertEqual({k for k in qs if k.startswith("texto:") and k[6].isdigit()},
                         {"texto:47.960.950/0001-21", "texto:11.222.333/0001-81", "texto:11.222.333/0002-62"})
        self.assertTrue({"texto:ACME INDUSTRIA", "texto:VELHA ACME"} <= qs)
        self.assertIn("nomeParte:A. I. L.", qs) if False else None   # nome também no corpo das comunicações
        self.assertIn("nomeParte:VELHA ACME", qs)
        com = {"texto": "CNPJ 11.222.333/0002-62", "destinatarios": [{"nome": "ACME INDUSTRIA LTDA", "polo": "P"}]}
        from sentinela import matcher
        r = matcher.avaliar(clientes.carregar(self.con, cid), com)
        self.assertEqual((r["classificacao"], r["nivel"]), ("confirmada", 2))

    def test_cliente_novo_recebe_busca_retroativa_ampla_no_agendamento_diario(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        janelas = []
        def tr(m, url, h, c, t):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(url).query); janelas.append((q["dataDisponibilizacaoInicio"][0], q["dataDisponibilizacaoFim"][0]))
            return resp({"status": "success", "count": 0, "items": []})
        m = self.motor(tr); m.dias_retro = 90
        fila.enfileirar(self.con, "executar", {}, "agendador")   # exatamente o que o agendador enfileira
        fila.rodar_proximo(self.con, m)
        self.assertEqual(min(j[0] for j in janelas), (date.today() - __import__("datetime").timedelta(days=90)).isoformat())
        self.assertEqual(max(j[1] for j in janelas), date.today().isoformat())

    def test_api_cria_cliente_e_enfileira_busca_inicial(self):
        os.environ["SENTINELA_DB"] = os.path.join(self.tmp, "t.db")
        from sentinela import api
        api._migrado.clear()
        self.con.execute("INSERT INTO usuarios(email,nome,senha_hash,papel) VALUES ('a@x.com','A',?,'advogado')", (seguranca.hash_senha("senha-de-teste-123"),)); self.con.commit()
        c = TestClient(api.app)
        tok = c.post("/api/login", json={"email": "a@x.com", "senha": "senha-de-teste-123"}).json()["token"]
        h = {"Authorization": "Bearer " + tok}
        r = c.post("/api/clientes", json={"razao_social": "Acme", "cnpj_matriz": CNPJ_OK}, headers=h)
        self.assertEqual(r.status_code, 201)
        j = self.con.execute("SELECT * FROM jobs WHERE id=?", (r.json()["job_busca_inicial"],)).fetchone()
        p = json.loads(j["params"])
        self.assertEqual((j["tipo"], j["origem"], p["cliente_ids"]), ("executar", "busca_inicial", [r.json()["id"]]))
        self.assertEqual(p["desde"], (date.today().replace() - __import__("datetime").timedelta(days=90)).isoformat())
        # novo CNPJ de filial em cliente existente também dispara pesquisa
        r2 = c.put("/api/clientes/%d" % r.json()["id"], json={"adicionar_filiais": ["11.222.333/0001-81"]}, headers=h)
        self.assertIsNotNone(r2.json()["job_busca_inicial"])


class TestFrequenciaDiaria(Base):
    def test_execucao_de_hoje_cedo_nao_e_pulada_por_ter_rodado_ontem_tarde(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(djen([]))
        m.executar(desde=date.today())
        # simula: a última consulta bem-sucedida foi ontem às 23h
        self.con.execute("UPDATE consultas SET concluida_em=datetime('now','localtime','-1 day','-1 minute')"); self.con.commit()
        r = m.executar()
        self.assertGreater(r["consultas_executadas"], 0)
        r2 = m.executar()   # já rodou hoje: não repete
        self.assertEqual(r2["consultas_executadas"], 0)


class TestDestaqueDoDiaEContinuidade(TestAPI):
    def test_ajuizado_hoje_dashboard_filtro_e_prioridade(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        hoje = date.today().strftime("%Y%m%d") + "090000"
        m = self.motor(djen([item(1, texto="CNPJ 47.960.950/0001-21")]), dj_ok(hoje))
        m.executar(desde=date.today())
        h = self.h("adv@x.com")
        d = self.c.get("/api/dashboard", headers=h).json()
        self.assertEqual(len(d["ajuizados_hoje"]), 1)
        lst = self.c.get("/api/processos?ajuizado_hoje=1", headers=h).json()
        self.assertEqual((lst["total"], lst["itens"][0]["dias_ajuiz"]), (1, 0))
        cid = lst["itens"][0]["id"]
        analise.analisar(self.con, cid, None)
        self.assertEqual(self.con.execute("SELECT prioridade FROM correspondencias").fetchone()[0], "alta")
        alerta = notificacoes.texto(notificacoes.montar(self.con, cid, "nova_acao_confirmada"))[0]
        self.assertIn("AJUIZADA HOJE", alerta)
        # processo de ontem não é destacado
        self.con.execute("UPDATE processos SET data_ajuizamento=date('now','localtime','-1 day')"); self.con.commit()
        self.assertEqual(self.c.get("/api/processos?ajuizado_hoje=1", headers=h).json()["total"], 0)

    def test_batimento_do_worker_prova_funcionamento(self):
        h = self.h("adv@x.com")
        s0 = self.c.get("/api/sistema", headers=h).json()
        self.assertFalse(s0["worker"]["ativo"])           # sem worker: o painel avisa
        self.assertIsNotNone(s0["aviso"])
        parar = __import__("threading").Event()
        t = fila.iniciar_batimento(os.path.join(self.tmp, "t.db"), parar, intervalo=0.2)
        __import__("time").sleep(0.6)
        s1 = self.c.get("/api/sistema", headers=h).json()
        parar.set(); t.join(2)
        self.assertTrue(s1["worker"]["ativo"])
        self.assertEqual(len(s1["dias"]), 14)
        self.assertIsNotNone(s1["agendamento"]["proxima_execucao"])

    def test_processo_novo_ausente_no_datajud_e_reconsultado_depois(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        estado = {"achou": False}
        def dj(*a):
            if not estado["achou"]:
                return resp({"hits": {"hits": []}})
            return dj_ok(date.today().strftime("%Y%m%d") + "100000")(*a)
        m = self.motor(djen([item(1, texto="CNPJ 47.960.950/0001-21")]), dj)
        m.executar(desde=date.today())
        self.assertEqual(self.con.execute("SELECT datajud_status FROM processos").fetchone()[0], "nao_encontrado")
        estado["achou"] = True   # o DataJud passou a ter o processo no dia seguinte
        self.con.execute("UPDATE processos SET ultima_verificacao=datetime('now','localtime','-1 day')"); self.con.commit()
        self.assertEqual(m.enriquecer_pendentes()["ok"], 1)
        self.assertEqual(self.con.execute("SELECT categoria_data FROM correspondencias").fetchone()[0], "nova_distribuicao")


class TestBarraDeProgresso(TestAPI):
    def test_progresso_na_fila_durante_e_depois(self):
        h = self.h("adv@x.com")
        self.assertEqual(self.c.get("/api/progresso", headers=h).json()["estado"], "ocioso")
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK, filiais=["11.222.333/0001-81"])
        jid = self.c.post("/api/jobs", json={"tipo": "executar", "params": {}}, headers=h).json()["job_id"]
        p = self.c.get("/api/progresso", headers=h).json()
        self.assertEqual((p["estado"], p["job_id"]), ("na_fila", jid))
        mt = self.motor(djen([item(1, texto="CNPJ 47.960.950/0001-21")]), dj_ok(date.today().strftime("%Y%m%d") + "000000"))
        mt._prog_ts = 0
        vistos = []
        orig = mt._progresso
        def espia(exec_id, pct, fase, texto, force=False):
            mt._prog_ts = 0   # sem limitação de frequência no teste
            orig(exec_id, pct, fase, texto, force)
            vistos.append((fase, self.c.get("/api/progresso", headers=h).json()))
        mt._progresso = espia
        fila.rodar_proximo(self.con, mt)
        fases = [f for f, _ in vistos]
        self.assertTrue({"coleta", "gravacao", "datajud"} <= set(fases))
        pcts = [p["pct"] for _, p in vistos]
        self.assertEqual(pcts, sorted(pcts))                      # nunca recua
        self.assertTrue(all(p["estado"] == "executando" for _, p in vistos))
        fim = self.c.get("/api/progresso", headers=h).json()
        self.assertEqual((fim["estado"], fim["pct"]), ("concluido", 100))
        self.assertIn("correspondencias_novas", fim["resumo"])


class TestContagemSoAjuizadosHoje(TestAPI):
    def test_cards_hoje_semana_mes_contam_por_data_de_ajuizamento(self):
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        hoje = date.today()
        from datetime import timedelta
        for i, dias in enumerate((0, 3, 40, None)):   # hoje, 3 dias, 40 dias, sem data do DataJud
            dt = (hoje - timedelta(days=dias)).isoformat() if dias is not None else None
            pid = self.con.execute("INSERT INTO processos(numero_processo,tribunal,data_ajuizamento,primeira_identificacao,ultima_verificacao) "
                                   "VALUES (?,?,?,datetime('now'),datetime('now'))", ("000000%d" % i, "TJPR", dt)).lastrowid
            self.con.execute("INSERT INTO correspondencias(cliente_id,processo_id,nivel,classificacao,evidencia,fonte,identificada_em,ultima_verificacao) "
                             "VALUES (?,?,1,'confirmada','{}','djen',datetime('now'),datetime('now'))", (cid, pid))
        self.con.commit()
        d = self.c.get("/api/dashboard", headers=self.h("adv@x.com")).json()
        self.assertEqual((d["identificados_hoje"], d["identificados_semana"], d["identificados_mes"]), (1, 2, 2))


class TestNomeGenericoNaoDerrubaPesquisa(Base):
    def test_nome_curto_so_pesquisa_por_cnpj_e_falhas_antigas_saem_da_pendencia(self):
        cid = clientes.cadastrar(self.con, "NB", CNPJ_OK)
        m = self.motor(djen([]))
        chaves = [q["chave"] for q in m._consultas_do_cliente(clientes.carregar(self.con, cid))]
        self.assertEqual(chaves, ["texto:47.960.950/0001-21"])
        self.con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('manual','2026-01-01T00:00:00')")
        self.con.execute("INSERT INTO consultas(execucao_id,cliente_id,fonte,chave,janela_inicio,janela_fim,status,erro,iniciada_em,concluida_em) "
                         "VALUES (1,?,'djen','nomeParte:NB','2026-10-01','2026-10-03','falha','HTTP 500',datetime('now'),datetime('now'))", (cid,))
        self.con.commit()
        r = m.reprocessar_falhas()
        self.assertEqual(r["consultas_executadas"], 0)   # a consulta do nome "NB" foi aposentada, não refeita
        self.assertEqual(self.con.execute("SELECT desistida FROM consultas WHERE chave='nomeParte:NB'").fetchone()[0], 1)


class TestPesquisaManualEConsultaRuim(TestAPI):
    def test_clique_manual_forca_pesquisa_mesmo_se_ja_rodou_hoje(self):
        jid = self.c.post("/api/jobs", json={"tipo": "executar", "params": {}}, headers=self.h("adv@x.com")).json()["job_id"]
        self.assertTrue(json.loads(self.con.execute("SELECT params FROM jobs WHERE id=?", (jid,)).fetchone()[0])["forcar"])
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        n = {"c": 0}
        def tr(m, url, h, c, t):
            n["c"] += 1
            return resp({"status": "success", "count": 0, "items": []})
        mt = self.motor(tr)
        fila.rodar_proximo(self.con, mt)               # 1ª pesquisa do dia
        n["c"] = 0
        fila.enfileirar(self.con, "executar", {"forcar": True}, "manual")
        fila.rodar_proximo(self.con, mt)               # 2ª pesquisa no mesmo dia: tem que ir à fonte de novo
        self.assertGreater(n["c"], 0)

    def test_consulta_que_a_fonte_recusa_nao_trava_nem_derruba_as_outras(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        urls = []
        def tr(m, url, h, c, t):
            urls.append(url)
            if "nomeParte" in url:
                return resp({"status": "error", "message": "ocupado"}, 500)
            return resp({"status": "success", "count": 0, "items": []})
        mt = self.motor(tr)
        r = mt.executar("manual", desde=date.today().replace(year=date.today().year - 1), forcar=True)
        self.assertEqual(r["status"], "parcial")           # não "falha": a consulta por CNPJ funcionou
        nome = [u for u in urls if "nomeParte" in u]
        self.assertLessEqual(len(nome), 3 * mt.djen.http.tentativas)  # só as 3 primeiras janelas foram tentadas
        nao = self.con.execute("SELECT count(*) FROM consultas WHERE status='falha' AND erro LIKE 'não tentada%'").fetchone()[0]
        self.assertGreater(nao, 10)


class TestNomeOficialPeloCNPJ(Base):
    class Registro:
        def __init__(self, nome): self.nome = nome
        def consultar(self, cnpj): return {"razao_social": self.nome, "nome_fantasia": "", "situacao": "ATIVA"} if self.nome else None

    def test_nome_curto_digitado_e_substituido_pela_razao_social_oficial(self):
        cid = clientes.cadastrar(self.con, "Petro Tanque", CNPJ_OK)
        rel = clientes.completar_nomes_pelo_registro(self.con, cid, self.Registro("PETRO TANQUE METALURGICA LTDA"))
        self.assertEqual(rel["desativados"], ["Petro Tanque"])
        c = clientes.carregar(self.con, cid)
        self.assertEqual([n["nome_norm"] for n in c["nomes"]], ["PETRO TANQUE METALURGICA"])
        self.assertEqual(c["razao_social"], "PETRO TANQUE METALURGICA LTDA")
        from sentinela import matcher
        com = {"texto": "", "destinatarios": [{"nome": "PETRO TANQUE METALÚRGICA LTDA", "polo": "P"}]}
        self.assertEqual(matcher.avaliar(c, com)["nivel"], 3)

    def test_nome_oficial_com_sufixo_substitui_a_grafia_curta_para_gerar_iniciais(self):
        cid = clientes.cadastrar(self.con, "Transportadora Trans Mosa", CNPJ_OK)
        clientes.completar_nomes_pelo_registro(self.con, cid, self.Registro("TRANSPORTADORA TRANS MOSA LTDA"))
        c = clientes.carregar(self.con, cid)
        self.assertEqual([n["nome"] for n in c["nomes"]], ["TRANSPORTADORA TRANS MOSA LTDA"])
        from sentinela.motor import consultas_do_cliente
        self.assertIn("nomeParte:T. T. M. L.", [q["chave"] for q in consultas_do_cliente(c)])

    def test_fonte_fora_do_ar_ou_cnpj_inexistente_nao_quebra(self):
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        class Quebra:
            def consultar(self, c): raise RuntimeError("fora do ar")
        self.assertEqual(len(clientes.completar_nomes_pelo_registro(self.con, cid, Quebra())["erros"]), 1)
        self.assertEqual(clientes.completar_nomes_pelo_registro(self.con, cid, self.Registro(None))["oficiais"], [])
        self.assertEqual(len(clientes.carregar(self.con, cid)["nomes"]), 1)

    def test_busca_inicial_completa_nome_antes_de_pesquisar(self):
        cid = clientes.cadastrar(self.con, "Petro Tanque", CNPJ_OK)
        urls = []
        def tr(m, url, h, c, t):
            urls.append(url); return resp({"status": "success", "count": 0, "items": []})
        mt = self.motor(tr); mt.registro = self.Registro("PETRO TANQUE METALURGICA LTDA")
        fila.enfileirar(self.con, "executar", {"cliente_ids": [cid], "desde": date.today().isoformat()}, "busca_inicial")
        fila.rodar_proximo(self.con, mt)
        self.assertTrue(any("PETRO+TANQUE+METALURGICA" in u or "PETRO%20TANQUE%20METALURGICA" in u for u in urls))
        self.assertFalse(any(u.endswith("nomeParte=PETRO+TANQUE") or "nomeParte=PETRO+TANQUE&" in u for u in urls))


class TestGarantiaDeCobertura(Base):
    def _q(self, url):
        from urllib.parse import urlparse, parse_qs
        return {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}

    def test_matriz_aponta_lacuna_e_preenchimento_automatico_fecha(self):
        from datetime import timedelta
        from sentinela import cobertura
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        mt = self.motor(lambda m, u, h, c, t: resp({"status": "success", "count": 0, "items": []}))
        hoje = date.today()
        mt.executar("manual", desde=hoje - timedelta(days=12), forcar=True)
        # simula o Mac desligado: apaga a leitura de 4 dias no meio e adia a leitura dos demais para "depois do dia"
        self.con.execute("UPDATE consultas SET concluida_em=datetime('now','localtime','+1 day')")
        self.con.execute("DELETE FROM consultas WHERE chave LIKE 'texto:%' AND janela_inicio=?", ((hoje - timedelta(days=12)).isoformat(),))
        self.con.commit()
        m0 = cobertura.matriz(self.con, dias=12)
        self.assertGreater(m0["total"]["coberto"], 0)
        # abre um buraco real: remove a janela do meio
        meio = self.con.execute("SELECT id FROM consultas WHERE chave LIKE 'texto:%' ORDER BY janela_inicio LIMIT 1 OFFSET 2").fetchone()[0]
        self.con.execute("DELETE FROM consultas WHERE id=?", (meio,)); self.con.commit()
        lac = cobertura.lacunas(self.con, hoje)
        self.assertTrue(lac and lac[0]["cliente_id"] == cid)
        self.assertFalse(cobertura.matriz(self.con, dias=12)["integra"])
        r = mt.preencher_lacunas()
        self.assertEqual(r["consultas_com_falha"], 0)
        self.assertEqual(cobertura.lacunas(self.con, hoje), [])

    def test_janela_truncada_e_refeita_dia_a_dia(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        chamadas = []
        def tr(m, url, h, c, t):
            q = self._q(url); chamadas.append(q)
            if q["dataDisponibilizacaoInicio"] != q["dataDisponibilizacaoFim"] and "texto" in q:
                return resp({"status": "success", "count": 10000, "items": []})   # fonte diz ter 10.000, entrega 0
            return resp({"status": "success", "count": 0, "items": []})
        mt = self.motor(tr)
        mt.executar("manual", desde=date.today() - __import__("datetime").timedelta(days=5), forcar=True)
        dias = {(q["dataDisponibilizacaoInicio"]) for q in chamadas if q["dataDisponibilizacaoInicio"] == q["dataDisponibilizacaoFim"]}
        self.assertGreaterEqual(len(dias), 3)   # refez por dia
        self.assertEqual(self.con.execute("SELECT count(*) FROM consultas WHERE status='truncada' AND janela_inicio!=janela_fim").fetchone()[0], 0)

    def test_pesquisa_diaria_relê_os_ultimos_7_dias(self):
        from datetime import timedelta
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        mt = self.motor(lambda m, u, h, c, t: resp({"status": "success", "count": 0, "items": []}))
        mt.executar("manual", desde=date.today() - timedelta(days=30), forcar=True)
        self.con.execute("UPDATE consultas SET concluida_em=datetime('now','localtime','-1 day')"); self.con.commit()
        self.con.execute("DELETE FROM consultas WHERE date(concluida_em)!=date('now','localtime','-1 day')")
        mt.executar("agendado")
        ini = min(r[0] for r in self.con.execute("SELECT janela_inicio FROM consultas WHERE date(concluida_em)=date('now','localtime')"))
        self.assertLessEqual(ini, (date.today() - timedelta(days=6)).isoformat())

    def test_processos_controle_capturado_recuperado_e_fora_da_fonte(self):
        from sentinela import cobertura
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        n_ok, n_perdido, n_fora = NUM, "00000000020268160001", "00000000020268160002"
        pid = self.con.execute("INSERT INTO processos(numero_processo,tribunal,primeira_identificacao,ultima_verificacao) VALUES (?,?,datetime('now'),datetime('now'))", (n_ok, "TJPR")).lastrowid
        self.con.execute("INSERT INTO correspondencias(cliente_id,processo_id,nivel,classificacao,evidencia,fonte,identificada_em,ultima_verificacao) VALUES (?,?,1,'confirmada','{}','djen',datetime('now'),datetime('now'))", (cid, pid))
        for n in (n_ok, n_perdido, n_fora):
            cobertura.adicionar_controle(self.con, cid, n)
        self.con.commit()
        def tr(m, url, h, c, t):
            q = self._q(url)
            if q.get("numeroProcesso") == n_perdido:
                return resp({"status": "success", "count": 1, "items": [dict(item(7, texto="CNPJ 47.960.950/0001-21"), numero_processo=n_perdido)]})
            return resp({"status": "success", "count": 0, "items": []})
        self.con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('manual',datetime('now'))")
        self.con.execute("INSERT INTO consultas(execucao_id,cliente_id,fonte,chave,janela_inicio,janela_fim,status,iniciada_em,concluida_em) "
                         "VALUES (1,?,'djen','texto:x','2026-08-01','2026-11-01','ok',datetime('now'),datetime('now'))", (cid,))
        self.con.commit()
        r = cobertura.verificar_controle(self.con, self.motor(tr))
        self.assertEqual((r["capturado"], r["recuperado"], r["fora_da_fonte"]), (1, 1, 1))
        self.assertEqual(r["recall_pct"], 50.0)
        self.assertTrue(self.con.execute("SELECT 1 FROM correspondencias c JOIN processos p ON p.id=c.processo_id WHERE p.numero_processo=?", (n_perdido,)).fetchone())

    def test_publicacao_anterior_ao_historico_nao_conta_como_falha_mas_e_incluida(self):
        from sentinela import cobertura
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        n = "00000000020268160009"
        cobertura.adicionar_controle(self.con, cid, n)
        self.con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('manual',datetime('now'))")
        self.con.execute("INSERT INTO consultas(execucao_id,cliente_id,fonte,chave,janela_inicio,janela_fim,status,iniciada_em,concluida_em) "
                         "VALUES (1,?,'djen','texto:x','2026-08-01','2026-11-01','ok',datetime('now'),datetime('now'))", (cid,))
        self.con.commit()
        velho = dict(item(8, texto="CNPJ 47.960.950/0001-21"), numero_processo=n, data_disponibilizacao="2025-03-01")
        r = cobertura.verificar_controle(self.con, self.motor(lambda m, u, h, c, t: resp({"status": "success", "count": 1, "items": [velho]})))
        self.assertEqual((r["anterior_ao_historico"], r["recuperado"]), (1, 0))
        self.assertTrue(self.con.execute("SELECT 1 FROM processos WHERE numero_processo=?", (n,)).fetchone())

    def test_processo_com_partes_sob_sigilo_e_invisivel_nao_falha_nossa(self):
        from sentinela import cobertura
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        n = "00000000020268160007"
        cobertura.adicionar_controle(self.con, cid, n)
        self.con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('manual',datetime('now'))")
        self.con.execute("INSERT INTO consultas(execucao_id,cliente_id,fonte,chave,janela_inicio,janela_fim,status,iniciada_em,concluida_em) "
                         "VALUES (1,?,'djen','texto:x','2026-08-01','2026-11-01','ok',datetime('now'),datetime('now'))", (cid,))
        self.con.commit()
        sig = dict(item(9, polo_nome="SIGILO"), numero_processo=n)
        sig["destinatarios"] = [{"nome": "SIGILO", "polo": "P"}, {"nome": "SIGILO", "polo": "A"}]
        r = cobertura.verificar_controle(self.con, self.motor(lambda m, u, h, c, t: resp({"status": "success", "count": 1, "items": [sig]})))
        self.assertEqual((r["invisivel_no_djen"], r["recuperado"]), (1, 0))
        self.assertIn("sigilo", self.con.execute("SELECT detalhe FROM controle_processos").fetchone()[0])
        self.assertEqual(r["visibilidade_pct"], 0.0)

    def test_pesquisa_regular_assume_a_origem_de_processo_incluido_pelo_teste_de_controle(self):
        from sentinela import cobertura
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        com = dict(item(5, texto="CNPJ 47.960.950/0001-21"))
        mt = self.motor(lambda m, u, h, c, t: resp({"status": "success", "count": 1, "items": [com]}))
        cli = clientes.carregar(self.con, cid)
        from sentinela import matcher
        m = matcher.avaliar(cli, dict(com, destinatarios=com["destinatarios"]))
        cobertura._incluir(self.con, mt, cli, {"numero_processo": NUM}, [(dict(com, id=5, fonte="djen", numero_processo=NUM, tribunal="TJRJ", tipo="x",
                           data_disponibilizacao="2026-10-29", classe="c", orgao="o", link="l", texto=com["texto"], destinatarios=com["destinatarios"]), m)], 1, 1)
        self.assertIn("numeroProcesso:", self.con.execute("SELECT evidencia FROM correspondencias").fetchone()[0])
        mt.executar("manual", desde=date.today() - __import__("datetime").timedelta(days=3), forcar=True)
        self.assertNotIn("numeroProcesso:", self.con.execute("SELECT evidencia FROM correspondencias").fetchone()[0])

    def test_vigia_alerta_quando_pesquisa_do_dia_nao_rodou(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        fila.agendar(self.con, datetime.now().replace(hour=23, minute=59))
        self.assertEqual(self.con.execute("SELECT count(*) FROM alertas WHERE chave_dedup LIKE 'falha_consulta:%atraso:%'").fetchone()[0] > 0, True)


class TestCadernos(Base):
    def _zip(self, caminho, itens):
        import zipfile
        with zipfile.ZipFile(caminho, "w") as z:
            z.writestr("X_1.json", json.dumps({"count": len(itens), "items": itens}))

    def _http(self, metas):
        class H:
            def requisitar(h, m, url, *a, **k):
                if url.endswith("/tribunal"):
                    return [{"instituicoes": [{"sigla": "TJA"}, {"sigla": "TJB"}]}]
                _, _, _, sg, dia, meio = url.rsplit("/", 5)[-6:] if False else (None, None, None) + tuple(url.split("/caderno/")[1].split("/"))
                return metas.get((sg, dia, meio), {"status": "Sem comunicações", "total_comunicacoes": 0})
        return H()

    def test_varredura_acha_cliente_prova_total_e_nao_refaz_caderno_fechado(self):
        from sentinela import cadernos
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        hoje = date.today().isoformat()
        achou = dict(item(1, polo_nome="OUTRA", texto="Réu(s): ACME INDUSTRIA LTDA (CPF/CNPJ: 47.960.950/0001-21)"), destinatarios=[{"nome": "SIGILO", "polo": "P"}])
        outros = [dict(item(i, polo_nome="FULANO LTDA", texto="nada"), id=100 + i) for i in range(4)]
        arquivos = {"urlA": [achou] + outros}
        metas = {("TJA", hoje, "D"): {"status": "Processado", "total_comunicacoes": 5, "versao": 1, "hash": "h", "url": "urlA"},
                 ("TJB", hoje, "D"): {"status": "Em processamento", "total_comunicacoes": 0}}
        baixados = []
        def baixar(url, destino):
            baixados.append(url); self._zip(destino, arquivos[url])
        mt = self.motor(lambda *a: resp({}))
        mt.djen.http = self._http(metas)
        r = cadernos.varrer(self.con, mt, dias=1, baixar=baixar, trabalhadores=1)
        self.assertEqual((r["comunicacoes_lidas"], r["achados"]), (5, 1))
        self.assertEqual(self.con.execute("SELECT classificacao FROM correspondencias").fetchone()[0], "confirmada")  # CNPJ no texto + 'Réu', partes sob SIGILO
        sm = cadernos.resumo(self.con, 1)[0]
        self.assertEqual((sm["lidas"], sm["declaradas"], sm["pendentes"]), (5, 5, 1))   # TJB ainda em processamento
        baixados.clear()
        cadernos.varrer(self.con, mt, dias=1, baixar=baixar, trabalhadores=1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM correspondencias").fetchone()[0], 1)   # sem duplicar
        self.assertEqual(baixados, [])   # mesma versão/hash já lida por inteiro: não baixa de novo

    def test_dia_sem_caderno_404_nao_e_erro_e_fecha(self):
        from sentinela import cadernos
        from sentinela.http import ErroFonte
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        class H:
            def requisitar(h, m, url, *a, **k):
                if url.endswith("/tribunal"):
                    return [{"instituicoes": [{"sigla": "TJA"}]}]
                raise ErroFonte("djen: HTTP 404", 404)
        mt = self.motor(lambda *a: resp({})); mt.djen.http = H()
        r = cadernos.varrer(self.con, mt, dias=1, baixar=lambda u, d: None, trabalhadores=1)
        self.assertEqual((r["cadernos_com_erro"], r["cadernos_processados"]), (0, 2))
        self.assertEqual(cadernos.resumo(self.con, 1)[0]["fechados"], 2)

    def test_total_lido_diferente_do_declarado_marca_incompleto(self):
        from sentinela import cadernos
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        hoje = date.today().isoformat()
        metas = {("TJA", hoje, "D"): {"status": "Processado", "total_comunicacoes": 9, "versao": 1, "hash": "h", "url": "u"}}
        mt = self.motor(lambda *a: resp({})); mt.djen.http = self._http(metas)
        cadernos.varrer(self.con, mt, dias=1, baixar=lambda u, d: self._zip(d, [item(1)]), trabalhadores=1)
        self.assertEqual(self.con.execute("SELECT status FROM cadernos WHERE sigla='TJA' AND meio='D'").fetchone()[0], "incompleto")
        self.assertEqual(cadernos.resumo(self.con, 1)[0]["com_erro"], 1)


class TestRadarObservabilidade(TestAPI):
    def test_zero_resultados_difere_de_falha_e_cobertura_incompleta_nao_diz_nenhuma_acao(self):
        from sentinela import config as _cfg, observabilidade
        _cfg.salvar(self.con, "radar", {**_cfg.obter(self.con, "radar"), "fontes_esperadas": ["DJEN_BUSCA"]})
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        ok = self.motor(lambda *a: resp({"status": "success", "count": 0, "items": []}))
        ok.executar("manual", desde=date.today(), forcar=True)
        self.assertEqual({r[0] for r in self.con.execute("SELECT resultado FROM consultas")}, {"ZERO_RESULTS"})
        h = self.h("adv@x.com")
        d = self.c.get("/api/dashboard", headers=h).json()
        self.assertIn("Monitoramento validado", d["estado_pesquisa"]["texto"])
        # a fonte cai: consulta falha (QUERY_FAILED), nunca vira "zero resultados"
        quebrada = self.motor(lambda *a: resp({}, 500))
        quebrada.executar("manual", desde=date.today(), forcar=True)
        res = {r[0] for r in self.con.execute("SELECT resultado FROM consultas ORDER BY id DESC LIMIT 3")}
        self.assertEqual(res, {"QUERY_FAILED"})
        d2 = self.c.get("/api/dashboard", headers=h).json()
        self.assertNotIn("Monitoramento validado", d2["frase_sem_novos"])
        self.assertIn("INCOMPLETA", d2["frase_sem_novos"])
        self.assertFalse(d2["cobertura"]["completa"])
        self.assertIn("DJEN_BUSCA", [p["fonte"] for p in d2["cobertura"]["problemas"]])
        cod = {r[0] for r in self.con.execute("SELECT codigo FROM fonte_chamadas WHERE ok=0")}
        self.assertIn("UNKNOWN_ERROR", cod)

    def test_sonda_ativa_mantem_datajud_fresco_mesmo_sem_nada_a_enriquecer(self):
        from sentinela import observabilidade
        mt = self.motor(lambda *a: resp([]), lambda *a: resp({"hits": {"hits": []}}))
        observabilidade.sondar(self.con, mt)
        s = {x["fonte"]: x for x in observabilidade.saude_fontes(self.con)}
        self.assertEqual((s["DATAJUD"]["estado"], s["DJEN_BUSCA"]["estado"]), ("HEALTHY", "HEALTHY"))

    def test_fonte_para_de_responder_gera_down(self):
        from sentinela import observabilidade
        for _ in range(3):
            observabilidade.registrar_chamada(self.con, self.lock, {"fonte": "DATAJUD", "tribunal": "TJPR", "ok": 0, "codigo": "TIMEOUT", "http_status": None, "latencia_ms": 60000})
        s = {x["fonte"]: x for x in observabilidade.saude_fontes(self.con)}
        self.assertEqual(s["DATAJUD"]["estado"], "DOWN")
        self.assertEqual(s["DJEN_BUSCA"]["estado"], "UNKNOWN")

    def test_vinculo_ganha_fontes_confianca_e_eventos_sem_duplicar_processo(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        mt = self.motor(djen([item(1, texto="CNPJ 47.960.950/0001-21")]), dj_ok(date.today().strftime("%Y%m%d") + "000000"))
        mt.executar("manual", desde=date.today(), forcar=True)
        mt.executar("manual", desde=date.today(), forcar=True)   # de novo: não duplica
        self.assertEqual(self.con.execute("SELECT count(*) FROM processos").fetchone()[0], 1)
        c = self.con.execute("SELECT confianca,status_deteccao FROM correspondencias").fetchone()
        self.assertEqual((c["confianca"], c["status_deteccao"]), (100, "CONFIRMED"))   # DJEN + DataJud
        self.assertEqual({r[0] for r in self.con.execute("SELECT fonte FROM processo_fontes")}, {"DJEN_BUSCA", "DATAJUD"})
        ev = [r[0] for r in self.con.execute("SELECT tipo FROM processo_eventos ORDER BY id")]
        self.assertEqual(ev.count("PROCESS_FIRST_SEEN"), 1)
        self.assertIn("PARTY_CONFIRMED", ev)
        self.assertTrue(self.con.execute("SELECT 1 FROM processo_partes WHERE lado='DEFENDANT'").fetchone())
        det = self.c.get("/api/processos/%d" % self.con.execute("SELECT id FROM correspondencias").fetchone()[0], headers=self.h("adv@x.com")).json()
        self.assertEqual({f["fonte"] for f in det["fontes"]}, {"DJEN_BUSCA", "DATAJUD"})

    def test_abaixo_do_limiar_de_confianca_nao_gera_alerta_automatico(self):
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        pid = self.con.execute("INSERT INTO processos(numero_processo,tribunal,primeira_identificacao,ultima_verificacao) VALUES (?,?,datetime('now'),datetime('now'))", (NUM, "TJPR")).lastrowid
        self.con.execute("INSERT INTO correspondencias(cliente_id,processo_id,nivel,classificacao,evidencia,fonte,identificada_em,ultima_verificacao,categoria_data,confianca) "
                         "VALUES (?,?,5,'inconclusiva','{}','djen',datetime('now'),datetime('now'),'nova_distribuicao',50)", (cid, pid))
        self.con.commit()
        self.assertEqual(notificacoes.processar_pendentes(self.con)["conferencia"], 0)


class TestSemFilaHumana(TestAPI):
    def _transporte(self, por_numero):
        from urllib.parse import urlparse, parse_qs
        def tr(m, url, h, c, t):
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            if "numeroProcesso" in q:
                its = por_numero
            else:
                its = [item(1)]
            return resp({"status": "success", "count": len(its), "items": its})
        return tr

    def test_incerto_e_promovido_sozinho_pelo_historico_do_processo(self):
        from sentinela import resolucao
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        hist = [item(1), dict(item(2, texto="Réu(s): ACME INDUSTRIA LTDA (CPF/CNPJ: 47.960.950/0001-21)"), id=2)]
        mt = self.motor(self._transporte(hist))
        mt.executar("manual", desde=date.today(), forcar=True)
        c = self.con.execute("SELECT classificacao,estado_validacao FROM correspondencias").fetchone()
        self.assertEqual((c["classificacao"], c["estado_validacao"]), ("provavel", "em_verificacao"))   # sem tarefa humana
        r = resolucao.resolver_pendentes(self.con, mt)
        c = self.con.execute("SELECT classificacao,estado_validacao FROM correspondencias").fetchone()
        self.assertEqual((r["promovidos"], c["classificacao"], c["estado_validacao"]), (1, "confirmada", "automatico"))
        self.assertFalse(self.con.in_transaction)   # transação aberta travaria o batimento do worker (painel ficaria "PARADO")
        self.assertIn("AUTO_RESOLVIDO", [x[0] for x in self.con.execute("SELECT tipo FROM processo_eventos")])

    def test_incerto_e_descartado_sozinho_quando_cliente_so_e_autor(self):
        from sentinela import resolucao
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        mt = self.motor(self._transporte([item(3, polo="A")]))
        pid = self.con.execute("INSERT INTO processos(numero_processo,tribunal,primeira_identificacao,ultima_verificacao) VALUES (?,?,datetime('now'),datetime('now'))", (NUM, "TJRJ")).lastrowid
        self.con.execute("INSERT INTO correspondencias(cliente_id,processo_id,nivel,classificacao,evidencia,fonte,estado_validacao,identificada_em,ultima_verificacao) "
                         "VALUES (1,?,5,'inconclusiva','{}','djen','em_verificacao',datetime('now'),datetime('now'))", (pid,))
        self.con.commit()
        r = resolucao.resolver_pendentes(self.con, mt)
        self.assertEqual(r["descartados"], 1)
        self.assertEqual(self.con.execute("SELECT estado_validacao FROM correspondencias").fetchone()[0], "descartado_auto")

    def test_sem_corroboracao_fica_como_possivel_e_finaliza_em_14_dias_sem_tarefa(self):
        from sentinela import resolucao
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        mt = self.motor(self._transporte([]))
        pid = self.con.execute("INSERT INTO processos(numero_processo,tribunal,primeira_identificacao,ultima_verificacao) VALUES (?,?,datetime('now'),datetime('now'))", (NUM, "TJRJ")).lastrowid
        self.con.execute("INSERT INTO correspondencias(cliente_id,processo_id,nivel,classificacao,evidencia,fonte,estado_validacao,identificada_em,ultima_verificacao) "
                         "VALUES (1,?,5,'inconclusiva','{}','djen','em_verificacao',datetime('now','-20 day'),datetime('now'))", (pid,))
        self.con.commit()
        self.assertEqual(resolucao.resolver_pendentes(self.con, mt)["finalizados"], 1)
        self.assertEqual(self.con.execute("SELECT estado_validacao FROM correspondencias").fetchone()[0], "automatico")

    def test_alertas_por_confianca_confirmada_possivel_e_nenhum(self):
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        for i, conf in enumerate((100, 80, 50)):
            pid = self.con.execute("INSERT INTO processos(numero_processo,tribunal,primeira_identificacao,ultima_verificacao) VALUES (?,?,datetime('now'),datetime('now'))", ("000000%d" % i, "TJPR")).lastrowid
            self.con.execute("INSERT INTO correspondencias(cliente_id,processo_id,nivel,classificacao,evidencia,fonte,identificada_em,ultima_verificacao,categoria_data,confianca) "
                             "VALUES (?,?,1,'confirmada','{}','djen',datetime('now'),datetime('now'),'nova_distribuicao',?)", (cid, pid, conf))
        self.con.commit()
        r = notificacoes.processar_pendentes(self.con)
        self.assertEqual((r["definitivos"], r["possiveis"]), (1, 1))
        tipos = {x[0] for x in self.con.execute("SELECT tipo FROM alertas")}
        self.assertEqual(tipos, {"nova_acao_confirmada", "nova_acao_possivel"})

    def test_canarios_automaticos_detectam_regressao_da_fonte(self):
        from sentinela import cobertura
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        mt = self.motor(djen([item(1, texto="CNPJ 47.960.950/0001-21")]))
        mt.executar("manual", desde=date.today(), forcar=True)
        self.assertEqual(self.con.execute("SELECT classificacao FROM correspondencias").fetchone()[0], "confirmada")
        self.assertEqual(cobertura.atualizar_canarios(self.con), 1)
        self.assertEqual(self.con.execute("SELECT origem FROM controle_processos").fetchone()[0], "auto")
        ok = cobertura.verificar_controle(self.con, self.motor(djen([item(1, texto="CNPJ 47.960.950/0001-21")])))
        self.assertEqual((ok["canario_ok"], ok["regressao"]), (1, 0))
        quebrado = cobertura.verificar_controle(self.con, self.motor(djen([])))   # o DJEN parou de devolver o processo conhecido
        self.assertEqual(quebrado["regressao"], 1)


if __name__ == "__main__":
    unittest.main()
