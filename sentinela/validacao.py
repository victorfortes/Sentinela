"""Correção manual OPCIONAL de um vínculo (não há fila de trabalho): uma pessoa pode sobrepor a decisão automática, com trilha de auditoria."""
import json
import sqlite3
from typing import Dict, List


def decidir(con: sqlite3.Connection, correspondencia_id: int, usuario: str, decisao: str, justificativa: str = "") -> None:
    if decisao not in ("confirmada", "descartada"):
        raise ValueError("decisão inválida")
    r = con.execute("SELECT classificacao FROM correspondencias WHERE id=?", (correspondencia_id,)).fetchone()
    if r is None:
        raise KeyError(correspondencia_id)
    con.execute("INSERT INTO validacoes(correspondencia_id,usuario,decisao,classificacao_anterior,justificativa) VALUES (?,?,?,?,?)",
                (correspondencia_id, usuario, decisao, r["classificacao"], justificativa))
    con.execute("UPDATE correspondencias SET classificacao=?,estado_validacao=? WHERE id=?",
                (decisao, "confirmado_manual" if decisao == "confirmada" else "descartado_manual", correspondencia_id))
    con.commit()


def decidir_em_lote(con: sqlite3.Connection, ids: List[int], usuario: str, decisao: str, justificativa: str) -> int:
    for i in ids:
        decidir(con, i, usuario, decisao, justificativa)
    return len(ids)
