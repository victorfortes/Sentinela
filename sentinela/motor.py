"""Motor de monitoramento: consulta as fontes, identifica o cliente nas partes, deduplica e persiste."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import clientes as _clientes
from . import cnpj as _cnpj
from . import nomes as _nomes
from . import radar as _radar
from . import matcher
from .conectores.djen import ConectorDJEN
from .conectores.datajud import ConectorDataJud
from .http import ErroFonte

RANK = {"descartada": 1, "inconclusiva": 2, "provavel": 3, "confirmada": 4}
# Não existe fila humana: o que não é certo fica "em_verificacao" e o próprio sistema o resolve (resolucao.py) por corroboração.
ESTADO_AUTO = {"confirmada": "automatico", "provavel": "em_verificacao", "inconclusiva": "em_verificacao", "descartada": "descartado_auto"}
ESTADOS_AUTOMATICOS = {"automatico", "em_verificacao", "pendente", "descartado_auto"}   # tudo que NÃO foi decidido por uma pessoa
CHUNK_DIAS = 3
LOTE_DATAJUD = 50
NOME_MIN_CARACTERES = 4      # nome mais curto que isso (ex.: "NB") não é pesquisado por nome: devolve milhares de homônimos e a fonte recusa
COLETOR_DJEN = "djen-coletor 1.2 / matcher 3"
RESCAN_DIAS = 7              # cada pesquisa diária relê os últimos 7 dias (publicações tardias no DJEN)
FALHAS_SEGUIDAS_ABORTA = 3   # numa execução, após 3 janelas seguidas falhando na mesma consulta, as demais não são tentadas
TENTATIVAS_REPROCESSO = 6    # depois disso, a consulta que continua falhando só é refeita uma vez por dia


def nome_generico(nome_norm: str) -> bool:
    return len((nome_norm or "").replace(" ", "")) < NOME_MIN_CARACTERES


def agora_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def registrar_saude(con: sqlite3.Connection, lock: threading.Lock, fonte: str, ok: bool, erro: str) -> None:
    with lock:
        agora = agora_iso()
        if ok:
            con.execute("INSERT INTO saude_conectores(fonte,estado,ultimo_sucesso,falhas_consecutivas,atualizado_em) "
                        "VALUES (?, 'operacional', ?, 0, ?) ON CONFLICT(fonte) DO UPDATE SET estado='operacional',"
                        "ultimo_sucesso=excluded.ultimo_sucesso, falhas_consecutivas=0, atualizado_em=excluded.atualizado_em",
                        (fonte, agora, agora))
        else:
            con.execute("INSERT INTO saude_conectores(fonte,estado,ultima_falha,ultimo_erro,falhas_consecutivas,atualizado_em) "
                        "VALUES (?, 'degradado', ?, ?, 1, ?) ON CONFLICT(fonte) DO UPDATE SET "
                        "estado=CASE WHEN falhas_consecutivas+1>=3 THEN 'indisponivel' ELSE 'degradado' END,"
                        "ultima_falha=excluded.ultima_falha, ultimo_erro=excluded.ultimo_erro,"
                        "falhas_consecutivas=falhas_consecutivas+1, atualizado_em=excluded.atualizado_em",
                        (fonte, agora, erro, agora))
        con.commit()


def _chunks(ini: date, fim: date) -> List[tuple]:
    out, cur = [], ini
    while cur <= fim:
        f = min(cur + timedelta(days=CHUNK_DIAS - 1), fim)
        out.append((cur, f))
        cur = f + timedelta(days=1)
    return out


def _normalizar_item(it: Dict) -> Dict:
    return {"id": it["id"], "fonte": "djen", "numero_processo": it.get("numero_processo") or "",
            "tribunal": it.get("siglaTribunal"), "tipo": it.get("tipoComunicacao"),
            "data_disponibilizacao": it.get("data_disponibilizacao"), "classe": it.get("nomeClasse"),
            "orgao": it.get("nomeOrgao"), "link": it.get("link"), "texto": it.get("texto") or "",
            "destinatarios": [{"nome": d.get("nome"), "polo": d.get("polo")} for d in (it.get("destinatarios") or [])]}


def consultas_do_cliente(c: Dict) -> List[Dict]:
    vistos, qs = set(), []
    for n in c["nomes"]:
        if n["nome_norm"] not in vistos:
            vistos.add(n["nome_norm"])
            if nome_generico(n["nome_norm"]):
                continue  # esse cliente continua monitorado pelo CNPJ (busca textual)
            qs.append(("nomeParte", n["nome_norm"]))  # sem sufixo societário: a fonte grafa "SIA", "S/A", "LTDA" de formas diversas
    for x in c["cnpjs"]:
        qs.append(("texto", _cnpj.formatar(x["cnpj"])))
    # alguns tribunais publicam a parte só com iniciais ("T. T. M. L."): busca também essa forma
    for n in c["nomes"]:
        ini = _nomes.iniciais(n["nome"])
        if ini and ("nomeParte", ini) not in qs:
            qs.append(("nomeParte", ini))
    # o nome também é buscado no CORPO das comunicações: o DJEN omite, abrevia (iniciais) ou não lista o cliente nas partes
    for p_, v_ in [q for q in qs if q[0] == "nomeParte" and not _nomes.e_abreviacao(q[1])]:
        qs.append(("texto", v_))
    return [{"param": p, "valor": v, "chave": "%s:%s" % (p, v)} for p, v in qs]


class Motor:
    def __init__(self, con: sqlite3.Connection, djen: ConectorDJEN, datajud: ConectorDataJud, dias_retroativos: int = 7,
                 dias_nova: int = 15, trabalhadores: int = 3, hoje: Optional[date] = None,
                 limite_enriquecimento: int = 200):
        self.con, self.djen, self.datajud = con, djen, datajud
        self.registro = None  # ConectorRegistroCNPJ (opcional): completa a razão social oficial do cliente
        self.dias_retro, self.dias_nova, self.trab = dias_retroativos, dias_nova, trabalhadores
        self.hoje = hoje or date.today()
        self.limite_enr = limite_enriquecimento
        self.trab_enr = 8

    # ---------- planejamento ----------
    def _consultas_do_cliente(self, c: Dict) -> List[Dict]:
        return consultas_do_cliente(c)

    def _aposentar_consultas_obsoletas(self, c: Dict) -> None:
        """Falhas de consultas que o plano atual do cliente não faz mais (ex.: nome curto demais) deixam de contar como pendentes."""
        chaves = [q["chave"] for q in self._consultas_do_cliente(c)]
        marcas = ",".join("?" * len(chaves)) or "''"
        self.con.execute("UPDATE consultas SET desistida=1 WHERE cliente_id=? AND status='falha' AND desistida=0 AND chave NOT IN (%s)" % marcas,
                         [c["id"]] + chaves)
        self.con.commit()

    def _janela(self, c, q, desde, ate, forcar) -> Optional[tuple]:
        fim = ate or self.hoje
        if desde:
            return (desde, fim)
        r = self.con.execute("SELECT max(janela_fim) m, max(concluida_em) c FROM consultas WHERE fonte='djen' AND chave=? "
                             "AND status='ok'", (q["chave"],)).fetchone()
        if r["m"]:
            if not forcar and r["c"]:
                ult = datetime.fromisoformat(r["c"])
                # frequência diária = no máximo uma vez por dia civil (evita pular o dia quando o horário de ontem foi mais tarde)
                recente = ult.date() >= self.hoje if c["frequencia_horas"] <= 24 else ult > datetime.now() - timedelta(hours=c["frequencia_horas"])
                if recente:
                    return None  # consulta recente: evita repetição desnecessária
            # sobreposição: relê os últimos RESCAN_DIAS dias a cada pesquisa, porque o DJEN indexa publicações com atraso
            return (min(date.fromisoformat(r["m"]) - timedelta(days=1), fim - timedelta(days=RESCAN_DIAS - 1)), fim)
        return (fim - timedelta(days=self.dias_retro), fim)

    def _coberto(self, chave: str, a: date, b: date) -> bool:
        """Janela já consultada com sucesso e encerrada (concluída depois do último dia consultado)."""
        return self.con.execute(
            "SELECT 1 FROM consultas WHERE fonte='djen' AND chave=? AND status='ok' AND janela_inicio<=? AND janela_fim>=? "
            "AND date(concluida_em)>?", (chave, a.isoformat(), b.isoformat(), b.isoformat())).fetchone() is not None

    # ---------- progresso (só a thread principal grava no banco) ----------
    def _progresso(self, exec_id: int, pct: float, fase: str, texto: str, force: bool = False) -> None:
        agora = datetime.now().timestamp()
        if not force and agora - getattr(self, "_prog_ts", 0) < 0.8:
            return
        self._prog_ts = agora
        self.con.execute("UPDATE execucoes SET progresso=? WHERE id=?",
                         (json.dumps({"pct": round(min(pct, 99.0), 1), "fase": fase, "texto": texto,
                                      "atualizado_em": agora_iso()}, ensure_ascii=False), exec_id))
        self.con.commit()

    def _tick(self) -> None:
        with self._prog_lock:
            self._prog_feito += 1

    # ---------- coleta (rede; roda em threads, sem tocar o banco) ----------
    def _coletar(self, c: Dict, plano: List[tuple]) -> List[Dict]:
        resultados, vistos, seguidas = [], {}, {}
        for q, a, b in plano:
            if True:
                if seguidas.get(q["chave"], 0) >= FALHAS_SEGUIDAS_ABORTA:
                    # fonte recusando esta consulta: não gasta minutos em tentativas inúteis; fica registrada para o reprocesso
                    resultados.append({"q": q, "ini": a.isoformat(), "fim": b.isoformat(), "inicio": agora_iso(), "achados": [],
                                       "status": "falha", "total": None, "lidos": 0,
                                       "erro": "não tentada: %d falhas seguidas nesta consulta" % FALHAS_SEGUIDAS_ABORTA})
                    self._tick()
                    continue
                r = self._ler_janela(c, q, a, b, vistos)
                if r["status"] == "truncada" and a < b:
                    # a fonte limita a 10.000 itens por consulta: refaz dia a dia para não perder nada
                    subs = [self._ler_janela(c, q, a + timedelta(days=i), a + timedelta(days=i), vistos) for i in range((b - a).days + 1)]
                    resultados.extend(subs)
                    seguidas[q["chave"]] = 0 if all(x["status"] != "falha" for x in subs) else seguidas.get(q["chave"], 0) + 1
                    self._tick()
                    continue
                seguidas[q["chave"]] = seguidas.get(q["chave"], 0) + 1 if r["status"] == "falha" else 0
                resultados.append(r)
                self._tick()
        return resultados

    def _ler_janela(self, c: Dict, q: Dict, a: date, b: date, vistos: Dict) -> Dict:
        r = {"q": q, "ini": a.isoformat(), "fim": b.isoformat(), "inicio": agora_iso(), "achados": []}
        try:
            itens, total, trunc = self.djen.consultar(q["param"], q["valor"], r["ini"], r["fim"])
            u = self.djen.http.ultima()
            r.update(status="truncada" if trunc else "ok", total=total, lidos=len(itens), erro=None, http_status=u.get("http_status"),
                     codigo_erro=None, hash_resposta=u.get("hash_resposta"),
                     resultado="TRUNCATED" if trunc else ("ZERO_RESULTS" if not itens else "RESULTS"))
            for it in itens:
                com = _normalizar_item(it)
                if com["id"] in vistos or len(com["numero_processo"]) != 20:
                    continue
                vistos[com["id"]] = True
                m = matcher.avaliar(c, com)
                if m:
                    r["achados"].append((com, m))
        except (ErroFonte, ValueError, KeyError, TypeError, AttributeError) as e:
            u = self.djen.http.ultima()
            r.update(status="falha", total=None, lidos=0, erro="%s: %s" % (type(e).__name__, e), http_status=u.get("http_status"),
                     codigo_erro=u.get("codigo") if u.get("codigo") != "SUCCESS" else "UNKNOWN_ERROR", hash_resposta=u.get("hash_resposta"),
                     resultado="QUERY_FAILED")
        return r

    # ---------- persistência ----------
    def _persistir(self, exec_id: int, c: Dict, resultados: List[Dict]) -> Dict[str, int]:
        con, agora, cont = self.con, agora_iso(), {"novas": 0, "atualizadas": 0}
        for r in resultados:
            cons_id = con.execute("INSERT INTO consultas(execucao_id,cliente_id,fonte,chave,janela_inicio,janela_fim,status,total_fonte,"
                                  "itens_lidos,erro,iniciada_em,concluida_em,resultado,http_status,codigo_erro,versao_coletor,hash_resposta) "
                                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  (exec_id, c["id"], "djen", r["q"]["chave"], r["ini"], r["fim"], r["status"], r["total"], r["lidos"],
                                   r["erro"], r["inicio"], agora, r.get("resultado") or ("QUERY_FAILED" if r["status"] == "falha" else ("ZERO_RESULTS" if not r["lidos"] else "RESULTS")),
                                   r.get("http_status"), r.get("codigo_erro"), COLETOR_DJEN, r.get("hash_resposta"))).lastrowid
            novas_antes = cont["novas"]
            fonte_label = "DJEN_CADERNO" if r["q"].get("param") == "caderno" else "DJEN_BUSCA"
            for com, m in r["achados"]:
                con.execute("INSERT OR IGNORE INTO comunicacoes(id,fonte,numero_processo,tribunal,tipo,data_disponibilizacao,classe,"
                            "orgao,link,texto,destinatarios,coletado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (com["id"], com["fonte"], com["numero_processo"], com["tribunal"], com["tipo"],
                             com["data_disponibilizacao"], com["classe"], com["orgao"], com["link"], com["texto"],
                             json.dumps(com["destinatarios"], ensure_ascii=False), agora))
                con.execute("INSERT OR IGNORE INTO processos(numero_processo,tribunal,classe,orgao,primeira_identificacao,"
                            "ultima_verificacao) VALUES (?,?,?,?,?,?)",
                            (com["numero_processo"], com["tribunal"], com["classe"], com["orgao"], agora, agora))
                pid = con.execute("SELECT id FROM processos WHERE numero_processo=?", (com["numero_processo"],)).fetchone()[0]
                ev = json.dumps({**m["evidencia"], "comunicacao_id": com["id"], "tipo_comunicacao": com["tipo"],
                                 "data_disponibilizacao": com["data_disponibilizacao"], "consulta": r["q"]["chave"]},
                                ensure_ascii=False)
                ex = con.execute("SELECT id,classificacao,estado_validacao,evidencia FROM correspondencias WHERE cliente_id=? AND processo_id=?",
                                 (c["id"], pid)).fetchone()
                if ex is None:
                    con.execute("INSERT INTO correspondencias(cliente_id,processo_id,comunicacao_id,nivel,classificacao,polo,"
                                "nome_na_fonte,evidencia,fonte,estado_validacao,identificada_em,ultima_verificacao) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (c["id"], pid, com["id"], m["nivel"], m["classificacao"], m["polo"], m["nome_na_fonte"], ev,
                                 "djen", ESTADO_AUTO[m["classificacao"]], agora, agora))
                    cont["novas"] += 1
                    _radar.evento(con, pid, "PROCESS_FIRST_SEEN", c["id"], fonte_label, "primeiro registro deste processo para o cliente (%s)" % m["classificacao"])
                elif ex["estado_validacao"] in ESTADOS_AUTOMATICOS and (
                        RANK[m["classificacao"]] > RANK[ex["classificacao"]]
                        or ("numeroProcesso:" in (ex["evidencia"] or "") and not r["q"]["chave"].startswith("numeroProcesso:")
                            and RANK[m["classificacao"]] >= RANK[ex["classificacao"]])):   # a pesquisa passa a ser a origem do registro
                    con.execute("UPDATE correspondencias SET comunicacao_id=?,nivel=?,classificacao=?,polo=?,nome_na_fonte=?,"
                                "evidencia=?,estado_validacao=?,ultima_verificacao=? WHERE id=?",
                                (com["id"], m["nivel"], m["classificacao"], m["polo"], m["nome_na_fonte"], ev,
                                 ESTADO_AUTO[m["classificacao"]], agora, ex["id"]))
                    cont["atualizadas"] += 1
                else:
                    con.execute("UPDATE correspondencias SET ultima_verificacao=? WHERE id=?", (agora, ex["id"]))
                _radar.registrar_visto(con, pid, fonte_label, com["id"], c["id"])
                _radar.registrar_partes(con, pid, com.get("destinatarios"), fonte_label)
                _radar.registrar_campos(con, pid, {"classe": com.get("classe"), "orgao": com.get("orgao")}, fonte_label)
                _radar.marcar_citacao(con, pid, c["id"], com.get("tipo"))
                _radar.reconciliar_processo(con, pid)
            con.execute("UPDATE consultas SET novos=? WHERE id=?", (cont["novas"] - novas_antes, cons_id))
        if resultados and all(r["status"] in ("ok", "truncada") for r in resultados):
            con.execute("UPDATE clientes SET ultima_verificacao=? WHERE id=?", (agora, c["id"]))
        con.commit()
        return cont

    def reavaliar_base(self) -> Dict[str, int]:
        """Reaplica o matcher atual às comunicações já guardadas; só mexe no que não foi decidido por pessoa."""
        con, res = self.con, {"avaliadas": 0, "alteradas": 0}
        for r in con.execute("SELECT c.id,c.cliente_id,c.classificacao,c.estado_validacao,m.* FROM correspondencias c "
                             "JOIN comunicacoes m ON m.id=c.comunicacao_id").fetchall():
            res["avaliadas"] += 1
            if r["estado_validacao"] not in ESTADOS_AUTOMATICOS:
                continue
            com = {"texto": r["texto"] or "", "destinatarios": json.loads(r["destinatarios"] or "[]")}
            m = matcher.avaliar(_clientes.carregar(con, r["cliente_id"]), com)
            if m and m["classificacao"] != r["classificacao"]:
                ev = json.dumps({**m["evidencia"], "comunicacao_id": r["id"], "reavaliada_em": agora_iso()}, ensure_ascii=False)
                con.execute("UPDATE correspondencias SET nivel=?,classificacao=?,polo=?,nome_na_fonte=?,evidencia=?,estado_validacao=?,"
                            "ultima_verificacao=? WHERE id=?", (m["nivel"], m["classificacao"], m["polo"], m["nome_na_fonte"], ev,
                                                                ESTADO_AUTO[m["classificacao"]], agora_iso(), r["id"]))
                res["alteradas"] += 1
        con.commit()
        return res

    # ---------- enriquecimento DataJud ----------
    def enriquecer_pendentes(self, exec_id: Optional[int] = None) -> Dict[str, int]:
        con, res = self.con, {"ok": 0, "nao_encontrado": 0, "falha": 0}
        rows = con.execute("SELECT DISTINCT p.id,p.numero_processo,p.tribunal FROM processos p JOIN correspondencias c "
                           "ON c.processo_id=p.id WHERE c.classificacao!='descartada' AND (p.datajud_status IN ('pendente','falha') OR "
                           "(p.datajud_status='nao_encontrado' AND date(p.primeira_identificacao)>=date('now','-14 day') "
                           "AND date(p.ultima_verificacao)<date('now','localtime'))) "
                           "ORDER BY (c.classificacao='confirmada') DESC, p.id LIMIT ?", (self.limite_enr,)).fetchall()
        por_trib: Dict[str, list] = {}
        for p in rows:
            por_trib.setdefault(p["tribunal"], []).append(p)
        lotes = [(t, ps[i:i + LOTE_DATAJUD]) for t, ps in por_trib.items() for i in range(0, len(ps), LOTE_DATAJUD)]

        def buscar(lote):
            t, ps = lote
            try:
                return ps, self.datajud.obter_lote([p["numero_processo"] for p in ps], t), None
            except ErroFonte as e:
                return ps, None, e
        with ThreadPoolExecutor(max_workers=self.trab_enr) as ex:  # rede em paralelo; banco só nesta thread
            for n, (ps, dados, err) in enumerate(ex.map(buscar, lotes), 1):
                if exec_id:
                    self._progresso(exec_id, 77 + 18.0 * n / max(len(lotes), 1), "datajud",
                                    "DataJud: lote %d de %d (%d processos)" % (n, len(lotes), len(rows)))
                agora = agora_iso()
                for p in ps:
                    d = None if dados is None else dados.get(p["numero_processo"])
                    if err is not None:
                        con.execute("UPDATE processos SET datajud_status='falha',ultima_verificacao=? WHERE id=?", (agora, p["id"]))
                        res["falha"] += 1
                    elif d is None:
                        con.execute("UPDATE processos SET datajud_status='nao_encontrado',ultima_verificacao=? WHERE id=?", (agora, p["id"]))
                        res["nao_encontrado"] += 1
                    else:
                        con.execute("UPDATE processos SET datajud_status='ok',data_ajuizamento=?,grau=?,sistema=?,assuntos=?,"
                                    "classe=COALESCE(?,classe),orgao=COALESCE(?,orgao),ultima_verificacao=? WHERE id=?",
                                    (d["data_ajuizamento"], d["grau"], d["sistema"], json.dumps(d["assuntos"], ensure_ascii=False),
                                     d["classe"], d["orgao"], agora, p["id"]))
                        res["ok"] += 1
                        _radar.registrar_visto(con, p["id"], "DATAJUD", d.get("sistema") or "")
                        _radar.registrar_campos(con, p["id"], {"data_ajuizamento": d["data_ajuizamento"], "classe": d["classe"], "orgao": d["orgao"],
                                                               "grau": d["grau"], "sistema": d["sistema"]}, "DATAJUD")
                        _radar.reconciliar_processo(con, p["id"])
                con.commit()
        self.classificar_datas()
        con.commit()
        return res

    def classificar_datas(self) -> None:
        """Distingue distribuição nova de processo antigo localizado agora (data de ajuizamento x data de identificação)."""
        for r in self.con.execute("SELECT c.id,c.identificada_em,p.data_ajuizamento FROM correspondencias c "
                                  "JOIN processos p ON p.id=c.processo_id").fetchall():
            if not r["data_ajuizamento"]:
                cat = "indeterminada"
            else:
                dias = (datetime.fromisoformat(r["identificada_em"]).date() - date.fromisoformat(r["data_ajuizamento"])).days
                cat = "nova_distribuicao" if dias <= self.dias_nova else "processo_antigo"
            self.con.execute("UPDATE correspondencias SET categoria_data=? WHERE id=?", (cat, r["id"]))

    # ---------- execução ----------
    def executar(self, gatilho: str = "manual", cliente_ids: Optional[List[int]] = None, desde: Optional[date] = None,
                 ate: Optional[date] = None, forcar: bool = False) -> Dict:
        con = self.con
        exec_id = con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES (?,?)", (gatilho, agora_iso())).lastrowid
        con.commit()
        cs = [c for c in _clientes.ativos(con) if not cliente_ids or c["id"] in cliente_ids]
        planos, puladas = [], 0
        for c in cs:
            self._aposentar_consultas_obsoletas(c)
            plano = []
            for q in self._consultas_do_cliente(c):
                j = self._janela(c, q, desde, ate, forcar)
                if j is None:
                    puladas += 1
                    continue
                for a, b in _chunks(*j):
                    if not forcar and desde and self._coberto(q["chave"], a, b):
                        puladas += 1
                    else:
                        plano.append((q, a, b))
            planos.append((c, plano))
        return self._rodar(exec_id, planos, puladas, len(cs))

    def _rodar(self, exec_id: int, planos: List[tuple], puladas: int, n_clientes: int) -> Dict:
        con = self.con
        self._prog_lock, self._prog_feito = threading.Lock(), 0
        total_q = sum(len(pl) for _, pl in planos)
        self._progresso(exec_id, 2, "coleta", "Iniciando: %d consulta(s) ao DJEN planejada(s)" % total_q, force=True)
        with ThreadPoolExecutor(max_workers=self.trab) as ex:
            futuros = [(c, ex.submit(self._coletar, c, pl)) for c, pl in planos if pl]
            pend = {f for _, f in futuros}
            while pend:
                _, pend = wait(pend, timeout=1.0)
                feito = self._prog_feito
                self._progresso(exec_id, 2 + 66.0 * feito / max(total_q, 1), "coleta",
                                "Consultando o DJEN: %d de %d consultas" % (feito, total_q))
            resultados = [(c, f.result()) for c, f in futuros]
        tot = {"novas": 0, "atualizadas": 0}
        todas = []
        for i, (c, rs) in enumerate(resultados, 1):
            self._progresso(exec_id, 68 + 8.0 * i / max(len(resultados), 1), "gravacao",
                            "Gravando resultados: cliente %d de %d" % (i, len(resultados)))
            k = self._persistir(exec_id, c, rs)
            tot["novas"] += k["novas"]; tot["atualizadas"] += k["atualizadas"]
            todas.extend(rs)
        self._progresso(exec_id, 77, "datajud", "Enriquecendo com a data de ajuizamento (DataJud)…", force=True)
        enr = self.enriquecer_pendentes(exec_id)
        falhas = sum(1 for r in todas if r["status"] == "falha")
        status = "sucesso" if falhas == 0 else ("falha" if falhas == len(todas) else "parcial")
        resumo = {"clientes": n_clientes, "consultas_executadas": len(todas), "consultas_com_falha": falhas,
                  "consultas_truncadas": sum(1 for r in todas if r["status"] == "truncada"),
                  "consultas_puladas_por_dedup": puladas, "correspondencias_novas": tot["novas"],
                  "correspondencias_atualizadas": tot["atualizadas"], "datajud": enr}
        con.execute("UPDATE execucoes SET fim=?,status=?,resumo=? WHERE id=?",
                    (agora_iso(), status, json.dumps(resumo, ensure_ascii=False), exec_id))
        con.commit()
        return {"execucao_id": exec_id, "status": status, **resumo}

    def preencher_lacunas(self, dias: int = 95) -> Dict:
        """Refaz os dias que o registro de cobertura aponta como não lidos (ex.: Mac desligado, fonte fora do ar)."""
        from . import cobertura
        con = self.con
        lac = cobertura.lacunas(con, self.hoje, dias)
        exec_id = con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('preenchimento',?)", (agora_iso(),)).lastrowid
        con.commit()
        por_cliente: Dict[int, tuple] = {}
        for g in lac:
            c = por_cliente.setdefault(g["cliente_id"], (_clientes.carregar(con, g["cliente_id"]), []))
            q = next((x for x in consultas_do_cliente(c[0]) if x["chave"] == g["chave"]), None)
            if q:
                c[1].extend((q, a, b) for a, b in _chunks(date.fromisoformat(g["ini"]), date.fromisoformat(g["fim"])))
        return self._rodar(exec_id, list(por_cliente.values()), 0, len(por_cliente))

    def reprocessar_falhas(self, horas: int = 48) -> Dict:
        """Refaz consultas que falharam e ainda não foram cobertas por uma consulta bem-sucedida."""
        con = self.con
        desde = (datetime.now() - timedelta(hours=horas)).isoformat(timespec="seconds")
        for c in _clientes.ativos(con):
            self._aposentar_consultas_obsoletas(c)
        ontem = (datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds")
        falhas = con.execute("SELECT cliente_id,chave,janela_inicio,janela_fim,count(*) tent,max(concluida_em) ult FROM consultas f "
                             "WHERE status='falha' AND desistida=0 AND concluida_em>=? AND NOT EXISTS (SELECT 1 FROM consultas o "
                             "WHERE o.cliente_id=f.cliente_id AND o.chave=f.chave AND o.status IN ('ok','truncada') "
                             "AND o.janela_inicio<=f.janela_inicio AND o.janela_fim>=f.janela_fim) "
                             "GROUP BY cliente_id,chave,janela_inicio,janela_fim HAVING tent<? OR ult<?",
                             (desde, TENTATIVAS_REPROCESSO, ontem)).fetchall()
        exec_id = con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('reprocessamento',?)", (agora_iso(),)).lastrowid
        con.commit()
        por_cliente: Dict[int, list] = {}
        for f in falhas:
            param, valor = f["chave"].split(":", 1)
            por_cliente.setdefault(f["cliente_id"], []).append(
                ({"param": param, "valor": valor, "chave": f["chave"]}, date.fromisoformat(f["janela_inicio"]), date.fromisoformat(f["janela_fim"])))
        planos = [(_clientes.carregar(con, cid), pl) for cid, pl in por_cliente.items()]
        return self._rodar(exec_id, planos, 0, len(planos))
