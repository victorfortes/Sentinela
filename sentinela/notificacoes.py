"""Alertas: interno, e-mail (SMTP) e Slack (webhook). Sem duplicidade (chave_dedup única). Segredos só por ambiente:
SENTINELA_SMTP_USUARIO, SENTINELA_SMTP_SENHA, SENTINELA_SLACK_WEBHOOK."""
import json
import os
import smtplib
import sqlite3
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from typing import Callable, Dict, List, Optional

from . import config as _config


def agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def montar(con: sqlite3.Connection, correspondencia_id: int, tipo: str) -> Dict:
    r = con.execute("SELECT c.*,p.numero_processo,p.tribunal,p.data_ajuizamento,p.classe,p.assuntos,cl.razao_social,cl.cnpj_matriz "
                    "FROM correspondencias c JOIN processos p ON p.id=c.processo_id JOIN clientes cl ON cl.id=c.cliente_id "
                    "WHERE c.id=?", (correspondencia_id,)).fetchone()
    com = con.execute("SELECT link FROM comunicacoes WHERE id=?", (r["comunicacao_id"],)).fetchone()
    an = con.execute("SELECT resumo,campos FROM analises WHERE processo_id=? ORDER BY id DESC LIMIT 1", (r["processo_id"],)).fetchone()
    valor = json.loads(an["campos"])["valor_causa"]["valor"] if an else None
    definitivo = tipo == "nova_acao_confirmada"
    return {"tipo": tipo, "definitivo": definitivo, "cliente": r["razao_social"], "numero_processo": r["numero_processo"],
            "tribunal": r["tribunal"], "data_distribuicao": r["data_ajuizamento"], "natureza": r["classe"],
            "valor_causa": valor, "sintese": an["resumo"] if an else "Análise ainda não gerada.",
            "link_oficial": (com["link"] if com and com["link"] else "https://portaldeservicos.pdpj.jus.br/consulta"),
            "prioridade": r["prioridade"] or "não classificada",
            "estado_validacao": {"automatico": "confirmada automaticamente", "pendente": "pendente de conferência",
                                 "confirmado_manual": "confirmada manualmente"}.get(r["estado_validacao"], r["estado_validacao"]),
            "classificacao": r["classificacao"], "categoria_data": r["categoria_data"], "correspondencia_id": correspondencia_id}


def texto(a: Dict) -> (str, str):
    if a["tipo"] == "falha_consulta":
        return "NGA Sentinela: falha nas consultas", a["detalhe"]
    cab = "NOVA AÇÃO CONTRA CLIENTE" if a["definitivo"] else "POSSÍVEL NOVA AÇÃO (CONFERIR)"
    if a.get("data_distribuicao") == datetime.now().date().isoformat():
        cab = "AJUIZADA HOJE — " + cab
    corpo = "\n".join("%s: %s" % (k, v) for k, v in [
        ("Cliente", a["cliente"]), ("Processo", a["numero_processo"]), ("Tribunal", a["tribunal"]),
        ("Distribuição", a["data_distribuicao"] or "não verificada"), ("Natureza", a["natureza"] or "não informada"),
        ("Valor da causa", a["valor_causa"] if a["valor_causa"] else "não disponível"), ("Prioridade", a["prioridade"]),
        ("Validação", a["estado_validacao"]), ("Síntese", a["sintese"]), ("Consulta oficial", a["link_oficial"])])
    return "NGA Sentinela — %s: %s" % (cab, a["cliente"]), corpo


def _enviar_email(cfg: Dict, assunto: str, corpo: str) -> None:
    m = EmailMessage()
    m["Subject"], m["From"], m["To"] = assunto, cfg["email_remetente"], ", ".join(cfg["emails"])
    m.set_content(corpo)
    with smtplib.SMTP(cfg["smtp_host"], int(cfg["smtp_porta"]), timeout=30) as s:
        s.starttls()
        u, p = os.environ.get("SENTINELA_SMTP_USUARIO"), os.environ.get("SENTINELA_SMTP_SENHA")
        if u and p:
            s.login(u, p)
        s.send_message(m)


