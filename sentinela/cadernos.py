"""Varredura dos CADERNOS do DJEN: o CNJ publica, por tribunal e por dia, o conjunto COMPLETO das comunicações (ZIP de JSON).
Em vez de depender da busca da API (limite de 10.000, nomes ocultos, regras de busca), o Sentinela baixa o caderno inteiro e procura
nele os clientes com o próprio matcher. Cada caderno fica registrado com o total declarado pelo CNJ x o total efetivamente lido:
esse é o livro de prova de que nada do DJEN daquele dia ficou de fora."""
import json
import os
import re
import tempfile
import unicodedata
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from . import cnpj as _cnpj
from . import matcher
from . import nomes as _nomes
from .http import ErroFonte
from .motor import _normalizar_item, agora_iso, nome_generico

URL_CADERNO = "https://comunicaapi.pje.jus.br/api/v1/caderno/%s/%s/%s"
URL_TRIBUNAIS = "https://comunicaapi.pje.jus.br/api/v1/comunicacao/tribunal"
MEIOS = ("D", "E")


def baixar_zip(url: str, destino: str, timeout: int = 300) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "NGA-Sentinela/0.3"})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(destino, "wb") as f:
        while True:
            pedaco = r.read(1 << 20)
            if not pedaco:
                break
            f.write(pedaco)


def siglas_tribunais(http) -> List[str]:
    lista = http.requisitar("GET", URL_TRIBUNAIS)
    return sorted({i["sigla"] for blk in lista for i in blk.get("instituicoes", []) if i.get("sigla")})


def _fold(t: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().upper())


class Filtro:
    """Pré-filtro barato: só itens que citam CNPJ, nome (corpo) ou iniciais de algum cliente passam ao matcher completo."""
    def __init__(self, clientes: List[Dict]):
        self.clientes = clientes
        self.cnpjs, self.nomes, self.iniciais = [], [], set()
        for c in clientes:
            for x in c["cnpjs"]:
                self.cnpjs += [_cnpj.formatar(x["cnpj"]), x["cnpj"]]
            for n in c["nomes"]:
                if not nome_generico(n["nome_norm"]):
                    self.nomes.append(" %s " % n["nome_norm"])
                ini = _nomes.iniciais(n["nome"])
                if ini:
                    self.iniciais.add(_nomes.normalizar_iniciais(ini))

    def passa(self, it: Dict) -> bool:
        txt = it.get("texto") or ""
        if any(c in txt for c in self.cnpjs):
            return True
        for d in it.get("destinatarios") or []:
            nm = d.get("nome") or ""
            if len(nm) <= 30 and self.iniciais and _nomes.e_abreviacao(nm) and _nomes.normalizar_iniciais(nm) in self.iniciais:
                return True
        f = _fold(txt)
        return any(n in f for n in self.nomes)


def escanear_zip(caminho: str, filtro: Filtro):
    """Devolve (total_lido, achados[(cliente_id, comunicacao, avaliacao)])."""
    total, achados = 0, []
    with zipfile.ZipFile(caminho) as z:
        for info in sorted(z.infolist(), key=lambda i: i.filename):
            d = json.loads(z.read(info))
            for it in d.get("items", []):
                total += 1
                if len(it.get("numero_processo") or "") != 20 or not filtro.passa(it):
                    continue
                com = _normalizar_item(it)
                for c in filtro.clientes:
                    m = matcher.avaliar(c, com)
                    if m:
                        achados.append((c["id"], com, m))
    return total, achados


def _processar(http, sigla: str, meio: str, dia: str, filtro: Filtro, baixar: Callable, pasta: str, anterior=None) -> Dict:
    try:
        meta = http.requisitar("GET", URL_CADERNO % (sigla, dia, meio))
    except ErroFonte as e:
        if e.status == 404:   # o CNJ não tem caderno nesse dia para esse tribunal (fim de semana/feriado/sem envio): fechado, não é erro
            return {"sigla": sigla, "meio": meio, "data": dia, "status": "Sem caderno", "versao": None, "hash": None, "total_declarado": 0,
                    "total_lido": 0, "achados": [], "erro": None}
        raise
    out = {"sigla": sigla, "meio": meio, "data": dia, "status": meta.get("status"), "versao": meta.get("versao"),
           "hash": meta.get("hash"), "total_declarado": meta.get("total_comunicacoes"), "total_lido": 0, "achados": [], "erro": None}
    if meta.get("status") != "Processado":
        return out   # 'Sem comunicações' é fechado; os demais ('Em processamento', 'Não Processado') ficam pendentes
    if anterior and anterior["status"] == "Processado" and anterior["total_lido"] == anterior["total_declarado"] \
            and anterior["versao"] == meta.get("versao") and anterior["hash"] == meta.get("hash"):
        out.update(total_lido=anterior["total_lido"], reuso=True)   # mesma versão já lida por inteiro: não baixa de novo
        return out
    caminho = os.path.join(pasta, "%s-%s-%s.zip" % (sigla, meio, dia))
    try:
        baixar(meta["url"], caminho)
        out["total_lido"], out["achados"] = escanear_zip(caminho, filtro)
    finally:
        if os.path.exists(caminho):
            os.remove(caminho)
    return out


def _fechado(r: Dict) -> bool:
    if r["status"] in ("Sem comunicações", "Sem caderno"):
        return True
    return r["status"] == "Processado" and r["erro"] is None and r["total_lido"] == r["total_declarado"]


