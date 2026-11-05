"""API HTTP (FastAPI). O motor e o worker funcionam sem ela; ela só lê o banco e enfileira jobs."""
import csv
import io
import json
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import adapters, analise, cadernos, clientes, cobertura, observabilidade, radar, config as _config, db, fila, matriz, seguranca, validacao

CAMINHO_DB = lambda: os.environ.get("SENTINELA_DB", "data/sentinela.db")
WEB = os.path.join(os.path.dirname(__file__), "web")
app = FastAPI(title="NGA Sentinela", version="0.3.0", docs_url="/api/docs", redoc_url=None)
_migrado: set = set()


def get_con():
    caminho = CAMINHO_DB()
    if caminho not in _migrado:
        db.conectar(caminho).close()
        _migrado.add(caminho)
    con = db.abrir(caminho)
    try:
        yield con
    finally:
        con.close()


def _chave() -> bytes:
    return seguranca.segredo(os.path.join(os.path.dirname(CAMINHO_DB()) or ".", ".segredo"))


def usuario_atual(authorization: str = Header(default=""), con: sqlite3.Connection = Depends(get_con)) -> sqlite3.Row:
    tok = authorization.removeprefix("Bearer ").strip()
    uid = seguranca.validar_token(_chave(), tok) if tok else None
    u = con.execute("SELECT * FROM usuarios WHERE id=? AND ativo=1", (uid,)).fetchone() if uid else None
    if u is None:
        raise HTTPException(401, "não autenticado")
    return u


def exigir(minimo: str):
    def dep(u: sqlite3.Row = Depends(usuario_atual)):
        if not seguranca.pode(u["papel"], minimo):
            raise HTTPException(403, "permissão insuficiente")
        return u
    return dep


def auditar(con, u, acao, entidade=None, entidade_id=None, detalhe=None):
    con.execute("INSERT INTO auditoria(usuario,acao,entidade,entidade_id,detalhe) VALUES (?,?,?,?,?)",
                (u["email"] if u else None, acao, entidade, str(entidade_id) if entidade_id is not None else None,
                 json.dumps(detalhe, ensure_ascii=False, default=str) if detalhe is not None else None))
    con.commit()


# ---------- autenticação ----------
class Login(BaseModel):
    email: str
    senha: str


_tentativas: Dict[str, List[float]] = {}


@app.post("/api/login")
def login(d: Login, con: sqlite3.Connection = Depends(get_con)):
    import time
    ag = time.time()
    t = [x for x in _tentativas.get(d.email.lower(), []) if ag - x < 300]
    if len(t) >= 8:
        raise HTTPException(429, "muitas tentativas; aguarde alguns minutos")
    u = con.execute("SELECT * FROM usuarios WHERE email=? AND ativo=1", (d.email.strip().lower(),)).fetchone()
    if not u or not seguranca.confere_senha(d.senha, u["senha_hash"]):
        _tentativas[d.email.lower()] = t + [ag]
        auditar(con, None, "login_falhou", "usuario", d.email)
        raise HTTPException(401, "credenciais inválidas")
    auditar(con, u, "login", "usuario", u["id"])
    return {"token": seguranca.emitir_token(_chave(), u["id"]), "usuario": {"id": u["id"], "nome": u["nome"], "email": u["email"], "papel": u["papel"]}}


@app.get("/api/eu")
def eu(u=Depends(usuario_atual)):
    return {"id": u["id"], "nome": u["nome"], "email": u["email"], "papel": u["papel"]}


