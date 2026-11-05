/* NGA Sentinela — painel. JS puro, sem build (ambiente sem Node). Todo texto vindo da API passa por esc(). */
"use strict";
const $app = document.getElementById("app");
let eu = null;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const tok = () => { try { return localStorage.getItem("ngs_tok") || ""; } catch (e) { return ""; } };
const fmtData = (s) => (s ? String(s).replace("T", " ").slice(0, 16) : "—");
const fmtDia = (s) => (s ? String(s).slice(0, 10).split("-").reverse().join("/") : "—");
const brl = (v) => (v == null ? "não disponível" : v.toLocaleString("pt-BR", { style: "currency", currency: "BRL" }));
const ROT = { confirmada: "Confirmada", provavel: "Provável", inconclusiva: "Inconclusiva", descartada: "Descartada",
  nova_distribuicao: "Nova distribuição", processo_antigo: "Processo antigo", indeterminada: "Data indeterminada",
  automatico: "Automático", pendente: "Pendente", confirmado_manual: "Confirmado manualmente", descartado_manual: "Descartado manualmente", descartado_auto: "Descartado (auto)",
  alta: "Alta", media: "Média", baixa: "Baixa", na_fila: "Na fila", executando: "Executando", concluido: "Concluído", falhou: "Falhou" };
const tag = (v, extra) => `<span class="tag ${esc(v)}">${esc(ROT[v] || v || "—")}${extra ? " " + esc(extra) : ""}</span>`;
const pode = (min) => ({ admin: 3, advogado: 2, leitura: 1 }[eu?.papel] || 0) >= { admin: 3, advogado: 2, leitura: 1 }[min];

async function api(url, opt = {}) {
  const h = { Authorization: "Bearer " + tok() };
  if (opt.body && !(opt.body instanceof FormData)) { h["Content-Type"] = "application/json"; opt.body = JSON.stringify(opt.body); }
  const r = await fetch("/api" + url, { ...opt, headers: h });
  if (r.status === 401 && url !== "/login") { sair(); throw new Error("sessão expirada"); }
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof d.detail === "string" ? d.detail : JSON.stringify(d.detail || d));
  return d;
}
function sair() { try { localStorage.removeItem("ngs_tok"); } catch (e) {} eu = null; renderLogin(); }
function aviso(msg) { alert(msg); }

/* ---------- login ---------- */
function renderLogin(erro) {
  document.title = "NGA Sentinela";
  $app.innerHTML = `<div class="auth"><div class="auth-brand"><img src="/static/logo.png" alt="NGA ADV"><p class="tag2">NGA LAB // INOVAÇÃO E IA</p>
    <h1>Nenhuma ação<br>passa <em>despercebida.</em></h1><p class="intro">Monitoramento diário de novas ações judiciais contra os clientes do escritório.<br>Alerta no dia, prova da origem, trilha de auditoria.</p></div>
    <form class="auth-card" id="f"><span class="eyebrow">ACESSO RESTRITO</span><h2>Entrar no Sentinela</h2>
    <label>E-mail</label><input name="email" type="email" required autocomplete="username"><label>Senha</label><input name="senha" type="password" required autocomplete="current-password">
    <div class="erro">${esc(erro || "")}</div><button>Entrar</button></form></div>`;
  document.getElementById("f").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const d = await api("/login", { method: "POST", body: { email: f.get("email"), senha: f.get("senha") } });
      try { localStorage.setItem("ngs_tok", d.token); } catch (x) {}
      eu = d.usuario; rotear();
    } catch (x) { renderLogin(x.message); }
  };
}

/* ---------- estrutura ---------- */
const MENU = [["dashboard", "Dashboard"], ["clientes", "Clientes"], ["processos", "Novos processos"], ["cobertura", "Garantia de cobertura"], ["validacao", "Central de validação"], ["fontes", "Monitoramento de fontes"], ["config", "Configurações"]];
let _topoTimer = null;
function moldura(ativo, html) {
  const nome = (MENU.find((m) => m[0] === ativo) || ["", "Processo"])[1];
  $app.innerHTML = `<aside><a class="wordmark" href="#/dashboard"><img src="/static/logo.png" alt="NGA ADV"><span>SENTINELA</span></a>
    <div class="labmark">NGA <span>LAB</span><small>MONITORAMENTO JUDICIAL</small></div><div class="navcaption">NAVEGAÇÃO</div>
    <nav>${MENU.map(([k, t]) => `<a href="#/${k}" class="${ativo === k ? "ativo" : ""}">${t}${k === "processos" ? `<span class="pulso" id="navhoje" hidden></span>` : ""}</a>`).join("")}</nav>
    <div class="account"><div class="avatar">${esc((eu.nome || "?")[0])}</div><div><strong>${esc(eu.nome)}</strong><small>${esc(eu.papel)}</small></div><button id="sair">Sair</button></div></aside>
    <div class="shell"><header class="topo"><div>NGA LAB<span class="sep">/</span>SENTINELA<span class="sep">/</span><b>${esc(nome.toUpperCase())}</b></div><div class="status" id="topostatus"><i></i><span>verificando worker…</span></div></header>
    <main>${html}</main></div>`;
  document.getElementById("sair").onclick = sair;
  atualizarTopo();
  if (!_topoTimer) _topoTimer = setInterval(atualizarTopo, 30000);
}
async function atualizarTopo() {
  try {
    const [s, h] = await Promise.all([api("/sistema"), api("/processos?ajuizado_hoje=1&limite=1")]);
    const el = document.getElementById("topostatus");
    if (el) {
      const w = s.worker;
      el.className = "status" + (w.ativo ? "" : " off");
      el.innerHTML = w.ativo ? `<i></i><span>WORKER ATIVO · batimento há ${w.segundos_desde_batimento}s · próxima pesquisa ${esc(fmtData(s.agendamento.proxima_execucao))}</span>` : `<i></i><span>WORKER PARADO — nenhuma pesquisa automática está ocorrendo</span>`;
    }
    const n = document.getElementById("navhoje");
    if (n) { n.hidden = !h.total; n.textContent = h.total; }
    document.title = (h.total ? `(${h.total}) ` : "") + "NGA Sentinela";
  } catch (e) { /* topo é informativo */ }
}
const carregando = (a) => moldura(a, `<p class="mudo">Carregando…</p>`);
const hero = (eb, tit, sub, acao) => `<div class="hero"><div><span class="eyebrow">${eb}</span><h1>${tit}</h1>${sub ? `<p class="sub">${sub}</p>` : ""}</div>${acao || ""}</div>`;
const falhou = (a, e) => moldura(a, `<div class="faixa falha_total">Erro: ${esc(e.message)}</div>`);

