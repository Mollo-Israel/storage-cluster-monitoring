let timer = null;

function fmt(n) {
  if (n === null || n === undefined) return "-";
  return String(n);
}

function fmtUptime(sec) {
  if (!sec) return "-";
  const s = Number(sec);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function setDot(ok) {
  const dot = document.getElementById("statusDot");
  dot.classList.toggle("ok", ok);
  dot.classList.toggle("bad", !ok);
}

async function loadTotals() {
  const res = await fetch("/api/totals");
  if (!res.ok) throw new Error("totals error");
  return await res.json();
}

async function loadNodes() {
  const res = await fetch("/api/nodes");
  if (!res.ok) throw new Error("nodes error");
  return await res.json();
}

function renderTotals(t) {
  const el = document.getElementById("totals");
  el.innerHTML = `
    <div class="kpi"><div class="k">Total</div><div class="v">${fmt(t.total_gb)} GB</div></div>
    <div class="kpi"><div class="k">Usado</div><div class="v">${fmt(t.used_gb)} GB</div></div>
    <div class="kpi"><div class="k">Libre</div><div class="v">${fmt(t.free_gb)} GB</div></div>
    <div class="kpi"><div class="k">% Utilización</div><div class="v">${fmt(t.percent)}%</div></div>
    <div class="kpi"><div class="k">Nodos activos</div><div class="v">${fmt(t.nodes_active)}/${fmt(t.nodes_total)}</div></div>
    <div class="kpi"><div class="k">Quorum</div><div class="v">${fmt(t.quorum)}</div></div>
    <div class="kpi"><div class="k">Growth cluster</div><div class="v">${fmt(t.cluster_growth_gb_per_day)} GB/día</div></div>
    <div class="kpi"><div class="k">Latencia ponderada</div><div class="v">${fmt(t.latency_weighted_ms)} ms</div></div>
  `;
}

function renderNodes(nodes) {
  const body = document.getElementById("nodesBody");
  body.innerHTML = "";

  for (const n of nodes) {
    const tr = document.createElement("tr");

    const status = n.status === "ACTIVE" ? "ACTIVO" : "NO REPORTA";
    tr.className = (n.status === "ACTIVE") ? "row-ok" : "row-bad";

    tr.innerHTML = `
      <td>${fmt(n.node_id)}</td>
      <td><span class="badge ${n.status === "ACTIVE" ? "b-ok" : "b-bad"}">${status}</span></td>
      <td>${fmt(n.disks_count)}</td>
      <td>${fmt(n.total_gb)}</td>
      <td>${fmt(n.used_gb)}</td>
      <td>${fmt(n.free_gb)}</td>
      <td>${fmt(n.percent)}</td>
      <td>${fmtUptime(n.uptime_sec)}</td>
      <td>${fmt(n.avg_latency_ms)}</td>
      <td>${fmt(n.growth_gb_per_day)}</td>
      <td>${fmt(n.availability_percent)}</td>
      <td>${fmt(n.failover_events)}</td>
    `;
    body.appendChild(tr);
  }
}

async function refresh() {
  try {
    const [t, n] = await Promise.all([loadTotals(), loadNodes()]);
    renderTotals(t);
    renderNodes(n);
    setDot(true);
  } catch (e) {
    setDot(false);
  }
}

function applyRefresh() {
  const s = Math.max(1, Number(document.getElementById("refreshSeconds").value || 3));
  if (timer) clearInterval(timer);
  timer = setInterval(refresh, s * 1000);
  refresh();
}

document.getElementById("applyRefresh").addEventListener("click", applyRefresh);
applyRefresh();