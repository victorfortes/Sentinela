"""Testes unitários com transporte HTTP falso. NÃO são evidência de funcionamento contra as fontes reais
(isso fica em tests/teste_real_fontes.py)."""
import json
import os
import tempfile
import threading
import unittest
from datetime import date

from sentinela import clientes, cnpj, db, matcher, nomes
from sentinela.conectores.datajud import ConectorDataJud, data_iso
from sentinela.conectores.djen import ConectorDJEN
from sentinela.http import ErroFonte, FonteHTTP
from sentinela.motor import Motor, registrar_saude

CNPJ_OK = "47.960.950/0001-21"
NUM = "08105237720268190210"


def resp(obj, status=200):
    return status, {}, json.dumps(obj).encode()


def item(i=1, polo_nome="ACME INDUSTRIA LTDA", polo="P", texto="Processo x"):
    return {"id": i, "numero_processo": NUM, "siglaTribunal": "TJRJ", "tipoComunicacao": "Intimação",
            "data_disponibilizacao": "2026-10-29", "nomeClasse": "Procedimento", "nomeOrgao": "Vara", "link": "http://x",
            "texto": texto, "destinatarios": [{"nome": "FULANO", "polo": "A"}, {"nome": polo_nome, "polo": polo}]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.con = db.conectar(os.path.join(self.tmp, "t.db"))
        self.lock = threading.Lock()
        self.cb = lambda f, ok, e: registrar_saude(self.con, self.lock, f, ok, e)

    def motor(self, transporte_djen, transporte_dj=None):
        nd = lambda s: None
        from sentinela import observabilidade
        aud = lambda d: observabilidade.registrar_chamada(self.con, self.lock, d)   # a trilha de auditoria funciona como em produção
        djen = ConectorDJEN(FonteHTTP("djen", 0, 3, 0, transporte=transporte_djen, dormir=nd, ao_resultado=self.cb, ao_chamada=aud))
        dj = ConectorDataJud(FonteHTTP("datajud", 0, 2, 0, transporte=transporte_dj or (lambda *a: resp({"hits": {"hits": []}})),
                                       dormir=nd, ao_resultado=self.cb, ao_chamada=aud))
        return Motor(self.con, djen, dj, hoje=date.today())


class TestCNPJ(unittest.TestCase):
    def test_validacao(self):
        self.assertTrue(cnpj.valido(CNPJ_OK))
        self.assertFalse(cnpj.valido("47.960.950/0001-22"))
        self.assertFalse(cnpj.valido("11111111111111"))
        self.assertEqual(cnpj.formatar("47960950000121"), CNPJ_OK)
        with self.assertRaises(ValueError):
            cnpj.normalizar("123")

    def test_nomes(self):
        self.assertEqual(nomes.normalizar("Acme Indústria e Comércio S/A - Em Recuperação Judicial"), "ACME INDUSTRIA E COMERCIO")
        self.assertEqual(nomes.normalizar("MAGAZINE LUIZA SIA"), nomes.normalizar("Magazine Luiza S.A."))


class TestClientes(Base):
    def test_importacao_csv_com_linha_invalida(self):
        p = os.path.join(self.tmp, "c.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("razao_social,cnpj_matriz,filiais,anteriores\nAcme Ltda,47960950000121,,Velha Acme\nRuim,123,,\n")
        r = clientes.importar_csv(self.con, p)
        self.assertEqual(r["importados"], 1)
        self.assertEqual(len(r["erros"]), 1)
        c = clientes.carregar(self.con, 1)
        self.assertIn("VELHA ACME", [n["nome_norm"] for n in c["nomes"]])


class TestItemSemDestinatarios(Base):
    def test_item_com_destinatarios_nulos_nao_derruba_a_leitura(self):
        from sentinela.motor import _normalizar_item
        i = item(1); i["destinatarios"] = None
        self.assertEqual(_normalizar_item(i)["destinatarios"], [])


class TestNomeNoCorpoDoTexto(Base):
    def cli(self):
        cid = clientes.cadastrar(self.con, "Transportadora Trans Mosa Ltda", CNPJ_OK)
        self.con.execute("UPDATE clientes SET confirmar_por_nome_exato=1"); self.con.commit()
        return clientes.carregar(self.con, cid)

    def av(self, texto, dest=None):
        if not hasattr(self, "_c"):
            self._c = self.cli()
        return matcher.avaliar(self._c, {"texto": texto, "destinatarios": dest or [{"nome": "SIGILO", "polo": "P"}]})

    def test_executado_e_em_face_de_confirmam_mesmo_com_partes_ocultas(self):
        self.assertEqual(self.av("Executado(s):  FULANO TRANSPORTADORA TRANS MOSA LTDA DECISÃO")["classificacao"], "confirmada")
        self.assertEqual(self.av("impugnação apresentada por Banco X em face de Transportadora Trans Mosa Ltda.")["polo"], "P")

    def test_recurso_e_recuperacao_nao_sao_descartados_nem_confirmados(self):
        self.assertEqual(self.av("recurso de apelação interposto por TRANSPORTADORA TRANS MOSA LTDA em face da sentença")["classificacao"], "inconclusiva")
        self.assertEqual(self.av("Trata-se de recuperação judicial de TRANSPORTADORA TRANS MOSA LTDA")["classificacao"], "inconclusiva")

    def test_autor_e_descartado_e_sem_nome_nao_casa(self):
        self.assertEqual(self.av("Autor(s): TRANSPORTADORA TRANS MOSA LTDA Réu(s): X")["classificacao"], "descartada")
        self.assertIsNone(self.av("texto sem relação"))

    def test_sem_confirmacao_por_nome_vira_provavel(self):
        c = self.cli(); c["confirmar_por_nome_exato"] = 0
        r = matcher.avaliar(c, {"texto": "Réu(s): TRANSPORTADORA TRANS MOSA LTDA", "destinatarios": []})
        self.assertEqual(r["classificacao"], "provavel")


class TestIniciais(Base):
    def test_iniciais_e_abreviacao(self):
        from sentinela import nomes
        self.assertEqual(nomes.iniciais("TRANSPORTADORA TRANS MOSA LTDA"), "T. T. M. L.")
        self.assertEqual(nomes.iniciais("Fortress Serviços Ltda"), "")          # curta demais: não pesquisa
        self.assertTrue(nomes.e_abreviacao("T. T. M. L."))
        self.assertFalse(nomes.e_abreviacao("FORTRESS SERVICOS"))
        self.assertFalse(nomes.e_abreviacao("T. T. M. L. DOS S."))

    def test_parte_abreviada_vira_provavel_e_nunca_confirmada_sozinha(self):
        cid = clientes.cadastrar(self.con, "Transportadora Trans Mosa Ltda", CNPJ_OK)
        self.con.execute("UPDATE clientes SET confirmar_por_nome_exato=1"); self.con.commit()
        c = clientes.carregar(self.con, cid)
        r = matcher.avaliar(c, {"texto": "", "destinatarios": [{"nome": "B. J. S. S. A.", "polo": "A"}, {"nome": "T. T. M. L.", "polo": "P"}]})
        self.assertEqual((r["classificacao"], r["polo"]), ("provavel", "P"))
        self.assertIsNone(matcher.avaliar(c, {"texto": "", "destinatarios": [{"nome": "X. Y. Z. W.", "polo": "P"}]}))


class TestMatcher(Base):
    def cli(self):
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK, filiais=["11.222.333/0001-81"], anteriores=["Velha Acme S/A"])
        return clientes.carregar(self.con, cid)

    def com(self, **k):
        i = item(**k)
        i["destinatarios"] = [{"nome": d["nome"], "polo": d["polo"]} for d in i["destinatarios"]]
        return i

    def test_confirmada_cnpj_e_polo_passivo(self):
        m = matcher.avaliar(self.cli(), self.com(texto="Acme CNPJ 47.960.950/0001-21 citada"))
        self.assertEqual((m["classificacao"], m["nivel"], m["polo"]), ("confirmada", 1, "P"))

    def test_provavel_nome_identico_sem_cnpj(self):
        m = matcher.avaliar(self.cli(), self.com())
        self.assertEqual((m["classificacao"], m["nivel"]), ("provavel", 3))

    def test_anterior_e_aproximado(self):
        c = self.cli()
        self.assertEqual(matcher.avaliar(c, self.com(polo_nome="VELHA ACME SA"))["nivel"], 4)
        m = matcher.avaliar(c, self.com(polo_nome="ACME INDUSTRIAS"))
        self.assertEqual((m["classificacao"], m["nivel"]), ("inconclusiva", 5))

    def test_polo_ativo_descartado_e_sem_relacao(self):
        c = self.cli()
        self.assertEqual(matcher.avaliar(c, self.com(polo="A"))["classificacao"], "descartada")
        self.assertIsNone(matcher.avaliar(c, self.com(polo_nome="OUTRA EMPRESA LTDA")))

    def test_cnpj_so_no_texto_e_inconclusivo(self):
        m = matcher.avaliar(self.cli(), self.com(polo_nome="OUTRA EMPRESA LTDA", texto="ref 47960950000121"))
        self.assertEqual((m["classificacao"], m["polo"]), ("inconclusiva", None))

    def test_cnpj_no_texto_como_reu_confirma_mesmo_com_nome_diferente_na_fonte(self):
        t = "Autor(s): JOAO (CPF/CNPJ: 111.444.777-35) Réu(s): ACME IND (CPF/CNPJ: 47.960.950/0001-21) Rua X"
        m = matcher.avaliar(self.cli(), self.com(polo_nome="OUTRA EMPRESA LTDA", texto=t))
        self.assertEqual((m["classificacao"], m["polo"], m["nivel"]), ("confirmada", "P", 1))

    def test_cnpj_no_texto_como_autor_terceiro_ou_embargante_nao_confirma(self):
        c = self.cli()
        for rot, esperado in (("Autor(s)", "descartada"), ("Terceiro(s)", "inconclusiva"), ("Embargante(s)", "inconclusiva")):
            m = matcher.avaliar(c, self.com(polo_nome="OUTRA EMPRESA LTDA", texto="%s:   ACME (CPF/CNPJ: 47.960.950/0001-21) Rua" % rot))
            self.assertEqual(m["classificacao"], esperado, rot)

    def test_filial(self):
        m = matcher.avaliar(self.cli(), self.com(texto="CNPJ 11.222.333/0001-81"))
        self.assertEqual((m["classificacao"], m["nivel"]), ("confirmada", 2))


class TestHTTPResiliente(Base):
    def test_retentativa_com_backoff(self):
        seq, esperas = [429, 503, 200], []
        def tr(*a):
            s = seq.pop(0)
            return resp({"ok": 1}, s)
        f = FonteHTTP("x", 0, 4, 2.0, transporte=tr, dormir=esperas.append, ao_resultado=self.cb)
        self.assertEqual(f.requisitar("GET", "http://x"), {"ok": 1})
        self.assertEqual([e for e in esperas if e], [2.0, 4.0])

    def test_falha_definitiva_registrada(self):
        f = FonteHTTP("x", 0, 3, 0, transporte=lambda *a: resp({}, 500), dormir=lambda s: None, ao_resultado=self.cb)
        with self.assertRaises(ErroFonte):
            f.requisitar("GET", "http://x")
        s = self.con.execute("SELECT * FROM saude_conectores WHERE fonte='x'").fetchone()
        self.assertEqual((s["estado"], s["falhas_consecutivas"]), ("degradado", 1))


class TestMotor(Base):
    def djen_ok(self, itens):
        def tr(m, url, h, c, t):
            return resp({"status": "success", "count": len(itens), "items": itens})
        return tr

    def test_fluxo_dedup_persistencia(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        itens = [item(1, texto="CNPJ 47.960.950/0001-21")]
        m = self.motor(self.djen_ok(itens), lambda *a: resp({"hits": {"hits": [{"_source": {"numeroProcesso": NUM,
            "dataAjuizamento": "20261025101010", "grau": "G1", "classe": {"nome": "Procedimento Comum"},
            "orgaoJulgador": {"nome": "Vara"}, "sistema": {"nome": "PJe"}, "assuntos": []}}]}}))
        r1 = m.executar(desde=date(2026, 10, 20))
        self.assertEqual(r1["status"], "sucesso")
        self.assertEqual(r1["correspondencias_novas"], 1)
        n = self.con.execute("SELECT count(*) FROM correspondencias").fetchone()[0]
        c = self.con.execute("SELECT * FROM correspondencias").fetchone()
        self.assertEqual((n, c["classificacao"], c["categoria_data"]), (1, "confirmada", "nova_distribuicao"))
        # reexecução forçada da mesma janela: nenhuma duplicidade de processo/correspondência/comunicação
        r2 = m.executar(desde=date(2026, 10, 20), forcar=True)
        self.assertEqual(r2["correspondencias_novas"], 0)
        for t in ("correspondencias", "processos", "comunicacoes"):
            self.assertEqual(self.con.execute("SELECT count(*) FROM %s" % t).fetchone()[0], 1)

    def test_janela_fechada_nao_repete_consulta(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(self.djen_ok([]))
        m.executar(desde=date(2026, 10, 1), ate=date(2026, 10, 5))
        antes = self.con.execute("SELECT count(*) FROM consultas").fetchone()[0]
        r = m.executar(desde=date(2026, 10, 1), ate=date(2026, 10, 5))
        self.assertEqual(self.con.execute("SELECT count(*) FROM consultas").fetchone()[0], antes)
        self.assertGreater(r["consultas_puladas_por_dedup"], 0)

    def test_processo_antigo_nao_e_nova_distribuicao(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(self.djen_ok([item(2, texto="CNPJ 47.960.950/0001-21")]), lambda *a: resp({"hits": {"hits": [{"_source": {"numeroProcesso": NUM,
            "dataAjuizamento": "20230101000000", "grau": "G1", "classe": {"nome": "X"}, "orgaoJulgador": {"nome": "V"},
            "sistema": {"nome": "PJe"}}}]}}))
        m.executar(desde=date(2026, 10, 20))
        self.assertEqual(self.con.execute("SELECT categoria_data FROM correspondencias").fetchone()[0], "processo_antigo")

    def test_falha_da_fonte_e_registrada_e_distinta_de_zero_resultados(self):
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(lambda *a: resp({}, 503))
        r = m.executar(desde=date(2026, 10, 28))
        self.assertEqual(r["status"], "falha")
        self.assertTrue(r["consultas_com_falha"] > 0)
        st = {x[0] for x in self.con.execute("SELECT status FROM consultas")}
        self.assertEqual(st, {"falha"})
        self.assertIn(self.con.execute("SELECT estado FROM saude_conectores WHERE fonte='djen'").fetchone()[0], ("degradado", "indisponivel"))
        self.assertIsNone(self.con.execute("SELECT ultima_verificacao FROM clientes").fetchone()[0])
        # zero resultados com sucesso é outra coisa
        m2 = self.motor(self.djen_ok([]))
        self.assertEqual(m2.executar(desde=date(2026, 10, 28), forcar=True)["status"], "sucesso")
        self.assertIsNotNone(self.con.execute("SELECT ultima_verificacao FROM clientes").fetchone()[0])

    def test_data_iso(self):
        self.assertEqual(data_iso("20260710124012"), "2026-07-10")
        self.assertEqual(data_iso("2026-07-10T12:40:12.000Z"), "2026-07-10")
        self.assertIsNone(data_iso(None))


if __name__ == "__main__":
    unittest.main()


class TestValidacaoENome(Base):
    def test_confirmar_por_nome_exato_so_quando_habilitado(self):
        cid = clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        c = clientes.carregar(self.con, cid)
        com = {"texto": "", "destinatarios": [{"nome": "ACME INDUSTRIA LTDA", "polo": "P"}]}
        self.assertEqual(matcher.avaliar(c, com)["classificacao"], "provavel")
        c["confirmar_por_nome_exato"] = 1
        self.assertEqual(matcher.avaliar(c, com)["classificacao"], "confirmada")

    def test_validacao_manual_auditada(self):
        from sentinela import validacao
        clientes.cadastrar(self.con, "Acme Indústria Ltda", CNPJ_OK)
        m = self.motor(lambda *a: resp({"status": "success", "count": 1, "items": [item(1)]}))
        m.executar(desde=date(2026, 10, 28))
        p = validacao.pendentes(self.con)
        self.assertEqual(len(p), 1)
        validacao.decidir(self.con, p[0]["id"], "dr.victor", "confirmada", "conferido no PJe")
        self.assertEqual(validacao.pendentes(self.con), [])
        v = self.con.execute("SELECT * FROM validacoes").fetchone()
        self.assertEqual((v["usuario"], v["classificacao_anterior"], v["decisao"]), ("dr.victor", "provavel", "confirmada"))
        # nova execução não rebaixa nem sobrescreve decisão humana
        m.executar(desde=date(2026, 10, 28), forcar=True)
        self.assertEqual(self.con.execute("SELECT classificacao FROM correspondencias").fetchone()[0], "confirmada")


class TestMigracaoConcorrente(unittest.TestCase):
    def test_duas_conexoes_migrando_ao_mesmo_tempo(self):
        import tempfile, threading
        caminho = os.path.join(tempfile.mkdtemp(), "m.db")
        erros = []
        def go():
            try: db.conectar(caminho).close()
            except Exception as e: erros.append(e)
        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(erros, [])
        con = db.conectar(caminho)
        self.assertEqual(con.execute("SELECT count(*) FROM schema_migracoes").fetchone()[0], len(db.MIGRACOES))
