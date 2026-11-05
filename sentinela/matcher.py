"""Identificação hierárquica do cliente nas partes de uma comunicação.
Níveis: 1 CNPJ matriz | 2 CNPJ filial | 3 razão social/fantasia normalizada | 4 denominação anterior/alternativa |
5 nome aproximado. O DJEN não informa o documento da parte: a evidência de CNPJ vem do texto da comunicação."""
import re
from typing import Dict, List, Optional

from . import cnpj as _cnpj
from . import nomes as _nomes

LIMIAR_APROX = 0.88
_RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")


def cnpjs_no_texto(texto: str) -> set:
    achados = set()
    for m in _RE_CNPJ.findall(texto or ""):
        d = _cnpj.somente_digitos(m)
        if _cnpj.valido(d):
            achados.add(d)
    return achados


_PASSIVO = {"reu", "re", "reus", "requerido", "requerida", "requeridos", "executado", "executada", "executados", "reclamado",
            "reclamada", "demandado", "demandada", "impetrado", "impetrada", "promovido", "promovida"}
_ATIVO = {"autor", "autora", "autores", "requerente", "requerentes", "exequente", "exequentes", "reclamante", "impetrante",
          "demandante", "promovente"}
_NAO_PAPEL = {"cnpj", "cpf", "rg", "mail", "email", "cep", "ssp", "oab", "endereco", "telefone"}
_RE_ROTULO = re.compile(r"([A-Za-zÀ-ÿ]+)(?:\(a\))?(?:\(s\))?\s*:")