/* ---------- dashboard ---------- */
let _graf = "dias";
function graficoBarras(d) {
  const dias = _graf === "dias";
  const serie = dias ? d.serie_dias.map((x) => ({ r: x.dia.slice(8) + "/" + x.dia.slice(5, 7), n: x.n, hoje: x.dia === d.hoje_iso, t: fmtDia(x.dia) }))
    : d.serie_meses.map((x) => ({ r: ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"][+x.mes.slice(5) - 1], n: x.n, hoje: x.mes === d.hoje_iso.slice(0, 7), t: x.mes.slice(5) + "/" + x.mes.slice(0, 4) }));
  const max = Math.max(1, ...serie.map((x) => x.n));
  const total = serie.reduce((a, x) => a + x.n, 0);
  return `<div class="graf-cab"><div class="abas"><button class="${dias ? "on" : ""}" data-g="dias">Últimos 30 dias</button><button class="${dias ? "" : "on"}" data-g="meses">Últimos 12 meses</button></div>
      <span class="mudo peq"><b>${total}</b> ação(ões) ajuizada(s) neste período · a barra <span class="amb">laranja</span> é ${dias ? "hoje" : "o mês atual"}</span></div>
    <div class="barras ${dias ? "dias" : "meses"}">${serie.map((x, i) => `<div class="col${x.hoje ? " agora" : ""}" title="${esc(x.t)}: ${x.n} ação(ões) ajuizada(s)">
      <span class="v">${x.n || ""}</span><div class="b" style="height:${x.n ? Math.max(6, Math.round(100 * x.n / max)) : 2}%"></div>
      <span class="r">${!dias || i % 5 === 0 || x.hoje ? esc(x.r) : ""}</span></div>`).join("")}</div>
    ${total ? "" : `<p class="mudo peq" style="margin:12px 0 0">Nenhuma ação ajuizada neste período entre as encontradas.</p>`}`;
}
async function pgDashboard() {
  carregando("dashboard");
  try {
    const [d, al, sis] = await Promise.all([api("/dashboard"), api("/alertas"), api("/sistema")]);
    d.hoje_iso = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
    const k = (n, t, extra, cls) => `<article class="${cls || ""}"><span>${esc(t)}</span><strong>${esc(n)}</strong><small>${esc(extra || "")}</small></article>`;
    const ue = d.ultima_execucao, hj = d.ajuizados_hoje, w = sis.worker;
    const soma = (n) => d.serie_dias.slice(-n).reduce((a, x) => a + x.n, 0);
    const nHoje = d.identificados_hoje;
    const blocoHoje = hj.length ? `<section class="hoje"><div class="cab"><div class="num">${hj.length}</div><div class="tit"><small>AGORA — AJUIZADAS HOJE, PRECISAM SER VISTAS</small>${hj.length === 1 ? "Nova ação ajuizada hoje" : "Novas ações ajuizadas hoje"}</div></div>
        <div class="lista">${hj.map((i) => `<a class="item" href="#/processo/${i.id}"><b>${esc(i.numero_processo)}</b> ${tag(i.classificacao)} ${i.prioridade ? tag(i.prioridade) : ""}<span>${esc(i.cliente)} · ${esc(i.tribunal)}${i.classe ? " · " + esc(i.classe) : ""}</span></a>`).join("")}</div></section>`
      : `<section class="hoje vazio"><div class="cab"><div class="num">0</div><div class="tit"><small>AGORA — AJUIZADAS HOJE</small>${d.cobertura.completa ? "Nenhuma ação nova identificada hoje" : "Nenhuma ação identificada — cobertura INCOMPLETA"}</div></div>
        <p class="mudo peq" style="margin:12px 0 0">${esc(d.frase_sem_novos)}<br>${d.ajuizados_ontem ? `Ontem foram ${d.ajuizados_ontem}. ` : ""}Os tribunais costumam registrar uma ação horas ou dias depois do protocolo; esta faixa acende sozinha quando uma aparecer.</p></section>`;
    const cad = sis.dias.map((x) => `<div class="${esc(x.resultado)}" title="${esc(x.dia)}: ${esc(x.resultado)} (${x.execucoes} execução(ões))"><b>${x.resultado === "sucesso" ? "✓" : x.resultado === "sem_execucao" ? "–" : "!"}</b>${esc(x.dia.slice(8))}/${esc(x.dia.slice(5, 7))}</div>`).join("");
    const maxc = Math.max(1, ...d.por_cliente.map((c) => c.total));
    const porCliente = d.por_cliente.length ? d.por_cliente.map((c) => `<a class="cli" href="#/processos" data-c="${c.id}">
        <div class="nome"><b>${esc(c.razao_social)}</b><small>${c.ultima ? "última ação ajuizada em " + esc(fmtDia(c.ultima)) : "nenhuma ação encontrada ainda"}</small></div>
        <div class="trilho"><div style="width:${Math.round(100 * c.total / maxc)}%"></div></div>
        <div class="nums"><span class="${c.hoje ? "alerta" : ""}"><b>${c.hoje}</b>hoje</span><span><b>${c.ult30}</b>30 dias</span><span><b>${c.total}</b>no total</span></div></a>`).join("")
      : `<p class="mudo">Nenhum cliente ativo. Cadastre em “Clientes”.</p>`;
    const fazer = [];
    if (nHoje) fazer.push(["urgente", `Veja as ${nHoje} ação(ões) ajuizada(s) hoje`, "Abra a lista, leia a ação e atribua a um advogado.", "#/processos", "ver_hoje"]);
    if (d.pendentes_validacao) fazer.push(["atencao", `${d.pendentes_validacao} caso(s) esperam sua conferência`, "O sistema não teve certeza; confirme ou descarte.", "#/validacao"]);
    if (d.consultas_a_reprocessar) fazer.push(["info", "Algumas consultas falharam", "O sistema tenta de novo sozinho a cada 30 minutos. Nada a fazer.", ""]);
    (d.clientes_so_cnpj || []).forEach((c) => fazer.push(["info", `${c.razao_social}: nome curto demais`, "Pesquisado só pelo CNPJ (pelo nome viria milhares de homônimos). Cadastre o nome completo ou alternativos para ampliar.", ""]));
    if (!d.cobertura.completa) fazer.push(["atencao", "Cobertura do monitoramento incompleta", d.cobertura.resumo + ". Veja detalhes em “Garantia de cobertura”.", "#/cobertura"]);
    if (!w.ativo) fazer.push(["urgente", "O robô de pesquisa está parado", "Nenhuma pesquisa automática ocorre até ele voltar (Mac desligado ou reiniciado).", "#/fontes"]);
    if (!fazer.length) fazer.push(["ok", "Nada pendente para você agora", "Quando algo precisar de atenção, aparece aqui.", ""]);
    const onde = d.estado_pesquisa.codigo, cobOk = d.cobertura.completa;
    const sem = onde === "falha_total" || !w.ativo ? "vermelho" : (onde === "falha_parcial" || !cobOk) ? "amarelo" : "verde";
    const semTxt = sem === "verde" ? `Tudo certo: ${d.cobertura.operacionais}/${d.cobertura.esperadas} fontes operacionais e a última pesquisa terminou sem problemas.` : sem === "amarelo" ? `Atenção — cobertura incompleta: ${d.cobertura.resumo}. O sistema tenta de novo sozinho.` : "Problema: a pesquisa não está funcionando direito. Avise o responsável técnico.";
    moldura("dashboard", hero("VISÃO GERAL", "Ações contra seus <em>clientes.</em>", `Última pesquisa: ${ue ? `${fmtData(ue.inicio)}` : "nenhuma"} · próxima automática: ${esc(fmtData(sis.agendamento.proxima_execucao))}`,
        pode("advogado") ? `<button id="rodar">Executar pesquisa agora</button>` : "") +
      `<div class="semaforo ${sem}"><i></i><span>${esc(semTxt)}</span></div>
      <div id="progresso" class="progresso" hidden></div>` + blocoHoje +
      `<h2>Quanto foi ajuizado</h2>
      <p class="mudo peq" style="margin:-6px 0 14px">“Ajuizada” = a data em que o processo foi protocolado no tribunal. Só entram ações contra seus clientes.</p>
      <div class="summarygrid placar">${k(nHoje, "Hoje", "ajuizadas hoje", "highlight")}${k(d.ajuizados_ontem, "Ontem", "ajuizadas ontem")}${k(soma(7), "Últimos 7 dias", "ajuizadas na semana")}${k(soma(30), "Últimos 30 dias", "ajuizadas no mês")}${k(d.ajuizadas_total, "Desde sempre", "todas que o sistema encontrou")}</div>
      <div class="panel" id="grafico">${graficoBarras(d)}</div>
      ${d.sem_data_ajuizamento ? `<p class="mudo peq">${d.sem_data_ajuizamento} processo(s) ainda sem data de ajuizamento (aguardando o DataJud) não aparecem nos números acima.</p>` : ""}
      <h2>Por cliente</h2><div class="panel clientes-lista">${porCliente}</div>
      <h2>O que preciso fazer</h2><div class="fazer">${fazer.map(([c, t, sub, href]) => `<a class="${c}" ${href ? `href="${href}"` : ""}><b>${esc(t)}</b><span>${esc(sub)}</span></a>`).join("")}</div>
      <details class="ajuda"><summary>Como ler este painel</summary><ul>
        <li><b>Ajuizada</b>: dia em que a ação foi protocolada no tribunal. É o que importa para saber se é nova.</li>
        <li><b>Identificada</b>: dia em que o Sentinela a encontrou. Pode ser depois do ajuizamento, pois os tribunais demoram a publicar.</li>
        <li><b>Confirmada</b>: o cliente está como réu (polo passivo) e o CNPJ ou o nome batem.</li>
        <li><b>Barra laranja / faixa “Agora”</b>: ações ajuizadas hoje, que o advogado precisa ver.</li>
        <li><b>Semáforo</b>: verde = pesquisando normalmente; amarelo = falhas que serão refeitas; vermelho = parado.</li></ul></details>
      <details class="ajuda"><summary>Detalhes técnicos (para o responsável pelo sistema)</summary>
      <div class="summarygrid">${k(d.clientes_monitorados, "Clientes monitorados")}${k(d.novas_acoes_confirmadas, "Novas ações confirmadas", "confirmadas e ajuizadas recentemente")}${k(d.pendentes_validacao, "Pendentes de validação")}
      ${k(d.falhas_consultas_24h, "Falhas de consulta (24 h)")}${k(d.consultas_a_reprocessar, "Consultas a reprocessar")}${k(d.tribunais_com_dados, "Tribunais com dados recebidos")}</div>
      <p class="mudo peq">${esc(d.nota_tribunais)}</p>
      <div class="panel"><div class="lin" style="gap:26px;align-items:flex-start">
        <div><h3>Worker</h3><span class="tag ${w.ativo ? "operacional" : "falha"}">${w.ativo ? "ATIVO" : "PARADO"}</span>
          <div class="peq mudo" style="margin-top:8px">Último batimento: ${esc(fmtData(w.ultimo_batimento))}${w.segundos_desde_batimento != null ? ` (há ${w.segundos_desde_batimento}s)` : ""}<br>Rodando desde: ${esc(fmtData(w.iniciado_em))}<br>Jobs na fila: ${sis.jobs_na_fila}</div></div>
        <div style="flex:1;min-width:300px"><h3>Últimos 14 dias de pesquisa automática</h3><div class="cadencia">${cad}</div>
          <p class="peq mudo" style="margin:10px 0 0">✓ pesquisa concluída · ! falha/parcial · – nenhuma execução (Mac desligado ou worker parado).</p></div></div>
        ${sis.aviso ? `<div class="faixa falha_total" style="margin:14px 0 0">${esc(sis.aviso)}</div>` : ""}</div>
      <h3>Alertas recentes</h3>${tabelaAlertas(al.slice(0, 8))}</details>`);
    const b = document.getElementById("rodar");
    if (b) b.onclick = async () => { try { b.disabled = true; await api("/jobs", { method: "POST", body: { tipo: "executar", params: {} } }); acompanhar(); } catch (e) { b.disabled = false; aviso(e.message); } };
    document.querySelectorAll("#grafico [data-g]").forEach((x) => (x.onclick = () => { _graf = x.dataset.g; document.getElementById("grafico").innerHTML = graficoBarras(d); pgDashboardLigarGrafico(d); }));
    document.querySelectorAll(".fazer a[href='#/processos']").forEach((x) => (x.onclick = () => { filtros.ajuizado_hoje = "1"; }));
    document.querySelectorAll(".cli[data-c]").forEach((x) => (x.onclick = () => { filtros.cliente_id = x.dataset.c; filtros.ajuizado_hoje = ""; }));
    acompanhar(true);
  } catch (e) { falhou("dashboard", e); }
}
function pgDashboardLigarGrafico(d) {
  document.querySelectorAll("#grafico [data-g]").forEach((x) => (x.onclick = () => { _graf = x.dataset.g; document.getElementById("grafico").innerHTML = graficoBarras(d); pgDashboardLigarGrafico(d); }));
}
/* barra de progresso da pesquisa */
let _poll = null;
function acompanhar(soSeAtiva) {
  clearTimeout(_poll);
  const caixa = document.getElementById("progresso");
  if (!caixa) return;
  const ciclo = async () => {
    if (!document.getElementById("progresso")) return;  // saiu do painel
    let p;
    try { p = await api("/progresso"); } catch (e) { _poll = setTimeout(ciclo, 4000); return; }
    const ativo = p.estado === "na_fila" || p.estado === "executando";
    if (p.estado === "ocioso") { caixa.hidden = true; caixa.dataset.fim = ""; return; }
    const r = p.resumo || {};
    const det = p.estado === "concluido" ? `${r.correspondencias_novas || 0} correspondência(s) nova(s) · ${r.consultas_executadas || 0} consulta(s) · ${r.consultas_com_falha || 0} falha(s)` : "";
    caixa.hidden = false;
    caixa.className = "progresso " + p.estado;
    caixa.innerHTML = `<div class="cab"><b>${p.estado === "concluido" ? "PESQUISA CONCLUÍDA" : p.estado === "falhou" ? "PESQUISA FALHOU" : p.estado === "na_fila" ? "NA FILA" : "PESQUISANDO"}</b><span>${Math.round(p.pct)}%</span></div>
      <div class="trilho"><div class="enche" style="width:${p.pct}%"></div></div>
      <div class="det">${esc(p.texto || "")}${det ? "<br>" + esc(det) : ""}${p.inicio && ativo ? ` · iniciada às ${esc(fmtData(p.inicio).slice(-5))}` : ""}</div>`;
    if (ativo) { caixa.dataset.fim = ""; _poll = setTimeout(ciclo, 1500); }
    else if (!caixa.dataset.fim) { caixa.dataset.fim = "1"; const bt = document.getElementById("rodar"); if (bt) bt.disabled = false; if (!soSeAtiva) setTimeout(pgDashboard, 2500); }
    else { const bt = document.getElementById("rodar"); if (bt) bt.disabled = false; }
  };
  ciclo();
}
function tabelaAlertas(al) {
  if (!al.length) return `<p class="mudo">Nenhum alerta gerado ainda.</p>`;
  return `<div class="tabela-rolar"><table><thead><tr><th>Data</th><th>Tipo</th><th>Canal</th><th>Cliente / processo</th><th>Envio</th></tr></thead><tbody>${al.map((a) => `<tr>
    <td>${fmtData(a.criado_em)}</td><td>${esc({ nova_acao_confirmada: "Nova ação confirmada", conferencia_pendente: "Conferência pendente", falha_consulta: "Falha de consulta" }[a.tipo] || a.tipo)}</td>
    <td>${esc(a.canal)}</td><td>${esc(a.payload.cliente || "")}${a.payload.numero_processo ? "<br><span class='mudo peq'>" + esc(a.payload.numero_processo) + "</span>" : ""}</td><td>${tag(a.status)}${a.erro ? "<br><span class='peq mudo'>" + esc(a.erro) + "</span>" : ""}</td></tr>`).join("")}</tbody></table></div>`;
}

/* ---------- clientes ---------- */
async function pgClientes() {
  carregando("clientes");
  try {
    const cs = await api("/clientes");
    moldura("clientes", `<div class="cabeca"><div><h1>Clientes</h1><div class="mudo peq">${cs.length} cadastrado(s)</div></div>
      <div class="lin">${pode("advogado") ? `<button id="novo">Novo cliente</button><label class="btn sec" style="margin:0">Importar CSV/Excel<input id="imp" type="file" accept=".csv,.xlsx" hidden></label>` : ""}</div></div>
      <div class="tabela-rolar"><table><thead><tr><th>Razão social</th><th>CNPJ matriz</th><th>CNPJs</th><th>Responsável</th><th>Monitoramento</th><th>Processos</th><th>Última verificação</th></tr></thead><tbody>
      ${cs.map((c) => `<tr class="clic" data-id="${c.id}"><td>${esc(c.razao_social)}</td><td>${esc(fmtCnpj(c.cnpj_matriz))}</td><td>${c.qtd_cnpjs}</td><td>${esc(c.advogado_responsavel || "—")}</td>
      <td>${tag(c.status_monitoramento === "ativo" ? "operacional" : "pendente", c.status_monitoramento)}</td><td>${c.processos}</td><td>${fmtData(c.ultima_verificacao)}</td></tr>`).join("") || `<tr><td colspan="7" class="mudo">Nenhum cliente cadastrado.</td></tr>`}</tbody></table></div>
      <p class="mudo peq">CSV/Excel: colunas razao_social, cnpj_matriz, nome_fantasia, filiais, anteriores, alternativos (listas separadas por ;), grupo_economico, advogado_responsavel, equipe.</p>`);
    document.querySelectorAll("tr.clic").forEach((tr) => (tr.onclick = () => (location.hash = "#/cliente/" + tr.dataset.id)));
    const n = document.getElementById("novo"); if (n) n.onclick = modalCliente;
    const imp = document.getElementById("imp");
    if (imp) imp.onchange = async () => { const fd = new FormData(); fd.append("arquivo", imp.files[0]); try { const r = await api("/clientes/importar", { method: "POST", body: fd }); aviso(`Importados: ${r.importados}\nErros: ${r.erros.length}${r.erros.slice(0, 8).map((e) => `\nlinha ${e.linha}: ${e.erro}`).join("")}`); pgClientes(); } catch (e) { aviso(e.message); } };
  } catch (e) { falhou("clientes", e); }
}
const fmtCnpj = (d) => (d && d.length === 14 ? `${d.slice(0, 2)}.${d.slice(2, 5)}.${d.slice(5, 8)}/${d.slice(8, 12)}-${d.slice(12)}` : d || "");
const lista = (s) => (s || "").split(/[;\n]/).map((x) => x.trim()).filter(Boolean);
function modalCliente() {
  const m = document.createElement("div"); m.className = "modal";
  m.innerHTML = `<div><h2 style="margin-top:0">Novo cliente</h2><form id="fc" class="form2">
    <div class="cheio"><label>Razão social *</label><input name="razao_social" required></div>
    <div><label>CNPJ matriz *</label><input name="cnpj_matriz" required placeholder="00.000.000/0000-00"></div><div><label>Nome fantasia</label><input name="nome_fantasia"></div>
    <div class="cheio"><label>CNPJs de filiais (separe por ;)</label><input name="filiais"></div>
    <div class="cheio"><label>Denominações anteriores (separe por ;)</label><input name="anteriores"></div>
    <div class="cheio"><label>Nomes alternativos (separe por ;)</label><input name="alternativos"></div>
    <div><label>Grupo econômico</label><input name="grupo_economico"></div><div><label>Advogado responsável</label><input name="advogado_responsavel"></div>
    <div><label>Equipe</label><input name="equipe"></div><div><label>Frequência (horas)</label><input name="frequencia_horas" type="number" value="24" min="1"></div>
    <div class="cheio erro" id="er"></div><div class="cheio lin"><button>Salvar</button><button type="button" class="sec" id="x">Cancelar</button></div></form></div>`;
  document.body.appendChild(m);
  m.querySelector("#x").onclick = () => m.remove();
  m.querySelector("#fc").onsubmit = async (e) => {
    e.preventDefault(); const f = Object.fromEntries(new FormData(e.target));
    try {
      await api("/clientes", { method: "POST", body: { razao_social: f.razao_social, cnpj_matriz: f.cnpj_matriz, nome_fantasia: f.nome_fantasia, filiais: lista(f.filiais), anteriores: lista(f.anteriores), alternativos: lista(f.alternativos),
        grupo_economico: f.grupo_economico || null, advogado_responsavel: f.advogado_responsavel || null, equipe: f.equipe || null, frequencia_horas: +f.frequencia_horas || 24 } });
      m.remove(); pgClientes();
    } catch (x) { m.querySelector("#er").textContent = x.message; }
  };
}
async function pgCliente(id) {
  carregando("clientes");
  try {
    const [c, p] = await Promise.all([api("/clientes/" + id), api("/processos?cliente_id=" + id + "&limite=50")]);
    const trib = JSON.parse(c.tribunais_prioritarios || "[]");
    moldura("clientes", `<div class="cabeca"><div><a href="#/clientes">← Clientes</a><h1>${esc(c.razao_social)}</h1><div class="mudo peq">Última verificação: ${fmtData(c.ultima_verificacao)}</div></div></div>
      <div class="cartao"><h3>Identificação</h3><dl class="dl"><dt>CNPJs vinculados</dt><dd>${c.cnpjs.map((x) => `${esc(fmtCnpj(x.cnpj))} <span class="mudo peq">(${esc(x.tipo)})</span>`).join("<br>")}</dd>
      <dt>Nomes monitorados</dt><dd>${c.nomes.map((x) => `${esc(x.nome)} <span class="mudo peq">(${esc(x.tipo)})</span>`).join("<br>")}</dd>
      <dt>Grupo econômico</dt><dd>${esc(c.grupo_economico || "—")}</dd><dt>Responsável / equipe</dt><dd>${esc(c.advogado_responsavel || "—")} / ${esc(c.equipe || "—")}</dd>
      <dt>Tribunais prioritários</dt><dd>${esc(trib.join(", ") || "—")}</dd></dl></div>
      ${pode("advogado") ? `<div class="cartao"><h3>Configuração do monitoramento</h3><form id="fe" class="form2">
      <div><label>Status</label><select name="status_monitoramento"><option value="ativo" ${c.status_monitoramento === "ativo" ? "selected" : ""}>Ativo</option><option value="pausado" ${c.status_monitoramento === "pausado" ? "selected" : ""}>Pausado</option></select></div>
      <div><label>Frequência mínima (horas)</label><input name="frequencia_horas" type="number" min="1" value="${c.frequencia_horas}"></div>
      <div><label>Tribunais prioritários (;)</label><input name="tribunais" value="${esc(trib.join(";"))}"></div><div><label>Advogado responsável</label><input name="advogado_responsavel" value="${esc(c.advogado_responsavel || "")}"></div>
      <div class="cheio"><label>Adicionar filiais (CNPJs ;)</label><input name="fil"></div><div class="cheio"><label>Adicionar denominações anteriores (;)</label><input name="ant"></div><div class="cheio"><label>Adicionar nomes alternativos (;)</label><input name="alt"></div>
      <div class="cheio"><label style="display:flex;gap:8px;align-items:center;color:inherit"><input type="checkbox" name="cne" style="width:auto" ${c.confirmar_por_nome_exato ? "checked" : ""}> Confirmar automaticamente quando o nome for idêntico no polo passivo (sem CNPJ na fonte). Use só para nomes distintivos: aumenta o risco de homônimos.</label></div>
      <div class="cheio erro" id="er"></div><div class="cheio"><button>Salvar</button></div></form></div>` : ""}
      <h2>Histórico de processos (${p.total})</h2>${tabelaProcessos(p.itens)}`);
    bindProcessos();
    const fe = document.getElementById("fe");
    if (fe) fe.onsubmit = async (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target));
      try { await api("/clientes/" + id, { method: "PUT", body: { status_monitoramento: f.status_monitoramento, frequencia_horas: +f.frequencia_horas, tribunais_prioritarios: lista(f.tribunais), advogado_responsavel: f.advogado_responsavel,
        adicionar_filiais: lista(f.fil), adicionar_anteriores: lista(f.ant), adicionar_alternativos: lista(f.alt), confirmar_por_nome_exato: e.target.cne.checked } }); pgCliente(id); } catch (x) { document.getElementById("er").textContent = x.message; } };
  } catch (e) { falhou("clientes", e); }
}

/* ---------- processos ---------- */
function tabelaProcessos(itens) {
  if (!itens.length) return `<p class="mudo">Nenhum processo para os filtros.</p>`;
  return `<div class="tabela-rolar"><table><thead><tr><th>Processo</th><th>Cliente</th><th>Tribunal</th><th>Classe</th><th>Distribuição</th><th>Identificado</th><th>Situação</th><th>Prioridade</th><th>Responsável</th></tr></thead><tbody>
    ${itens.map((i) => `<tr class="clic${i.dias_ajuiz === 0 ? " dia-hoje" : ""}" data-p="${i.id}"><td>${esc(i.numero_processo)}</td><td>${esc(i.cliente)}</td><td>${esc(i.tribunal)}</td><td>${esc(i.classe || "—")}</td>
    <td>${i.dias_ajuiz === 0 ? `<span class="selo-hoje">AJUIZADO HOJE</span><br>` : i.dias_ajuiz === 1 ? `<span class="selo-ontem">ONTEM</span><br>` : ""}${fmtDia(i.data_ajuizamento)}<br>${tag(i.categoria_data)}</td><td>${fmtDia(i.identificada_em)}</td><td>${tag(i.classificacao)}<br><span class="peq mudo">${esc(ROT[i.estado_validacao] || i.estado_validacao)}</span></td>
    <td>${i.prioridade ? tag(i.prioridade) : "—"}</td><td>${esc(i.responsavel || "—")}</td></tr>`).join("")}</tbody></table></div>`;
}
function bindProcessos() { document.querySelectorAll("tr[data-p]").forEach((tr) => (tr.onclick = () => (location.hash = "#/processo/" + tr.dataset.p))); }
const filtros = { classificacao: "", categoria: "", cliente_id: "", tribunal: "", de: "", ate: "", classe: "", prioridade: "", responsavel_id: "", valor_min: "", valor_max: "", ajuizado_hoje: "", pagina: 1 };
async function pgProcessos() {
  carregando("processos");
  try {
    const [cs, us] = await Promise.all([api("/clientes"), api("/usuarios")]);
    const qs = () => Object.entries(filtros).filter(([, v]) => v !== "" && v != null).map(([k, v]) => k + "=" + encodeURIComponent(v)).join("&") + "&limite=50";
    const r = await api("/processos?" + qs());
    const sel = (n, ops, v) => `<select name="${n}"><option value="">Todos</option>${ops.map(([a, b]) => `<option value="${esc(a)}" ${String(v) === String(a) ? "selected" : ""}>${esc(b)}</option>`).join("")}</select>`;
    moldura("processos", `<div class="cabeca"><div><h1>Novos processos</h1><div class="mudo peq">${r.total} resultado(s)</div></div></div>
      <form class="filtros" id="ff"><div><label>Cliente</label>${sel("cliente_id", cs.map((c) => [c.id, c.razao_social]), filtros.cliente_id)}</div>
      <div><label>Situação</label>${sel("classificacao", [["confirmada", "Confirmada"], ["provavel", "Provável"], ["inconclusiva", "Inconclusiva"], ["descartada", "Descartada"]], filtros.classificacao)}</div>
      <div><label>Distribuição</label>${sel("categoria", [["nova_distribuicao", "Nova distribuição"], ["processo_antigo", "Processo antigo"], ["indeterminada", "Indeterminada"]], filtros.categoria)}</div>
      <div><label>Tribunal</label><input name="tribunal" size="7" value="${esc(filtros.tribunal)}" placeholder="TRT9"></div><div><label>Classe</label><input name="classe" size="14" value="${esc(filtros.classe)}"></div>
      <div><label>Identificado de</label><input type="date" name="de" value="${esc(filtros.de)}"></div><div><label>até</label><input type="date" name="ate" value="${esc(filtros.ate)}"></div>
      <div><label>Valor mín.</label><input name="valor_min" size="9" value="${esc(filtros.valor_min)}"></div><div><label>Valor máx.</label><input name="valor_max" size="9" value="${esc(filtros.valor_max)}"></div>
      <div><label>Risco</label>${sel("prioridade", [["alta", "Alta"], ["media", "Média"], ["baixa", "Baixa"]], filtros.prioridade)}</div>
      <div><label>Responsável</label>${sel("responsavel_id", us.map((u) => [u.id, u.nome]), filtros.responsavel_id)}</div>
      <div><label style="display:flex;gap:7px;align-items:center;margin:0 0 9px"><input type="checkbox" name="ajuizado_hoje" value="1" ${filtros.ajuizado_hoje ? "checked" : ""}> Só ajuizados hoje</label></div><button>Filtrar</button><button type="button" class="sec" id="lim">Limpar</button></form>
      ${tabelaProcessos(r.itens)}
      <div class="lin" style="margin-top:10px"><button class="sec" id="ant" ${filtros.pagina <= 1 ? "disabled" : ""}>← Anterior</button><span class="mudo peq">Página ${filtros.pagina}</span><button class="sec" id="prox" ${filtros.pagina * 50 >= r.total ? "disabled" : ""}>Próxima →</button></div>`);
    bindProcessos();
    document.getElementById("ff").onsubmit = (e) => { e.preventDefault(); const f = new FormData(e.target); for (const k of Object.keys(filtros)) if (k !== "pagina") filtros[k] = f.has(k) ? f.get(k) : ""; filtros.pagina = 1; pgProcessos(); };
    document.getElementById("lim").onclick = () => { for (const k of Object.keys(filtros)) filtros[k] = ""; filtros.pagina = 1; pgProcessos(); };
    document.getElementById("ant").onclick = () => { filtros.pagina--; pgProcessos(); };
    document.getElementById("prox").onclick = () => { filtros.pagina++; pgProcessos(); };
  } catch (e) { falhou("processos", e); }
}
async function pgProcesso(id) {
  carregando("processos");
  try {
    const [d, us] = await Promise.all([api("/processos/" + id), api("/usuarios")]);
    const a = d.analise, ev = d.evidencia;
    const campo = (k, rot) => { const c = a?.campos?.[k]; if (!c) return ""; const v = Array.isArray(c.valor) ? c.valor.join("; ") : c.valor === true ? "Sim" : c.valor === false ? "Não" : c.valor;
      return `<dt>${rot}</dt><dd>${v == null || v === "" ? "<span class='mudo'>não disponível</span>" : esc(k === "valor_causa" ? brl(c.valor) : v)}<span class="origem ${esc(c.origem)}" title="${esc(c.nota || "")}">${esc({ fonte: "fonte", inferencia: "inferência", nao_verificado: "não verificado" }[c.origem])}</span></dd>`; };
    moldura("processos", `<div class="cabeca"><div><a href="#/processos">← Processos</a><h1>${esc(d.numero_processo)}</h1>
      ${d.data_ajuizamento === new Date().toLocaleDateString("en-CA") ? `<div class="faixa falha_parcial" style="border-color:#f0a32e;box-shadow:0 0 30px #f0a32e30"><span class="selo-hoje">AJUIZADO HOJE</span> &nbsp;Esta ação foi distribuída hoje. Priorize a conferência e a ciência do responsável.</div>` : ""}<div class="lin">${tag(d.classificacao)} ${tag(d.categoria_data)} ${tag(d.estado_validacao)} ${d.prioridade ? "Prioridade " + tag(d.prioridade) : ""}</div></div>
      ${pode("advogado") ? `<div class="lin"><select id="resp"><option value="">Sem responsável</option>${us.filter((u) => u.ativo).map((u) => `<option value="${u.id}" ${u.id === d.responsavel_id ? "selected" : ""}>${esc(u.nome)}</option>`).join("")}</select><button class="sec" id="atr">Atribuir</button><button class="sec" id="ana">Gerar análise</button></div>` : ""}</div>
      <div class="cartao"><h3>Dados processuais</h3><dl class="dl"><dt>Cliente</dt><dd>${esc(d.cliente)} <span class="mudo peq">${esc(fmtCnpj(d.cnpj_matriz))}</span></dd><dt>Tribunal</dt><dd>${esc(d.tribunal)}</dd><dt>Órgão julgador</dt><dd>${esc(d.orgao || "—")}</dd>
      <dt>Classe</dt><dd>${esc(d.classe || "—")}</dd><dt>Assuntos</dt><dd>${esc(d.assuntos.join("; ") || "—")}</dd><dt>Sistema / grau</dt><dd>${esc(d.sistema || "—")} / ${esc(d.grau || "—")}</dd>
      <dt>Data de ajuizamento</dt><dd>${fmtDia(d.data_ajuizamento)} <span class="mudo peq">(DataJud: ${esc(d.datajud_status)})</span></dd><dt>Identificado pelo Sentinela</dt><dd>${fmtData(d.primeira_identificacao)}</dd>
      <dt>Última verificação</dt><dd>${fmtData(d.ultima_verificacao)}</dd><dt>Responsável</dt><dd>${esc(d.responsavel || "—")}</dd></dl></div>
      <div class="cartao"><h3>Identificação do cliente e fundamento</h3><dl class="dl"><dt>Critério</dt><dd>Nível ${d.nivel} — ${esc(ev.regra || "")}</dd><dt>Posição processual</dt><dd>${d.polo === "P" ? "Polo passivo (réu)" : d.polo === "A" ? "Polo ativo (autor)" : "Não determinada"}</dd>
      <dt>Nome na fonte</dt><dd>${esc(d.nome_na_fonte || "—")}</dd><dt>Similaridade</dt><dd>${esc(ev.similaridade ?? "—")}</dd><dt>CNPJ no texto da fonte</dt><dd>${esc(ev.cnpj_no_texto ? fmtCnpj(ev.cnpj_no_texto) : "não encontrado")}</dd>
      <dt>Confiança do vínculo</dt><dd><b>${d.confianca ?? "—"}%</b> · ${d.status_deteccao === "CONFIRMED" ? "CONFIRMADO por mais de uma fonte" : d.status_deteccao === "DETECTED" ? "DETECTADO (uma fonte)" : esc(d.status_deteccao || "—")}${d.citacao_confirmada ? " · citação publicada" : ""}<br><span class="mudo peq">≥${d.limiares.confianca_auto}% confirma sozinho · ${d.limiares.confianca_revisao}–${d.limiares.confianca_auto - 1}% vai para revisão · abaixo de ${d.limiares.confianca_revisao}% não gera alerta automático</span></dd>
      <dt>Fontes que viram o processo</dt><dd>${d.fontes.length ? d.fontes.map((f) => `${esc(f.fonte)} <span class="mudo peq">(1ª vez ${esc(fmtData(f.primeiro_visto))}, ${f.vezes}×)</span>`).join("<br>") : "—"}</dd></dl>
      ${Object.keys(d.campos_divergentes || {}).length ? `<div class="faixa falha_parcial" style="margin-top:12px"><b>Fontes divergem:</b> ${Object.entries(d.campos_divergentes).map(([k, v]) => esc(k) + ": " + v.map((x) => esc(x.valor) + " (" + esc(x.fonte) + ")").join(" × ")).join(" · ")}. Nada foi sobrescrito.</div>` : ""}
      ${(d.partes || []).length ? `<p class="peq mudo" style="margin:12px 0 0"><b>Partes:</b> ${d.partes.map((x) => esc(x.nome) + " <i>(" + ({ PLAINTIFF: "autor", DEFENDANT: "réu", INTERESTED: "interessado", THIRD_PARTY: "outro", UNKNOWN: "?" }[x.lado] || x.lado) + ")</i>").join(" · ")}</p>` : ""}
      ${(d.eventos || []).length ? `<details class="ajuda" style="margin-top:12px"><summary>Cronologia do processo no Sentinela</summary><ul>${d.eventos.map((x) => `<li><b>${esc(fmtData(x.criado_em))}</b> — ${esc(x.tipo)}${x.fonte ? " [" + esc(x.fonte) + "]" : ""}: ${esc(x.detalhe || "")}</li>`).join("")}</ul></details>` : ""}
      ${["provavel", "inconclusiva"].includes(d.classificacao) && d.estado_validacao === "pendente" && pode("advogado") ? `<div class="lin" style="margin-top:12px"><button id="conf">Confirmar ação contra o cliente</button><button class="perigo" id="desc">Descartar</button></div>` : ""}</div>
      <div class="cartao"><h3>Análise jurídica</h3>${a ? `<p>${esc(a.resumo)}</p><dl class="dl">${campo("classe", "Classe")}${campo("assunto", "Assunto")}${campo("autor", "Autor")}${campo("reu", "Réu")}${campo("valor_causa", "Valor da causa")}${campo("tutela_provisoria", "Tutela provisória")}${campo("principais_fatos", "Principais fatos")}${campo("fundamentos_juridicos", "Fundamentos")}${campo("pedidos", "Pedidos")}
        <dt>Urgência aparente</dt><dd>${a.urgencia_aparente ? "Sim" : "Não"}</dd><dt>Análise imediata</dt><dd>${a.requer_analise_imediata ? "Recomendada" : "Não indicada"}</dd><dt>Motivos da prioridade</dt><dd>${esc((a.campos._motivos_prioridade?.valor || []).join("; ") || "nenhum critério atingido")}</dd></dl>
        <p class="mudo peq">Gerada em ${fmtData(a.gerada_em)} · modelo: ${esc(a.modelo)} · petição inicial: ${esc(a.documentos.peticao_inicial === "indisponivel_nas_fontes_publicas" ? "indisponível nas fontes públicas" : a.documentos.peticao_inicial)}. Verde = extraído da fonte; amarelo = inferência; cinza = não verificado.</p>` : `<p class="mudo">Análise ainda não gerada.</p>`}</div>
      <h2>Comunicações coletadas (${d.comunicacoes.length})</h2>${d.comunicacoes.map((c) => `<div class="cartao"><div class="lin"><b>${esc(c.tipo || "Comunicação")}</b><span class="mudo peq">${fmtDia(c.data_disponibilizacao)} · ${esc(c.tribunal)} · ${esc(c.orgao || "")}</span>${c.link ? `<a href="${esc(c.link)}" target="_blank" rel="noopener noreferrer">consulta oficial ↗</a>` : ""}</div>
        <div class="peq mudo">${c.destinatarios.map((x) => esc(x.polo === "P" ? "Passivo" : "Ativo") + ": " + esc(x.nome)).join(" · ")}</div><div class="texto">${esc(c.texto || "")}</div></div>`).join("")}
      <h2>Histórico</h2><div class="cartao peq">${d.validacoes.map((v) => `${fmtData(v.decidida_em)} — ${esc(v.usuario)}: ${esc(v.decisao)} (era ${esc(v.classificacao_anterior)}) ${esc(v.justificativa || "")}`).join("<br>") || "Sem decisões manuais."}
        <br>${d.alertas.map((v) => `${fmtData(v.criado_em)} — alerta ${esc(v.tipo)} via ${esc(v.canal)}: ${esc(v.status)}`).join("<br>")}</div>`);
    const at = document.getElementById("atr"); if (at) at.onclick = async () => { try { await api("/processos/" + id + "/atribuir", { method: "POST", body: { usuario_id: +document.getElementById("resp").value || null } }); pgProcesso(id); } catch (e) { aviso(e.message); } };
    const an = document.getElementById("ana"); if (an) an.onclick = async () => { try { await api("/processos/" + id + "/analisar", { method: "POST" }); aviso("Análise enfileirada. Atualize em instantes."); } catch (e) { aviso(e.message); } };
    for (const [bid, dec] of [["conf", "confirmada"], ["desc", "descartada"]]) { const b = document.getElementById(bid); if (b) b.onclick = async () => { const j = prompt("Justificativa (registrada na auditoria):", ""); if (j === null) return; try { await api("/validacao/" + id, { method: "POST", body: { decisao: dec, justificativa: j } }); pgProcesso(id); } catch (e) { aviso(e.message); } }; }
  } catch (e) { falhou("processos", e); }
}

/* ---------- central de validação ---------- */
const COB_ROT = { coberto: "Lido e conferido", provisorio: "Lido, ainda sendo relido", em_curso: "Dia em curso", lacuna: "NÃO lido (lacuna)", incompleto: "Lido incompleto", sem_historico: "Fora do histórico buscado" };
const CTRL_ROT = { capturado: ["Capturado", "ok"], recuperado: ["FALHA NOSSA — recuperado agora", "alerta"], revisar: ["Revisar nomes", "alerta"], invisivel_no_djen: ["Invisível no DJEN (limite da fonte)", "info"], anterior_ao_historico: ["Anterior ao histórico (incluído)", "info"], fora_da_fonte: ["Fora da fonte (DJEN não publica)", "info"], erro: ["Erro ao verificar", "alerta"] };
async function pgCobertura() {
  carregando("cobertura");
  try {
    const [d, rc, mt, ch] = await Promise.all([api("/cobertura?dias=45"), api("/radar/cobertura"), api("/radar/metricas?dias=1"), api("/radar/chamadas?limite=12")]);
    const t = d.total, lac = d.lacunas || [];
    const veredito = d.integra ? ["verde", "Nenhuma lacuna: todos os dias de todos os clientes foram lidos por completo."] : ["vermelho", `${t.lacuna + t.incompleto} dia(s) de cliente SEM leitura completa. O sistema já está refazendo; confira abaixo.`];
    const ctl = d.controle, n_ = (k) => ctl.filter((x) => x.resultado === k).length;
    const baseR = n_("capturado") + n_("recuperado"), recall = baseR ? Math.round(1000 * n_("capturado") / baseR) / 10 : null;
    const vis = n_("capturado") + n_("recuperado") + n_("anterior_ao_historico"), totV = vis + n_("revisar") + n_("invisivel_no_djen");
    const visib = totV ? Math.round(1000 * vis / totV) / 10 : null;
    const faixa = (c) => `<div class="tira">${c.dias.map((x) => `<i class="${esc(x.status)}" title="${esc(fmtDia(x.dia))} — ${esc(COB_ROT[x.status])}"></i>`).join("")}</div>`;
    moldura("cobertura", hero("PROVA DE COBERTURA", "Estamos lendo <em>tudo?</em>", "Registro, dia a dia, do que foi realmente lido em cada fonte para cada cliente. O que não está provado aparece como lacuna.",
        pode("advogado") ? `<button id="lac">Preencher lacunas agora</button>` : "") +
      `<div class="semaforo ${veredito[0]}"><i></i><span>${esc(veredito[1])}</span></div>
      <div class="summarygrid placar">${[["Lidos e conferidos", t.coberto, "highlight"], ["Ainda sendo relidos", t.provisorio + t.em_curso, ""], ["Lacunas", t.lacuna, t.lacuna ? "ruim" : ""], ["Leituras incompletas", t.incompleto, t.incompleto ? "ruim" : ""]]
        .map(([n, v, c]) => `<article class="${c}"><span>${n}</span><strong>${v}</strong><small>dias de cliente, últimos ${d.dias} dias</small></article>`).join("")}</div>
      <h2>Cobertura do monitoramento — fontes</h2>
      <div class="semaforo ${rc.completa ? "verde" : "amarelo"}"><i></i><span><b>${rc.operacionais}/${rc.esperadas} fontes operacionais.</b> ${rc.completa ? "Cobertura completa nas fontes ativas." : esc(rc.resumo.split("; ").slice(1).join("; "))}${rc.ultima_verificacao ? " · última consulta: " + esc(fmtData(rc.ultima_verificacao)) : ""}</span></div>
      <div class="summarygrid fontes">${rc.fontes.filter((f) => rc.esperadas_lista.includes(f.fonte)).map((f) => `<article class="est-${esc(f.estado)}"><span>${esc(f.rotulo)}</span><strong style="font-size:26px">${esc({ HEALTHY: "● SAUDÁVEL", DEGRADED: "● DEGRADADA", DOWN: "● FORA DO AR", UNKNOWN: "● SEM DADOS" }[f.estado])}</strong>
        <small>última consulta: ${esc(fmtData(f.ultima_consulta))}<br>sucesso 24 h: ${f.taxa_sucesso_24h ?? "—"}% · 7 d: ${f.taxa_sucesso_7d ?? "—"}% · latência ${f.latencia_media_ms ?? "—"} ms${f.indisponivel_desde ? "<br>problema desde " + esc(fmtData(f.indisponivel_desde)) + (f.ultimo_erro ? " (" + esc(f.ultimo_erro) + ")" : "") : ""}${f.tribunais_com_problema.length ? "<br>tribunais com falha: " + f.tribunais_com_problema.map((t) => esc(t.tribunal)).join(", ") : ""}</small></article>`).join("")}</div>
      ${rc.nao_monitoradas.length ? `<div class="panel"><h3>Fontes previstas que NÃO estão cobrindo (e por quê)</h3>${rc.nao_monitoradas.map((n) => `<div class="lin" style="gap:14px;align-items:flex-start;margin:8px 0"><span class="cres alerta" style="min-width:150px">${esc(n.estado)}</span><div><b>${esc(n.rotulo)}</b><div class="mudo peq">${esc(n.motivo || "")}${n.verificado_em ? " · verificado " + esc(fmtData(n.verificado_em)) : ""}</div></div></div>`).join("")}</div>` : ""}
      <div class="panel"><h3>Por cliente</h3>${rc.clientes.map((c) => `<div class="cobcli"><div class="nome"><b>${esc(c.cliente)}</b><small>${c.operacionais}/${c.esperadas} fontes operacionais${c.falhas.length ? " · " + c.falhas.map((x) => esc(x.rotulo) + (x.detalhe ? " (" + esc(x.detalhe) + ")" : " " + esc(x.estado))).join("; ") : ""}</small></div>
        <div class="lin" style="gap:8px;flex-wrap:wrap">${c.fontes.map((x) => `<span class="cres ${x.estado === "HEALTHY" ? "ok" : "alerta"}" title="${esc(x.rotulo)} — última: ${esc(fmtData(x.ultima_consulta))}">${esc(x.fonte)} ${x.estado === "HEALTHY" ? "✓" : "✕"}</span>`).join("")}</div></div>`).join("")}</div>
      <div class="panel"><h3>Métricas das últimas 24 h</h3><div class="lin" style="gap:26px;flex-wrap:wrap">${[["Chamadas às fontes", mt.chamadas], ["Falhas", mt.chamadas_falhas], ["Latência média", (mt.latencia_media_ms ?? "—") + " ms"], ["Consultas com ZERO resultados", mt.consultas_com_resultado_zero], ["Consultas que FALHARAM", mt.consultas_falhas], ["Itens lidos", (mt.itens_lidos || 0).toLocaleString("pt-BR")], ["Processos novos", mt.processos_novos], ["Falsos positivos", mt.falsos_positivos]].map(([n, v]) => `<div><span class="mudo peq">${n}</span><br><b style="font:500 28px var(--cond)">${v}</b></div>`).join("")}</div>
        <p class="mudo peq" style="margin:12px 0 0">“Zero resultados” = a fonte respondeu corretamente e não havia nada. “Falhou” = não sabemos; nunca é tratado como zero.</p></div>
      <details class="ajuda"><summary>Trilha de auditoria — últimas chamadas às fontes</summary><div class="tabela-rolar" style="margin:0 18px 18px"><table><thead><tr><th>Quando</th><th>Fonte</th><th>Tribunal</th><th>Resultado</th><th>HTTP</th><th>ms</th><th>Resposta (hash)</th></tr></thead><tbody>${ch.map((x) => `<tr><td>${esc(fmtData(x.quando))}</td><td>${esc(x.fonte)}</td><td>${esc(x.tribunal || "—")}</td><td><span class="cres ${x.ok ? "ok" : "alerta"}">${esc(x.codigo)}${x.final ? "" : " (nova tentativa)"}</span></td><td>${esc(x.http_status ?? "—")}</td><td>${esc(x.latencia_ms ?? "—")}</td><td class="peq mudo">${esc(x.hash_resposta || "—")}</td></tr>`).join("")}</tbody></table></div></details>
      <h2>Cadernos do DJEN — prova por tribunal e dia</h2>
      <p class="mudo peq" style="margin:-6px 0 14px">O CNJ publica, por tribunal e por dia, o caderno COMPLETO das comunicações. O Sentinela baixa cada caderno e procura os clientes dentro dele. “Lidas = declaradas” prova que nenhuma comunicação do dia ficou de fora.</p>
      <div class="panel">${(d.cadernos || []).some((x) => x.cadernos) ? `<div class="tabela-rolar"><table><thead><tr><th>Dia</th><th>Cadernos fechados</th><th>Comunicações lidas / declaradas pelo CNJ</th><th>Achados</th><th>Situação</th></tr></thead><tbody>${d.cadernos.map((x) => `<tr><td>${esc(fmtDia(x.dia))}</td><td>${x.fechados} de ${x.cadernos}</td><td>${x.lidas.toLocaleString("pt-BR")} / ${x.declaradas.toLocaleString("pt-BR")}</td><td>${x.achados}</td><td><span class="cres ${!x.cadernos ? "info" : x.com_erro ? "alerta" : x.pendentes ? "info" : "ok"}">${!x.cadernos ? "ainda não varrido" : x.com_erro ? x.com_erro + " com erro" : x.pendentes ? x.pendentes + " pendente(s)" : "completo"}</span></td></tr>`).join("")}</tbody></table></div>` : `<p class="mudo">Nenhum caderno varrido ainda. ${pode("advogado") ? `<button id="vcad" class="sec">Varrer cadernos agora</button>` : ""}</p>`}</div>
      <h2>Dia a dia por cliente</h2><div class="panel">
        <div class="legenda">${["coberto", "provisorio", "lacuna", "incompleto", "sem_historico"].map((k) => `<span><i class="${k}"></i>${esc(COB_ROT[k])}</span>`).join("")}</div>
        ${d.clientes.map((c) => `<div class="cobcli"><div class="nome"><b>${esc(c.cliente)}</b><small>${c.pct_coberto == null ? "sem histórico ainda" : c.pct_coberto + "% dos dias já conferidos"} · ${c.consultas.length} consulta(s): ${esc(c.consultas.join(" · "))}</small></div>${faixa(c)}</div>`).join("")}
        <p class="mudo peq" style="margin:14px 0 0">Esquerda = mais antigo, direita = hoje. Cada pesquisa diária relê os últimos ${d.rescan_dias} dias, porque o DJEN às vezes publica com atraso.</p></div>
      ${lac.length ? `<h2>Lacunas abertas</h2><div class="panel">${lac.map((g) => `<div class="lin"><b>${esc((d.clientes.find((c) => c.cliente_id === g.cliente_id) || {}).cliente || g.cliente_id)}</b><span class="mudo">${esc(g.chave)}</span><span>${esc(fmtDia(g.ini))} a ${esc(fmtDia(g.fim))}</span></div>`).join("")}<p class="mudo peq" style="margin:10px 0 0">O agendador refaz estas janelas sozinho a cada 30 minutos.</p></div>` : ""}
      <h2>Teste de captura (processos-controle)</h2>
      <p class="mudo peq" style="margin:-6px 0 14px">A prova mais forte: cadastre números de processos que vocês SABEM que existem contra o cliente (de outra fonte: carteira, e-mail do cliente, cautelar). O sistema confere se foram capturados. Se algum não foi, ele inclui na hora e mostra aqui como falha.</p>
      <div class="panel">
        <div class="lin" style="gap:34px;margin-bottom:14px;flex-wrap:wrap">
          <div><span class="mudo peq">Captura do que o DJEN mostra</span><br><b style="font:600 40px var(--cond);color:var(--amber2)">${recall == null ? "—" : recall + "%"}</b><div class="mudo peq" style="max-width:300px">processos com o cliente nomeado no DJEN que a pesquisa trouxe (${n_("capturado")} de ${baseR})</div></div>
          <div><span class="mudo peq">Visibilidade no DJEN</span><br><b style="font:600 40px var(--cond);color:${visib != null && visib < 70 ? "var(--red)" : "var(--amber2)"}">${visib == null ? "—" : visib + "%"}</b><div class="mudo peq" style="max-width:300px">processos conhecidos em que o DJEN chega a nomear o cliente (${vis} de ${totV}). O restante é invisível a qualquer busca por nome/CNPJ.</div></div>
          <div class="mudo peq" style="max-width:360px">${n_("anterior_ao_historico") ? n_("anterior_ao_historico") + " processo(s) são anteriores ao histórico pesquisado e foram incluídos. " : ""}${n_("fora_da_fonte") ? n_("fora_da_fonte") + " não têm nenhuma comunicação no DJEN." : ""}</div></div>
        ${ctl.length ? `<div class="tabela-rolar"><table><thead><tr><th>Cliente</th><th>Processo</th><th>Resultado</th><th>Detalhe</th><th></th></tr></thead><tbody>${ctl.map((x) => { const r = CTRL_ROT[x.resultado] || ["Ainda não verificado", "info"]; return `<tr><td>${esc(x.cliente)}</td><td>${esc(x.numero_processo)}</td><td><span class="cres ${r[1]}">${esc(r[0])}</span></td><td class="peq mudo">${esc(x.detalhe || "")}${x.verificado_em ? "<br>" + esc(fmtData(x.verificado_em)) : ""}</td><td>${pode("advogado") ? `<button class="sec" data-del="${x.id}">remover</button>` : ""}</td></tr>`; }).join("")}</tbody></table></div>` : ""}
        ${pode("advogado") ? `<div class="lin" style="gap:10px;margin-top:14px;flex-wrap:wrap"><select id="ccli">${d.clientes.map((c) => `<option value="${c.cliente_id}">${esc(c.cliente)}</option>`).join("")}</select>
          <input id="cnum" placeholder="0000000-00.0000.0.00.0000" style="min-width:260px"><button id="cadd">Adicionar</button><button id="cver" class="sec">Verificar agora</button></div>` : ""}</div>
      <h2>O que isto garante — e o que não garante</h2>
      <div class="panel ajuda-txt"><b>Garante:</b><ul><li>Que cada dia de publicação do DJEN foi lido por inteiro, para cada nome e CNPJ de cada cliente (lidos = total informado pela fonte; nada cortado pelo limite de 10.000).</li><li>Que dias não lidos (computador desligado, fonte fora do ar) aparecem como lacuna e são refeitos sozinhos.</li><li>Que as publicações tardias são pegas: cada pesquisa relê os últimos ${d.rescan_dias} dias.</li></ul>
        <b>Não garante (limites das fontes públicas):</b><ul><li>Processo que ainda não gerou nenhuma comunicação no DJEN (ex.: acabou de ser distribuído e o réu não foi citado) só aparece quando o DJEN publicar algo.</li><li>Tribunais ou processos que não alimentam o DJEN (segredo de justiça, alguns sistemas).</li><li>Cliente com nome muito curto é pesquisado só por CNPJ.</li></ul>
        Por isso existe o teste de captura: ele mede, com processos reais conhecidos, quanto escapa.</div>`);
    const post = async (tipo, msg) => { try { await api("/jobs", { method: "POST", body: { tipo, params: {} } }); aviso(msg); } catch (e) { aviso(e.message); } };
    const vc = document.getElementById("vcad"); if (vc) vc.onclick = () => post("varrer_cadernos", "Varredura de cadernos enfileirada (pode levar bastante tempo: são centenas de MB por dia).");
    const l = document.getElementById("lac"); if (l) l.onclick = () => post("preencher_lacunas", "Preenchimento enfileirado: o worker refaz os dias sem leitura.");
    const v = document.getElementById("cver"); if (v) v.onclick = async () => { await post("verificar_controle", "Verificação enfileirada. Atualize em instantes."); };
    const a = document.getElementById("cadd"); if (a) a.onclick = async () => { try { await api("/controle", { method: "POST", body: { cliente_id: +document.getElementById("ccli").value, numero_processo: document.getElementById("cnum").value } }); pgCobertura(); } catch (e) { aviso(e.message); } };
    document.querySelectorAll("[data-del]").forEach((b) => (b.onclick = async () => { if (confirm("Remover este processo-controle?")) { await api("/controle/" + b.dataset.del, { method: "DELETE" }); pgCobertura(); } }));
  } catch (e) { falhou("cobertura", e); }
}
async function pgValidacao() {
  carregando("validacao");
  try {
    const r = await api("/validacao/pendentes?limite=100");
    moldura("validacao", `<div class="cabeca"><div><h1>Central de validação</h1><div class="mudo peq">${r.total} correspondência(s) aguardando conferência — mostrando ${r.itens.length}</div></div>
      ${pode("advogado") ? `<div class="lin"><button id="lc">Confirmar selecionadas</button><button class="perigo" id="ld">Descartar selecionadas</button></div>` : ""}</div>
      <p class="mudo peq">Somente correspondências <b>confirmadas</b> geram alerta definitivo de nova ação. Confira polo, nome e fonte antes de decidir. Decisões em lote exigem justificativa e ficam na auditoria.</p>
      <div class="tabela-rolar"><table><thead><tr><th></th><th>Processo</th><th>Cliente</th><th>Nome na fonte</th><th>Critério</th><th>Distribuição</th><th></th></tr></thead><tbody>
      ${r.itens.map((i) => `<tr><td><input type="checkbox" class="sel" value="${i.id}"></td><td><a href="#/processo/${i.id}">${esc(i.numero_processo)}</a><br><span class="peq mudo">${esc(i.tribunal)}</span></td><td>${esc(i.razao_social)}</td>
      <td>${esc(i.nome_na_fonte || "—")}<br><span class="peq mudo">${i.polo === "P" ? "polo passivo" : i.polo === "A" ? "polo ativo" : "polo desconhecido"}</span></td>
      <td>${tag(i.classificacao)}<br><span class="peq mudo">nível ${i.nivel}: ${esc(i.evidencia.regra || "")}</span></td><td>${fmtDia(i.data_ajuizamento)}<br>${tag(i.categoria_data)}</td><td><a href="#/processo/${i.id}">abrir</a></td></tr>`).join("") || `<tr><td colspan="7" class="mudo">Nada pendente.</td></tr>`}</tbody></table></div>`);
    const lote = (dec) => async () => { const ids = [...document.querySelectorAll(".sel:checked")].map((x) => +x.value); if (!ids.length) return aviso("Selecione ao menos uma."); const j = prompt(`Justificativa para ${dec === "confirmada" ? "CONFIRMAR" : "DESCARTAR"} ${ids.length} correspondência(s):`, ""); if (!j || !j.trim()) return;
      try { const x = await api("/validacao/lote", { method: "POST", body: { ids, decisao: dec, justificativa: j } }); aviso(x.decididas + " decisão(ões) registrada(s)."); pgValidacao(); } catch (e) { aviso(e.message); } };
    const lc = document.getElementById("lc"); if (lc) { lc.onclick = lote("confirmada"); document.getElementById("ld").onclick = lote("descartada"); }
  } catch (e) { falhou("validacao", e); }
}

/* ---------- fontes ---------- */
async function pgFontes() {
  carregando("fontes");
  try {
    const [f, ex, jb] = await Promise.all([api("/fontes"), api("/execucoes"), api("/jobs")]);
    moldura("fontes", `<div class="cabeca"><div><h1>Monitoramento de fontes</h1><div class="mudo peq">Estado real dos conectores; matriz atualizada em ${esc(f.matriz_atualizada_em)}</div></div>
      ${pode("advogado") ? `<button id="rep" class="sec">Reprocessar falhas</button>` : ""}</div>
      <div class="grade">${["djen", "datajud"].map((n) => { const s = f.saude.find((x) => x.fonte === n); const c = f.consultas_7d[n] || {}; return `<div class="kpi"><h3>${n.toUpperCase()}</h3>${s ? tag(s.estado) : tag("pendente", "sem dados")}
        <div class="peq mudo" style="margin-top:6px">Último sucesso: ${fmtData(s?.ultimo_sucesso)}<br>Última falha: ${fmtData(s?.ultima_falha)}${s?.ultimo_erro ? " (" + esc(s.ultimo_erro) + ")" : ""}<br>Falhas consecutivas: ${s?.falhas_consecutivas ?? 0}<br>Consultas 7 d: ${c.ok ?? 0} ok · ${c.falhas ?? 0} falhas · ${c.truncadas ?? 0} truncadas</div></div>`; }).join("")}</div>
      <h2>Matriz de cobertura</h2><div class="tabela-rolar"><table><thead><tr><th>Fonte</th><th>Estado</th><th>CNPJ</th><th>Nome</th><th>Partes</th><th>Distribuição</th><th>Auth</th><th>Restrições</th><th>Último teste real</th></tr></thead><tbody>
      ${f.matriz.map((m) => `<tr><td><b>${esc(m.fonte)}</b><br><span class="peq mudo">${esc(m.sistema)}</span></td><td>${esc(m.estado)}</td><td>${esc(m.cnpj)}</td><td>${esc(m.nome)}</td><td>${esc(m.partes)}</td><td>${esc(m.distribuicao)}</td><td>${esc(m.auth)}</td><td class="peq">${esc(m.restricoes)}</td><td class="peq">${esc(m.ultimo_teste)}</td></tr>`).join("")}</tbody></table></div>
      <h2>Execuções</h2><div class="tabela-rolar"><table><thead><tr><th>#</th><th>Gatilho</th><th>Início</th><th>Status</th><th>Resumo</th></tr></thead><tbody>${ex.slice(0, 15).map((e) => { const r = e.resumo ? JSON.parse(e.resumo) : {}; return `<tr><td>${e.id}</td><td>${esc(e.gatilho)}</td><td>${fmtData(e.inicio)}</td><td>${tag(e.status)}</td><td class="peq">${e.resumo ? `${r.consultas_executadas} consultas · ${r.consultas_com_falha} falhas · ${r.consultas_puladas_por_dedup} puladas (dedup) · ${r.correspondencias_novas} novas` : "—"}</td></tr>`; }).join("") || `<tr><td colspan="5" class="mudo">Sem execuções.</td></tr>`}</tbody></table></div>
      <h2>Fila de jobs</h2><div class="tabela-rolar"><table><thead><tr><th>#</th><th>Tipo</th><th>Origem</th><th>Status</th><th>Criado</th><th>Tentativas</th></tr></thead><tbody>${jb.slice(0, 10).map((j) => `<tr><td>${j.id}</td><td>${esc(j.tipo)}</td><td>${esc(j.origem)}</td><td>${tag(j.status)}</td><td>${fmtData(j.criado_em)}</td><td>${j.tentativas}</td></tr>`).join("") || `<tr><td colspan="6" class="mudo">Vazia.</td></tr>`}</tbody></table></div>`);
    const rep = document.getElementById("rep"); if (rep) rep.onclick = async () => { try { await api("/jobs", { method: "POST", body: { tipo: "reprocessar_falhas" } }); aviso("Reprocessamento enfileirado."); pgFontes(); } catch (e) { aviso(e.message); } };
  } catch (e) { falhou("fontes", e); }
}

/* ---------- configurações ---------- */
async function pgConfig() {
  carregando("config");
  try {
    const us = await api("/usuarios");
    let cfg = null, aud = [];
    if (pode("admin")) { [cfg, aud] = await Promise.all([api("/config"), api("/auditoria")]); }
    const bloco = (k, t, dica) => `<div class="cartao"><h3>${t}</h3><p class="mudo peq">${dica}</p><textarea id="cfg_${k}" ${pode("admin") ? "" : "disabled"}>${esc(JSON.stringify(cfg?.config?.[k] ?? {}, null, 2))}</textarea>${pode("admin") ? `<button data-k="${k}" class="salvar">Salvar</button>` : ""}</div>`;
    moldura("config", `<div class="cabeca"><div><h1>Configurações</h1></div></div>
      ${cfg ? `${bloco("agendamento", "Agendamento", "hora (HH:MM) da pesquisa diária, dias retroativos e intervalo (min) do reprocessamento automático.")}
      ${bloco("notificacoes", "Notificações", "Canais interno, e-mail e Slack. Senhas e webhook NÃO ficam aqui: use variáveis de ambiente no servidor.")}
      <div class="cartao peq"><b>Segredos detectados no ambiente do servidor:</b> SMTP usuário ${cfg.segredos_no_ambiente.smtp_usuario_definido ? "✓" : "✗"} · SMTP senha ${cfg.segredos_no_ambiente.smtp_senha_definida ? "✓" : "✗"} · Slack webhook ${cfg.segredos_no_ambiente.slack_webhook_definido ? "✓" : "✗"} · Chave de IA ${cfg.segredos_no_ambiente.ia_definida ? "✓" : "✗"}</div>
      ${bloco("prioridade", "Critérios de prioridade", "Termos e classes que elevam a prioridade e faixas de valor da causa.")}${bloco("motor", "Motor", "Janela (dias) para considerar uma distribuição como nova.")}` : `<p class="mudo">Configurações técnicas: somente administradores.</p>`}
      <h2>Usuários e permissões</h2><div class="tabela-rolar"><table><thead><tr><th>Nome</th><th>E-mail</th><th>Papel</th><th>Ativo</th>${pode("admin") ? "<th></th>" : ""}</tr></thead><tbody>${us.map((u) => `<tr><td>${esc(u.nome)}</td><td>${esc(u.email)}</td><td>${esc(u.papel)}</td><td>${u.ativo ? "sim" : "não"}</td>${pode("admin") ? `<td>${u.id !== eu.id ? `<button class="sec tgl" data-id="${u.id}" data-a="${u.ativo}">${u.ativo ? "Desativar" : "Ativar"}</button>` : ""}</td>` : ""}</tr>`).join("")}</tbody></table></div>
      ${pode("admin") ? `<div class="cartao" style="margin-top:12px"><h3>Novo usuário</h3><form id="fu" class="form2"><div><label>Nome</label><input name="nome" required></div><div><label>E-mail</label><input name="email" type="email" required></div>
        <div><label>Senha (mín. 10)</label><input name="senha" type="password" minlength="10" required autocomplete="new-password"></div><div><label>Papel</label><select name="papel"><option value="advogado">Advogado</option><option value="leitura">Leitura</option><option value="admin">Administrador</option></select></div>
        <div class="cheio erro" id="eu"></div><div class="cheio"><button>Criar</button></div></form></div>
        <h2>Auditoria (últimos 200 eventos)</h2><div class="tabela-rolar"><table><thead><tr><th>Quando</th><th>Usuário</th><th>Ação</th><th>Entidade</th><th>Detalhe</th></tr></thead><tbody>${aud.map((a) => `<tr><td>${fmtData(a.em)}</td><td>${esc(a.usuario || "—")}</td><td>${esc(a.acao)}</td><td>${esc(a.entidade || "")} ${esc(a.entidade_id || "")}</td><td class="peq">${esc((a.detalhe || "").slice(0, 120))}</td></tr>`).join("")}</tbody></table></div>` : ""}`);
    document.querySelectorAll(".salvar").forEach((b) => (b.onclick = async () => { try { await api("/config/" + b.dataset.k, { method: "PUT", body: JSON.parse(document.getElementById("cfg_" + b.dataset.k).value) }); aviso("Salvo."); } catch (e) { aviso("Erro: " + e.message); } }));
    document.querySelectorAll(".tgl").forEach((b) => (b.onclick = async () => { try { await api("/usuarios/" + b.dataset.id, { method: "PATCH", body: { ativo: b.dataset.a !== "1" } }); pgConfig(); } catch (e) { aviso(e.message); } }));
    const fu = document.getElementById("fu"); if (fu) fu.onsubmit = async (e) => { e.preventDefault(); const f = Object.fromEntries(new FormData(e.target)); try { await api("/usuarios", { method: "POST", body: f }); pgConfig(); } catch (x) { document.getElementById("eu").textContent = x.message; } };
  } catch (e) { falhou("config", e); }
}

/* ---------- roteador ---------- */
async function rotear() {
  if (!tok()) return renderLogin();
  if (!eu) { try { eu = await api("/eu"); } catch (e) { return renderLogin(); } }
  const [, p, id] = (location.hash || "#/dashboard").split("/");
  const rotas = { dashboard: pgDashboard, clientes: pgClientes, processos: pgProcessos, cobertura: pgCobertura, validacao: pgValidacao, fontes: pgFontes, config: pgConfig };
  if (p === "cliente" && id) return pgCliente(id);
  if (p === "processo" && id) return pgProcesso(id);
  (rotas[p] || pgDashboard)();
}
window.addEventListener("hashchange", rotear);
rotear();
