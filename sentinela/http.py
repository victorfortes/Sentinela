"""Cliente HTTP resiliente: limite de taxa por fonte, retentativas com espera progressiva e relato de saúde."""
import hashlib
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple


class ErroFonte(Exception):
    def __init__(self, mensagem: str, status: int = 0):
        super().__init__(mensagem)
        self.status = status


def transporte_urllib(metodo, url, headers, corpo, timeout) -> Tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, data=corpo, headers=headers, method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


COLETOR_VERSAO = "http-1.1"


def codigo_do_status(status: int) -> str:
    """Taxonomia de resultado de uma chamada (pedida na especificação do Radar Judicial)."""
    if status == 200:
        return "SUCCESS"
    return {404: "NOT_FOUND", 429: "RATE_LIMITED", 401: "AUTH_REQUIRED", 403: "BLOCKED", 503: "MAINTENANCE"}.get(status, "UNKNOWN_ERROR")


def rotulo_fonte(nome: str, url: str) -> str:
    if nome == "djen":
        return "DJEN_CADERNO" if "/caderno/" in url else "DJEN_BUSCA"
    return {"datajud": "DATAJUD", "cnpj_registro": "CNPJ_REGISTRO"}.get(nome, nome.upper())


def tribunal_da_url(url: str) -> Optional[str]:
    m = re.search(r"api_publica_([a-z0-9]+)/", url) or re.search(r"/caderno/([A-Za-z0-9-]+)/", url) or re.search(r"siglaTribunal=([A-Za-z0-9-]+)", url)
    return m.group(1).upper() if m else None


class FonteHTTP:
    def __init__(self, nome: str, min_intervalo: float = 1.0, tentativas: int = 4, espera_base: float = 2.0,
                 timeout: float = 60.0, transporte: Callable = transporte_urllib, dormir: Callable = time.sleep,
                 ao_resultado: Optional[Callable[[str, bool, str], None]] = None,
                 status_ok: Tuple[int, ...] = (200,),
                 ao_chamada: Optional[Callable[[Dict], None]] = None):
        self.nome, self.min_intervalo, self.tentativas = nome, min_intervalo, tentativas
        self.espera_base, self.timeout = espera_base, timeout
        self.status_ok = status_ok  # as fontes respondem 200; o Nexus responde 201 na ingestão
        self._transporte, self._dormir, self._cb = transporte, dormir, ao_resultado
        self._ao_chamada, self._local = ao_chamada, threading.local()
        self._lock = threading.Lock()
        self._ultimo = 0.0

    def _esperar_vez(self):
        with self._lock:
            falta = self._ultimo + self.min_intervalo - time.monotonic()
            if falta > 0:
                self._dormir(falta)
            self._ultimo = time.monotonic()

    def requisitar(self, metodo: str, url: str, headers: Optional[Dict[str, str]] = None, json_corpo=None):
        h = {"User-Agent": "NGA-Sentinela/0.2", "Accept": "application/json", **(headers or {})}
        corpo = None
        if json_corpo is not None:
            corpo = json.dumps(json_corpo).encode()
            h["Content-Type"] = "application/json"
        ultimo_erro, status = "", 0
        self._local.registrada = False
        for t in range(self.tentativas):
            self._local.registrada = False
            self._esperar_vez()
            t0 = time.monotonic()
            try:
                status, hdr, raw = self._transporte(metodo, url, h, corpo, self.timeout)
                self._registrar_chamada(metodo, url, status, t0, raw, final=not (status not in self.status_ok and status in (429, 500, 502, 503, 504) and t < self.tentativas - 1))
                if status in self.status_ok:
                    dados = json.loads(raw)
                    restante = hdr.get("x-ratelimit-remaining")
                    if restante is not None and restante.isdigit() and int(restante) <= 2:
                        self._dormir(15)  # perto do teto de requisições da fonte
                    self._relatar(True, "")
                    return dados
                ultimo_erro = "HTTP %d" % status
                if status not in (429, 500, 502, 503, 504):
                    break  # erro definitivo: não adianta repetir
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                ultimo_erro = "%s: %s" % (type(e).__name__, e)
                if not getattr(self._local, "registrada", False):
                    self._registrar_chamada(metodo, url, 0, t0, b"", "TIMEOUT" if isinstance(e, (TimeoutError, socket.timeout)) or "timed out" in str(e) else "UNKNOWN_ERROR",
                                            final=t >= self.tentativas - 1)
            if t < self.tentativas - 1:
                # o DJEN orienta aguardar 1 minuto ao receber 429 (swagger oficial); insistir antes pode levar a bloqueio
                self._dormir(max(60, self.espera_base * (2 ** t)) if status == 429 and self.nome == "djen" else self.espera_base * (2 ** t))
        self._relatar(False, ultimo_erro)
        raise ErroFonte("%s: %s" % (self.nome, ultimo_erro), status)

    def ultima(self) -> Dict:
        """Dados da última chamada feita por esta thread (status HTTP, código, hash da resposta)."""
        return dict(getattr(self._local, "ultima", {}) or {})

    def _registrar_chamada(self, metodo, url, status, t0, raw, codigo=None, final=True):
        codigo = codigo or codigo_do_status(status)
        d = {"fonte": rotulo_fonte(self.nome, url), "tribunal": tribunal_da_url(url), "metodo": metodo, "consulta": re.sub(r"https?://[^/]+", "", url)[:200],
             "http_status": status or None, "latencia_ms": int((time.monotonic() - t0) * 1000),
             "ok": 1 if (status in self.status_ok or status == 404) else 0, "final": 1 if final else 0, "codigo": codigo,
             "hash_resposta": hashlib.sha256(raw or b"").hexdigest()[:16] if raw else None, "versao_coletor": COLETOR_VERSAO}
        self._local.ultima = d
        self._local.registrada = True
        if self._ao_chamada:
            try:
                self._ao_chamada(d)
            except Exception:   # a trilha de auditoria nunca pode derrubar a consulta
                pass

    def _relatar(self, ok, erro):
        if self._cb:
            self._cb(self.nome, ok, erro)