def _sem_acento(t: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn").lower()


def papel_do_cnpj_no_texto(texto: str, cnpj_digitos: str) -> Optional[str]:
    """Papel declarado no próprio texto da comunicação para a parte que traz este CNPJ ('Réu(s): NOME (CPF/CNPJ: ...)').
    Só devolve P/A para rótulos de polo originário; papéis recursais (embargante, agravante...) e terceiros ficam indefinidos."""
    for m in _RE_CNPJ.finditer(texto or ""):
        if _cnpj.somente_digitos(m.group(0)) != cnpj_digitos:
            continue
        antes = texto[max(0, m.start() - 260):m.start()]
        rot = [x for x in (_sem_acento(r.group(1)) for r in _RE_ROTULO.finditer(antes)) if x not in _NAO_PAPEL]
        if rot:
            if rot[-1] in _PASSIVO:
                return "P"
            if rot[-1] in _ATIVO:
                return "A"
    return None


_REC = {"apelante", "apelado", "apelada", "embargante", "embargado", "embargada", "agravante", "agravado", "agravada",
        "recorrente", "recorrido", "recorrida"}
_TERC = {"terceiro", "terceira", "interessado", "interessada", "litisconsorte"}
_RE_ROT2 = re.compile(r"([A-Z]+)(?:\(A\))?(?:\(S\))?\s*:")


def _dobrar(t: str) -> str:
    """Maiúsculas sem acento, mantendo o tamanho do texto (para localizar posições)."""
    import unicodedata
    return "".join((unicodedata.normalize("NFKD", c)[:1] or c) for c in (t or "")).upper()


def papel_do_nome_no_texto(texto: str, nome_norm: str) -> Optional[Dict[str, object]]:
    """Procura o nome do cliente no CORPO da comunicação (o DJEN às vezes omite, abrevia ou esconde a parte na lista de destinatários)
    e deduz o papel pelo rótulo/expressão que o antecede. Devolve {'papel': P|A|REC|T|RJ|None, 'trecho': str} ou None se o nome não consta."""
    toks = nome_norm.split()
    if not toks or not texto:
        return None
    F = _dobrar(texto)
    padrao = re.compile(r"(?<![A-Z0-9])" + r"[\W_]+".join(re.escape(t) for t in toks) + r"(?![A-Z0-9])")
    achados = []
    for m in padrao.finditer(F):
        pre, antes = F[max(0, m.start() - 45):m.start()], F[max(0, m.start() - 260):m.start()]
        if re.search(r"(EM FACE (DE|DA|DO)|CONTRA)\W*$", pre):
            papel = "P"
        elif re.search(r"(RECUPERACAO JUDICIAL|FALENCIA|RECUPERANDA)\W*(DE|DA|DO)?\W*$", pre):
            papel = "RJ"
        elif re.search(r"(INTERPOSTO|APRESENTAD[AO]) POR\W*$", pre):
            papel = "REC"   # recurso/petição incidental: quem recorre pode ser o réu da ação originária
        elif re.search(r"(PROPOST[AO]|MOVID[AO]|AJUIZAD[AO]) POR\W*$", pre):
            papel = "A"
        else:
            papel = None
            for r in _RE_ROT2.finditer(antes):
                w = r.group(1).lower()
                if w in _PASSIVO:
                    papel = "P"
                elif w in _ATIVO:
                    papel = "A"
                elif w in _REC:
                    papel = "REC"
                elif w in _TERC:
                    papel = "T"
        achados.append((papel, F[max(0, m.start() - 60):m.end() + 40].replace("\n", " ")))
    if not achados:
        return None
    for pref in ("P", "REC", "T", "RJ", None, "A"):
        for papel, trecho in achados:
            if papel == pref:
                return {"papel": papel, "trecho": " ".join(trecho.split())}
    return None


def _por_texto(cliente: Dict, comunicacao: Dict) -> Optional[Dict]:
    melhor = None
    for n in cliente["nomes"]:
        if len(n["nome_norm"].replace(" ", "")) < 4:
            continue
        r = papel_do_nome_no_texto(comunicacao.get("texto", ""), n["nome_norm"])
        if not r:
            continue
        papel, conf = r["papel"], bool(cliente.get("confirmar_por_nome_exato"))
        ev = {"nome_cliente": n["nome"], "tipo_nome": n["tipo"], "trecho": r["trecho"], "papel_no_texto": papel, "cnpj_no_texto": None}
        if papel == "P":
            res = {"nivel": 3, "classificacao": "confirmada" if conf else "provavel", "polo": "P", "nome_na_fonte": None,
                   "evidencia": {**ev, "regra": "nome do cliente no corpo da comunicação, no polo passivo (rótulo Réu/Executado… ou 'em face de'); lista de destinatários não o traz"}}
        elif papel == "A":
            res = {"nivel": 5, "classificacao": "descartada", "polo": "A", "nome_na_fonte": None,
                   "evidencia": {**ev, "regra": "nome do cliente no corpo da comunicação, como autor"}}
        else:
            res = {"nivel": 5, "classificacao": "inconclusiva", "polo": None, "nome_na_fonte": None,
                   "evidencia": {**ev, "regra": "nome do cliente citado no corpo da comunicação, papel %s: requer conferência" %
                                 {"REC": "recursal (apelante/embargante…)", "T": "de terceiro interessado", "RJ": "na recuperação judicial/falência", None: "indefinido"}[papel]}}
        if melhor is None or RANK[res["classificacao"]] > RANK[melhor["classificacao"]]:
            melhor = res
    return melhor


RANK = {"descartada": 1, "inconclusiva": 2, "provavel": 3, "confirmada": 4}


def _melhor_nome(parte: str, nomes: List[Dict[str, str]]):
    """(tipo_nome, similaridade, nome_cliente) do nome do cliente mais parecido com a parte."""
    np_ = _nomes.normalizar(parte)
    melhor = (None, 0.0, None)
    for n in nomes:
        s = 1.0 if np_ and np_ == n["nome_norm"] else _nomes.similaridade(parte, n["nome"])
        if s > melhor[1]:
            melhor = (n["tipo"], s, n["nome"])
    return melhor


def avaliar(cliente: Dict, comunicacao: Dict) -> Optional[Dict]:
    """Devolve None (sem relação) ou dict com nivel, classificacao, polo, nome_na_fonte, evidencia."""
    partes = comunicacao.get("destinatarios") or []
    cnpjs_cli = {c["cnpj"]: c["tipo"] for c in cliente["cnpjs"]}
    no_texto = cnpjs_no_texto(comunicacao.get("texto", "")) & set(cnpjs_cli)
    cnpj_hit = sorted(no_texto, key=lambda c: cnpjs_cli[c] != "matriz")[0] if no_texto else None
    nivel_cnpj = (1 if cnpjs_cli[cnpj_hit] == "matriz" else 2) if cnpj_hit else None

    cand = []  # (parte, tipo_nome, sim)
    for p in partes:
        tipo, sim, nome_cli = _melhor_nome(p.get("nome", ""), cliente["nomes"])
        if sim >= LIMIAR_APROX:
            cand.append((p, tipo, sim, nome_cli))
    # partes abreviadas por iniciais (TJSP): "T. T. M. L." = iniciais do nome do cliente; sem corroboração só pode ser 'provável'
    ini_cli = {_nomes.normalizar_iniciais(_nomes.iniciais(n["nome"])): n for n in cliente["nomes"] if _nomes.iniciais(n["nome"])}
    abrev = [p for p in partes if _nomes.e_abreviacao(p.get("nome", "")) and _nomes.normalizar_iniciais(p.get("nome", "")) in ini_cli]
    passivo = [c for c in cand if c[0].get("polo") == "P"]
    ativo = [c for c in cand if c[0].get("polo") == "A"]

    def ev(parte, tipo, sim, nome_cli, extra):
        return {"nome_na_fonte": parte.get("nome"), "polo": parte.get("polo"), "nome_cliente": nome_cli,
                "tipo_nome": tipo, "similaridade": round(sim, 3), "cnpj_no_texto": cnpj_hit, **extra}

    if passivo:
        p, tipo, sim, nome_cli = max(passivo, key=lambda c: c[2])
        if cnpj_hit:
            return {"nivel": nivel_cnpj, "classificacao": "confirmada", "polo": "P", "nome_na_fonte": p["nome"],
                    "evidencia": ev(p, tipo, sim, nome_cli, {"regra": "CNPJ no texto + parte no polo passivo com nome compatível"})}
        if sim == 1.0 and tipo in ("razao_social", "fantasia"):
            conf = bool(cliente.get("confirmar_por_nome_exato")) and tipo == "razao_social"  # nome fantasia sozinho nunca auto-confirma
            return {"nivel": 3, "classificacao": "confirmada" if conf else "provavel", "polo": "P", "nome_na_fonte": p["nome"],
                    "evidencia": ev(p, tipo, sim, nome_cli, {"regra": ("nome idêntico no polo passivo; confirmação por nome habilitada para este cliente" if conf else "nome idêntico no polo passivo, sem CNPJ na fonte")})}
        if sim == 1.0:
            return {"nivel": 4, "classificacao": "provavel", "polo": "P", "nome_na_fonte": p["nome"],
                    "evidencia": ev(p, tipo, sim, nome_cli, {"regra": "denominação anterior/alternativa idêntica no polo passivo"})}
        return {"nivel": 5, "classificacao": "inconclusiva", "polo": "P", "nome_na_fonte": p["nome"],
                "evidencia": ev(p, tipo, sim, nome_cli, {"regra": "nome aproximado no polo passivo: requer conferência"})}
    papel = papel_do_cnpj_no_texto(comunicacao.get("texto", ""), cnpj_hit) if cnpj_hit else None
    if papel == "P" and not ativo:
        return {"nivel": nivel_cnpj, "classificacao": "confirmada", "polo": "P", "nome_na_fonte": None,
                "evidencia": {"cnpj_no_texto": cnpj_hit, "regra": "CNPJ do cliente consta no texto como parte no polo passivo (rótulo Réu/Requerido/Executado…)"}}
    if papel == "A" and not passivo:
        return {"nivel": nivel_cnpj, "classificacao": "descartada", "polo": "A", "nome_na_fonte": None,
                "evidencia": {"cnpj_no_texto": cnpj_hit, "regra": "CNPJ do cliente consta no texto como autor/polo ativo"}}
    if ativo:
        p, tipo, sim, nome_cli = max(ativo, key=lambda c: c[2])
        return {"nivel": 5, "classificacao": "descartada", "polo": "A", "nome_na_fonte": p["nome"],
                "evidencia": ev(p, tipo, sim, nome_cli, {"regra": "cliente aparece como autor/polo ativo"})}
    ab_p = [p for p in abrev if p.get("polo") == "P"]
    if ab_p and not ativo:
        n = ini_cli[_nomes.normalizar_iniciais(ab_p[0]["nome"])]
        return {"nivel": 5, "classificacao": "provavel", "polo": "P", "nome_na_fonte": ab_p[0]["nome"],
                "evidencia": {"nome_cliente": n["nome"], "tipo_nome": "iniciais", "cnpj_no_texto": cnpj_hit, "papel_no_texto": None,
                              "regra": "parte publicada só com iniciais coincide com as iniciais do nome do cliente (ocorre no TJSP); iniciais podem coincidir com outra empresa: conferir"}}
    por_texto = _por_texto(cliente, comunicacao)
    if cnpj_hit:
        base = {"nivel": nivel_cnpj, "classificacao": "inconclusiva", "polo": None, "nome_na_fonte": None,
                "evidencia": {"cnpj_no_texto": cnpj_hit, "regra": "CNPJ citado no texto, mas cliente não consta entre as partes: posição desconhecida"}}
        return por_texto if por_texto and RANK[por_texto["classificacao"]] > RANK["inconclusiva"] else base
    return por_texto
