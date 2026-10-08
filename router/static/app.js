const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[m]));
$("token").value=localStorage.getItem("router_token")||"";
$("token").addEventListener("change",()=>localStorage.setItem("router_token",$("token").value.trim()));

async function jsonFetch(url,opts={}){
  const r=await fetch(url,opts); let body={};
  try{body=await r.json()}catch{}
  if(!r.ok) throw new Error(body.detail||body.error||("HTTP "+r.status));
  return body;
}
async function refresh(){
  $("serviceStatus").textContent="conectando…"; $("serviceStatus").className="pill muted";
  try{
    const [health,catalog]=await Promise.all([jsonFetch("/health"),jsonFetch("/catalog")]);
    $("serviceStatus").textContent="online"; $("serviceStatus").className="pill ok";
    const ps=health.providers||[], cps=catalog.providers||[];
    $("providerCount").textContent=ps.length;
    $("availableCount").textContent=ps.filter(p=>p.available).length;
    $("modelCount").textContent=cps.reduce((n,p)=>n+(p.models?.length||0),0);
    $("providers").innerHTML=ps.map(p=>{
      const rate=Math.round((p.success_rate||0)*100);
      return `<div class="provider"><div class="provider-head"><div><h3>${esc(p.provider)}</h3><small>${esc(p.health_status||"unknown")}</small></div><span class="pill ${p.available?"ok":"bad"}">${p.available?"disponible":"no disponible"}</span></div><div class="bar"><i style="width:${Math.max(0,Math.min(100,rate))}%"></i></div><small>éxito ${rate}% · latencia ${p.latency_ms?Math.round(p.latency_ms)+" ms":"—"}</small></div>`;
    }).join("")||"<p class='muted'>Sin proveedores cargados.</p>";
  }catch(e){$("serviceStatus").textContent="offline";$("serviceStatus").className="pill bad";$("providers").innerHTML="<p class='muted'>"+esc(e.message)+"</p>";}
}
function traceHtml(data){
  const rows=[];
  (data.attempts||[]).forEach(a=>rows.push(["Intento",`${a.phase||"route"} · ${a.provider}/${a.model} · ${a.ok?"OK":"falló"}`]));
  const o=data.orchestration;
  if(o){
    if(o.plan) rows.push(["Plan",o.plan]);
    (o.subtasks||[]).forEach(s=>rows.push(["Subtarea",`${s.id} · ${s.role} · ${s.provider||"—"}/${s.model||"—"} · ${s.ok?"OK":s.error||"falló"}`]));
    if(o.verification) rows.push(["Verificación",o.verification]);
  }
  return rows.map(([a,b])=>`<div class="trace-row"><b>${esc(a)}</b><span>${esc(b)}</span></div>`).join("")||"<p class='muted'>Sin traza.</p>";
}
async function run(){
  const token=$("token").value.trim();
  if(!token){$("runState").textContent="Necesita token de app para ejecutar.";return}
  const task=$("task").value.trim();
  if(!task){$("runState").textContent="Escribí una tarea.";return}
  $("runBtn").disabled=true;$("runState").textContent="Ejecutando…";
  const decomp=$("decompose").value;
  const payload={task,required_capabilities:$("capabilities").value.split(",").map(x=>x.trim()).filter(Boolean),max_subtasks:Number($("maxSubtasks").value)||12,timeout_s:Number($("timeout").value)||45,application_name:$("appName").value.trim()||"router-ui"};
  if(decomp!=="") payload.decompose=decomp==="true";
  try{
    const data=await jsonFetch("/route",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},body:JSON.stringify(payload)});
    $("resultCard").classList.remove("hidden");
    $("resultText").textContent=data.text||data.error||"Sin respuesta";
    $("resultMeta").textContent=`${data.ok?"OK":"ERROR"} · ${data.provider||"—"} / ${data.model||"—"}`;
    $("trace").innerHTML=traceHtml(data);
    $("runState").textContent=data.ok?"Listo.":"Terminó con error.";
  }catch(e){$("runState").textContent=e.message}
  finally{$("runBtn").disabled=false}
}
$("runBtn").addEventListener("click",run);$("refreshBtn").addEventListener("click",refresh);
if("serviceWorker" in navigator) navigator.serviceWorker.register("/app/sw.js").catch(()=>{});
refresh();