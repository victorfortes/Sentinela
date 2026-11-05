"""Garantia de cobertura: registro de quais dias de publicação foram lidos, por cliente e por consulta.
Um dia só é 'coberto' se TODAS as consultas atuais do cliente leram o dia inteiro, sem truncamento, depois de ele terminar.
O que não está provado como lido aparece como lacuna e é refeito automaticamente."""
import json
import sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from . import clientes as _clientes
from .motor import consultas_do_cliente

ORDEM = ["lacuna", "incompleto", "em_curso", "provisorio", "coberto", "sem_historico"]  # do pior para o melhor


def _status_chave(con, cid: int, chave: str, hoje: date, dias: int) -> Dict[str, str]:
    rows = con.execute("SELECT janela_inicio ji,janela_fim jf,status,date(concluida_em) lido FROM consultas "
                       "WHERE cliente_id=? AND chave=? AND status IN ('ok','truncada')", (cid, chave)).fetchall()
    out: Dict[str, str] = {}
    inicio = min((r["ji"] for r in rows), default=None)
    for i in range(dias - 1, -1, -1):
        d = hoje - timedelta(days=i)
        di = d.isoformat()
        if inicio is None or di < inicio:
            out[di] = "sem_historico"
            continue
        cobre = [r for r in rows if r["ji"] <= di <= r["jf"]]
        oks = [r for r in cobre if r["status"] == "ok"]
        if not oks:
            out[di] = "incompleto" if cobre else ("em_curso" if d == hoje else "lacuna")
        elif d == hoje or not any(r["lido"] and r["lido"] > di for r in oks):
            out[di] = "provisorio"
        else:
            out[di] = "coberto"
    return out


def matriz(con: sqlite3.Connection, hoje: Optional[date] = None, dias: int = 30) -> Dict:
    hoje = hoje or date.today()
    clientes = []
    for c in _clientes.ativos(con):
        chaves = [q["chave"] for q in consultas_do_cliente(c)]
        por_chave = {k: _status_chave(con, c["id"], k, hoje, dias) for k in chaves}
        dias_out = []
        for i in range(dias - 1, -1, -1):
            di = (hoje - timedelta(days=i)).isoformat()
            sts = [por_chave[k][di] for k in chaves] or ["sem_historico"]
            com_hist = [x for x in sts if x != "sem_historico"] or ["sem_historico"]
            pior = min(com_hist, key=ORDEM.index)
            dias_out.append({"dia": di, "status": pior, "consultas": len(chaves)})
        cont = {k: sum(1 for x in dias_out if x["status"] == k) for k in ORDEM}
        fechados = cont["coberto"] + cont["lacuna"] + cont["incompleto"] + cont["provisorio"]
        clientes.append({"cliente_id": c["id"], "cliente": c["razao_social"], "dias": dias_out, "contagem": cont,
                         "consultas": chaves, "pct_coberto": round(100.0 * cont["coberto"] / fechados, 1) if fechados else None})
    total = {k: sum(c["contagem"][k] for c in clientes) for k in ORDEM}
    return {"gerado_em": datetime.now().isoformat(timespec="seconds"), "hoje": hoje.isoformat(), "dias": dias,
            "clientes": clientes, "total": total,
            "integra": total["lacuna"] == 0 and total["incompleto"] == 0}