def varrer(con, motor, dias: int = 3, hoje: Optional[date] = None, baixar: Callable = baixar_zip, trabalhadores: int = 3,
           siglas: Optional[List[str]] = None) -> Dict:
    """Baixa e escaneia os cadernos que faltam dos últimos `dias` dias. Idempotente: caderno fechado e sem nova versão não é refeito."""
    from . import clientes as _cl
    hoje = hoje or date.today()
    clientes = _cl.ativos(con)
    http = motor.djen.http
    siglas = siglas or siglas_tribunais(http)
    tarefas = []
    for i in range(dias - 1, -1, -1):
        dia = (hoje - timedelta(days=i)).isoformat()
        for sg in siglas:
            for meio in MEIOS:
                ex = con.execute("SELECT status,total_declarado,total_lido,versao,hash FROM cadernos WHERE sigla=? AND meio=? AND data=?", (sg, meio, dia)).fetchone()
                fechado_antes = ex and (ex["status"] in ("Sem comunicações", "Sem caderno") or (ex["status"] == "Processado" and ex["total_lido"] == ex["total_declarado"]))
                if not fechado_antes or dia >= (hoje - timedelta(days=1)).isoformat():   # hoje e ontem podem ganhar nova versão; versão igual não é baixada de novo
                    tarefas.append((sg, meio, dia, dict(ex) if ex else None))
    exec_id = con.execute("INSERT INTO execucoes(gatilho,inicio) VALUES ('caderno',?)", (agora_iso(),)).lastrowid
    con.commit()
    filtro = Filtro(clientes)
    res = {"cadernos_processados": 0, "cadernos_pendentes": 0, "cadernos_com_erro": 0, "comunicacoes_lidas": 0, "achados": 0, "tarefas": len(tarefas)}
    pasta = tempfile.mkdtemp(prefix="cadernos_")

    def trabalhar(t):
        sg, meio, dia, anterior = t
        try:
            return _processar(http, sg, meio, dia, filtro, baixar, pasta, anterior)
        except (ErroFonte, OSError, ValueError, zipfile.BadZipFile) as e:
            return {"sigla": sg, "meio": meio, "data": dia, "status": "erro", "versao": None, "hash": None, "total_declarado": None,
                    "total_lido": 0, "achados": [], "erro": "%s: %s" % (type(e).__name__, e)}

    with ThreadPoolExecutor(max_workers=trabalhadores) as ex:
        for r in ex.map(trabalhar, tarefas):
            por_cli: Dict[int, list] = {}
            for cid, com, m in r["achados"]:
                por_cli.setdefault(cid, []).append((com, m))
            for cid, ach in por_cli.items():
                cli = next(c for c in clientes if c["id"] == cid)
                motor._persistir(exec_id, cli, [{"q": {"chave": "caderno:%s:%s" % (r["sigla"], r["meio"]), "param": "caderno", "valor": r["sigla"]},
                                                 "ini": r["data"], "fim": r["data"], "inicio": agora_iso(), "status": "ok",
                                                 "total": r["total_declarado"], "lidos": r["total_lido"], "erro": None, "achados": ach}])
            if r.get("reuso"):
                res["cadernos_processados"] += 1
                res["comunicacoes_lidas"] += r["total_lido"]
                continue
            status = r["status"] if _fechado(r) or r["status"] in ("Em processamento", "Não Processado", "erro") else "incompleto"
            con.execute("INSERT INTO cadernos(sigla,meio,data,status,versao,hash,total_declarado,total_lido,achados,erro,atualizado_em) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sigla,meio,data) DO UPDATE SET status=excluded.status,versao=excluded.versao,"
                        "hash=excluded.hash,total_declarado=excluded.total_declarado,total_lido=excluded.total_lido,achados=excluded.achados,"
                        "erro=excluded.erro,atualizado_em=excluded.atualizado_em",
                        (r["sigla"], r["meio"], r["data"], status, r["versao"], r["hash"], r["total_declarado"], r["total_lido"],
                         len(r["achados"]), r["erro"], agora_iso()))
            con.commit()
            res["comunicacoes_lidas"] += r["total_lido"]
            res["achados"] += len(r["achados"])
            res["cadernos_processados" if _fechado(r) else ("cadernos_com_erro" if status in ("erro", "incompleto") else "cadernos_pendentes")] += 1
    try:
        os.rmdir(pasta)
    except OSError:
        pass
    motor.enriquecer_pendentes(exec_id)
    con.execute("UPDATE execucoes SET fim=?,status=?,resumo=? WHERE id=?",
                (agora_iso(), "sucesso" if not res["cadernos_com_erro"] else "parcial", json.dumps(res), exec_id))
    con.commit()
    return {"execucao_id": exec_id, "status": "sucesso" if not res["cadernos_com_erro"] else "parcial", **res}


def resumo(con, dias: int = 7, hoje: Optional[date] = None) -> List[Dict]:
    """Por dia: quantos cadernos estão fechados, pendentes ou com erro; comunicações declaradas pelo CNJ x lidas."""
    hoje = hoje or date.today()
    out = []
    for i in range(dias - 1, -1, -1):
        dia = (hoje - timedelta(days=i)).isoformat()
        rows = con.execute("SELECT status,total_declarado,total_lido,achados FROM cadernos WHERE data=?", (dia,)).fetchall()
        fech = [r for r in rows if r["status"] in ("Sem comunicações", "Sem caderno") or (r["status"] == "Processado" and r["total_lido"] == r["total_declarado"])]
        out.append({"dia": dia, "cadernos": len(rows), "fechados": len(fech), "pendentes": sum(1 for r in rows if r["status"] in ("Em processamento", "Não Processado")),
                    "com_erro": sum(1 for r in rows if r["status"] in ("erro", "incompleto")),
                    "declaradas": sum(r["total_declarado"] or 0 for r in rows), "lidas": sum(r["total_lido"] or 0 for r in rows),
                    "achados": sum(r["achados"] or 0 for r in rows)})
    return out