def _enviar_slack(assunto: str, corpo: str) -> None:
    url = os.environ["SENTINELA_SLACK_WEBHOOK"]
    req = urllib.request.Request(url, data=json.dumps({"text": "*%s*\n%s" % (assunto, corpo)}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=20).read()


def emitir(con: sqlite3.Connection, a: Dict, ref: str, enviar_email: Callable = None, enviar_slack: Callable = None) -> Dict[str, str]:
    """Envia o alerta `a` nos canais habilitados. `ref` identifica o fato gerador (dedup). Devolve {canal: status}."""
    cfg = _config.obter(con, "notificacoes")
    assunto, corpo = texto(a)
    canais = [c for c in ("interno", "email", "slack") if cfg.get(c)]
    res = {}
    for canal in canais:
        chave = "%s:%s:%s" % (a["tipo"], canal, ref)
        ex = con.execute("SELECT id,status FROM alertas WHERE chave_dedup=?", (chave,)).fetchone()
        if ex and ex["status"] in ("enviado", "ignorado"):
            res[canal] = "duplicado_ignorado"
            continue
        status, erro = "enviado", None
        try:
            if canal == "email":
                if not (cfg["smtp_host"] and cfg["emails"]):
                    raise RuntimeError("e-mail habilitado sem servidor SMTP ou destinatários")
                (enviar_email or (lambda s, c: _enviar_email(cfg, s, c)))(assunto, corpo)
            elif canal == "slack":
                (enviar_slack or _enviar_slack)(assunto, corpo)
        except Exception as e:
            status, erro = "falha", str(e)[:300]
        payload = json.dumps(a, ensure_ascii=False)
        if ex:
            con.execute("UPDATE alertas SET status=?,erro=?,enviado_em=?,payload=? WHERE id=?", (status, erro, agora(), payload, ex["id"]))
        else:
            con.execute("INSERT INTO alertas(chave_dedup,tipo,canal,correspondencia_id,status,erro,payload,criado_em,enviado_em) "
                        "VALUES (?,?,?,?,?,?,?,?,?)", (chave, a["tipo"], canal, a.get("correspondencia_id"), status, erro, payload, agora(), agora()))
        res[canal] = status
    con.commit()
    return res


def processar_pendentes(con: sqlite3.Connection, **kw) -> Dict[str, int]:
    """Gera alertas para correspondências recentes. Processos antigos achados agora e descartados não alertam."""
    cont = {"definitivos": 0, "conferencia": 0, "duplicados": 0, "falhas_envio": 0}
    minimo = _config.obter(con, "radar")["confianca_revisao"]
    rows = con.execute("SELECT id,classificacao,estado_validacao FROM correspondencias WHERE classificacao!='descartada' "
                       "AND categoria_data!='processo_antigo' AND (confianca IS NULL OR confianca>=?)", (minimo,)).fetchall()
    for r in rows:
        definitivo = r["classificacao"] == "confirmada" and con.execute(
            "SELECT categoria_data FROM correspondencias WHERE id=?", (r["id"],)).fetchone()[0] == "nova_distribuicao"
        tipo = "nova_acao_confirmada" if definitivo else "conferencia_pendente"
        if tipo == "conferencia_pendente" and r["estado_validacao"] not in ("pendente", "automatico"):
            continue
        res = emitir(con, montar(con, r["id"], tipo), str(r["id"]), **kw)
        for st in res.values():
            if st == "duplicado_ignorado":
                cont["duplicados"] += 1
            elif st == "falha":
                cont["falhas_envio"] += 1
            else:
                cont["definitivos" if definitivo else "conferencia"] += 1
    return cont


def alertar_falha_execucao(con: sqlite3.Connection, execucao_id: int, resumo: Dict, **kw) -> Dict[str, str]:
    detalhe = ("A execução %d terminou com falhas: %d de %d consultas falharam. Isso NÃO significa ausência de novos processos; "
               "as consultas serão reprocessadas." % (execucao_id, resumo["consultas_com_falha"], resumo["consultas_executadas"]))
    return emitir(con, {"tipo": "falha_consulta", "definitivo": False, "detalhe": detalhe, "cliente": "(sistema)",
                        "correspondencia_id": None}, str(execucao_id), **kw)