def lacunas(con: sqlite3.Connection, hoje: Optional[date] = None, dias: int = 95) -> List[Dict]:
    """Intervalos contínuos de dias fechados (antes de hoje) não lidos, por cliente e consulta."""
    hoje = hoje or date.today()
    out = []
    for c in _clientes.ativos(con):
        for q in consultas_do_cliente(c):
            st = _status_chave(con, c["id"], q["chave"], hoje, dias)
            ini = fim = None
            for di in sorted(st):
                ruim = st[di] in ("lacuna", "incompleto") and di < hoje.isoformat()
                if ruim and st[di] == "incompleto":  # já tentado hoje e continua truncado: não insiste
                    ruim = not con.execute("SELECT 1 FROM consultas WHERE cliente_id=? AND chave=? AND status='truncada' "
                                           "AND janela_inicio<=? AND janela_fim>=? AND date(concluida_em)=?",
                                           (c["id"], q["chave"], di, di, hoje.isoformat())).fetchone()
                if ruim:
                    ini = ini or di
                    fim = di
                elif ini:
                    out.append({"cliente_id": c["id"], "chave": q["chave"], "ini": ini, "fim": fim})
                    ini = fim = None
            if ini:
                out.append({"cliente_id": c["id"], "chave": q["chave"], "ini": ini, "fim": fim})
    return out


def gravar_relatorio(con: sqlite3.Connection) -> Dict:
    """Guarda a prova do dia (o que foi lido e o que faltou) para auditoria."""
    m = matriz(con, dias=30)
    resumo = {"gerado_em": m["gerado_em"], "integra": m["integra"], "total": m["total"],
              "clientes": [{"cliente": c["cliente"], "contagem": c["contagem"], "pct_coberto": c["pct_coberto"], "consultas": c["consultas"]}
                           for c in m["clientes"]], "lacunas": lacunas(con),
              "execucoes_do_dia": [dict(r) for r in con.execute(
                  "SELECT id,gatilho,inicio,fim,status FROM execucoes WHERE date(inicio)=date('now','localtime') ORDER BY id")]}
    con.execute("INSERT INTO relatorios_cobertura(dia,gerado_em,conteudo) VALUES (date('now','localtime'),?,?) "
                "ON CONFLICT(dia) DO UPDATE SET gerado_em=excluded.gerado_em,conteudo=excluded.conteudo",
                (resumo["gerado_em"], json.dumps(resumo, ensure_ascii=False)))
    con.commit()
    return resumo


def _motivo_invisivel(coms) -> str:
    import re
    nomes = [d.get("nome") or "" for c in coms for d in c.get("destinatarios", [])]
    if nomes and sum(1 for n in nomes if n.strip().upper() == "SIGILO") >= len(nomes) / 2:
        return "partes sob sigilo: o DJEN omite os nomes"
    if nomes and sum(1 for n in nomes if re.fullmatch(r"(?:[A-Z]\.? ?)+", n.strip().upper())) >= len(nomes) / 2:
        return "nomes abreviados por iniciais"
    return "cliente aparece só como terceiro ou citado no texto"


def _incluir(con, motor, cli, r, achados, total, n):
    from .motor import agora_iso
    ex = con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('controle',?)", (agora_iso(),)).lastrowid
    con.commit()
    hoje = date.today().isoformat()
    motor._persistir(ex, cli, [{"q": {"chave": "numeroProcesso:" + r["numero_processo"], "param": "numeroProcesso", "valor": r["numero_processo"]},
                                "ini": hoje, "fim": hoje, "inicio": agora_iso(), "status": "ok", "total": total, "lidos": n, "erro": None,
                                "achados": achados}])
    con.execute("UPDATE execucoes SET fim=?,status='sucesso' WHERE id=?", (agora_iso(), ex))


# ---------- processos-controle: a única medida real de "estamos perdendo algo?" ----------
def adicionar_controle(con, cliente_id: int, numero: str, observacao: str = "", usuario: str = "") -> int:
    d = "".join(ch for ch in numero if ch.isdigit())
    if len(d) != 20:
        raise ValueError("número CNJ deve ter 20 dígitos (NNNNNNN-DD.AAAA.J.TR.OOOO)")
    return con.execute("INSERT INTO controle_processos(cliente_id,numero_processo,observacao,criado_por) VALUES (?,?,?,?)",
                       (cliente_id, d, observacao, usuario)).lastrowid


