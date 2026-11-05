"""Auditoria de chamadas, saúde das fontes e motor de cobertura (Radar Judicial).
Regra de ouro: ZERO_RESULTS (consulta válida, nada encontrado) nunca se confunde com QUERY_FAILED (não sabemos)."""
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from . import clientes as _clientes
from . import config as _config

FONTES = ["DJEN_BUSCA", "DJEN_CADERNO", "DATAJUD", "CNPJ_REGISTRO"]
ROTULO = {"DJEN_BUSCA": "DJEN (busca por parte/texto)", "DJEN_CADERNO": "DJEN (cadernos completos)", "DATAJUD": "DataJud (CNJ)",
          "CNPJ_REGISTRO": "Cadastro de CNPJ (Receita)"}


def registrar_chamada(con: sqlite3.Connection, lock: threading.Lock, d: Dict) -> None:
    with lock:
        con.execute("INSERT INTO fonte_chamadas(fonte,tribunal,quando,metodo,consulta,http_status,latencia_ms,ok,final,codigo,hash_resposta,versao_coletor) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (d["fonte"], d.get("tribunal"), datetime.now().isoformat(timespec="seconds"), d.get("metodo"), d.get("consulta"),
                     d.get("http_status"), d.get("latencia_ms"), d["ok"], d.get("final", 1), d["codigo"], d.get("hash_resposta"), d.get("versao_coletor")))
        con.commit()


def sondar(con: sqlite3.Connection, motor) -> Dict:
    """Sondas baratas, independentes de haver trabalho a fazer: garantem que a saúde de cada fonte seja medida continuamente."""
    res = {}
    try:
        motor.datajud.http.requisitar("POST", "https://api-publica.datajud.cnj.jus.br/api_publica_tjpr/_search",
                                      {"Authorization": "APIKey " + motor.datajud.chave}, {"size": 0, "query": {"match_all": {}}})
        res["DATAJUD"] = "ok"
    except Exception as e:
        res["DATAJUD"] = "falha: %s" % e
    try:
        motor.djen.http.requisitar("GET", "https://comunicaapi.pje.jus.br/api/v1/comunicacao/tribunal")
        res["DJEN_BUSCA"] = "ok"
    except Exception as e:
        res["DJEN_BUSCA"] = "falha: %s" % e
    return res


def podar(con: sqlite3.Connection) -> int:
    dias = _config.obter(con, "radar")["chamadas_dias_retencao"]
    n = con.execute("DELETE FROM fonte_chamadas WHERE quando<?", ((datetime.now() - timedelta(days=dias)).isoformat(timespec="seconds"),)).rowcount
    con.commit()
    return n


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def saude_fontes(con: sqlite3.Connection, agora: Optional[datetime] = None) -> List[Dict]:
    agora = agora or datetime.now()
    cfg = _config.obter(con, "radar")
    out = []
    for f in FONTES:
        def agg(horas):
            r = con.execute("SELECT count(*) n, sum(ok) ok, avg(latencia_ms) lat FROM fonte_chamadas WHERE fonte=? AND final=1 AND quando>=?",
                            (f, _iso(agora - timedelta(hours=horas)))).fetchone()
            return r["n"] or 0, r["ok"] or 0, r["lat"]
        n24, ok24, lat24 = agg(24)
        n7, ok7, lat7 = agg(24 * 7)
        ult_ok = con.execute("SELECT max(quando) FROM fonte_chamadas WHERE fonte=? AND ok=1", (f,)).fetchone()[0]
        ult_falha = con.execute("SELECT max(quando) FROM fonte_chamadas WHERE fonte=? AND ok=0 AND final=1", (f,)).fetchone()[0]
        ultimas = [r["ok"] for r in con.execute("SELECT ok FROM fonte_chamadas WHERE fonte=? AND final=1 ORDER BY id DESC LIMIT 3", (f,))]
        cod = con.execute("SELECT codigo FROM fonte_chamadas WHERE fonte=? AND ok=0 AND final=1 ORDER BY id DESC LIMIT 1", (f,)).fetchone()
        taxa24 = (ok24 / n24) if n24 else None
        taxa7 = (ok7 / n7) if n7 else None
        frescor = cfg["frescor_horas"].get(f, 36)
        velho = ult_ok is None or datetime.fromisoformat(ult_ok) < agora - timedelta(hours=frescor)
        if n7 == 0:
            estado = "UNKNOWN"
        elif len(ultimas) >= 3 and not any(ultimas):            # as 3 chamadas finais mais recentes falharam
            estado = "DOWN"
        elif velho or not all(ultimas):                          # falha entre as 3 últimas ou dado velho demais; a taxa de 24h/7d é só informativa
            estado = "DEGRADED"
        else:
            estado = "HEALTHY"
        desde = None
        if estado in ("DOWN", "DEGRADED") and ult_falha and (ult_ok is None or ult_falha > ult_ok):
            r = con.execute("SELECT min(quando) FROM fonte_chamadas WHERE fonte=? AND ok=0 AND final=1 AND quando>?", (f, ult_ok or "0000")).fetchone()
            desde = r[0]
        probl = [dict(r) for r in con.execute(
            "SELECT tribunal,count(*) n,sum(ok) ok FROM fonte_chamadas WHERE fonte=? AND final=1 AND tribunal IS NOT NULL AND quando>=? GROUP BY tribunal "
            "HAVING (1.0*sum(ok)/count(*))<0.9 ORDER BY count(*)-sum(ok) DESC LIMIT 8", (f, _iso(agora - timedelta(hours=24))))]
        out.append({"fonte": f, "rotulo": ROTULO[f], "estado": estado, "ultima_consulta": con.execute(
            "SELECT max(quando) FROM fonte_chamadas WHERE fonte=?", (f,)).fetchone()[0], "ultimo_sucesso": ult_ok, "ultima_falha": ult_falha,
            "taxa_sucesso_24h": None if taxa24 is None else round(100 * taxa24, 1), "taxa_sucesso_7d": None if taxa7 is None else round(100 * taxa7, 1),
            "latencia_media_ms": None if (lat24 or lat7) is None else int(lat24 or lat7), "chamadas_24h": n24,
            "ultimo_erro": cod["codigo"] if cod else None, "indisponivel_desde": desde,
            "tribunais_com_problema": probl, "frescor_max_horas": frescor})
    return out


