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

const columns=["chat","code","reasoning","vision","document","image_generation","transcription","speech","tools"];
let availablePlatforms=[], catalogPlatforms=[];
function verified(m,c){return (m.evidence||[]).some(e=>e.capability===c&&e.status==="verified"&&e.evidence!=="migrated_from_v1")}
function renderInventory(){
 const live=catalogPlatforms.filter(p=>availablePlatforms.includes(p.id));
 const routes=live.flatMap(p=>(p.models||[]).map(m=>({platform:p.id,model:m})));
 const ids=new Set(routes.map(x=>x.model.id.toLowerCase().replace(/:free$/,"")));
 const verifiedRoutes=routes.filter(x=>columns.some(c=>verified(x.model,c))).length;
 const declaredRoutes=routes.filter(x=>(x.model.declared_capabilities||[]).length).length;
 $("inventorySummary").textContent=live.length+" plataformas · "+routes.length+" rutas · "+ids.size+" identificadores normalizados (aproximados) · "+declaredRoutes+" rutas con declaraciones · "+verifiedRoutes+" con evidencia verificada.";
 $("inventoryPlatforms").innerHTML=live.map(p=>{
  const models=p.models||[],tested=models.filter(m=>columns.some(c=>verified(m,c))).length;
  return "<div class='inventory-item'><b>"+esc(p.id)+"</b><span>"+models.length+" rutas · "+tested+" verificadas</span></div>";
 }).join("")+"<p class='hint'>Capacidades (verificadas / declaradas): "+columns.map(c=>esc(c)+": "+routes.filter(x=>verified(x.model,c)).length+"/"+routes.filter(x=>(x.model.declared_capabilities||[]).includes(c)).length).join(" · ")+"</p>";
}
function renderMatrix(){
 const group=$("matrixPlatform").value, kind=$("matrixCapability").value, term=$("matrixSearch").value.toLowerCase();
 const groups=catalogPlatforms.filter(p=>availablePlatforms.includes(p.id));
 const models=groups.filter(p=>!group||p.id===group).flatMap(p=>(p.models||[]).map(m=>({platform:p.id,model:m}))).filter(x=>x.model.id.toLowerCase().includes(term));
 const caps=kind?[kind]:columns;
 $("capabilityTotals").textContent=columns.map(c=>c+": "+groups.reduce((n,p)=>n+(p.models||[]).filter(m=>verified(m,c)).length,0)).join(" · ");
 $("matrixInfo").textContent=models.length+" rutas visibles; totales de plataformas conectadas, solo pruebas verificadas.";
 $("matrixHead").innerHTML="<tr><th>Plataforma</th><th>Modelo</th>"+caps.map(c=>"<th>"+esc(c)+"</th>").join("")+"</tr>";
 $("matrixBody").innerHTML=models.map(x=>"<tr><td>"+esc(x.platform)+"</td><td>"+esc(x.model.id)+"</td>"+caps.map(c=>{const e=(x.model.evidence||[]).find(e=>e.capability===c);const ok=verified(x.model,c);return "<td title='"+esc(e?.evidence||"Sin verificar")+"'>"+(ok?"✓":e?.status==="unsupported"?"×":(x.model.declared_capabilities||[]).includes(c)?"○":"·")+"</td>"}).join("")+"</tr>").join("")||"<tr><td colspan='"+(caps.length+2)+"'>Sin resultados</td></tr>";
}
function updateProbeModels(){
 const p=catalogPlatforms.find(p=>p.id===$("probePlatform").value);
 $("probeModel").innerHTML="";
 $("probeModel").add(new Option("Seleccionar modelo",""));
 (p?.models||[]).forEach(m=>$("probeModel").add(new Option(m.id,m.id)));
}
$("probePlatform").addEventListener("change",updateProbeModels);
columns.forEach(c=>$("probeCapability").add(new Option(c,c)));
$("probeButton").addEventListener("click",async()=>{
 const provider=$("probePlatform").value,model=$("probeModel").value,capability=$("probeCapability").value,token=$("probeAdminToken").value.trim();
 if(!provider||!model||!token){$("probeMessage").textContent="Elegí plataforma y modelo e ingresá el token administrador.";return}
 $("probeButton").disabled=true;$("probeMessage").textContent="Probando con el proveedor…";
 try{
   const result=await jsonFetch("/capabilities/probe",{method:"POST",headers:{"Content-Type":"application/json","Authorization":"Bearer "+token},body:JSON.stringify({provider,model,capability})});
   $("probeMessage").textContent=(result.status||"sin resultado")+" · "+(result.evidence||"")+" · "+provider+"/"+model;
   await refresh();
 }catch(err){$("probeMessage").textContent=err.message}
 finally{$("probeButton").disabled=false}
});
$("matrixPlatform").onchange=renderMatrix;
$("matrixCapability").onchange=renderMatrix;
$("matrixSearch").oninput=renderMatrix;
columns.forEach(c=>{$("matrixCapability").add(new Option(c,c))});

async function refresh(){
  $("serviceStatus").textContent="conectando…"; $("serviceStatus").className="pill muted";
  try{
    const [health,catalog]=await Promise.all([jsonFetch("/health"),jsonFetch("/catalog")]);
    $("serviceStatus").textContent="online"; $("serviceStatus").className="pill ok";
    const ps=health.providers||[], cps=catalog.providers||[];
    availablePlatforms=ps.map(p=>p.provider);catalogPlatforms=cps;
    const oldProbe=$("probePlatform").value,oldModel=$("probeModel").value;
    $("probePlatform").innerHTML="";
    $("probePlatform").add(new Option("Seleccionar plataforma",""));
    availablePlatforms.forEach(p=>$("probePlatform").add(new Option(p,p)));
    $("probePlatform").value=availablePlatforms.includes(oldProbe)?oldProbe:"";
    updateProbeModels();
    if([...$("probeModel").options].some(o=>o.value===oldModel)) $("probeModel").value=oldModel;
    const saved=$('matrixPlatform').value;
    $('matrixPlatform').innerHTML='<option value="">Todas las conectadas</option>';
    availablePlatforms.forEach(p=>$('matrixPlatform').add(new Option(p,p)));
    $('matrixPlatform').value=availablePlatforms.includes(saved)?saved:'';
    renderInventory();
    renderMatrix();
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