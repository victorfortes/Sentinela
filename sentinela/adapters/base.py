from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Saude:
    status: str                 # HEALTHY | DEGRADED | DOWN | SOURCE_CAPTCHA | SOURCE_AUTH_REQUIRED | SOURCE_BLOCKED | NOT_IMPLEMENTED
    detail: str = ""
    http_status: Optional[int] = None


class FonteBloqueada(Exception):
    """A fonte exige algo que o Sentinela não faz (CAPTCHA, login, certificado)."""
    def __init__(self, codigo: str, detalhe: str):
        super().__init__("%s: %s" % (codigo, detalhe))
        self.codigo, self.detalhe = codigo, detalhe


class TribunalAdapter:
    court = ""
    family = ""   # PJe | eproc | Projudi | eSAJ
    parser_version = "0"

    def __init__(self):
        self._ultimo = Saude("NOT_IMPLEMENTED", "adapter ainda não foi sondado")

    def ultimo_estado(self) -> Saude:
        return self._ultimo

    def health_check(self) -> Saude:
        raise NotImplementedError

    def search_by_cnpj(self, cnpj: str) -> List[dict]:
        raise FonteBloqueada("SOURCE_NOT_IMPLEMENTED", "busca direta por CNPJ não implementada para %s" % self.court)

    def search_by_name(self, nome: str) -> List[dict]:
        raise FonteBloqueada("SOURCE_NOT_IMPLEMENTED", "busca direta por nome não implementada para %s" % self.court)

    def get_process(self, cnj: str) -> Optional[dict]:
        raise FonteBloqueada("SOURCE_NOT_IMPLEMENTED", "consulta direta de processo não implementada para %s" % self.court)
