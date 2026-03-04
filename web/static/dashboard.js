let timer = null;
let paused = false;
let currentNodeId = null;

function fmt(n){ return (n === null || n === undefined) ? "—" : String(n); }

function fmtUptime(sec){
  if (!sec) return "—";
  const s = Number(sec);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h ${m}m`;
  if (h > 0) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtCapGB(gb){
  if (gb === null || gb === undefined) return "—";
  const x = Number(gb);
  if (!isFinite(x)) return "—";
  if (x >= 1024) return `${(x/1024).toFixed(2)} TB (${x.toFixed(2)} GB)`;
  return `${x.toFixed(2)} GB`;
}

function setDot(ok){
  const dot = document.getElementById("statusDot");
  dot.classList.toggle("ok", ok);
  dot.classList.toggle("bad", !ok);
}
function setLastUpdate(text){ document.getElementById("lastUpdate").textContent = text; }

async function loadTotals(){
  const res = await fetch("/api/totals");
  if (!res.ok) throw new Error("totals error");
  return await res.json();
}
async function loadNodes(){
  const res = await fetch("/api/nodes");
  if (!res.ok) throw new Error("nodes error");
  return await res.json();
}
async function loadNodeDetail(nodeId){
  const res = await fetch(`/api/node/${encodeURIComponent(nodeId)}/detail`);
  if (!res.ok) throw new Error("node detail error");
  return await res.json();
}
async function sendCommand(nodeId, action, value, message){
  const res = await fetch("/api/commands/send", {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({node_id: nodeId, action, value, message})
  });
  const j = await res.json();
  if (!res.ok) throw new Error(j.error || "command send error");
  return j;
}

function renderTotals(t){
  const el = document.getElementById("totals");
  const growth = (t.cluster_growth_gb_per_month === null || t.cluster_growth_gb_per_month === undefined)
    ? "N/A (datos insuf.)"
    : `${t.cluster_growth_gb_per_month} GB/mes`;

  document.getElementById("clusterMeta").textContent =
    `Nodos activos: ${t.nodes_active}/${t.nodes_total} · Quorum: ${t.quorum} · Target availability: ${t.availability_target}`;

  el.innerHTML = `
    <div class="kpi"><div class="k">Total</div><div class="v">${fmtCapGB(t.total_gb)}</div></div>
    <div class="kpi"><div class="k">Usado</div><div class="v">${fmtCapGB(t.used_gb)}</div></div>
    <div class="kpi"><div class="k">Libre</div><div class="v">${fmtCapGB(t.free_gb)}</div></div>
    <div class="kpi"><div class="k">% Utilización</div><div class="v">${fmt(t.percent)}%</div></div>
    <div class="kpi"><div class="k">Nodos activos</div><div class="v">${fmt(t.nodes_active)}/${fmt(t.nodes_total)}</div></div>
    <div class="kpi"><div class="k">Quorum</div><div class="v">${fmt(t.quorum)}</div></div>
    <div class="kpi"><div class="k">Growth cluster</div><div class="v">${growth}</div></div>
    <div class="kpi"><div class="k">Latencia ponderada</div><div class="v">${fmt(t.latency_weighted_ms)} ms</div></div>
  `;
}

function percentColor(pct){
  const p = Number(pct);
  if (!isFinite(p)) return "var(--good)";
  if (p >= 90) return "var(--bad)";
  if (p >= 75) return "var(--warn)";
  return "var(--good)";
}

function diskIconSVG(){
  return `
  <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
    <path d="M4 7.5C4 6.12 5.12 5 6.5 5h11C18.88 5 20 6.12 20 7.5V16.5c0 1.38-1.12 2.5-2.5 2.5h-11C5.12 19 4 17.88 4 16.5V7.5Z" stroke="#0f172a" stroke-width="1.6"/>
    <path d="M7 9h10" stroke="#0f172a" stroke-width="1.6" stroke-linecap="round"/>
    <circle cx="8" cy="15.5" r="1" fill="#0f172a"/>
    <circle cx="12" cy="15.5" r="1" fill="#0f172a"/>
  </svg>`;
}

function renderCards(nodes){
  const wrap = document.getElementById("nodesCards");
  wrap.innerHTML = "";

  if (!nodes || nodes.length === 0){
    wrap.textContent = "No hay nodos registrados todavía.";
    return;
  }

  for (const n of nodes){
    const statusOk = n.status === "ACTIVE";
    const pct = Number(n.percent || 0);
    const color = percentColor(pct);

    const card = document.createElement("div");
    card.className = "card-node";
    card.addEventListener("click", async ()=> openNodeModal(n.node_id));

    card.innerHTML = `
      <div class="card-top">
        <div class="node-left">
          <div class="disk-ico">${diskIconSVG()}</div>
          <div>
            <div class="node-name">${fmt(n.node_id)}</div>
            <div class="node-sub">${fmt(n.disks_count)} discos · Uptime ${fmtUptime(n.uptime_sec)}</div>
          </div>
        </div>
        <div class="badge ${statusOk ? "ok" : "bad"}">${statusOk ? "ACTIVO" : "NO REPORTA"}</div>
      </div>

      <div class="metrics-row">
        <div>Total: <b>${fmtCapGB(n.total_gb)}</b></div>
        <div>Usado: <b>${fmtCapGB(n.used_gb)}</b></div>
        <div>Libre: <b>${fmtCapGB(n.free_gb)}</b></div>
        <div>Lat: <b>${fmt(n.avg_latency_ms)} ms</b></div>
      </div>

      <div class="bar" title="Utilización">
        <span style="width:${Math.max(0, Math.min(100, pct))}%; background:${color};"></span>
      </div>
      <div class="metrics-row" style="margin-top:8px;">
        <div>% Uso: <b>${fmt(n.percent)}%</b></div>
        <div>Avail: <b>${fmt(n.availability_percent)}%</b></div>
        <div>Failover: <b>${fmt(n.failover_events)}</b></div>
        <div>Skew: <b>${fmt(n.clock_skew_ms)} ms</b></div>
      </div>
    `;
    wrap.appendChild(card);
  }
}

function renderNodesTable(nodes){
  const body = document.getElementById("nodesBody");
  body.innerHTML = "";

  for (const n of nodes){
    const tr = document.createElement("tr");
    tr.classList.add(n.status === "ACTIVE" ? "row-ok" : "row-bad");
    tr.style.cursor = "pointer";
    tr.addEventListener("click", async ()=> openNodeModal(n.node_id));

    const growth = (n.growth_gb_per_month === null || n.growth_gb_per_month === undefined) ? "N/A" : String(n.growth_gb_per_month);

    tr.innerHTML = `
      <td>${fmt(n.node_id)}</td>
      <td>${n.status === "ACTIVE" ? "ACTIVO" : "NO REPORTA"}</td>
      <td>${fmt(n.disks_count)}</td>
      <td>${fmtCapGB(n.total_gb)}</td>
      <td>${fmtCapGB(n.used_gb)}</td>
      <td>${fmtCapGB(n.free_gb)}</td>
      <td>${fmt(n.percent)}</td>
      <td>${fmtUptime(n.uptime_sec)}</td>
      <td>${fmt(n.avg_latency_ms)}</td>
      <td>${growth}</td>
      <td>${fmt(n.availability_percent)}</td>
      <td>${fmt(n.failover_events)}</td>
      <td>${fmt(n.clock_skew_ms)}</td>
    `;
    body.appendChild(tr);
  }
}

/* ---------- HISTÓRICO PRO (canvas con ejes y ticks) ---------- */
function fmtShortDate(iso){
  try{
    const d = new Date(iso);
    const dd = String(d.getDate()).padStart(2,"0");
    const mm = String(d.getMonth()+1).padStart(2,"0");
    return `${dd}/${mm}`;
  }catch{ return "—"; }
}

function niceTicks(min, max, count=5){
  const span = Math.max(1e-9, max - min);
  const raw = span / (count-1);
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / pow;
  let step = 1;
  if (norm >= 5) step = 5;
  else if (norm >= 2) step = 2;
  else step = 1;
  step *= pow;

  const start = Math.floor(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step; v += step) ticks.push(v);
  return ticks;
}

function drawHistory(canvas, history){
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0,0,canvas.width,canvas.height);

  if (!history || history.length < 2){
    ctx.font = "12px system-ui";
    ctx.fillText("No hay suficientes datos para graficar.", 12, 22);
    return {note:"No hay suficientes datos."};
  }

  // y en TB (como referencia)
  const points = history.map(p => ({
    x: p.received_at,
    y_tb: (Number(p.used_sum) / 1024.0)
  }));

  let minY = Math.min(...points.map(p => p.y_tb));
  let maxY = Math.max(...points.map(p => p.y_tb));
  if (minY === maxY){ minY = Math.max(0, minY-1); maxY = maxY+1; }

  const padL = 52, padR = 18, padT = 18, padB = 42;
  const W = canvas.width, H = canvas.height;
  const w = W - padL - padR;
  const h = H - padT - padB;

  // grid + axes
  ctx.lineWidth = 1;
  ctx.strokeStyle = "#e2e8f0";
  ctx.fillStyle = "#64748b";
  ctx.font = "11px system-ui";

  const yticks = niceTicks(minY, maxY, 5);
  for (const v of yticks){
    const y = padT + h - ((v - minY)/(maxY-minY))*h;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(W - padR, y);
    ctx.stroke();

    ctx.fillText(v.toFixed(1), 10, y+4);
  }
  ctx.fillText("TB", 10, padT-2);

  // x ticks (max 6)
  const xTickCount = Math.min(6, points.length);
  for (let i=0; i<xTickCount; i++){
    const idx = Math.round(i*(points.length-1)/(xTickCount-1));
    const x = padL + (idx/(points.length-1))*w;
    ctx.beginPath();
    ctx.moveTo(x, padT);
    ctx.lineTo(x, padT+h);
    ctx.stroke();
    ctx.fillText(fmtShortDate(points[idx].x), x-16, H-16);
  }
  ctx.fillText("Fecha", W - 60, H-16);

  // line
  ctx.strokeStyle = "#0b1220";
  ctx.lineWidth = 2;
  ctx.beginPath();
  points.forEach((p, i)=>{
    const x = padL + (i/(points.length-1))*w;
    const y = padT + h - ((p.y_tb - minY)/(maxY-minY))*h;
    if (i===0) ctx.moveTo(x,y);
    else ctx.lineTo(x,y);
  });
  ctx.stroke();

  // points
  ctx.fillStyle = "#0b1220";
  points.forEach((p,i)=>{
    const x = padL + (i/(points.length-1))*w;
    const y = padT + h - ((p.y_tb - minY)/(maxY-minY))*h;
    ctx.beginPath();
    ctx.arc(x,y,2.2,0,Math.PI*2);
    ctx.fill();
  });

  const first = points[0].y_tb;
  const last = points[points.length-1].y_tb;
  return {note:`Uso total: ${first.toFixed(2)} → ${last.toFixed(2)} TB (puntos: ${points.length})`};
}

function showModal(show){
  document.getElementById("modalBackdrop").classList.toggle("hidden", !show);
  document.getElementById("nodeModal").classList.toggle("hidden", !show);
}

async function openNodeModal(nodeId){
  currentNodeId = nodeId;
  showModal(true);

  document.getElementById("modalTitle").textContent = `Nodo ${nodeId}`;
  document.getElementById("modalSub").textContent = "Cargando...";

  const data = await loadNodeDetail(nodeId);

  const s = data.summary;
  const node = data.node;

  document.getElementById("modalSub").textContent =
    `Estado: ${node.status} · last_seen: ${node.last_seen || "—"} · addr: ${node.last_addr || "—"} · skew: ${fmt(s.clock_skew_ms)} ms`;

  document.getElementById("mTotal").textContent = fmtCapGB(s.total_gb);
  document.getElementById("mUsed").textContent  = fmtCapGB(s.used_gb);
  document.getElementById("mFree").textContent  = fmtCapGB(s.free_gb);
  document.getElementById("mPct").textContent   = `${fmt(s.percent)}%`;

  // disks
  const disksBody = document.getElementById("disksBody");
  disksBody.innerHTML = "";
  for (const d of data.disks){
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmt(d.disk_name)}</td>
      <td>${fmt(d.mountpoint)}</td>
      <td>${fmt(d.disk_type)}</td>
      <td>${fmtCapGB(d.total_gb)}</td>
      <td>${fmtCapGB(d.used_gb)}</td>
      <td>${fmtCapGB(d.free_gb)}</td>
      <td>${fmt(d.percent)}</td>
      <td>${fmt(d.iops)}</td>
      <td>${fmt(d.timestamp)}</td>
      <td>${fmt(d.received_at)}</td>
    `;
    disksBody.appendChild(tr);
  }

  // history table (Fecha / TB)
  const histBody = document.getElementById("histTableBody");
  histBody.innerHTML = "";
  const lastN = data.history.slice(-12); // como la referencia, tabla corta
  for (const h of lastN){
    const usedTb = (Number(h.used_sum) / 1024.0);
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${fmtShortDate(h.received_at)} ${new Date(h.received_at).toLocaleTimeString()}</td><td>${usedTb.toFixed(1)}</td>`;
    histBody.appendChild(tr);
  }

  // chart
  const canvas = document.getElementById("historyChart");
  const info = drawHistory(canvas, data.history);
  document.getElementById("chartNote").textContent = info.note;

  // commands list
  const cmdsBody = document.getElementById("cmdsBody");
  cmdsBody.innerHTML = "";
  for (const c of data.commands){
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${fmt(c.cmd_id)}</td>
      <td>${fmt(c.action)}</td>
      <td>${fmt(c.value)}</td>
      <td>${fmt(c.message)}</td>
      <td><span class="pill ${String(c.status).toLowerCase()}">${fmt(c.status)}</span></td>
      <td>${fmt(c.created_at)}</td>
      <td>${fmt(c.acked_at)}</td>
      <td>${fmt(c.last_error)}</td>
    `;
    cmdsBody.appendChild(tr);
  }

  document.getElementById("cmdStatus").textContent =
    `Avail: ${fmt(s.availability_percent)}% · Failover: ${fmt(s.failover_events)} · Growth: ${s.growth_gb_per_month ?? "N/A"} GB/mes`;
}

