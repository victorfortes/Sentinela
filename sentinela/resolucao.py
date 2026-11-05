"""Resolução AUTOMÁTICA de vínculos incertos. Não existe fila humana: o que não é certo (provável/inconclusivo) é verificado pelo
próprio sistema, que lê TODO o histórico do processo no DJEN (consulta por número) e reavalia o vínculo com todas as comunicações:
- se alguma comunicação confirma o cliente (CNPJ no texto, parte nomeada, papel no corpo) -> promove;
- se o cliente aparece só como autor -> descarta;
- sem corroboração -> continua visível como 'possível' (alerta informativo) e é reavaliado a cada nova comunicação;
  após 14 dias sem novidade o estado vira definitivo ('possível', sem tarefa para ninguém)."""
import json
from datetime import datetime, timedelta
from typing import Dict

from . import clientes as _clientes
from . import matcher, radar
from .motor import RANK, _normalizar_item, agora_iso

DIAS_SEM_CORROBORACAO = 14
INTERVALO_HORAS = 6


def resolver_pendentes(con, motor, limite: int = 60) -> Dict[str, int]:
    from . import cobertura
    res = {"verificados": 0, "promovidos": 0, "descartados": 0, "mantidos": 0, "finalizados": 0, "erros": 0}
    corte = (datetime.now() - timedelta(hours=INTERVALO_HORAS)).isoformat(timespec="seconds")
    rows = con.execute("SELECT c.id,c.cliente_id,c.classificacao,c.identificada_em,p.numero_processo FROM correspondencias c "
                       "JOIN processos p ON p.id=c.processo_id WHERE c.classificacao IN ('provavel','inconclusiva') "
                       "AND c.estado_validacao IN ('em_verificacao','pendente') AND (c.resolvida_em IS NULL OR c.resolvida_em<?) "
                       "ORDER BY coalesce(c.resolvida_em,'') LIMIT ?", (corte, limite)).fetchall()
    for r in rows:
        cli = _clientes.carregar(con, r["cliente_id"])
        try:
            itens, total, _ = motor.djen.consultar("numeroProcesso", r["numero_processo"], None, None)
        except Exception:
            res["erros"] += 1
            continue
        res["verificados"] += 1
        coms = [_normalizar_item(i) for i in itens if len(i.get("numero_processo") or "") == 20]
        avals = [(c, matcher.avaliar(cli, c)) for c in coms]
        melhores = [(c, m) for c, m in avals if m and RANK[m["classificacao"]] > RANK[r["classificacao"]] and m["classificacao"] in ("confirmada", "provavel")]
        if melhores:
            cobertura._incluir(con, motor, cli, {"numero_processo": r["numero_processo"]}, melhores, total, len(coms))
            novo = con.execute("SELECT classificacao FROM correspondencias WHERE id=?", (r["id"],)).fetchone()["classificacao"]
            if novo == "confirmada":
                con.execute("UPDATE correspondencias SET estado_validacao='automatico' WHERE id=?", (r["id"],))
                radar.evento(con, motor_pid(con, r["id"]), "AUTO_RESOLVIDO", r["cliente_id"], "DJEN", "promovido a confirmado pelo histórico do processo")
                res["promovidos"] += 1
                con.execute("UPDATE correspondencias SET resolvida_em=? WHERE id=?", (agora_iso(), r["id"]))
                continue
        achou_algo = [m for c, m in avals if m]
        if achou_algo and all(m["classificacao"] == "descartada" for m in achou_algo):
            con.execute("UPDATE correspondencias SET classificacao='descartada',estado_validacao='descartado_auto',confianca=0,status_deteccao='DISCARDED' WHERE id=?", (r["id"],))
            radar.evento(con, motor_pid(con, r["id"]), "AUTO_DESCARTADO", r["cliente_id"], "DJEN", "o histórico do processo mostra o cliente apenas como autor")
            res["descartados"] += 1
        elif datetime.fromisoformat(r["identificada_em"]) < datetime.now() - timedelta(days=DIAS_SEM_CORROBORACAO):
            con.execute("UPDATE correspondencias SET estado_validacao='automatico' WHERE id=?", (r["id"],))
            radar.evento(con, motor_pid(con, r["id"]), "AUTO_FINALIZADO", r["cliente_id"], None, "sem corroboração adicional em %d dias: mantido como possível" % DIAS_SEM_CORROBORACAO)
            res["finalizados"] += 1
        else:
            res["mantidos"] += 1
        con.execute("UPDATE correspondencias SET resolvida_em=? WHERE id=?", (agora_iso(), r["id"]))
        con.commit()
    con.commit()
    return res


def motor_pid(con, correspondencia_id: int) -> int:
    return con.execute("SELECT processo_id FROM correspondencias WHERE id=?", (correspondencia_id,)).fetchone()[0]