def _fontes_nao_monitoradas(con) -> List[Dict]:
    from . import adapters
    return adapters.status_planejadas(con)


def _hhmm(iso: Optional[str]) -> str:
    return iso[11:16] if iso else "?"


def cobertura_monitoramento(con: sqlite3.Connection, agora: Optional[datetime] = None) -> Dict:
    """Quais fontes ESPERADAS estão operando, global e por cliente. O percentual nunca aparece sozinho."""
    agora = agora or datetime.now()
    cfg = _config.obter(con, "radar")
    saude = {s["fonte"]: s for s in saude_fontes(con, agora)}
    esperadas = [f for f in cfg["fontes_esperadas"] if f in saude]
    clientes = []
    for c in _clientes.ativos(con):
        fontes = []
        for f in esperadas:
            s = saude[f]
            estado, det, ult = s["estado"], None, s["ultimo_sucesso"]
            if f == "DJEN_BUSCA":
                from .motor import consultas_do_cliente
                lim = _iso(agora - timedelta(hours=cfg["frescor_horas"]["DJEN_BUSCA"]))
                faltam = [q["chave"] for q in consultas_do_cliente(c) if not con.execute(
                    "SELECT 1 FROM consultas WHERE cliente_id=? AND chave=? AND status IN ('ok','truncada') AND concluida_em>=?", (c["id"], q["chave"], lim)).fetchone()]
                ult = con.execute("SELECT max(concluida_em) FROM consultas WHERE cliente_id=? AND status='ok'", (c["id"],)).fetchone()[0]
                if faltam:
                    estado, det = ("DEGRADED" if estado == "HEALTHY" else estado), "%d consulta(s) sem leitura recente" % len(faltam)
            fontes.append({"fonte": f, "rotulo": ROTULO[f], "estado": estado, "ultima_consulta": ult, "detalhe": det or (
                "indisponível desde %s" % _hhmm(s["indisponivel_desde"]) if estado in ("DOWN", "DEGRADED") and s["indisponivel_desde"] else None)})
        ok = [x for x in fontes if x["estado"] == "HEALTHY"]
        clientes.append({"cliente_id": c["id"], "cliente": c["razao_social"], "esperadas": len(fontes), "operacionais": len(ok),
                         "completa": len(ok) == len(fontes) and bool(fontes), "fontes": fontes,
                         "falhas": [x for x in fontes if x["estado"] != "HEALTHY"]})
    ok_g = [f for f in esperadas if saude[f]["estado"] == "HEALTHY"]
    completa = len(ok_g) == len(esperadas) and all(c["completa"] for c in clientes)
    ult_geral = max([s["ultima_consulta"] for s in saude.values() if s["ultima_consulta"]], default=None)
    ruins = [saude[f] for f in esperadas if saude[f]["estado"] != "HEALTHY"]
    nao = _fontes_nao_monitoradas(con)
    return {"gerado_em": _iso(agora), "esperadas": len(esperadas), "operacionais": len(ok_g), "completa": completa, "ultima_verificacao": ult_geral,
            "fontes": [saude[f] for f in FONTES], "esperadas_lista": esperadas, "clientes": clientes, "nao_monitoradas": nao,
            "problemas": [{"fonte": r["fonte"], "estado": r["estado"], "desde": r["indisponivel_desde"], "erro": r["ultimo_erro"]} for r in ruins],
            "resumo": "%d/%d fontes operacionais" % (len(ok_g), len(esperadas)) + ("" if completa else "; " + "; ".join(
                "%s %s%s" % (r["rotulo"], "indisponível" if r["estado"] == "DOWN" else "degradada" if r["estado"] == "DEGRADED" else "sem dados",
                             " desde %s" % _hhmm(r["indisponivel_desde"]) if r["indisponivel_desde"] else "") for r in ruins))}


