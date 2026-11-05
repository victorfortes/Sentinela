"""Banco SQLite com migrações versionadas (esquema relacional portável para PostgreSQL)."""
import sqlite3

MIGRACOES = [
    ("0001_base", """
    CREATE TABLE clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        razao_social TEXT NOT NULL,
        nome_fantasia TEXT,
        cnpj_matriz TEXT NOT NULL UNIQUE,
        grupo_economico TEXT,
        advogado_responsavel TEXT,
        equipe TEXT,
        status_monitoramento TEXT NOT NULL DEFAULT 'ativo',
        tribunais_prioritarios TEXT NOT NULL DEFAULT '[]',
        frequencia_horas INTEGER NOT NULL DEFAULT 24,
        ultima_verificacao TEXT,
        criado_em TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE cliente_cnpjs (
        cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
        cnpj TEXT NOT NULL,
        tipo TEXT NOT NULL CHECK (tipo IN ('matriz','filial')),
        PRIMARY KEY (cliente_id, cnpj)
    );
    CREATE TABLE cliente_nomes (
        cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
        nome TEXT NOT NULL,
        nome_norm TEXT NOT NULL,
        tipo TEXT NOT NULL CHECK (tipo IN ('razao_social','fantasia','anterior','alternativo')),
        PRIMARY KEY (cliente_id, nome_norm)
    );
    CREATE TABLE execucoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        gatilho TEXT NOT NULL,
        inicio TEXT NOT NULL,
        fim TEXT,
        status TEXT NOT NULL DEFAULT 'em_andamento',
        resumo TEXT
    );
    CREATE TABLE consultas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        execucao_id INTEGER REFERENCES execucoes(id),
        cliente_id INTEGER REFERENCES clientes(id),
        fonte TEXT NOT NULL,
        chave TEXT NOT NULL,
        janela_inicio TEXT,
        janela_fim TEXT,
        status TEXT NOT NULL CHECK (status IN ('ok','falha','truncada')),
        total_fonte INTEGER,
        itens_lidos INTEGER,
        erro TEXT,
        iniciada_em TEXT NOT NULL,
        concluida_em TEXT
    );
    CREATE INDEX idx_consultas_chave ON consultas(fonte, chave, janela_inicio, janela_fim, status);
    CREATE TABLE saude_conectores (
        fonte TEXT PRIMARY KEY,
        estado TEXT NOT NULL,
        ultimo_sucesso TEXT,
        ultima_falha TEXT,
        ultimo_erro TEXT,
        falhas_consecutivas INTEGER NOT NULL DEFAULT 0,
        atualizado_em TEXT NOT NULL
    );
    CREATE TABLE comunicacoes (
        id INTEGER PRIMARY KEY,
        fonte TEXT NOT NULL,
        numero_processo TEXT NOT NULL,
        tribunal TEXT,
        tipo TEXT,
        data_disponibilizacao TEXT,
        classe TEXT,
        orgao TEXT,
        link TEXT,
        texto TEXT,
        destinatarios TEXT NOT NULL,
        coletado_em TEXT NOT NULL
    );
    CREATE INDEX idx_com_proc ON comunicacoes(numero_processo);
    CREATE TABLE processos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        numero_processo TEXT NOT NULL UNIQUE,
        tribunal TEXT,
        classe TEXT,
        orgao TEXT,
        grau TEXT,
        sistema TEXT,
        assuntos TEXT,
        data_ajuizamento TEXT,
        datajud_status TEXT NOT NULL DEFAULT 'pendente',
        primeira_identificacao TEXT NOT NULL,
        ultima_verificacao TEXT NOT NULL
    );
    CREATE TABLE correspondencias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL REFERENCES clientes(id),
        processo_id INTEGER NOT NULL REFERENCES processos(id),
        comunicacao_id INTEGER REFERENCES comunicacoes(id),
        nivel INTEGER NOT NULL,
        classificacao TEXT NOT NULL CHECK (classificacao IN ('confirmada','provavel','inconclusiva','descartada')),
        polo TEXT,
        nome_na_fonte TEXT,
        evidencia TEXT NOT NULL,
        fonte TEXT NOT NULL,
        estado_validacao TEXT NOT NULL DEFAULT 'automatico',
        categoria_data TEXT NOT NULL DEFAULT 'indeterminada',
        identificada_em TEXT NOT NULL,
        ultima_verificacao TEXT NOT NULL,
        UNIQUE (cliente_id, processo_id)
    );
    """),
    ("0002_validacao", """
    ALTER TABLE clientes ADD COLUMN confirmar_por_nome_exato INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE validacoes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        correspondencia_id INTEGER NOT NULL REFERENCES correspondencias(id),
        usuario TEXT NOT NULL,
        decisao TEXT NOT NULL CHECK (decisao IN ('confirmada','descartada')),
        classificacao_anterior TEXT NOT NULL,
        justificativa TEXT,
        decidida_em TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """),
    ("0003_plataforma", """
    CREATE TABLE usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL UNIQUE,
        nome TEXT NOT NULL,
        senha_hash TEXT NOT NULL,
        papel TEXT NOT NULL CHECK (papel IN ('admin','advogado','leitura')),
        ativo INTEGER NOT NULL DEFAULT 1,
        criado_em TEXT NOT NULL DEFAULT (datetime('now'))
    );
    ALTER TABLE correspondencias ADD COLUMN responsavel_id INTEGER REFERENCES usuarios(id);
    ALTER TABLE correspondencias ADD COLUMN prioridade TEXT;
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT NOT NULL,
        params TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'na_fila' CHECK (status IN ('na_fila','executando','concluido','falhou')),
        origem TEXT NOT NULL DEFAULT 'manual',
        tentativas INTEGER NOT NULL DEFAULT 0,
        resultado TEXT,
        criado_em TEXT NOT NULL,
        iniciado_em TEXT,
        concluido_em TEXT
    );
    CREATE TABLE alertas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chave_dedup TEXT NOT NULL UNIQUE,
        tipo TEXT NOT NULL,
        canal TEXT NOT NULL,
        correspondencia_id INTEGER REFERENCES correspondencias(id),
        status TEXT NOT NULL CHECK (status IN ('enviado','falha','ignorado')),
        erro TEXT,
        payload TEXT NOT NULL,
        lido INTEGER NOT NULL DEFAULT 0,
        criado_em TEXT NOT NULL,
        enviado_em TEXT
    );
    CREATE TABLE configuracoes (chave TEXT PRIMARY KEY, valor TEXT NOT NULL, atualizado_em TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE analises (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processo_id INTEGER NOT NULL REFERENCES processos(id),
        gerada_em TEXT NOT NULL,
        modelo TEXT NOT NULL,
        campos TEXT NOT NULL,
        resumo TEXT,
        prioridade TEXT NOT NULL,
        urgencia_aparente INTEGER NOT NULL DEFAULT 0,
        requer_analise_imediata INTEGER NOT NULL DEFAULT 0,
        documentos TEXT NOT NULL
    );
    CREATE TABLE auditoria (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT,
        acao TEXT NOT NULL,
        entidade TEXT,
        entidade_id TEXT,
        detalhe TEXT,
        em TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """),
    ("0004_batimento", """
    CREATE TABLE worker_status (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        pid INTEGER,
        iniciado_em TEXT,
        ultimo_batimento TEXT,
        versao TEXT
    );
    """),
    ("0005_progresso", """
    ALTER TABLE execucoes ADD COLUMN progresso TEXT;
    """),
    ("0006_consulta_desistida", """
    ALTER TABLE consultas ADD COLUMN desistida INTEGER NOT NULL DEFAULT 0;
    """),
    ("0007_nome_ativo", """
    ALTER TABLE cliente_nomes ADD COLUMN ativo INTEGER NOT NULL DEFAULT 1;
    """),
    ("0008_cobertura", """
    CREATE TABLE controle_processos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
        numero_processo TEXT NOT NULL,
        observacao TEXT,
        criado_por TEXT,
        criado_em TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        resultado TEXT,
        detalhe TEXT,
        verificado_em TEXT,
        UNIQUE (cliente_id, numero_processo)
    );
    CREATE TABLE relatorios_cobertura (
        dia TEXT PRIMARY KEY,
        gerado_em TEXT NOT NULL,
        conteudo TEXT NOT NULL
    );
    """),
    ("0009_nexus", """
    CREATE TABLE entregas_nexus (
        correspondencia_id INTEGER PRIMARY KEY REFERENCES correspondencias(id) ON DELETE CASCADE,
        numero_processo TEXT NOT NULL,
        enviada_em TEXT NOT NULL,
        movimentacoes INTEGER NOT NULL DEFAULT 0,
        resposta TEXT NOT NULL
    );
    CREATE INDEX idx_entregas_nexus_proc ON entregas_nexus(numero_processo);
    """),
    ("0009_cadernos", """
    CREATE TABLE cadernos (
        sigla TEXT NOT NULL,
        meio TEXT NOT NULL,
        data TEXT NOT NULL,
        status TEXT NOT NULL,
        versao INTEGER,
        hash TEXT,
        total_declarado INTEGER,
        total_lido INTEGER,
        achados INTEGER NOT NULL DEFAULT 0,
        erro TEXT,
        atualizado_em TEXT NOT NULL,
        PRIMARY KEY (sigla, meio, data)
    );
    """),
    ("0010_auditoria_chamadas", """
    ALTER TABLE consultas ADD COLUMN resultado TEXT;
    ALTER TABLE consultas ADD COLUMN http_status INTEGER;
    ALTER TABLE consultas ADD COLUMN codigo_erro TEXT;
    ALTER TABLE consultas ADD COLUMN novos INTEGER;
    ALTER TABLE consultas ADD COLUMN versao_coletor TEXT;
    ALTER TABLE consultas ADD COLUMN hash_resposta TEXT;
    CREATE TABLE fonte_chamadas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fonte TEXT NOT NULL,
        tribunal TEXT,
        quando TEXT NOT NULL,
        metodo TEXT,
        consulta TEXT,
        http_status INTEGER,
        latencia_ms INTEGER,
        ok INTEGER NOT NULL,
        final INTEGER NOT NULL DEFAULT 1,
        codigo TEXT NOT NULL,
        hash_resposta TEXT,
        versao_coletor TEXT
    );
    CREATE INDEX idx_chamadas_fonte_quando ON fonte_chamadas(fonte, quando);
    CREATE INDEX idx_chamadas_quando ON fonte_chamadas(quando);
    """),
    ("0011_radar_processos", """
    CREATE TABLE processo_fontes (
        processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
        fonte TEXT NOT NULL,
        identificador TEXT,
        primeiro_visto TEXT NOT NULL,
        ultimo_visto TEXT NOT NULL,
        vezes INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (processo_id, fonte)
    );
    CREATE TABLE processo_eventos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
        cliente_id INTEGER,
        tipo TEXT NOT NULL,
        fonte TEXT,
        detalhe TEXT,
        criado_em TEXT NOT NULL
    );
    CREATE INDEX idx_eventos_processo ON processo_eventos(processo_id, id);
    CREATE TABLE processo_partes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
        nome TEXT NOT NULL,
        nome_norm TEXT NOT NULL,
        lado TEXT NOT NULL,
        fonte TEXT NOT NULL,
        UNIQUE (processo_id, nome_norm, lado)
    );
    CREATE TABLE processo_campos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        processo_id INTEGER NOT NULL REFERENCES processos(id) ON DELETE CASCADE,
        campo TEXT NOT NULL,
        valor TEXT NOT NULL,
        fonte TEXT NOT NULL,
        observado_em TEXT NOT NULL,
        UNIQUE (processo_id, campo, fonte, valor)
    );
    ALTER TABLE correspondencias ADD COLUMN confianca INTEGER;
    ALTER TABLE correspondencias ADD COLUMN status_deteccao TEXT;
    ALTER TABLE correspondencias ADD COLUMN citacao_confirmada INTEGER NOT NULL DEFAULT 0;
    CREATE TABLE adapter_saude (
        court TEXT PRIMARY KEY,
        family TEXT,
        status TEXT NOT NULL,
        detalhe TEXT,
        http_status INTEGER,
        parser_version TEXT,
        verificado_em TEXT NOT NULL
    );
    """),
    ("0012_sem_fila_humana", """
    ALTER TABLE correspondencias ADD COLUMN resolvida_em TEXT;
    ALTER TABLE controle_processos ADD COLUMN origem TEXT NOT NULL DEFAULT 'manual';
    UPDATE correspondencias SET estado_validacao='em_verificacao' WHERE estado_validacao='pendente';
    """),
]


