"""Adapters de consulta direta aos tribunais (Fonte 3 do Radar Judicial) e demais fontes planejadas.
Cada adapter declara com HONESTIDADE o que consegue fazer: se a consulta pública exige CAPTCHA ou login, o estado é
SOURCE_CAPTCHA / SOURCE_AUTH_REQUIRED e a fonte NÃO conta como cobertura. Nada é contornado."""
from typing import Dict, List

from .base import TribunalAdapter, Saude
from .tjpr import TJPRAdapter

ADAPTERS: List[TribunalAdapter] = [TJPRAdapter()]


def sondar(con) -> List[Dict]:
    """Executa o health check de cada adapter e grava o resultado (worker); a API só lê a tabela."""
    from datetime import datetime
    res = []
    for a in ADAPTERS:
        s = a.health_check()
        con.execute("INSERT INTO adapter_saude(court,family,status,detalhe,http_status,parser_version,verificado_em) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(court) DO UPDATE SET family=excluded.family,status=excluded.status,detalhe=excluded.detalhe,"
                    "http_status=excluded.http_status,parser_version=excluded.parser_version,verificado_em=excluded.verificado_em",
                    (a.court, a.family, s.status, s.detail, s.http_status, a.parser_version, datetime.now().isoformat(timespec="seconds")))
        res.append({"court": a.court, "status": s.status, "detalhe": s.detail})
    con.commit()
    return res


def status_planejadas(con=None) -> List[Dict]:
    """Fontes previstas que ainda NÃO contribuem para a cobertura, com o motivo."""
    out = [{"fonte": "DOMICILIO", "rotulo": "Domicílio Judicial Eletrônico", "estado": "NOT_CONFIGURED",
            "motivo": "exige autorização do cliente (e-CNPJ/gov.br) e acesso à API do CNJ; ver docs/FONTES_E_COMO_FECHAR_A_LACUNA.md"}]
    for a in ADAPTERS:
        r = con.execute("SELECT status,detalhe,verificado_em FROM adapter_saude WHERE court=?", (a.court,)).fetchone() if con is not None else None
        out.append({"fonte": "TRIBUNAL_" + a.court, "rotulo": "%s direto (%s)" % (a.court, a.family),
                    "estado": r["status"] if r else "NOT_IMPLEMENTED", "motivo": (r["detalhe"] if r else "ainda não sondado"),
                    "verificado_em": r["verificado_em"] if r else None})
    return out