# ---------- dashboard ----------
@app.get("/api/dashboard")
def dashboard(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    hoje = date.today()
    def contar(desde):
        """Só o que foi AJUIZADO no período (data de ajuizamento do DataJud), não o que o Sentinela viu pela primeira vez."""
        return con.execute("SELECT count(DISTINCT p.id) FROM processos p JOIN correspondencias c ON c.processo_id=p.id "
                           "WHERE c.classificacao!='descartada' AND p.data_ajuizamento>=? AND p.data_ajuizamento<=?",
                           (desde.isoformat(), hoje.isoformat())).fetchone()[0]
    ult = con.execute("SELECT * FROM execucoes WHERE gatilho!='reprocessamento' ORDER BY id DESC LIMIT 1").fetchone()
    falhas24 = con.execute("SELECT count(*) FROM consultas WHERE status='falha' AND desistida=0 AND concluida_em>=?",
                           ((datetime.now() - timedelta(hours=24)).isoformat(timespec="seconds"),)).fetchone()[0]
    pendentes_falha = con.execute("SELECT count(*) FROM (SELECT DISTINCT cliente_id,chave,janela_inicio,janela_fim FROM consultas f "
                                  "WHERE status='falha' AND desistida=0 AND NOT EXISTS (SELECT 1 FROM consultas o WHERE o.cliente_id=f.cliente_id AND o.chave=f.chave "
                                  "AND o.status IN ('ok','truncada') AND o.janela_inicio<=f.janela_inicio AND o.janela_fim>=f.janela_fim))").fetchone()[0]
    if ult is None:
        estado = {"codigo": "nunca_executou", "texto": "Nenhuma pesquisa executada ainda."}
    elif ult["status"] == "em_andamento":
        estado = {"codigo": "falha_parcial", "texto": "Execução em andamento ou interrompida sem conclusão: o resultado ainda não é confiável. O worker recupera execuções interrompidas ao reiniciar."}
    elif ult["status"] == "falha":
        estado = {"codigo": "falha_total", "texto": "NÃO foi possível pesquisar: todas as consultas da última execução falharam. Ausência de resultados aqui não significa ausência de ações."}
    elif ult["status"] in ("parcial", "interrompida"):
        estado = {"codigo": "falha_parcial", "texto": "Pesquisa incompleta: parte das consultas falhou e será reprocessada."}
    else:
        novos = con.execute("SELECT count(*) FROM correspondencias WHERE categoria_data='nova_distribuicao' AND classificacao!='descartada' "
                            "AND date(identificada_em)>=?", ((hoje - timedelta(days=1)).isoformat(),)).fetchone()[0]
        estado = {"codigo": "ok_com_novos" if novos else "ok_sem_novos",
                  "texto": ("Pesquisa concluída com sucesso: %d possível(is) nova(s) ação(ões) nas últimas 24 h." % novos) if novos
                  else "Pesquisa concluída com sucesso: nenhuma nova ação encontrada nas fontes consultadas (cobertura limitada às fontes validadas)."}
    cob = observabilidade.cobertura_monitoramento(con)
    if estado["codigo"] == "ok_sem_novos":
        estado["texto"] = observabilidade.frase_sem_novos(cob)
        if not cob["completa"]:
            estado["codigo"] = "falha_parcial"
    aj_base = ("FROM correspondencias c JOIN processos p ON p.id=c.processo_id JOIN clientes cl ON cl.id=c.cliente_id "
               "WHERE c.classificacao!='descartada' AND p.data_ajuizamento=?")
    aj_cols = "SELECT c.id,c.classificacao,c.estado_validacao,c.prioridade,p.numero_processo,p.tribunal,p.classe,p.orgao,p.data_ajuizamento,cl.razao_social cliente "
    aj_hoje = [dict(r) for r in con.execute(aj_cols + aj_base + " ORDER BY (c.classificacao='confirmada') DESC, p.id DESC LIMIT 30", (hoje.isoformat(),))]
    aj_ontem = con.execute("SELECT count(*) " + aj_base, ((hoje - timedelta(days=1)).isoformat(),)).fetchone()[0]
    base_aj = ("FROM processos p JOIN correspondencias c ON c.processo_id=p.id WHERE c.classificacao!='descartada' "
               "AND p.data_ajuizamento IS NOT NULL ")
    por_dia = {r[0]: r[1] for r in con.execute("SELECT p.data_ajuizamento d,count(DISTINCT p.id) " + base_aj +
                                               "AND p.data_ajuizamento>=? GROUP BY 1", ((hoje - timedelta(days=29)).isoformat(),))}
    serie_dias = [{"dia": (hoje - timedelta(days=i)).isoformat(), "n": por_dia.get((hoje - timedelta(days=i)).isoformat(), 0)}
                  for i in range(29, -1, -1)]
    mes0 = hoje.replace(day=1)
    meses = []
    for i in range(11, -1, -1):
        a = mes0.year * 12 + mes0.month - 1 - i
        meses.append("%04d-%02d" % (a // 12, a % 12 + 1))
    por_mes = {r[0]: r[1] for r in con.execute("SELECT substr(p.data_ajuizamento,1,7),count(DISTINCT p.id) " + base_aj +
                                               "AND p.data_ajuizamento>=? GROUP BY 1", (meses[0] + "-01",))}
    serie_meses = [{"mes": m, "n": por_mes.get(m, 0)} for m in meses]
    por_cliente = [dict(r) for r in con.execute(
        "SELECT cl.id,cl.razao_social,count(DISTINCT CASE WHEN p.data_ajuizamento IS NOT NULL THEN p.id END) total,"
        "count(DISTINCT CASE WHEN p.data_ajuizamento>=? THEN p.id END) ult30,count(DISTINCT CASE WHEN p.data_ajuizamento=? THEN p.id END) hoje,"
        "max(p.data_ajuizamento) ultima FROM clientes cl LEFT JOIN correspondencias c ON c.cliente_id=cl.id AND c.classificacao!='descartada' "
        "LEFT JOIN processos p ON p.id=c.processo_id WHERE cl.status_monitoramento='ativo' GROUP BY cl.id ORDER BY ult30 DESC,total DESC",
        ((hoje - timedelta(days=29)).isoformat(), hoje.isoformat()))]
    return {
        "cobertura": {k: cob[k] for k in ("completa", "esperadas", "operacionais", "resumo", "problemas", "ultima_verificacao", "nao_monitoradas")},
        "frase_sem_novos": observabilidade.frase_sem_novos(cob),
        "clientes_so_cnpj": [dict(r) for r in con.execute(
            "SELECT cl.id,cl.razao_social FROM clientes cl WHERE cl.status_monitoramento='ativo' AND NOT EXISTS ("
            "SELECT 1 FROM cliente_nomes n WHERE n.cliente_id=cl.id AND length(replace(n.nome_norm,' ',''))>=4)")],
        "serie_dias": serie_dias, "serie_meses": serie_meses, "por_cliente": por_cliente,
        "ajuizadas_total": con.execute("SELECT count(DISTINCT p.id) " + base_aj).fetchone()[0],
        "sem_data_ajuizamento": con.execute("SELECT count(DISTINCT p.id) FROM processos p JOIN correspondencias c ON c.processo_id=p.id "
                                            "WHERE c.classificacao!='descartada' AND p.data_ajuizamento IS NULL").fetchone()[0],
        "ajuizados_hoje": aj_hoje, "ajuizados_ontem": aj_ontem,
        "estado_pesquisa": estado, "ultima_execucao": dict(ult) if ult else None,
        "identificados_hoje": contar(hoje), "identificados_semana": contar(hoje - timedelta(days=7)), "identificados_mes": contar(hoje - timedelta(days=30)),
        "clientes_monitorados": con.execute("SELECT count(*) FROM clientes WHERE status_monitoramento='ativo'").fetchone()[0],
        "novas_acoes_confirmadas": con.execute("SELECT count(*) FROM correspondencias WHERE classificacao='confirmada' AND categoria_data='nova_distribuicao'").fetchone()[0],
        "pendentes_validacao": con.execute("SELECT count(*) FROM correspondencias WHERE estado_validacao='pendente'").fetchone()[0],
        "falhas_consultas_24h": falhas24, "consultas_a_reprocessar": pendentes_falha,
        "tribunais_com_dados": con.execute("SELECT count(DISTINCT tribunal) FROM processos").fetchone()[0],
        "tribunais_com_conector_validado": 0,
        "nota_tribunais": "Fontes nacionais (DJEN/DataJud) cobrem vários tribunais, mas nenhum tribunal está validado individualmente com CNPJ autorizado.",
        "alertas_nao_lidos": con.execute("SELECT count(*) FROM alertas WHERE canal='interno' AND lido=0 AND status='enviado'").fetchone()[0]}


def _busca_inicial(con, ids: List[int]) -> int:
    """Cliente novo (ou com CNPJ/nome novo): pesquisa retroativa imediata, sem esperar o horário diário."""
    dias = _config.obter(con, "motor")["dias_busca_inicial"]
    return fila.enfileirar(con, "executar", {"cliente_ids": ids, "desde": (date.today() - timedelta(days=dias)).isoformat()}, "busca_inicial")


# ---------- clientes ----------
class ClienteIn(BaseModel):
    razao_social: str
    cnpj_matriz: str
    nome_fantasia: str = ""
    filiais: List[str] = []
    anteriores: List[str] = []
    alternativos: List[str] = []
    grupo_economico: Optional[str] = None
    advogado_responsavel: Optional[str] = None
    equipe: Optional[str] = None
    status_monitoramento: str = "ativo"
    tribunais_prioritarios: List[str] = []
    frequencia_horas: int = 24
    confirmar_por_nome_exato: bool = False


@app.get("/api/clientes")
def listar_clientes(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    rows = con.execute("SELECT c.*,(SELECT count(*) FROM correspondencias x WHERE x.cliente_id=c.id AND x.classificacao!='descartada') processos,"
                       "(SELECT count(*) FROM cliente_cnpjs k WHERE k.cliente_id=c.id) qtd_cnpjs FROM clientes c ORDER BY razao_social").fetchall()
    return [dict(r) for r in rows]


@app.get("/api/clientes/{cid}")
def ver_cliente(cid: int, u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    if not con.execute("SELECT 1 FROM clientes WHERE id=?", (cid,)).fetchone():
        raise HTTPException(404)
    return clientes.carregar(con, cid)


@app.post("/api/clientes", status_code=201)
def criar_cliente(d: ClienteIn, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    try:
        cid = clientes.cadastrar(con, d.razao_social, d.cnpj_matriz, d.nome_fantasia, d.filiais, d.anteriores, d.alternativos,
                                 grupo_economico=d.grupo_economico, advogado_responsavel=d.advogado_responsavel, equipe=d.equipe,
                                 status_monitoramento=d.status_monitoramento, tribunais_prioritarios=d.tribunais_prioritarios,
                                 frequencia_horas=d.frequencia_horas)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "CNPJ já cadastrado")
    if d.confirmar_por_nome_exato:
        con.execute("UPDATE clientes SET confirmar_por_nome_exato=1 WHERE id=?", (cid,)); con.commit()
    auditar(con, u, "cliente_criado", "cliente", cid)
    jid = _busca_inicial(con, [cid])
    return {"id": cid, "job_busca_inicial": jid}


class ClienteEdit(BaseModel):
    razao_social: Optional[str] = None
    nome_fantasia: Optional[str] = None
    grupo_economico: Optional[str] = None
    advogado_responsavel: Optional[str] = None
    equipe: Optional[str] = None
    status_monitoramento: Optional[str] = None
    tribunais_prioritarios: Optional[List[str]] = None
    frequencia_horas: Optional[int] = None
    confirmar_por_nome_exato: Optional[bool] = None
    adicionar_filiais: List[str] = []
    adicionar_anteriores: List[str] = []
    adicionar_alternativos: List[str] = []


@app.put("/api/clientes/{cid}")
def editar_cliente(cid: int, d: ClienteEdit, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    if not con.execute("SELECT 1 FROM clientes WHERE id=?", (cid,)).fetchone():
        raise HTTPException(404)
    if d.status_monitoramento and d.status_monitoramento not in ("ativo", "pausado"):
        raise HTTPException(422, "status inválido")
    campos = {k: v for k, v in d.dict(exclude={"adicionar_filiais", "adicionar_anteriores", "adicionar_alternativos"}).items() if v is not None}
    if "tribunais_prioritarios" in campos:
        campos["tribunais_prioritarios"] = json.dumps(campos["tribunais_prioritarios"])
    if "confirmar_por_nome_exato" in campos:
        campos["confirmar_por_nome_exato"] = int(campos["confirmar_por_nome_exato"])
    for k, v in campos.items():
        con.execute("UPDATE clientes SET %s=? WHERE id=?" % k, (v, cid))  # k vem do modelo, não do usuário
    try:
        from . import cnpj as _c
        for f in d.adicionar_filiais:
            con.execute("INSERT OR IGNORE INTO cliente_cnpjs VALUES (?,?,'filial')", (cid, _c.normalizar(f)))
    except ValueError as e:
        raise HTTPException(422, str(e))
    for lista, tipo in ((d.adicionar_anteriores, "anterior"), (d.adicionar_alternativos, "alternativo")):
        for n in lista:
            clientes._add_nome(con, cid, n, tipo)
    con.commit()
    auditar(con, u, "cliente_editado", "cliente", cid, d.dict(exclude_none=True))
    novo = bool(d.adicionar_filiais or d.adicionar_anteriores or d.adicionar_alternativos)
    return {"ok": True, "job_busca_inicial": _busca_inicial(con, [cid]) if novo else None}


@app.post("/api/clientes/importar")
async def importar(arquivo: UploadFile = File(...), u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    dados = await arquivo.read()
    if len(dados) > 5_000_000:
        raise HTTPException(413, "arquivo grande demais")
    nome = (arquivo.filename or "").lower()
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w", encoding="utf-8", newline="") as t:
        if nome.endswith(".xlsx"):
            import openpyxl
            ws = openpyxl.load_workbook(io.BytesIO(dados), read_only=True, data_only=True).active
            w = csv.writer(t)
            for row in ws.iter_rows(values_only=True):
                w.writerow(["" if c is None else str(c) for c in row])
        else:
            t.write(dados.decode("utf-8-sig", errors="replace"))
    try:
        r = clientes.importar_csv(con, t.name)
    finally:
        os.unlink(t.name)
    auditar(con, u, "clientes_importados", "cliente", None, {"importados": r["importados"], "erros": len(r["erros"])})
    if r["ids"]:
        r["job_busca_inicial"] = _busca_inicial(con, r["ids"])
    return r


# ---------- processos ----------
@app.get("/api/processos")
def listar_processos(cliente_id: Optional[int] = None, tribunal: Optional[str] = None, de: Optional[str] = None, ate: Optional[str] = None,
                     classe: Optional[str] = None, prioridade: Optional[str] = None, responsavel_id: Optional[int] = None,
                     classificacao: Optional[str] = None, categoria: Optional[str] = None, estado: Optional[str] = None,
                     valor_min: Optional[float] = None, valor_max: Optional[float] = None, ajuizado_hoje: Optional[int] = None, limite: int = 100, pagina: int = 1,
                     u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    where, args = ["1=1"], []
    def f(cond, v):
        if v is not None and v != "":
            where.append(cond); args.append(v)
    f("c.cliente_id=?", cliente_id); f("p.tribunal=?", tribunal); f("date(c.identificada_em)>=?", de); f("date(c.identificada_em)<=?", ate)
    f("p.classe LIKE ?", "%" + classe + "%" if classe else None); f("c.prioridade=?", prioridade); f("c.responsavel_id=?", responsavel_id)
    f("c.classificacao=?", classificacao); f("c.categoria_data=?", categoria); f("c.estado_validacao=?", estado)
    val = "(SELECT json_extract(a.campos,'$.valor_causa.valor') FROM analises a WHERE a.processo_id=p.id ORDER BY a.id DESC LIMIT 1)"
    if valor_min is not None:
        where.append(val + ">=?"); args.append(valor_min)
    if valor_max is not None:
        where.append(val + "<=?"); args.append(valor_max)
    if ajuizado_hoje:
        where.append("p.data_ajuizamento=date('now','localtime')")
    limite = max(1, min(limite, 500))
    base = ("FROM correspondencias c JOIN processos p ON p.id=c.processo_id JOIN clientes cl ON cl.id=c.cliente_id "
            "LEFT JOIN usuarios r ON r.id=c.responsavel_id WHERE " + " AND ".join(where))
    total = con.execute("SELECT count(*) " + base, args).fetchone()[0]
    rows = con.execute("SELECT c.id,c.classificacao,c.nivel,c.polo,c.categoria_data,c.estado_validacao,c.prioridade,c.identificada_em,"
                       "c.ultima_verificacao,p.numero_processo,p.tribunal,p.classe,p.orgao,p.data_ajuizamento,p.datajud_status,cl.razao_social cliente,"
                       "cl.id cliente_id,r.nome responsavel,CAST(julianday(date('now','localtime'))-julianday(p.data_ajuizamento) AS INTEGER) dias_ajuiz," + val + " valor_causa " + base +
                       " ORDER BY (p.data_ajuizamento=date('now','localtime')) DESC, (c.classificacao='confirmada') DESC, c.identificada_em DESC LIMIT ? OFFSET ?",
                       args + [limite, (max(pagina, 1) - 1) * limite]).fetchall()
    return {"total": total, "itens": [dict(r) for r in rows]}


@app.get("/api/processos/{pid}")
def detalhe(pid: int, u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    c = con.execute("SELECT c.*,p.numero_processo,p.tribunal,p.classe,p.orgao,p.grau,p.sistema,p.assuntos,p.data_ajuizamento,p.datajud_status,"
                    "p.primeira_identificacao,p.ultima_verificacao ult_proc,cl.razao_social cliente,cl.cnpj_matriz,r.nome responsavel "
                    "FROM correspondencias c JOIN processos p ON p.id=c.processo_id JOIN clientes cl ON cl.id=c.cliente_id "
                    "LEFT JOIN usuarios r ON r.id=c.responsavel_id WHERE c.id=?", (pid,)).fetchone()
    if not c:
        raise HTTPException(404)
    d = dict(c)
    d["evidencia"] = json.loads(d["evidencia"])
    d["assuntos"] = json.loads(d["assuntos"]) if d["assuntos"] else []
    coms = con.execute("SELECT id,tipo,data_disponibilizacao,tribunal,orgao,classe,link,texto,destinatarios FROM comunicacoes "
                       "WHERE numero_processo=? ORDER BY data_disponibilizacao DESC", (d["numero_processo"],)).fetchall()
    d["comunicacoes"] = [{**dict(x), "destinatarios": json.loads(x["destinatarios"])} for x in coms]
    a = con.execute("SELECT * FROM analises WHERE processo_id=? ORDER BY id DESC LIMIT 1", (d["processo_id"],)).fetchone()
    d["analise"] = ({**dict(a), "campos": json.loads(a["campos"]), "documentos": json.loads(a["documentos"])} if a else None)
    d["validacoes"] = [dict(x) for x in con.execute("SELECT usuario,decisao,classificacao_anterior,justificativa,decidida_em FROM validacoes WHERE correspondencia_id=? ORDER BY id", (pid,))]
    d["alertas"] = [dict(x) for x in con.execute("SELECT tipo,canal,status,erro,criado_em FROM alertas WHERE correspondencia_id=? ORDER BY id", (pid,))]
    d["fontes"] = [dict(x) for x in con.execute("SELECT fonte,identificador,primeiro_visto,ultimo_visto,vezes FROM processo_fontes WHERE processo_id=? ORDER BY primeiro_visto", (d["processo_id"],))]
    d["eventos"] = [dict(x) for x in con.execute("SELECT tipo,fonte,detalhe,criado_em FROM processo_eventos WHERE processo_id=? ORDER BY id", (d["processo_id"],))]
    d["partes"] = [dict(x) for x in con.execute("SELECT nome,lado,fonte FROM processo_partes WHERE processo_id=? ORDER BY lado,nome", (d["processo_id"],))]
    d["campos_divergentes"] = radar.conflitos(con, d["processo_id"])
    d["limiares"] = {k: _config.obter(con, "radar")[k] for k in ("confianca_auto", "confianca_revisao")}
    return d


class Atrib(BaseModel):
    usuario_id: Optional[int] = None


@app.post("/api/processos/{pid}/atribuir")
def atribuir(pid: int, d: Atrib, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    if d.usuario_id and not con.execute("SELECT 1 FROM usuarios WHERE id=? AND ativo=1", (d.usuario_id,)).fetchone():
        raise HTTPException(422, "usuário inexistente")
    n = con.execute("UPDATE correspondencias SET responsavel_id=? WHERE id=?", (d.usuario_id, pid)).rowcount
    if not n:
        raise HTTPException(404)
    con.commit(); auditar(con, u, "processo_atribuido", "correspondencia", pid, {"usuario_id": d.usuario_id})
    return {"ok": True}


@app.post("/api/processos/{pid}/analisar")
def pedir_analise(pid: int, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    if not con.execute("SELECT 1 FROM correspondencias WHERE id=?", (pid,)).fetchone():
        raise HTTPException(404)
    jid = fila.enfileirar(con, "analisar", {"correspondencia_id": pid}, "manual")
    auditar(con, u, "analise_solicitada", "correspondencia", pid)
    return {"job_id": jid}


# ---------- validação ----------
@app.get("/api/validacao/pendentes")
def pendentes(cliente_id: Optional[int] = None, limite: int = 100, u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    itens = validacao.pendentes(con, cliente_id, max(1, min(limite, 500)))
    for i in itens:
        i["evidencia"] = json.loads(i["evidencia"])
    total = con.execute("SELECT count(*) FROM correspondencias WHERE estado_validacao='pendente'").fetchone()[0]
    return {"total": total, "itens": itens}


class Decisao(BaseModel):
    decisao: str
    justificativa: str = ""


class DecisaoLote(Decisao):
    ids: List[int]


@app.post("/api/validacao/lote")
def decidir_lote(d: DecisaoLote, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    if d.decisao not in ("confirmada", "descartada") or not d.justificativa.strip():
        raise HTTPException(422, "decisão e justificativa são obrigatórias em lote")
    n = validacao.decidir_em_lote(con, d.ids, u["email"], d.decisao, d.justificativa)
    auditar(con, u, "validacao_lote", "correspondencia", None, {"ids": len(d.ids), "decisao": d.decisao})
    return {"decididas": n}


@app.post("/api/validacao/{cid}")
def decidir(cid: int, d: Decisao, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    try:
        validacao.decidir(con, cid, u["email"], d.decisao, d.justificativa)
    except KeyError:
        raise HTTPException(404)
    except ValueError as e:
        raise HTTPException(422, str(e))
    auditar(con, u, "validacao", "correspondencia", cid, {"decisao": d.decisao})
    return {"ok": True}


# ---------- fontes, execuções, jobs, alertas ----------
@app.get("/api/fontes")
def fontes(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    saude = [dict(r) for r in con.execute("SELECT * FROM saude_conectores")]
    ult = {r["fonte"]: dict(r) for r in con.execute(
        "SELECT fonte,max(concluida_em) ultima_consulta,sum(status='ok') ok,sum(status='falha') falhas,sum(status='truncada') truncadas "
        "FROM consultas WHERE concluida_em>=datetime('now','-7 day') GROUP BY fonte")}
    return {"saude": saude, "consultas_7d": ult, "matriz": matriz.FONTES, "matriz_atualizada_em": matriz.ATUALIZADA_EM}


@app.get("/api/execucoes")
def execucoes(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    return [dict(r) for r in con.execute("SELECT * FROM execucoes ORDER BY id DESC LIMIT 50")]


class JobIn(BaseModel):
    tipo: str
    params: Dict[str, Any] = {}


@app.post("/api/jobs", status_code=201)
def novo_job(d: JobIn, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    if d.tipo not in ("executar", "reprocessar_falhas", "alertas", "preencher_lacunas", "verificar_controle", "varrer_cadernos", "saude_fontes", "enriquecer"):
        raise HTTPException(422, "tipo inválido")
    params = dict(d.params)
    if d.tipo == "executar":
        params.setdefault("forcar", True)   # clique manual = pesquisar de verdade agora, não "já fiz hoje"
    jid = fila.enfileirar(con, d.tipo, params, "manual")
    auditar(con, u, "job_enfileirado", "job", jid, d.dict())
    return {"job_id": jid}


@app.get("/api/progresso")
def progresso(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    """Estado da pesquisa em curso (ou da última, por alguns minutos) para a barra de progresso do painel."""
    tipos = ("executar", "reprocessar_falhas")
    j = con.execute("SELECT * FROM jobs WHERE tipo IN (?,?) AND status IN ('executando','na_fila') "
                    "ORDER BY (status='executando') DESC, id LIMIT 1", tipos).fetchone()
    if j and j["status"] == "na_fila":
        n = con.execute("SELECT count(*) FROM jobs WHERE status='na_fila' AND id<=?", (j["id"],)).fetchone()[0]
        return {"estado": "na_fila", "pct": 0, "fase": "fila", "job_id": j["id"],
                "texto": "Na fila, aguardando o worker (%d à frente ou em execução)" % n}
    if j:
        e = con.execute("SELECT * FROM execucoes WHERE status='em_andamento' AND inicio>=? ORDER BY id DESC LIMIT 1",
                        (j["iniciado_em"],)).fetchone()
        pr = json.loads(e["progresso"]) if e and e["progresso"] else {"pct": 1, "fase": "inicio", "texto": "Preparando a pesquisa…"}
        return {"estado": "executando", "job_id": j["id"], "execucao_id": e["id"] if e else None, "inicio": j["iniciado_em"], **pr}
    f = con.execute("SELECT * FROM jobs WHERE tipo IN (?,?) AND status IN ('concluido','falhou') ORDER BY id DESC LIMIT 1", tipos).fetchone()
    if f and f["concluido_em"] and (datetime.now() - datetime.fromisoformat(f["concluido_em"])).total_seconds() < 600:
        r = {}
        try:
            r = json.loads(f["resultado"] or "{}").get("execucao", {})
        except ValueError:
            pass
        ok = f["status"] == "concluido"
        return {"estado": "concluido" if ok else "falhou", "pct": 100 if ok else 0, "fase": "fim", "job_id": f["id"],
                "texto": "Pesquisa concluída" if ok else "A pesquisa falhou: " + (f["resultado"] or "")[:200],
                "concluido_em": f["concluido_em"], "resumo": r}
    return {"estado": "ocioso", "pct": 0}


# ---------- garantia de cobertura ----------
@app.get("/api/cobertura")
def cobertura_api(dias: int = 30, u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    m = cobertura.matriz(con, dias=max(7, min(dias, 120)))
    m["lacunas"] = cobertura.lacunas(con)
    ult = con.execute("SELECT * FROM execucoes WHERE gatilho IN ('agendado','manual','preenchimento') AND status IN ('sucesso','parcial') "
                      "ORDER BY id DESC LIMIT 1").fetchone()
    m["ultima_pesquisa_valida"] = dict(ult) if ult else None
    m["controle"] = [dict(r) for r in con.execute("SELECT c.*,cl.razao_social cliente FROM controle_processos c JOIN clientes cl ON cl.id=c.cliente_id ORDER BY c.id DESC")]
    m["rescan_dias"] = 7
    m["cadernos"] = cadernos.resumo(con, 7)
    return m


class ControleIn(BaseModel):
    cliente_id: int
    numero_processo: str
    observacao: str = ""


@app.post("/api/controle", status_code=201)
def controle_novo(d: ControleIn, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    try:
        cid = cobertura.adicionar_controle(con, d.cliente_id, d.numero_processo, d.observacao, u["email"])
    except ValueError as e:
        raise HTTPException(422, str(e))
    except sqlite3.IntegrityError:
        raise HTTPException(409, "processo-controle já cadastrado para este cliente")
    con.commit()
    auditar(con, u, "controle_criado", "controle", cid, d.dict())
    return {"id": cid}


@app.delete("/api/controle/{cid}")
def controle_apagar(cid: int, u=Depends(exigir("advogado")), con: sqlite3.Connection = Depends(get_con)):
    con.execute("DELETE FROM controle_processos WHERE id=?", (cid,)); con.commit()
    auditar(con, u, "controle_removido", "controle", cid, {})
    return {"ok": True}


@app.get("/api/cobertura/relatorios")
def cobertura_relatorios(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    return [{"dia": r["dia"], "gerado_em": r["gerado_em"], **{k: v for k, v in json.loads(r["conteudo"]).items() if k in ("integra", "total", "lacunas")}}
            for r in con.execute("SELECT * FROM relatorios_cobertura ORDER BY dia DESC LIMIT 60")]


@app.get("/api/radar/cobertura")
def radar_cobertura(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    return observabilidade.cobertura_monitoramento(con)


@app.get("/api/radar/metricas")
def radar_metricas(dias: int = 1, u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    return observabilidade.metricas(con, max(1, min(dias, 30)))


@app.get("/api/radar/chamadas")
def radar_chamadas(fonte: Optional[str] = None, limite: int = 100, u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    """Trilha de auditoria: o que foi consultado, quando, com qual resposta."""
    q, a = "SELECT * FROM fonte_chamadas", []
    if fonte:
        q += " WHERE fonte=?"; a.append(fonte)
    return [dict(r) for r in con.execute(q + " ORDER BY id DESC LIMIT ?", a + [max(1, min(limite, 500))])]


@app.get("/api/jobs")
def jobs(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    return [dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 50")]


@app.get("/api/alertas")
def alertas(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    rows = con.execute("SELECT * FROM alertas ORDER BY id DESC LIMIT 100").fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


@app.post("/api/alertas/{aid}/lido")
def lido(aid: int, u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    con.execute("UPDATE alertas SET lido=1 WHERE id=?", (aid,)); con.commit()
    return {"ok": True}


# ---------- configurações, usuários, auditoria ----------
@app.get("/api/config")
def ver_config(u=Depends(exigir("admin")), con: sqlite3.Connection = Depends(get_con)):
    segredos = {"smtp_usuario_definido": bool(os.environ.get("SENTINELA_SMTP_USUARIO")), "smtp_senha_definida": bool(os.environ.get("SENTINELA_SMTP_SENHA")),
                "slack_webhook_definido": bool(os.environ.get("SENTINELA_SLACK_WEBHOOK")), "ia_definida": bool(os.environ.get("ANTHROPIC_API_KEY"))}
    return {"config": _config.todas(con), "segredos_no_ambiente": segredos}


@app.put("/api/config/{chave}")
def salvar_config(chave: str, valor: Dict[str, Any], u=Depends(exigir("admin")), con: sqlite3.Connection = Depends(get_con)):
    try:
        _config.salvar(con, chave, valor)
    except KeyError:
        raise HTTPException(404)
    auditar(con, u, "config_alterada", "config", chave, valor)
    return {"ok": True}


class UsuarioIn(BaseModel):
    email: str
    nome: str
    senha: str
    papel: str = "advogado"


@app.get("/api/usuarios")
def usuarios(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    return [dict(r) for r in con.execute("SELECT id,email,nome,papel,ativo FROM usuarios ORDER BY nome")]


@app.post("/api/usuarios", status_code=201)
def criar_usuario(d: UsuarioIn, u=Depends(exigir("admin")), con: sqlite3.Connection = Depends(get_con)):
    if d.papel not in seguranca.PAPEIS or len(d.senha) < 10:
        raise HTTPException(422, "papel inválido ou senha com menos de 10 caracteres")
    try:
        uid = con.execute("INSERT INTO usuarios(email,nome,senha_hash,papel) VALUES (?,?,?,?)",
                          (d.email.strip().lower(), d.nome, seguranca.hash_senha(d.senha), d.papel)).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "e-mail já cadastrado")
    con.commit(); auditar(con, u, "usuario_criado", "usuario", uid, {"papel": d.papel})
    return {"id": uid}


class UsuarioEdit(BaseModel):
    papel: Optional[str] = None
    ativo: Optional[bool] = None


@app.patch("/api/usuarios/{uid}")
def editar_usuario(uid: int, d: UsuarioEdit, u=Depends(exigir("admin")), con: sqlite3.Connection = Depends(get_con)):
    if uid == u["id"]:
        raise HTTPException(422, "não altere o próprio acesso")
    if d.papel:
        if d.papel not in seguranca.PAPEIS:
            raise HTTPException(422, "papel inválido")
        con.execute("UPDATE usuarios SET papel=? WHERE id=?", (d.papel, uid))
    if d.ativo is not None:
        con.execute("UPDATE usuarios SET ativo=? WHERE id=?", (int(d.ativo), uid))
    con.commit(); auditar(con, u, "usuario_alterado", "usuario", uid, d.dict(exclude_none=True))
    return {"ok": True}


@app.get("/api/auditoria")
def auditoria(u=Depends(exigir("admin")), con: sqlite3.Connection = Depends(get_con)):
    return [dict(r) for r in con.execute("SELECT * FROM auditoria ORDER BY id DESC LIMIT 200")]


@app.get("/api/sistema")
def sistema(u=Depends(usuario_atual), con: sqlite3.Connection = Depends(get_con)):
    """Prova de funcionamento contínuo: batimento do worker, próxima execução e histórico diário dos últimos 14 dias."""
    w = con.execute("SELECT * FROM worker_status WHERE id=1").fetchone()
    seg = None
    if w and w["ultimo_batimento"]:
        seg = int((datetime.now() - datetime.fromisoformat(w["ultimo_batimento"])).total_seconds())
    ativo = seg is not None and seg < 60
    dias = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        rows = con.execute("SELECT status,count(*) n FROM execucoes WHERE date(inicio)=? AND gatilho IN ('agendado','manual','teste_real') GROUP BY status", (d,)).fetchall()
        st = {r["status"]: r["n"] for r in rows}
        res = "sem_execucao" if not st else ("falha" if st.get("falha") and not st.get("sucesso") else ("parcial" if st.get("parcial") or st.get("falha") or st.get("interrompida") else "sucesso"))
        dias.append({"dia": d, "resultado": res, "execucoes": sum(st.values())})
    cfg = _config.obter(con, "agendamento")
    return {"worker": {"ativo": ativo, "segundos_desde_batimento": seg, "ultimo_batimento": w["ultimo_batimento"] if w else None,
                       "iniciado_em": w["iniciado_em"] if w else None, "pid": w["pid"] if w else None},
            "agendamento": {"ativo": cfg["ativo"], "hora": cfg["hora"], "proxima_execucao": fila.proxima_execucao(con)},
            "jobs_na_fila": con.execute("SELECT count(*) FROM jobs WHERE status='na_fila'").fetchone()[0],
            "dias": dias,
            "aviso": None if ativo else "O worker não está enviando batimento: nenhuma pesquisa automática ocorrerá até ele voltar. Se o Mac dormiu ou foi reiniciado, o worker precisa ser iniciado de novo."}


@app.get("/api/saude")
def saude():
    return {"status": "ok", "versao": app.version}


# ---------- frontend estático ----------
class _SemCache(StaticFiles):
    async def get_response(self, path, scope):
        r = await super().get_response(path, scope)
        r.headers["Cache-Control"] = "no-cache"
        return r


app.mount("/static", _SemCache(directory=os.path.join(WEB, "static")), name="static")


@app.get("/")
def raiz():
    return FileResponse(os.path.join(WEB, "index.html"), headers={"Cache-Control": "no-cache"})