def conectar(caminho: str = "data/sentinela.db") -> sqlite3.Connection:
    con = sqlite3.connect(caminho, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        con.execute("PRAGMA journal_mode = WAL")  # persistente; se outra conexão está trocando o modo, o resultado é o mesmo
    except sqlite3.OperationalError:
        pass
    migrar(con)
    return con


def migrar(con: sqlite3.Connection) -> None:
    """Aplica migrações pendentes dentro de uma transação exclusiva: API e worker podem subir ao mesmo tempo."""
    con.execute("CREATE TABLE IF NOT EXISTS schema_migracoes (nome TEXT PRIMARY KEY, aplicada_em TEXT NOT NULL DEFAULT (datetime('now')))")
    con.commit()
    modo = con.isolation_level
    con.isolation_level = None
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            feitas = {r[0] for r in con.execute("SELECT nome FROM schema_migracoes")}
            for nome, sql in MIGRACOES:
                if nome in feitas:
                    continue
                for stmt in sql.split(";"):
                    if stmt.strip():
                        con.execute(stmt)
                con.execute("INSERT INTO schema_migracoes(nome) VALUES (?)", (nome,))
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.isolation_level = modo


def abrir(caminho: str) -> sqlite3.Connection:
    """Conexão leve (sem migrar) para uso por requisição."""
    con = sqlite3.connect(caminho, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con