document.getElementById("closeModal").addEventListener("click", ()=> showModal(false));
document.getElementById("modalBackdrop").addEventListener("click", ()=> showModal(false));

document.getElementById("sendCmd").addEventListener("click", async ()=>{
  if (!currentNodeId) return;
  const action = document.getElementById("cmdAction").value;
  const value = document.getElementById("cmdValue").value.trim();
  const message = document.getElementById("cmdMessage").value.trim();

  const statusEl = document.getElementById("cmdStatus");
  statusEl.textContent = "Enviando comando...";

  try{
    const r = await sendCommand(currentNodeId, action, value || null, message || null);
    statusEl.textContent = `✅ Command queued. cmd_id=${r.cmd_id}`;
    await openNodeModal(currentNodeId);
  }catch(e){
    statusEl.textContent = `❌ Error enviando comando: ${e.message || e}`;
  }
});

async function refresh(){
  if (paused) return;
  try{
    const [t, n] = await Promise.all([loadTotals(), loadNodes()]);
    renderTotals(t);
    renderCards(n);
    renderNodesTable(n);
    setDot(true);
    setLastUpdate(`Updated: ${new Date().toLocaleTimeString()}`);
  }catch{
    setDot(false);
    setLastUpdate("Error actualizando");
  }
}

function applyRefresh(){
  const s = Math.max(1, Number(document.getElementById("refreshSeconds").value || 3));
  if (timer) clearInterval(timer);
  timer = setInterval(refresh, s * 1000);
  refresh();
}

document.getElementById("applyRefresh").addEventListener("click", applyRefresh);

document.getElementById("pauseRefresh").addEventListener("click", ()=>{
  paused = !paused;
  document.getElementById("pauseRefresh").textContent = paused ? "Reanudar" : "Pausar";
  if (!paused) refresh();
});

applyRefresh();