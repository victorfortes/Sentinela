"""Radar Judicial: fontes por processo, eventos, partes, proveniência de campos, confiança e reconciliação.
Um processo é guardado UMA vez (UNIQUE numero_processo); fontes novas só se somam a ele e elevam a confiança."""
import json
import sqlite3
from datetime import datetime
from typing import Dict, Iterable, Optional

from . import nomes as _nomes

LADO = {"A": "PLAINTIFF", "P": "DEFENDANT", "T": "INTERESTED", "D": "THIRD_PARTY"}
FAMILIA = {"DJEN_BUSCA": "DJEN", "DJEN_CADERNO": "DJEN"}


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def pontuar(nivel: int, classificacao: str, polo: Optional[str], ev: Dict) -> int:
    """Pontuação do VÍNCULO cliente x processo (hierarquia da especificação: CNPJ 100, razão social exata 95, alias 90, fantasia 60…)."""
    if classificacao == "descartada":
        return 0
    tipo = ev.get("tipo_nome")
    if tipo == "iniciais":
        return 55
    if ev.get("papel_no_texto") is not None or "trecho" in ev:       # nome achado no corpo da comunicação
        if ev.get("papel_no_texto") == "P":
            return 90 if tipo == "razao_social" else 60
        return 50
    if nivel in (1, 2):
        return 100 if polo == "P" else 70
    if nivel == 3:
        return 95 if tipo == "razao_social" else 60
    if nivel == 4:
        return 95 if tipo == "anterior" else 90
    return 80 if (ev.get("similaridade") or 0) >= 0.95 else 50


def evento(con, processo_id: int, tipo: str, cliente_id: Optional[int] = None, fonte: Optional[str] = None, detalhe: str = "") -> None:
    con.execute("INSERT INTO processo_eventos(processo_id,cliente_id,tipo,fonte,detalhe,criado_em) VALUES (?,?,?,?,?,?)",
                (processo_id, cliente_id, tipo, fonte, detalhe, _agora()))


def registrar_visto(con, processo_id: int, fonte: str, identificador: str = "", cliente_id: Optional[int] = None) -> bool:
    """Marca que `fonte` viu o processo. Devolve True se é a primeira vez desta fonte para o processo."""
    agora = _agora()
    ex = con.execute("SELECT 1 FROM processo_fontes WHERE processo_id=? AND fonte=?", (processo_id, fonte)).fetchone()
    if ex:
        con.execute("UPDATE processo_fontes SET ultimo_visto=?,vezes=vezes+1 WHERE processo_id=? AND fonte=?", (agora, processo_id, fonte))
        return False
    con.execute("INSERT INTO processo_fontes(processo_id,fonte,identificador,primeiro_visto,ultimo_visto) VALUES (?,?,?,?,?)",
                (processo_id, fonte, str(identificador), agora, agora))
    evento(con, processo_id, "SOURCE_ADDED", cliente_id, fonte, "fonte %s passou a confirmar o processo" % fonte)
    return True


def registrar_partes(con, processo_id: int, destinatarios: Iterable[Dict], fonte: str) -> None:
    for d in destinatarios or []:
        nome = (d.get("nome") or "").strip()
        if nome:
            con.execute("INSERT OR IGNORE INTO processo_partes(processo_id,nome,nome_norm,lado,fonte) VALUES (?,?,?,?,?)",
                        (processo_id, nome, _nomes.normalizar(nome) or nome.upper(), LADO.get(d.get("polo"), "UNKNOWN"), fonte))


def registrar_campos(con, processo_id: int, campos: Dict[str, Optional[str]], fonte: str) -> None:
    """Proveniência: guarda cada valor observado com a fonte; valores divergentes coexistem (nada é sobrescrito em silêncio)."""
    for k, v in campos.items():
        if v not in (None, ""):
            con.execute("INSERT OR IGNORE INTO processo_campos(processo_id,campo,valor,fonte,observado_em) VALUES (?,?,?,?,?)",
                        (processo_id, k, str(v), fonte, _agora()))


def conflitos(con, processo_id: int) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for r in con.execute("SELECT campo,valor,fonte FROM processo_campos WHERE processo_id=? ORDER BY campo", (processo_id,)):
        out.setdefault(r["campo"], []).append({"valor": r["valor"], "fonte": r["fonte"]})
    return {k: v for k, v in out.items() if len({x["valor"].strip().upper() for x in v}) > 1}


def reconciliar_processo(con, processo_id: int) -> None:
    """Recalcula confiança e status (DETECTED -> CONFIRMED) de cada vínculo do processo a partir das fontes que o viram."""
    fontes = {FAMILIA.get(r["fonte"], r["fonte"]) for r in con.execute("SELECT fonte FROM processo_fontes WHERE processo_id=?", (processo_id,))}
    for c in con.execute("SELECT id,cliente_id,nivel,classificacao,polo,evidencia,confianca,status_deteccao,citacao_confirmada FROM correspondencias WHERE processo_id=?",
                         (processo_id,)).fetchall():
        try:
            ev = json.loads(c["evidencia"] or "{}")
        except ValueError:
            ev = {}
        base = pontuar(c["nivel"], c["classificacao"], c["polo"], ev)
        if base == 0:
            conf, status = 0, "DISCARDED"
        else:
            extra = (8 if "DATAJUD" in fontes else 0) + 5 * len(fontes - {"DJEN", "DATAJUD"}) + (2 if c["citacao_confirmada"] else 0)
            conf = min(100, base + extra)
            status = "CONFIRMED" if len(fontes) >= 2 else "DETECTED"
        if c["status_deteccao"] == "DETECTED" and status == "CONFIRMED":
            evento(con, processo_id, "PARTY_CONFIRMED", c["cliente_id"], None, "confirmado por %d fontes: %s" % (len(fontes), ", ".join(sorted(fontes))))
        con.execute("UPDATE correspondencias SET confianca=?,status_deteccao=? WHERE id=?", (conf, status, c["id"]))


def marcar_citacao(con, processo_id: int, cliente_id: int, tipo_comunicacao: Optional[str]) -> None:
    if "cita" not in (tipo_comunicacao or "").lower():
        return
    ja = con.execute("SELECT citacao_confirmada FROM correspondencias WHERE processo_id=? AND cliente_id=?", (processo_id, cliente_id)).fetchone()
    if ja and not ja["citacao_confirmada"]:
        con.execute("UPDATE correspondencias SET citacao_confirmada=1 WHERE processo_id=? AND cliente_id=?", (processo_id, cliente_id))
        evento(con, processo_id, "CITATION_FOUND", cliente_id, "DJEN", "comunicação do tipo citação publicada")
