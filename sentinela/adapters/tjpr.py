"""TJPR / Projudi (consulta pública). Sonda de saúde: a busca por parte/CPF-CNPJ existe, mas é protegida por reCAPTCHA.
O Sentinela NÃO contorna CAPTCHA: o adapter reporta SOURCE_CAPTCHA e a fonte não conta como cobertura."""
import re
import time
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

from .base import Saude, TribunalAdapter

URL_BASE = "https://consulta.tjpr.jus.br/projudi_consulta/"


class TJPRAdapter(TribunalAdapter):
    court = "TJPR"
    family = "Projudi"
    parser_version = "TJPR_PROJUDI_PARSER v0.1.0"

    def health_check(self) -> Saude:
        jar = CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        op.addheaders = [("User-Agent", "Mozilla/5.0 (NGA-Sentinela health check)")]
        try:
            raiz = op.open(URL_BASE, timeout=30).read().decode("iso-8859-1", "ignore")
            m = re.search(r'src="(/projudi_consulta/processo/consultaPublica\.do[^"]+)"', raiz)
            if not m:
                self._ultimo = Saude("DEGRADED", "estrutura da página inicial mudou (frame de consulta não encontrado): parser precisa de revisão")
                return self._ultimo
            r = op.open("https://consulta.tjpr.jus.br" + m.group(1), timeout=30)
            html = r.read().decode("iso-8859-1", "ignore")
        except urllib.error.HTTPError as e:
            self._ultimo = Saude("DOWN", "HTTP %d" % e.code, e.code)
            return self._ultimo
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            self._ultimo = Saude("DOWN", "%s: %s" % (type(e).__name__, e))
            return self._ultimo
        tem_busca = all(c in html for c in ("nomeParte", "cpfCnpj"))
        if re.search(r"g-recaptcha", html, re.I):
            self._ultimo = Saude("SOURCE_CAPTCHA", "a consulta por parte/CNPJ exige reCAPTCHA; não é contornado. Disponível apenas busca manual pelo advogado.", 200)
        elif tem_busca:
            self._ultimo = Saude("NOT_IMPLEMENTED", "busca por parte/CNPJ sem CAPTCHA detectada, mas o parser ainda não foi escrito", 200)
        else:
            self._ultimo = Saude("DEGRADED", "formulário de busca por parte não encontrado: layout mudou", 200)
        return self._ultimo