def frase_sem_novos(cob: Dict) -> str:
    """Regra de apresentação: nunca dizer 'nenhuma ação' como se fosse certeza quando a cobertura está incompleta."""
    nao = cob["nao_monitoradas"]
    complemento = (" Não monitorado: %s." % ", ".join(n["rotulo"] for n in nao)) if nao else ""
    if cob["completa"]:
        return ("Nenhum novo processo identificado. Monitoramento validado: %d/%d fontes consultadas com sucesso. Última verificação: %s.%s"
                % (cob["operacionais"], cob["esperadas"], _hhmm(cob["ultima_verificacao"]), complemento))
    return "Nenhum novo processo identificado nas fontes consultadas. Cobertura INCOMPLETA: %s.%s" % (cob["resumo"].split("; ", 1)[-1], complemento)


def metricas(con: sqlite3.Connection, dias: int = 1) -> Dict:
    lim = _iso(datetime.now() - timedelta(days=dias))
    r = con.execute("SELECT count(*) n,sum(ok) ok,avg(latencia_ms) lat FROM fonte_chamadas WHERE quando>=?", (lim,)).fetchone()
    q = con.execute("SELECT count(*) n, sum(status='ok') ok, sum(status='falha') f, coalesce(sum(itens_lidos),0) lidos, coalesce(sum(novos),0) novos, "
                    "sum(resultado='ZERO_RESULTS') zero FROM consultas WHERE concluida_em>=?", (lim,)).fetchone()
    por_codigo = {x["codigo"]: x["n"] for x in con.execute("SELECT codigo,count(*) n FROM fonte_chamadas WHERE quando>=? GROUP BY codigo", (lim,))}
    return {"periodo_dias": dias, "chamadas": r["n"] or 0, "chamadas_ok": r["ok"] or 0, "chamadas_falhas": (r["n"] or 0) - (r["ok"] or 0),
            "latencia_media_ms": int(r["lat"]) if r["lat"] else None, "por_codigo": por_codigo,
            "consultas_logicas": q["n"] or 0, "consultas_com_resultado_zero": q["zero"] or 0, "consultas_falhas": q["f"] or 0,
            "itens_lidos": q["lidos"], "processos_novos": q["novos"],
            "matches_automaticos": con.execute("SELECT count(*) FROM correspondencias WHERE estado_validacao IN ('automatico') AND identificada_em>=?", (lim,)).fetchone()[0],
            "matches_manuais": con.execute("SELECT count(*) FROM validacoes WHERE decidida_em>=?", (lim,)).fetchone()[0],
            "falsos_positivos": con.execute("SELECT count(*) FROM validacoes WHERE decisao='descartada' AND decidida_em>=?", (lim,)).fetchone()[0],
            "fontes_indisponiveis": [s["fonte"] for s in saude_fontes(con) if s["estado"] in ("DOWN", "DEGRADED")]}