def verificar_controle(con: sqlite3.Connection, motor) -> Dict:
    """Para cada processo-controle: está na base? Se não, o DJEN o publicou? Se sim, é falha nossa e é reparada na hora."""
    from . import matcher
    from .motor import _normalizar_item, agora_iso
    res = {"capturado": 0, "recuperado": 0, "revisar": 0, "anterior_ao_historico": 0, "invisivel_no_djen": 0, "fora_da_fonte": 0, "erro": 0}
    for r in con.execute("SELECT * FROM controle_processos").fetchall():
        cli = _clientes.carregar(con, r["cliente_id"])
        # a pesquisa só olha para trás até o início do histórico buscado do cliente; o que o DJEN publicou antes disso não é "perda"
        inicio = con.execute("SELECT min(janela_inicio) FROM consultas WHERE cliente_id=? AND status IN ('ok','truncada') "
                             "AND chave NOT LIKE 'numeroProcesso:%'", (r["cliente_id"],)).fetchone()[0] or "9999-12-31"
        na_base = con.execute("SELECT 1 FROM correspondencias c JOIN processos p ON p.id=c.processo_id WHERE p.numero_processo=? "
                              "AND c.cliente_id=? AND c.classificacao IN ('confirmada','provavel') "
                              "AND c.evidencia NOT LIKE '%numeroProcesso:%'", (r["numero_processo"], r["cliente_id"])).fetchone()  # incluído por este próprio teste não conta como captura
        if na_base:
            res_i, det = "capturado", "já está na base como ação contra o cliente"
        else:
            try:
                itens, total, _ = motor.djen.consultar("numeroProcesso", r["numero_processo"], None, None)
            except Exception as e:
                res_i, det = "erro", str(e)[:200]
            else:
                coms = [_normalizar_item(i) for i in itens if len(i.get("numero_processo") or "") == 20]
                avals = [(c, matcher.avaliar(cli, c)) for c in coms]
                achados = [(c, m) for c, m in avals if m and m["classificacao"] in ("confirmada", "provavel")]
                recentes = [(c, m) for c, m in achados if (c.get("data_disponibilizacao") or "") >= inicio]
                duvida = [1 for c, m in avals if m and m["classificacao"] == "inconclusiva" and (c.get("data_disponibilizacao") or "") >= inicio]
                if not coms:
                    res_i, det = "fora_da_fonte", "o DJEN não tem comunicações deste processo (limite da fonte, não da pesquisa)"
                elif recentes:
                    _incluir(con, motor, cli, r, achados, total, len(coms))
                    res_i, det = "recuperado", "FALHA NOSSA: o DJEN nomeou o cliente numa comunicação DENTRO do período pesquisado e a pesquisa não pegou; incluído agora (investigar a causa)"
                elif achados:
                    _incluir(con, motor, cli, r, achados, total, len(coms))
                    res_i, det = "anterior_ao_historico", "o DJEN só nomeou o cliente antes do início do histórico pesquisado (%s): não é falha; incluído agora no histórico" % inicio
                elif duvida:
                    res_i, det = "revisar", "o DJEN tem partes parecidas com o cliente, mas sem certeza pelas regras: conferir nomes"
                else:
                    res_i, det = "invisivel_no_djen", "o DJEN tem comunicações, mas NENHUMA nomeia o cliente (%s): invisível para busca por nome/CNPJ" % _motivo_invisivel(coms)
        con.execute("UPDATE controle_processos SET resultado=?,detalhe=?,verificado_em=? WHERE id=?",
                    (res_i, det, datetime.now().isoformat(timespec="seconds"), r["id"]))
        con.commit()   # não segura a trava de escrita durante as chamadas de rede seguintes
        res[res_i] += 1
    con.commit()
    base = res["capturado"] + res["recuperado"]
    res["recall_pct"] = round(100.0 * res["capturado"] / base, 1) if base else None
    vis = res["capturado"] + res["recuperado"] + res["anterior_ao_historico"]
    tot = vis + res["revisar"] + res["invisivel_no_djen"]
    res["visibilidade_pct"] = round(100.0 * vis / tot, 1) if tot else None
    return res
