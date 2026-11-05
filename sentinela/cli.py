"""Interface de linha de comando do motor (independente de qualquer frontend)."""
import argparse
import json
import sys
import threading
from datetime import date

from . import clientes, db
from .conectores.datajud import ConectorDataJud
from .conectores.cnpj_registro import ConectorRegistroCNPJ
from .conectores.djen import ConectorDJEN
from .http import FonteHTTP
from .motor import Motor, registrar_saude


def montar_motor(con, **kw) -> Motor:
    from . import config
    cfg = config.obter(con, "motor")
    kw.setdefault("dias_retroativos", cfg["dias_busca_inicial"])
    kw.setdefault("dias_nova", cfg["dias_nova_distribuicao"])
    lock = threading.Lock()
    # conexão PRÓPRIA para saúde/auditoria: chamadas HTTP feitas em várias threads não competem pela transação da conexão principal
    import sqlite3
    caminho = con.execute("PRAGMA database_list").fetchone()[2]
    con_log = sqlite3.connect(caminho, timeout=30, check_same_thread=False)
    con_log.row_factory = sqlite3.Row
    from . import observabilidade
    cb = lambda f, ok, e: registrar_saude(con_log, lock, f, ok, e)
    aud = lambda d: observabilidade.registrar_chamada(con_log, lock, d)
    djen = ConectorDJEN(FonteHTTP("djen", min_intervalo=1.0, ao_resultado=cb, ao_chamada=aud))
    dj = ConectorDataJud(FonteHTTP("datajud", min_intervalo=0.2, timeout=60, ao_resultado=cb, ao_chamada=aud))
    m = Motor(con, djen, dj, **kw)
    m.registro = ConectorRegistroCNPJ(FonteHTTP("cnpj_registro", min_intervalo=1.0, ao_resultado=cb, ao_chamada=aud))
    return m


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sentinela")
    ap.add_argument("--db", default="data/sentinela.db")
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("cadastrar"); c.add_argument("razao_social"); c.add_argument("cnpj")
    c.add_argument("--fantasia", default=""); c.add_argument("--filial", action="append", default=[])
    c.add_argument("--anterior", action="append", default=[]); c.add_argument("--alternativo", action="append", default=[])
    i = sp.add_parser("importar"); i.add_argument("csv")
    e = sp.add_parser("executar"); e.add_argument("--desde"); e.add_argument("--ate"); e.add_argument("--forcar", action="store_true")
    e.add_argument("--cliente", type=int, action="append"); e.add_argument("--dias-retroativos", type=int, default=None)
    sp.add_parser("enriquecer"); sp.add_parser("reavaliar"); cc = sp.add_parser("completar-clientes"); cc.add_argument("--cliente", type=int, action="append"); sp.add_parser("worker"); sp.add_parser("api")
    r = sp.add_parser("redefinir-senha"); r.add_argument("email")
    u = sp.add_parser("criar-usuario"); u.add_argument("email"); u.add_argument("nome"); u.add_argument("--papel", default="advogado", choices=["admin","advogado","leitura"])
    sp.add_parser("clientes"); sp.add_parser("saude"); sp.add_parser("execucoes")
    p = sp.add_parser("processos"); p.add_argument("--classificacao")
    n = sp.add_parser("nexus-enviar", help="entrega ao NGA Nexus o que foi confirmado")
    n.add_argument("--url", default=None, help="base do Nexus; padrão: variável NEXUS_URL")
    n.add_argument("--incluir-automaticas", action="store_true",
                   help="envia também o que ainda não foi validado por pessoa")
    n.add_argument("--incluir-provaveis", action="store_true",
                   help="envia também correspondência apenas provável")
    n.add_argument("--limite", type=int, default=50)
    n.add_argument("--simular", action="store_true", help="mostra o lote sem enviar nada")
    a = ap.parse_args(argv)
    con = db.conectar(a.db)
    if a.cmd == "cadastrar":
        print(clientes.cadastrar(con, a.razao_social, a.cnpj, a.fantasia, a.filial, a.anterior, a.alternativo))
    elif a.cmd == "importar":
        print(json.dumps(clientes.importar_csv(con, a.csv), ensure_ascii=False, indent=1))
    elif a.cmd == "executar":
        m = montar_motor(con, **({"dias_retroativos": a.dias_retroativos} if a.dias_retroativos else {}))
        r = m.executar("manual", a.cliente, date.fromisoformat(a.desde) if a.desde else None,
                       date.fromisoformat(a.ate) if a.ate else None, a.forcar)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r["status"] == "sucesso" else 2
    elif a.cmd == "criar-usuario":
        import getpass, os
        from . import seguranca
        senha = os.environ.get("SENTINELA_NOVA_SENHA") or getpass.getpass("Senha: ")
        if len(senha) < 10:
            print("senha deve ter ao menos 10 caracteres"); return 1
        con.execute("INSERT INTO usuarios(email,nome,senha_hash,papel) VALUES (?,?,?,?)", (a.email, a.nome, seguranca.hash_senha(senha), a.papel))
        con.commit(); print("usuário criado")
    elif a.cmd == "redefinir-senha":
        import getpass, os
        from . import seguranca
        senha = os.environ.get("SENTINELA_NOVA_SENHA") or getpass.getpass("Nova senha: ")
        if len(senha) < 10:
            print("senha deve ter ao menos 10 caracteres"); return 1
        n = con.execute("UPDATE usuarios SET senha_hash=? WHERE email=?", (seguranca.hash_senha(senha), a.email.lower())).rowcount
        con.commit(); print("senha redefinida" if n else "usuário não encontrado")
    elif a.cmd == "worker":
        from . import fila, analise
        fila.laco_worker(con, montar_motor(con), analise.ia_do_ambiente())
    elif a.cmd == "api":
        import uvicorn, os
        os.environ["SENTINELA_DB"] = a.db
        uvicorn.run("sentinela.api:app", host=os.environ.get("SENTINELA_HOST", "127.0.0.1"), port=int(os.environ.get("SENTINELA_PORT", "8000")))
    elif a.cmd == "completar-clientes":
        from . import clientes as _cl, fila
        m = montar_motor(con)
        for cid in a.cliente or [c["id"] for c in _cl.ativos(con)]:
            print(_cl.completar_nomes_pelo_registro(con, cid, m.registro))
    elif a.cmd == "reavaliar":
        print(montar_motor(con).reavaliar_base())
    elif a.cmd == "enriquecer":
        print(montar_motor(con, limite_enriquecimento=100000).enriquecer_pendentes())
    elif a.cmd == "clientes":
        for r in con.execute("SELECT id,razao_social,cnpj_matriz,status_monitoramento,ultima_verificacao FROM clientes"):
            print(tuple(r))
    elif a.cmd == "saude":
        for r in con.execute("SELECT * FROM saude_conectores"):
            print(dict(r))
    elif a.cmd == "execucoes":
        for r in con.execute("SELECT * FROM execucoes ORDER BY id DESC LIMIT 20"):
            print(dict(r))
    elif a.cmd == "nexus-enviar":
        import os
        from . import nexus
        # O token vem SÓ do ambiente, nunca de flag: argumento de linha de
        # comando aparece em `ps` e fica no histórico do shell.
        token = os.environ.get("NEXUS_TOKEN", "")
        url = a.url or os.environ.get("NEXUS_URL", "")
        if not url:
            print("informe --url ou a variável NEXUS_URL"); return 1
        if not a.simular and not token:
            print("defina NEXUS_TOKEN com a credencial de integração"); return 1
        try:
            r = nexus.enviar(con, url, token, a.incluir_automaticas, a.incluir_provaveis,
                             a.limite, a.simular)
        except Exception as e:
            print("falha ao entregar: %s" % e); return 2
        print(json.dumps(r, ensure_ascii=False, indent=1))
        # Afrouxar o filtro é escolha de quem chamou, e o aviso registra isso
        # na saída do comando — que costuma ir para log de cron.
        if r["automaticas_incluidas"]:
            print("ATENÇÃO: %d correspondência(s) sem validação humana foram enviadas."
                  % r["automaticas_incluidas"], file=sys.stderr)
        return 0
    elif a.cmd == "processos":
        q = ("SELECT p.numero_processo,p.tribunal,p.data_ajuizamento,c.classificacao,c.nivel,c.polo,c.categoria_data,"
             "c.estado_validacao,cl.razao_social FROM correspondencias c JOIN processos p ON p.id=c.processo_id "
             "JOIN clientes cl ON cl.id=c.cliente_id")
        args = ()
        if a.classificacao:
            q += " WHERE c.classificacao=?"; args = (a.classificacao,)
        for r in con.execute(q + " ORDER BY c.identificada_em DESC", args):
            print(tuple(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
