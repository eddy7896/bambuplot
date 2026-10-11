const $=id=>document.getElementById(id);
const PAPERS={A5:[148,210],A6:[105,148]};
const STORE='bambuplot.v1';
const PLAN_KEYS=new Set(['area.w','area.h','pen.width_mm','touch_off']);
let L=null, DEF=null, effects=[], job={effect:'waves',params:{},prep:{},image:null};
let plan=null, checks=[], sel='area', drag=null, V=null, snap=true;
let lineStr=[], travelStr=[], totalPts=0, planSeq=0;

/* ---------- geometry (printer frame: +Y back, angles CCW in degrees) ---------- */
const R=(x,y,w,h)=>({x,y,w,h});
const x2=r=>r.x+r.w, y2=r=>r.y+r.h;
const grow=(r,m)=>R(r.x-m,r.y-m,r.w+2*m,r.h+2*m);
const BED=R(0,0,180,180);
const normAng=a=>{a=((a+180)%360+360)%360-180;return Math.round(a*10)/10||0};
const rotPt=(x,y,deg)=>{const t=deg*Math.PI/180,c=Math.cos(t),s=Math.sin(t);return [x*c-y*s,x*s+y*c]};
function paperBox(){let [w,h]=PAPERS[L.paper.size];if(L.paper.orientation==='landscape')[w,h]=[h,w];return {x:L.paper.x,y:L.paper.y,w,h,rot:L.paper.rotation||0}}
const areaBox=()=>({...L.area,rot:L.area.rotation||0});
const centerOf=b=>[b.x+b.w/2,b.y+b.h/2];
/* local point (origin at the box's front-left corner, along its edges) → world */
function toWorld(b,lx,ly){const [cx,cy]=centerOf(b),[dx,dy]=rotPt(lx-b.w/2,ly-b.h/2,b.rot);return [cx+dx,cy+dy]}
const corners=b=>[[0,0],[b.w,0],[b.w,b.h],[0,b.h]].map(([u,v])=>toWorld(b,u,v));
const clipRect=c=>R(c.x,c.y,L.clip_w,L.clip_d);
const keepouts=()=>L.clips.map(c=>grow(clipRect(c),L.clip_keep_out));
const reach=()=>R(L.pen.offset_x,L.pen.offset_y,180,180);
const snapv=v=>snap?Math.round(v):Math.round(v*10)/10;
const fmt=n=>String(Math.round(n*100)/100);
function localTouch(){const a=L.area;return {fl:[0,0],fr:[a.w,0],bl:[0,a.h],br:[a.w,a.h],c:[a.w/2,a.h/2]}[L.touch_off]}

/* ---------- persistence ---------- */
function deepMerge(base,over){
  if(Array.isArray(base))return Array.isArray(over)?over:base;
  if(base&&typeof base==='object'){const o={};for(const k in base)o[k]=deepMerge(base[k],over?.[k]);return o}
  return (over!==undefined&&typeof over===typeof base)?over:base;
}
/* Projects save themselves: every change schedules a save a moment later, like a Mac document. */
let saveT=0,saving=false,saveAgain=false,dirty=false,currentProject=null;
function restore(){try{return JSON.parse(localStorage.getItem(STORE)||'null')}catch(_){return null}}
function setDocState(text,err){const d=$('docName');d.textContent=text;d.classList.toggle('err',!!err)}
function docInfo(){return job.image?job.image.name:'Sample picture'}
function save(){
  if(!currentProject)return;
  dirty=true;setDocState(`Edited · ${docInfo()}`);
  clearTimeout(saveT);saveT=setTimeout(saveNow,900);
}
const projectBody=()=>({name:$('projName').value,state:{layout:L,effect:job.effect,params:job.params,prep:job.prep,aiModel:job.aiModel},image_id:job.image?.id||null});
async function saveNow(){
  clearTimeout(saveT);
  if(!currentProject||!dirty)return;
  if(saving){saveAgain=true;return}
  saving=true;dirty=false;const pid=currentProject;setDocState(`Saving… · ${docInfo()}`);
  try{
    const r=await fetch(`/api/projects/${pid}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(projectBody())});
    if(!r.ok)throw new Error();
    if(pid===currentProject)setDocState(dirty?`Edited · ${docInfo()}`:`Saved · ${docInfo()}`);
  }catch(_){dirty=true;if(pid===currentProject)setDocState('Couldn’t save. Retrying when you make the next change.',true)}
  finally{saving=false;if(saveAgain){saveAgain=false;saveNow()}}
}
window.addEventListener('beforeunload',()=>{
  if(currentProject&&dirty)fetch(`/api/projects/${currentProject}`,{method:'PUT',keepalive:true,headers:{'Content-Type':'application/json'},body:JSON.stringify(projectBody())});
});

/* ---------- server calls ---------- */
const post=(url,body)=>fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
const planBody=()=>({layout:L,effect:job.effect,params:job.params[job.effect]||{},prep:job.prep,image_id:job.image?.id||null});
function setStatus(t,err){const s=$('status');s.textContent=t;s.classList.toggle('err',!!err)}

let planT=0;
function schedulePlan(delay=250){clearTimeout(planT);planT=setTimeout(runPlan,delay);$('inkg').classList.add('stale')}
async function runPlan(){
  const seq=++planSeq;setStatus('Updating preview…');
  try{
    const r=await post('/api/plan',planBody());
    if(seq!==planSeq)return;
    if(r.status===404&&job.image){job.image=null;renderThumb();buildEffects();setStatus('The server lost the file (it was restarted). Choose it again.',true);schedulePlan(0);return}
    if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||r.statusText);
    const d=await r.json();if(seq!==planSeq)return;
    plan={lines:d.lines,stats:d.stats,w:L.area.w,h:L.area.h};checks=d.checks;
    buildInk();renderChecks();renderOver();renderStats();setStatus(`${job.effect} · ${d.stats.points.toLocaleString()} points`);
  }catch(e){if(seq===planSeq)setStatus('Preview failed: '+e.message,true)}
  finally{if(seq===planSeq)$('inkg').classList.remove('stale')}
}
let chkBusy=false,chkAgain=false;
async function refreshChecks(){
  if(chkBusy){chkAgain=true;return}chkBusy=true;
  try{const r=await post('/api/checks',L);if(r.ok){checks=await r.json();renderChecks();renderOver()}}catch(_){}
  chkBusy=false;if(chkAgain){chkAgain=false;refreshChecks()}
}
async function fitRequest(mode){
  setStatus(mode==='fit'?'Finding the largest safe area…':'Centering…');
  try{
    const r=await post('/api/fit/'+mode,L),d=await r.json();
    if(!r.ok){setStatus(typeof d.detail==='string'?d.detail:'No safe spot found.',true);return}
    const resized=d.w!==L.area.w||d.h!==L.area.h;
    L.area={...L.area,x:Math.round(d.x*10)/10,y:Math.round(d.y*10)/10,w:d.w,h:d.h};
    changed(resized?['area.w']:['area.x']);setStatus(mode==='fit'?`Fitted ${fmt(d.w)} × ${fmt(d.h)} mm`:'Centered');
  }catch(_){setStatus('Could not reach the BambuPlot server.',true)}
}
async function download(url,body,name){
  $('problems').hidden=true;
  try{
    const r=await post(url,body);
    if(!r.ok){const e=await r.json().catch(()=>({}));showProblems(e.detail);return}
    const blob=await r.blob(),a=document.createElement('a');
    a.href=URL.createObjectURL(blob);a.download=name;document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(a.href),4000);setStatus(`Saved ${name}`);
  }catch(e){showProblems({message:'Could not reach the BambuPlot server. Is `bambuplot serve` still running?'})}
}
function showProblems(d){
  const p=$('problems');p.hidden=false;
  const msg=typeof d==='string'?d:(d?.message||'The server refused this job.');
  const list=(d?.problems||[]).map(x=>`<li>${esc(x)}</li>`).join('');
  p.innerHTML=`<b>${esc(msg)}</b>${list?`<ul>${list}</ul>`:''}<button class="x" type="button" aria-label="Dismiss">×</button>`;
  p.querySelector('.x').onclick=()=>{p.hidden=true};
}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

/* ---------- rendering ---------- */
const svg=$('plate');
function computeView(){
  const pts=[...corners(paperBox()),...corners({...reach(),rot:0}),...corners({...BED,rot:0}),...L.clips.flatMap(c=>corners({...clipRect(c),rot:0}))];
  const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
  V={x0:Math.min(...xs)-14,y0:Math.min(...ys)-16,x1:Math.max(...xs)+10,y1:Math.max(...ys)+18};
  svg.setAttribute('viewBox',`0 0 ${V.x1-V.x0} ${V.y1-V.y0}`);
}
const rA=r=>`x="${(r.x-V.x0).toFixed(2)}" y="${(V.y1-y2(r)).toFixed(2)}" width="${Math.max(0,r.w).toFixed(2)}" height="${Math.max(0,r.h).toFixed(2)}"`;
const X=x=>(x-V.x0).toFixed(2),Y=y=>(V.y1-y).toFixed(2);
/* SVG rotate() is clockwise on screen; our angles are CCW seen from above. */
const rotAttr=b=>{const [cx,cy]=centerOf(b);return b.rot?`transform="rotate(${-b.rot} ${X(cx)} ${Y(cy)})"`:''};
const isBad=()=>checks.some(c=>c.state==='bad');

function renderUnder(){
  const p=paperBox(),pr=R(p.x,p.y,p.w,p.h),m=L.paper.margin;
  let h=`<defs><mask id="offbed" maskUnits="userSpaceOnUse" x="0" y="0" width="${V.x1-V.x0}" height="${V.y1-V.y0}"><rect x="0" y="0" width="${V.x1-V.x0}" height="${V.y1-V.y0}" fill="white"></rect><rect ${rA(BED)} fill="black"></rect></mask></defs>`;
  h+=`<rect class="bed" ${rA(BED)} rx="2"></rect>`;
  for(let i=10;i<180;i+=10)h+=`<line class="grid" x1="${X(i)}" y1="${Y(0)}" x2="${X(i)}" y2="${Y(180)}"></line><line class="grid" x1="${X(0)}" y1="${Y(i)}" x2="${X(180)}" y2="${Y(i)}"></line>`;
  h+=`<text class="bedlbl" x="${X(178)}" y="${Y(176)}" text-anchor="end">A1 MINI PLATE 180 × 180</text>`;
  h+=`<g ${rotAttr(p)}><rect class="paper${sel==='paper'?' sel':''}" data-drag="paper" ${rA(pr)}></rect></g>`;
  h+=`<g mask="url(#offbed)"><g ${rotAttr(p)}><rect class="overhang" ${rA(pr)}></rect></g></g>`;
  h+=`<g ${rotAttr(p)}><rect class="pmargin" ${rA(R(p.x+m,p.y+m,p.w-2*m,p.h-2*m))}></rect>`;
  h+=`<text class="paperlbl" x="${X(p.x+2)}" y="${Y(y2(pr)-4.5)}">${L.paper.size} ${p.w} × ${p.h}${p.rot?` · ${fmt(p.rot)}°`:''}</text></g>`;
  keepouts().forEach(k=>h+=`<rect class="keep" ${rA(k)}></rect>`);
  $('under').innerHTML=h;
}
function knob(b,kind){
  const [bx,by]=toWorld(b,b.w/2,b.h),[kx,ky]=toWorld(b,b.w/2,b.h+9);
  return `<line class="knobline" x1="${X(bx)}" y1="${Y(by)}" x2="${X(kx)}" y2="${Y(ky)}"></line>`+
    `<circle class="knob" data-drag="rot-${kind}" cx="${X(kx)}" cy="${Y(ky)}" r="2.6"><title>Drag to rotate</title></circle>`+
    `<text class="angle" x="${(kx-V.x0+4).toFixed(2)}" y="${(V.y1-ky+1.2).toFixed(2)}">${fmt(b.rot)}°</text>`;
}
function renderOver(){
  const a=areaBox(),ar=R(a.x,a.y,a.w,a.h);let h='';
  if(L.pen.offset_x||L.pen.offset_y){const r=reach();h+=`<rect class="reach" ${rA(r)}></rect><text class="offlbl" x="${X(x2(r))}" y="${(V.y1-y2(r)-1.5).toFixed(2)}" text-anchor="end" style="fill:var(--warn)">pen reach</text>`}
  h+=`<g ${rotAttr(a)}><rect class="area${sel==='area'?' sel':''}${isBad()?' bad':''}" data-drag="area" ${rA(ar)}></rect>`;
  h+=`<text class="dim" x="${X(a.x+a.w/2)}" y="${(V.y1-a.y+5).toFixed(2)}" text-anchor="middle">${fmt(a.w)} × ${fmt(a.h)} mm</text></g>`;
  [['fl',0,0],['fr',a.w,0],['bl',0,a.h],['br',a.w,a.h]].forEach(([k,u,v])=>{
    const [x,y]=toWorld(a,u,v);
    h+=`<rect class="handle" data-drag="h-${k}" x="${(x-V.x0-2).toFixed(2)}" y="${(V.y1-y-2).toFixed(2)}" width="4" height="4" rx=".6" style="cursor:crosshair"></rect>`;
  });
  const [lx,ly]=localTouch(),[tx,ty]=toWorld(a,lx,ly);
  h+=`<path class="touch" d="M${X(tx-3)} ${Y(ty)}H${X(tx+3)}M${X(tx)} ${Y(ty-3)}V${Y(ty+3)}"></path><circle class="touch" cx="${X(tx)}" cy="${Y(ty)}" r="1.6"></circle>`;
  L.clips.forEach((c,i)=>{const r=clipRect(c);h+=`<rect class="clip${sel==='clip:'+i?' sel':''}" data-drag="clip:${i}" ${rA(r)} rx="1"></rect><text class="cliplbl" x="${X(r.x+r.w/2)}" y="${(V.y1-r.y-r.h/2+1.3).toFixed(2)}" text-anchor="middle">${i+1}</text>`});
  if(sel==='area')h+=knob(a,'area');
  if(sel==='paper')h+=knob(paperBox(),'paper');
  h+=`<path class="axis" d="M${X(0)} ${Y(0)}m0 7h12M${X(0)} ${Y(0)}m-7 0v-12"></path><path class="axisfill" d="M${X(12)} ${(V.y1+7).toFixed(2)}l-2.4 -1.3v2.6zM${(-V.x0-7).toFixed(2)} ${Y(12)}l-1.3 2.4h2.6z"></path>`;
  h+=`<text class="offlbl" x="${X(14)}" y="${(V.y1+8.2).toFixed(2)}">X</text><text class="offlbl" x="${(-V.x0-8.2).toFixed(2)}" y="${Y(14)}">Y</text><text class="offlbl" x="${X(0)}" y="${(V.y1+12.5).toFixed(2)}" text-anchor="middle">0,0</text>`;
  $('over').innerHTML=h;
}
function placeInk(){
  const a=areaBox(),g=$('inkg'),pw=plan?.w||a.w,ph=plan?.h||a.h,[cx,cy]=centerOf(a);
  g.setAttribute('transform',`translate(${X(cx)} ${Y(cy)}) rotate(${-a.rot}) scale(${a.w/pw} ${a.h/ph}) translate(${-pw/2} ${-ph/2})`);
  const c=$('inkClipRect');c.setAttribute('width',pw);c.setAttribute('height',ph);
  const raster=srcKind()==='raster',turn=raster?(+job.prep.rotate||0):0,side=turn===90||turn===270;
  const iw=job.image?.w||1,ih=job.image?.h||1,cr=(raster&&job.prep.crop)||[0,0,1,1];
  const sw=side?ph:pw,sh=side?pw:ph,wrap=$('srcwrap'),im=$('srcimg');
  wrap.setAttribute('x',(pw-sw)/2);wrap.setAttribute('y',(ph-sh)/2);wrap.setAttribute('width',sw);wrap.setAttribute('height',sh);
  wrap.setAttribute('viewBox',`${cr[0]*iw} ${cr[1]*ih} ${(cr[2]-cr[0])*iw} ${(cr[3]-cr[1])*ih}`);
  wrap.setAttribute('preserveAspectRatio',!raster||job.prep.fit==='fit'?'xMidYMid meet':'xMidYMid slice');
  im.setAttribute('width',iw);im.setAttribute('height',ih);
  $('srcturn').setAttribute('transform',turn?`rotate(${turn} ${pw/2} ${ph/2})`:'');
  $('srcturn').setAttribute('visibility',job.image&&$('showImg').checked?'visible':'hidden');
  $('ink').style.strokeWidth=L.pen.width_mm;
}
function buildInk(){
  lineStr=[];travelStr=[];totalPts=0;let prev=null;const h=plan.h;
  for(const l of plan.lines){
    const pts=l.length===1?[l[0],[l[0][0]+0.01,l[0][1]]]:l;
    let s='M'+pts[0][0]+' '+(h-pts[0][1]);for(let i=1;i<pts.length;i++)s+='L'+pts[i][0]+' '+(h-pts[i][1]);
    lineStr.push(s);totalPts+=l.length;
    travelStr.push(prev?`M${prev[0]} ${h-prev[1]}L${l[0][0]} ${h-l[0][1]}`:'');prev=l[l.length-1];
  }
  drawProgress();placeInk();
}
function drawProgress(){
  if(!plan)return;const f=+$('progress').value/1000;
  let ink='',tr='';
  if(f>=1){ink=lineStr.join('');tr=travelStr.join('')}
  else{
    let budget=Math.round(totalPts*f);const h=plan.h;
    for(let i=0;i<plan.lines.length&&budget>0;i++){
      const l=plan.lines[i];tr+=travelStr[i];
      if(l.length<=budget){ink+=lineStr[i];budget-=l.length}
      else{let s='M'+l[0][0]+' '+(h-l[0][1]);for(let k=1;k<budget;k++)s+='L'+l[k][0]+' '+(h-l[k][1]);ink+=s;budget=0}
    }
  }
  $('ink').setAttribute('d',ink);$('travel').setAttribute('d',$('showTravel').checked?tr:'');
}
function renderChecks(){
  $('checks').innerHTML=checks.map(c=>`<li class="${c.state}"><b>${esc(c.title)}</b><small>${esc(c.detail)}</small></li>`).join('');
  const bad=checks.filter(c=>c.state==='bad').length,warn=checks.filter(c=>c.state==='warn').length,b=$('checksBtn');
  b.className='badge '+(bad?'bad':warn?'warn':'ok');
  b.textContent=bad?`${bad} safety problem${bad>1?'s':''}`:warn?`Ready · ${warn} note${warn>1?'s':''}`:'Ready to plot';
  b.title=bad?'Fix these before downloading or sending':'All safety checks pass';
  $('dlG').disabled=$('dl3mf').disabled=$('dlSmoke').disabled=$('dlPress').disabled=$('sendOpen').disabled=isBad();
}
function renderStats(){
  const s=plan?.stats;if(!s)return;
  $('nInk').textContent=`${s.draw_m} m`;$('nLifts').textContent=s.pen_lifts.toLocaleString();
  $('nTravel').textContent=`${s.travel_m} m`;$('nTime').textContent=s.minutes<60?`${Math.max(1,Math.round(s.minutes))} min`:`${Math.floor(s.minutes/60)} h ${Math.round(s.minutes%60)} m`;
}
function render(){if(!drag)computeView();renderUnder();renderOver();placeInk();syncInputs();fillAll()}

/* ---------- inputs ---------- */
const getP=(o,p)=>p.split('.').reduce((a,k)=>a?.[k],o);
const setP=(o,p,v)=>{const ks=p.split('.'),l=ks.pop();ks.reduce((a,k)=>a[k],o)[l]=v};
const layoutInputs=[...document.querySelectorAll('[id^="L."]')];
const prepInputs=[...document.querySelectorAll('input[id^="P."],select[id^="P."]')];
function syncInputs(){
  for(const el of layoutInputs){if(el===document.activeElement)continue;const v=getP(L,el.id.slice(2));el.value=typeof v==='number'?Math.round(v*100)/100:v}
  for(const el of prepInputs){const v=job.prep[el.id.slice(2)];if(el.type==='range'){const o=$(el.id+'.out');if(o)o.textContent=fmtVal(v,el.step,el.dataset.unit||'')}if(el===document.activeElement)continue;if(el.type==='checkbox')el.checked=!!v;else el.value=v}
  document.querySelectorAll('#orient button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.o===L.paper.orientation));
  $('cliplist').innerHTML=L.clips.map((c,i)=>`<li data-i="${i}" class="${sel==='clip:'+i?'sel':''}"><span>Clip ${i+1} · X ${fmt(c.x)} Y ${fmt(c.y)}</span><button type="button" aria-label="Remove clip ${i+1}" data-rm="${i}">×</button></li>`).join('');
  $('aspect').disabled=!job.image;
}
function changed(keys){
  render();refreshChecks();save();
  if(keys.some(k=>PLAN_KEYS.has(k)||k.startsWith('feed.')||k.startsWith('z.')))schedulePlan();
}
for(const el of layoutInputs)el.addEventListener('input',()=>{
  const key=el.id.slice(2);let v=el.value;
  if(el.type==='number'){v=parseFloat(v);if(!isFinite(v))return;if(el.min!==''&&v<+el.min)return;if(el.max!==''&&v>+el.max)return}
  if(key.endsWith('rotation'))v=normAng(v);
  setP(L,key,v);changed([key]);
});
for(const el of prepInputs)el.addEventListener('input',()=>{
  const key=el.id.slice(2);let v=el.type==='checkbox'?el.checked:el.dataset.kind==='text'?el.value:parseFloat(el.value);
  if(el.type==='number'&&(!isFinite(v)||(el.min!==''&&v<+el.min)))return;
  job.prep[key]=v;if(el.type==='range'){const o=$(el.id+'.out');if(o)o.textContent=fmtVal(v,el.step,el.dataset.unit||'')}save();placeInk();schedulePlan(400);
});
document.querySelectorAll('#orient button').forEach(b=>b.onclick=()=>{
  if(L.paper.orientation===b.dataset.o)return;const [cx,cy]=centerOf(paperBox());
  L.paper.orientation=b.dataset.o;const q=paperBox();L.paper.x=snapv(cx-q.w/2);L.paper.y=snapv(cy-q.h/2);changed(['paper']);
});
/* rotation buttons: data-rot="area:90" adds, "area:=0" sets, "area:=paper" copies the paper angle */
function rotate(target,spec){
  const obj=target==='paper'?L.paper:L.area,cur=obj.rotation||0;
  obj.rotation=normAng(spec==='=paper'?L.paper.rotation:spec.startsWith('=')?+spec.slice(1):cur+ +spec);
  sel=target;changed([target+'.rotation']);
}
document.querySelectorAll('[data-rot]').forEach(b=>b.onclick=()=>{const [t,s]=b.dataset.rot.split(':');rotate(t,s)});
$('cliplist').addEventListener('click',e=>{
  const rm=e.target.closest('[data-rm]');if(rm){L.clips.splice(+rm.dataset.rm,1);sel='area';changed(['clips']);return}
  const li=e.target.closest('li');if(li){sel='clip:'+li.dataset.i;render();svg.focus()}
});
$('addClip').onclick=()=>{L.clips.push({x:82,y:-6});sel='clip:'+(L.clips.length-1);changed(['clips'])};
$('fit').onclick=()=>fitRequest('fit');
$('center').onclick=()=>fitRequest('center');
$('aspect').onclick=()=>{
  if(!job.image)return;const a=L.area,[cx,cy]=centerOf(a),turn=srcKind()==='svg'?0:(+job.prep.rotate||0);
  const [iw,ih]=turn===90||turn===270?[job.image.h,job.image.w]:[job.image.w,job.image.h];
  a.h=Math.round(a.w*ih/iw*10)/10;a.x=cx-a.w/2;a.y=snapv(cy-a.h/2);changed(['area.h']);
};
$('snap').onchange=e=>{snap=e.target.checked};
$('progress').oninput=drawProgress;$('showTravel').onchange=drawProgress;$('showImg').onchange=placeInk;
for(const [btn,box,fn] of [['travelBtn','showTravel',drawProgress],['imgBtn','showImg',placeInk]]){
  $(btn).onclick=()=>{const c=$(box);c.checked=!c.checked;$(btn).setAttribute('aria-pressed',c.checked);fn()};
}
function selectTab(id){
  document.querySelectorAll('.tabs [role=tab]').forEach(x=>{const on=x.id===id;x.setAttribute('aria-selected',on);$(x.getAttribute('aria-controls')).hidden=!on});
}
document.querySelectorAll('.tabs [role=tab]').forEach(t=>t.onclick=()=>selectTab(t.id));

/* effect + params */
const GROUP_ORDER=['Trace to vector lines','Single continuous line','Hatching & engraving','Dots','Vector file'];
const srcKind=()=>job.image?.kind==='svg'?'svg':'raster';
function buildEffects(){
  const kind=srcKind(),usable=effects.filter(e=>(e.source||'raster')===kind);
  if(!usable.some(e=>e.name===job.effect)){
    job.effect=kind==='svg'?'svg':(usable.some(e=>e.name===job.lastRaster)?job.lastRaster:'waves');
  }
  if(kind==='raster')job.lastRaster=job.effect;
  const opt=e=>`<option value="${e.name}">${esc(e.label)}</option>`;
  const groups=[...new Set([...GROUP_ORDER,...usable.map(e=>e.group)])].filter(g=>usable.some(e=>e.group===g));
  $('effect').innerHTML=groups.length<2?usable.map(opt).join(''):
    groups.map(g=>`<optgroup label="${esc(g)}">${usable.filter(e=>e.group===g).map(opt).join('')}</optgroup>`).join('');
  $('effect').value=job.effect;$('toneControls').hidden=kind==='svg';buildParams();
}
/* Number shown beside a slider: trims to the step's decimals and adds the unit. */
function fmtVal(v,step,unit){
  const dec=Math.max(0,Math.min(3,String(step??1).split('.')[1]?.length||0));
  const n=Number(v).toFixed(dec);
  return unit?(unit==='°'||unit==='×'?n+unit:`${n} ${unit}`):n;
}
function sliderHTML(id,label,v,min,max,step,unit,help){
  return `<div class="srow"${help?` title="${esc(help)}"`:''}><label for="${id}">${esc(label)}</label>`+
    `<input type="range" id="${id}" min="${min}" max="${max}" step="${step??'any'}" value="${v}">`+
    `<output id="${id}.out">${fmtVal(v,step,unit)}</output></div>`;
}
function buildParams(){
  const e=effects.find(x=>x.name===job.effect)||effects[0];job.effect=e.name;
  $('effectDesc').textContent=e.description;
  const cur=job.params[e.name]||(job.params[e.name]={});
  $('params').innerHTML=e.params.map(p=>{
    const v=cur[p.name]??p.default,id='E.'+p.name,help=p.help?`<small>${esc(p.help)}</small>`:'';
    let html;
    const tip=p.help?` title="${esc(p.help)}"`:'';
    if(p.kind==='bool')html=`<div class="srow sw"${tip}><label for="${id}">${esc(p.label)}</label><input type="checkbox" class="switch" id="${id}" ${v?'checked':''}></div>`;
    else if(p.kind==='choice')html=`<div class="srow sel"${tip}><label for="${id}">${esc(p.label)}</label><select id="${id}">${p.choices.map(c=>`<option ${c===v?'selected':''}>${esc(c)}</option>`).join('')}</select></div>`;
    else if(p.min!=null&&p.max!=null)html=sliderHTML(id,p.label,v,p.min,p.max,p.step,p.unit,p.help);
    else html=`<div class="srow sel"${tip}><label for="${id}">${esc(p.label)}</label><input type="number" id="${id}" value="${v}"></div>`;
    return `<div class="param" data-p="${p.name}">${html}</div>`;
  }).join('');
  const current=name=>{const p=e.params.find(q=>q.name===name);return cur[name]??p?.default};
  const applyWhen=()=>e.params.forEach(p=>{
    const box=document.querySelector(`#params [data-p="${p.name}"]`);
    box.hidden=!!p.when&&!Object.entries(p.when).every(([k,vals])=>vals.includes(current(k)));
  });
  e.params.forEach(p=>{const el=$('E.'+p.name);el.addEventListener('input',()=>{
    let v=p.kind==='bool'?el.checked:p.kind==='choice'?el.value:parseFloat(el.value);
    if((p.kind==='float'||p.kind==='int')&&!isFinite(v))return;
    if(p.kind==='int')v=Math.round(v);
    const out=$('E.'+p.name+'.out');if(out)out.textContent=fmtVal(v,p.step,p.unit);
    cur[p.name]=v;applyWhen();save();schedulePlan(p.kind==='int'||p.kind==='float'?400:0);
  })});
  applyWhen();
}
$('resetParams').onclick=()=>{job.params[job.effect]={};buildParams();save();schedulePlan(0)};
$('effect').onchange=e=>{job.effect=e.target.value;buildParams();save();schedulePlan(0)};

/* image */
function renderThumb(){
  const t=$('thumb'),n=$('srcNotes'),im=job.image;
  n.hidden=!(im?.warnings?.length);n.innerHTML=(im?.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('');
  $('tbEdit').disabled=im?.kind!=='raster';if(currentProject)setDocState(`${dirty?'Edited':'Saved'} · ${docInfo()}`);
  if(!im){t.innerHTML='<label class="drop" for="file">Drop a PNG, JPG or SVG here, or click to choose.<br>The preview uses a sample sphere until then.</label>';$('srcimg').setAttribute('href','');}
  else{
    const info=im.kind==='svg'
      ?`SVG · ${im.strokes} line${im.strokes===1?'':'s'}, ${im.fills} filled shape${im.fills===1?'':'s'} · ${im.w} × ${im.h} mm`
      :`${im.w} × ${im.h} px`;
    t.innerHTML=`<img src="${im.url}" alt=""><span>${esc(im.name)}<br><small>${info}</small></span><button class="btn small" type="button" id="clearSrc">Remove</button>`;
    $('srcimg').setAttribute('href',im.url);
    $('clearSrc').onclick=()=>{URL.revokeObjectURL(im.url);job.image=null;job.prep.crop=null;$('file').value='';renderThumb();buildEffects();save();schedulePlan(0)};
  }
  $('selBox').hidden=im?.kind!=='raster';renderCropInfo();
  if(im?.kind==='raster'){$('selInfo').textContent=selText(im.sel);$('selOpts').hidden=!im.sel?.has_mask;if(im.sel?.has_mask)$('srcimg').setAttribute('href',im.sel.preview)}
  placeInk();syncInputs();
}
$('file').onchange=e=>{const f=e.target.files[0];if(f)openFile(f)};
async function openFile(f){
  setStatus('Reading file…');
  const fd=new FormData();fd.append('file',f);
  try{
    const r=await fetch('/api/image',{method:'POST',body:fd});const d=await r.json();
    if(!r.ok)throw new Error(d.detail||r.statusText);
    if(job.image?.url)URL.revokeObjectURL(job.image.url);
    job.prep.crop=null;
    job.image={id:d.id,kind:d.kind,w:d.width,h:d.height,name:f.name,warnings:d.warnings||[],strokes:d.strokes,fills:d.fills,sel:d.selection||null,url:URL.createObjectURL(f)};
    renderThumb();buildEffects();save();schedulePlan(0);selectTab('tab-effect');
  }catch(err){setStatus('Upload failed: '+err.message,true)}
}

/* layout file */
$('exportLayout').onclick=()=>{const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(L,null,2)],{type:'application/json'}));a.download='bambuplot-layout.json';a.click();setTimeout(()=>URL.revokeObjectURL(a.href),4000)};
$('importLayout').onchange=async e=>{
  const f=e.target.files[0];if(!f)return;
  try{L=deepMerge(DEF.layout,JSON.parse(await f.text()));changed(['area.w']);setStatus('Layout loaded')}catch(_){setStatus('That file is not a BambuPlot layout.',true)}
  e.target.value='';
};

/* downloads */
for(const id of ['dlG','dl3mf','dlSmoke','dlPress'])$(id).addEventListener('click',()=>$('dlMenu').hidePopover?.());
$('dlG').onclick=()=>download('/api/gcode',planBody(),`bambuplot-${job.effect}.gcode`);
$('dl3mf').onclick=async()=>{
  $('problems').hidden=true;
  try{
    const r=await post('/api/package',{...planBody(),kind:'art'});
    if(!r.ok){const e=await r.json().catch(()=>({}));showProblems(r.status===404?'The server is out of date. Restart it.':e.detail);return}
    const name=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||'bambuplot.gcode.3mf';
    const a=document.createElement('a');a.href=URL.createObjectURL(await r.blob());a.download=name;document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(a.href),4000);setStatus(`Saved ${name}`);
  }catch(_){showProblems('Could not reach the BambuPlot server.')}
};
$('dlSmoke').onclick=()=>download('/api/calibrate/smoke',L,'bambuplot-smoke.gcode');
$('dlPress').onclick=()=>download('/api/calibrate/press',L,'bambuplot-press.gcode');

/* ---------- dragging ---------- */
function toMM(e){const pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const q=pt.matrixTransform(svg.getScreenCTM().inverse());return {x:q.x+V.x0,y:V.y1-q.y}}
svg.addEventListener('pointerdown',e=>{
  const t=e.target.closest('[data-drag]');if(!t)return;
  const k=t.dataset.drag;drag={k,m:toMM(e),paper:{...L.paper},area:{...L.area},clips:L.clips.map(c=>({...c}))};
  sel=k.startsWith('h-')||k==='rot-area'?'area':k==='rot-paper'?'paper':k;
  svg.setPointerCapture(e.pointerId);svg.focus({preventScroll:true});e.preventDefault();render();
});
/* Resize keeps the opposite corner fixed in the world, working in the area's own axes. */
function resizeArea(o,corner,dx,dy){
  const b={...o,rot:o.rotation||0},left=corner[1]==='l',front=corner[0]==='f';
  const [ox,oy]=toWorld(b,left?b.w:0,front?b.h:0);          // fixed opposite corner
  const [mx,my]=toWorld(b,left?0:b.w,front?0:b.h);          // dragged corner
  const [lu,lv]=rotPt(mx+dx-ox,my+dy-oy,-b.rot);             // drag vector in local axes
  let w=snapv(left?-lu:lu),h=snapv(front?-lv:lv);w=Math.max(10,w);h=Math.max(10,h);
  const [cu,cv]=rotPt((left?-w:w)/2,(front?-h:h)/2,b.rot);   // new center, measured from the fixed corner
  const cx=ox+cu,cy=oy+cv;
  return {...o,w,h,x:Math.round((cx-w/2)*100)/100,y:Math.round((cy-h/2)*100)/100};
}
let raf=0;
svg.addEventListener('pointermove',e=>{
  if(!drag)return;const m=toMM(e),dx=m.x-drag.m.x,dy=m.y-drag.m.y,k=drag.k;
  if(k==='paper'){L.paper.x=snapv(drag.paper.x+dx);L.paper.y=snapv(drag.paper.y+dy)}
  else if(k==='area'){L.area.x=snapv(drag.area.x+dx);L.area.y=snapv(drag.area.y+dy)}
  else if(k.startsWith('clip:')){const i=+k.slice(5),o=drag.clips[i];L.clips[i]={x:snapv(o.x+dx),y:snapv(o.y+dy)}}
  else if(k.startsWith('rot-')){
    const obj=k==='rot-area'?L.area:L.paper,b=k==='rot-area'?areaBox():paperBox(),[cx,cy]=centerOf(b);
    let ang=Math.atan2(m.y-cy,m.x-cx)*180/Math.PI-90;const step=e.shiftKey?15:1;
    obj.rotation=normAng(Math.round(ang/step)*step);
  }
  else L.area=resizeArea(drag.area,k.slice(2),dx,dy);
  if(!raf)raf=requestAnimationFrame(()=>{raf=0;render();refreshChecks()});
});
const end=()=>{if(!drag)return;const resized=drag.k.startsWith('h-');drag=null;changed(resized?['area.w']:['moved'])};
svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
svg.addEventListener('keydown',e=>{
  if(e.key==='['||e.key===']'||e.key==='{'||e.key==='}'){
    const t=sel==='paper'?'paper':sel==='area'?'area':null;if(!t)return;e.preventDefault();
    const step=e.shiftKey?15:1;rotate(t,String(e.key==='['||e.key==='{'?step:-step));return;
  }
  const d={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,1],ArrowDown:[0,-1]}[e.key];if(!d)return;e.preventDefault();
  const s=e.shiftKey?10:1,ax=d[0]*s,ay=d[1]*s;
  if(sel==='paper'){L.paper.x+=ax;L.paper.y+=ay}else if(sel==='area'){L.area.x+=ax;L.area.y+=ay}
  else if(sel.startsWith('clip:')){const c=L.clips[+sel.slice(5)];if(c){c.x+=ax;c.y+=ay}}
  changed(['moved']);
});


/* ---------- printer ---------- */
const errText=d=>typeof d==='string'?d:(d?.message?d.message+(d.problems?.length?': '+d.problems.slice(0,3).join('; '):''):'Something went wrong.');
function msg(id,text,kind){const m=$(id);m.textContent=text;m.className='msg'+(kind?' '+kind:'')}
let printerCfg=null;
function renderPrinter(c){
  printerCfg=c;$('pr-ip').value=c.ip;$('pr-serial').value=c.serial;$('pr-url').value=c.url_prefix;
  const code=$('pr-code');code.value='';code.disabled=c.access_code_from_env;
  code.placeholder=c.access_code_from_env?'Set by BAMBU_ACCESS_CODE':c.has_access_code?'Saved. Leave blank to keep it.':'8 characters, from the printer screen';
  const t=c.template;
  $('tpl-info').textContent=!t?'No template yet. Plain .gcode will be uploaded.':t.error?t.error:
    `Template ready: sliced for the ${t.printer_model||'unknown printer'}, plots go into ${t.plate}.${t.warning?' '+t.warning:''}`;
  $('tpl-remove').disabled=!t;
}
async function loadPrinter(){
  try{const r=await fetch('/api/printer');
    if(r.status===404){msg('pr-msg','The server is out of date. Stop it (Ctrl+C) and run `uv run bambuplot serve` again.','err');return}
    renderPrinter(await r.json())}catch(_){}
}
async function savePrinter(){
  const code=$('pr-code').value.trim();
  const r=await post('/api/printer',{ip:$('pr-ip').value,serial:$('pr-serial').value,url_prefix:$('pr-url').value||'file:///sdcard/',access_code:code?code:null});
  if(!r.ok){msg('pr-msg',r.status===404?'The server is out of date. Stop it (Ctrl+C) and run `uv run bambuplot serve` again.':'Could not save the settings.','err');return false}
  renderPrinter(await r.json());return true;
}
$('pr-save').onclick=async()=>{if(await savePrinter())msg('pr-msg','Saved.','ok')};
$('pr-test').onclick=async()=>{
  if(!await savePrinter())return;msg('pr-msg','Connecting…');
  try{const r=await post('/api/printer/test',{}),d=await r.json();
    if(!r.ok){msg('pr-msg',errText(d.detail),'err');return}
    msg('pr-msg',`Connected. The printer is ${String(d.state||'unknown').toLowerCase()}.`,'ok');
  }catch(_){msg('pr-msg','Could not reach the BambuPlot server.','err')}
};
$('tpl-file').onchange=async e=>{
  const f=e.target.files[0];if(!f)return;const fd=new FormData();fd.append('file',f);
  const r=await fetch('/api/printer/template',{method:'POST',body:fd}),d=await r.json();e.target.value='';
  if(!r.ok){$('tpl-info').textContent=errText(d.detail);return}
  loadPrinter();
};
$('tpl-remove').onclick=async()=>{await fetch('/api/printer/template',{method:'DELETE'});loadPrinter()};
$('sendOpen').onclick=()=>{
  if(isBad()){$('checksPop').showPopover?.();return}
  $('send-ack').checked=false;$('send-start').disabled=$('send-connect').disabled=true;msg('send-msg','');loadPrinter();
  $('sendSheet').showModal();
};
$('send-cancel').onclick=()=>$('sendSheet').close();
$('send-ack').onchange=e=>{$('send-start').disabled=$('send-connect').disabled=!e.target.checked};
async function sendJob(start){
  const kind=$('send-kind').value;
  if(isBad()){msg('send-msg','Fix the failing safety checks first.','err');return}
  if(printerCfg&&(!printerCfg.ip||!printerCfg.has_access_code)){msg('send-msg','Add the printer IP and access code in the Printer tab first.','err');return}
  $('send-upload').disabled=$('send-start').disabled=true;msg('send-msg',start?'Uploading and starting…':'Uploading…');
  try{
    const r=await post('/api/send',{...planBody(),kind,start}),d=await r.json();
    if(!r.ok){msg('send-msg',errText(d.detail),'err');return}
    msg('send-msg',d.started?`Started ${d.file}. Go to the printer: at Pause 1, put the pen in and touch it to the paper.`
      :`Uploaded ${d.file}. Start it from the printer's screen when you're ready.`,'ok');
    $('send-ack').checked=false;
  }catch(_){msg('send-msg','Could not reach the BambuPlot server.','err')}
  finally{$('send-upload').disabled=false;$('send-start').disabled=!$('send-ack').checked}
}
$('send-upload').onclick=()=>sendJob(false);
$('send-connect').onclick=async()=>{
  if(isBad()){msg('send-msg','Fix the failing safety checks first.','err');return}
  $('send-connect').disabled=true;msg('send-msg','Packaging for Bambu Connect…');
  try{
    const r=await post('/api/connect',{...planBody(),kind:$('send-kind').value}),d=await r.json();
    if(!r.ok){msg('send-msg',errText(d.detail),'err');return}
    msg('send-msg',`Opened ${d.file} in Bambu Connect. Choose your printer there and send it, then start it at the printer. Saved at ${d.path}.`,'ok');
  }catch(_){msg('send-msg','Could not reach the BambuPlot server.','err')}
  finally{$('send-connect').disabled=!$('send-ack').checked}
};
$('send-start').onclick=()=>sendJob(true);


/* ---------- subject selection / background removal ---------- */
const ED={tool:'box',img:null,mask:null,busy:false,dw:0,dh:0,drag:null,hover:null};
function selText(d){
  if(!d?.has_mask)return 'Nothing removed yet.';
  const how=!d.method?'':d.method.startsWith('ai:')?` (AI: ${d.method.slice(3).toLowerCase()})`:` (found a ${d.method==='photo'?'subject in a photo':'plain background'})`;
  return `Background removed: ${Math.round(d.kept*100)}% of the picture kept${how}.`;
}
function applySel(d){
  if(!job.image||!d)return;
  job.image.sel=d;
  $('selInfo').textContent=selText(d);$('selOpts').hidden=!d.has_mask;
  $('srcimg').setAttribute('href',d.has_mask?d.preview:job.image.url);
  const st=$('edStatus');st.classList.remove('err');
  st.textContent=ED.tool==='crop'?cropText():d.has_mask?selText(d)+' Grey checks show what will be left blank.':'Drag a box around the subject, or press Remove background.';
  $('edUndo').disabled=!d.can_undo;
  if(d.mask){const im=new Image();im.onload=()=>{ED.mask=im;drawOverlay()};im.src=d.mask}else{ED.mask=null;drawOverlay()}
  schedulePlan(0);
}
async function maskOp(body,where='edStatus'){
  if(!job.image||job.image.kind!=='raster'||ED.busy)return;
  ED.busy=true;const st=$(where);st.classList?.remove('err');
  const aiM=AI.models.find(x=>x.name===body.model);
  st.textContent=body.op==='ai'?(aiM&&!aiM.downloaded?`Downloading the ${aiM.label.toLowerCase()} model (${aiM.size_mb} MB) the first time, then running it…`:'Running the AI model…'):
    body.op==='auto'?'Looking for the subject…':body.op==='select'||body.smart?'Finding the subject\'s edges…':'Updating…';
  try{
    const r=await post(`/api/image/${job.image.id}/mask`,body),d=await r.json();
    if(!r.ok){st.textContent=typeof d.detail==='string'?d.detail:'That didn\'t work.';st.classList?.add('err');return}
    applySel(d);save();if(where!=='edStatus')$('selInfo').textContent=selText(d);
    if(body.op==='ai')loadAI();
  }catch(_){st.textContent='Could not reach the BambuPlot server.';st.classList?.add('err')}
  finally{ED.busy=false;drawEdStroke()}
}
function layoutEd(){
  if(!ED.img)return;
  const stage=$('edStage').getBoundingClientRect(),pad=28,ar=ED.img.naturalWidth/ED.img.naturalHeight;
  let w=stage.width-pad,h=w/ar;if(h>stage.height-pad){h=stage.height-pad;w=h*ar}
  ED.dw=Math.max(50,Math.floor(w));ED.dh=Math.max(50,Math.floor(h));
  const box=$('edCanvas');box.style.width=ED.dw+'px';box.style.height=ED.dh+'px';
  const dpr=window.devicePixelRatio||1;
  for(const id of ['edImg','edOver','edDraw']){const c=$(id);c.width=Math.round(ED.dw*dpr);c.height=Math.round(ED.dh*dpr);c.getContext('2d').setTransform(dpr,0,0,dpr,0,0)}
  const g=$('edImg').getContext('2d');g.imageSmoothingQuality='high';g.drawImage(ED.img,0,0,ED.dw,ED.dh);
  drawOverlay();
}
function drawOverlay(){
  const c=$('edOver');if(!c.width)return;const g=c.getContext('2d');g.clearRect(0,0,ED.dw,ED.dh);
  if(!ED.mask)return;
  const mw=ED.mask.naturalWidth,mh=ED.mask.naturalHeight,off=document.createElement('canvas');off.width=mw;off.height=mh;
  const og=off.getContext('2d');og.drawImage(ED.mask,0,0);const px=og.getImageData(0,0,mw,mh),d=px.data;
  const cell=Math.max(4,Math.round(mw/90));
  for(let y=0;y<mh;y++)for(let x=0;x<mw;x++){
    const i=(y*mw+x)*4;
    if(d[i]<128){const alt=((x/cell|0)+(y/cell|0))&1;d[i]=d[i+1]=d[i+2]=alt?226:246;d[i+3]=215}else d[i+3]=0;
  }
  og.putImageData(px,0,0);g.imageSmoothingEnabled=true;g.drawImage(off,0,0,ED.dw,ED.dh);
}
function openEditor(tool){
  if(!job.image||job.image.kind!=='raster')return;
  ED.crop=job.prep.crop?[...job.prep.crop]:[0,0,1,1];
  if(tool)setTool(tool);
  $('editor').showModal();
  const im=new Image();im.onload=()=>{ED.img=im;layoutEd();applySel(job.image.sel)};im.src=job.image.url;
}
function setTool(t){
  ED.tool=t;document.querySelectorAll('#edTools button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.tool===t));
  $('edCropOpts').hidden=t!=='crop';$('edBrushOpts').hidden=t==='crop'||t==='box';
  if(t==='crop')$('edStatus').textContent=cropText();
  drawEdStroke();
}
document.querySelectorAll('#edTools button').forEach(b=>b.onclick=()=>setTool(b.dataset.tool));
$('edSize').oninput=e=>{$('edSize.out').textContent=e.target.value+' px'};
$('selEdit').onclick=()=>openEditor('box');
$('cropOpen').onclick=()=>openEditor('crop');
$('selAuto').onclick=()=>maskOp({op:'auto'},'selInfo');
const AI={available:false,models:[]};
async function loadAI(){
  try{const d=await (await fetch('/api/ai')).json();AI.available=d.available;AI.models=d.models||[]}catch(_){AI.available=false}
  $('aiBox').hidden=!AI.available;$('aiInstall').hidden=AI.available;$('edAI').hidden=!AI.available;
  if(!AI.available)return;
  const sel=$('aiModel');
  if(!AI.models.some(m=>m.name===job.aiModel))job.aiModel=AI.models[0]?.name;
  sel.innerHTML=AI.models.map(m=>`<option value="${m.name}">${esc(m.label)} · ${m.downloaded?'ready':m.size_mb+' MB download'}</option>`).join('');
  sel.value=job.aiModel;
  $('edAI').title=`Uses the ${AI.models.find(m=>m.name===job.aiModel)?.label.toLowerCase()} model, chosen in the Source panel`;
}
$('aiModel').onchange=e=>{job.aiModel=e.target.value;save();loadAI()};
$('selAI').onclick=()=>maskOp({op:'ai',model:job.aiModel},'selInfo');
$('edAI').onclick=()=>maskOp({op:'ai',model:job.aiModel});
$('edAuto').onclick=()=>maskOp({op:'auto'});
$('edInvert').onclick=()=>maskOp({op:'invert'});
$('edGrow').onclick=()=>maskOp({op:'grow',amount:3});
$('edShrink').onclick=()=>maskOp({op:'grow',amount:-3});
$('edUndo').onclick=()=>maskOp({op:'undo'});
$('edReset').onclick=()=>maskOp({op:'reset'});
$('edDone').onclick=()=>$('editor').close();
$('editor').addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key.toLowerCase()==='z'){e.preventDefault();maskOp({op:'undo'})}
  else if(e.key==='c')setTool('crop');else if(e.key==='b')setTool('box');else if(e.key==='e')setTool('erase');else if(e.key==='r')setTool('keep')});
window.addEventListener('resize',()=>{if($('editor').open)layoutEd()});
const edPt=e=>{const r=$('edDraw').getBoundingClientRect();return [Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))]};
/* ----- crop ----- */
const isFull=c=>c[0]<=0.0005&&c[1]<=0.0005&&c[2]>=0.9995&&c[3]>=0.9995;
function cropText(){
  const c=ED.crop||[0,0,1,1];
  if(isFull(c))return 'Drag on the picture to crop it. The plot uses only what is inside the box.';
  const iw=job.image.w,ih=job.image.h;
  return `Crop: ${Math.round((c[2]-c[0])*iw)} × ${Math.round((c[3]-c[1])*ih)} px (${Math.round((c[2]-c[0])*100)}% × ${Math.round((c[3]-c[1])*100)}% of the picture). Drag inside to move it, or a corner to resize.`;
}
/* Target shape as width/height in picture pixels, or null for free. */
function cropAspect(){
  const v=$('edAspect').value,iw=job.image.w,ih=job.image.h,turn=+job.prep.rotate||0;
  if(v==='area'){const a=L.area.w/L.area.h;return turn===90||turn===270?1/a:a}
  if(v==='original')return iw/ih;
  if(v==='square')return 1;
  return null;
}
/* Box from fixed corner a to point p, held to the target shape and inside the picture. */
function shapeBox(a,p){
  let du=p[0]-a[0],dv=p[1]-a[1];const asp=cropAspect();
  if(asp){
    const r=asp*job.image.h/job.image.w; // du/dv in normalized units
    if(Math.abs(du)/r>Math.abs(dv))dv=Math.sign(dv||1)*Math.abs(du)/r;else du=Math.sign(du||1)*Math.abs(dv)*r;
    const lim=Math.min(1,(du>0?(1-a[0]):a[0])/Math.max(Math.abs(du),1e-9),(dv>0?(1-a[1]):a[1])/Math.max(Math.abs(dv),1e-9));
    du*=lim;dv*=lim;
  }else{du=Math.min(Math.max(du,-a[0]),1-a[0]);dv=Math.min(Math.max(dv,-a[1]),1-a[1])}
  return [Math.min(a[0],a[0]+du),Math.min(a[1],a[1]+dv),Math.max(a[0],a[0]+du),Math.max(a[1],a[1]+dv)];
}
function commitCrop(){
  const c=ED.crop;
  if(c[2]-c[0]<0.02||c[3]-c[1]<0.02){ED.crop=job.prep.crop?[...job.prep.crop]:[0,0,1,1];drawEdStroke();return}
  job.prep.crop=isFull(c)?null:c.map(v=>Math.round(v*10000)/10000);
  renderCropInfo();save();placeInk();schedulePlan(0);$('edStatus').textContent=cropText();
}
function renderCropInfo(){
  const c=job.prep.crop,el=$('cropInfo');
  el.hidden=!c;if(c)el.textContent=`Cropped to ${Math.round((c[2]-c[0])*100)}% × ${Math.round((c[3]-c[1])*100)}% of the picture.`;
}
$('edAspect').onchange=()=>{
  const asp=cropAspect();if(!asp){$('edStatus').textContent=cropText();return}
  // Largest box of the new shape inside the current crop, centered on it.
  const c=ED.crop,iw=job.image.w,ih=job.image.h,cw=(c[2]-c[0])*iw,ch=(c[3]-c[1])*ih;
  let w=cw,h=w/asp;if(h>ch){h=ch;w=h*asp}
  const cx=(c[0]+c[2])/2,cy=(c[1]+c[3])/2;
  ED.crop=[cx-w/iw/2,cy-h/ih/2,cx+w/iw/2,cy+h/ih/2];commitCrop();drawEdStroke();
};
$('edCropReset').onclick=()=>{ED.crop=[0,0,1,1];commitCrop();drawEdStroke()};
$('edMatchArea').onclick=()=>{
  const c=ED.crop,turn=+job.prep.rotate||0;let asp=((c[2]-c[0])*job.image.w)/((c[3]-c[1])*job.image.h);
  if(turn===90||turn===270)asp=1/asp;
  const a=L.area,[,cy]=centerOf(a);a.h=Math.round(a.w/asp*10)/10;a.y=Math.round((cy-a.h/2)*10)/10;
  changed(['area.h']);$('edStatus').textContent=`Drawing area reshaped to ${fmt(a.w)} × ${fmt(a.h)} mm to match the crop. Check the safety checks.`;
};
function drawCrop(g){
  const c=ED.crop||[0,0,1,1],on=ED.tool==='crop';
  if(isFull(c)&&!on)return;
  const x0=c[0]*ED.dw,y0=c[1]*ED.dh,x1=c[2]*ED.dw,y1=c[3]*ED.dh;
  g.save();g.fillStyle=on?'rgba(12,16,20,.55)':'rgba(12,16,20,.35)';
  g.beginPath();g.rect(0,0,ED.dw,ED.dh);g.rect(x0,y0,x1-x0,y1-y0);g.fill('evenodd');
  g.strokeStyle='#fff';g.lineWidth=on?2:1;g.strokeRect(x0,y0,x1-x0,y1-y0);
  if(on){
    g.globalAlpha=.5;g.lineWidth=1;g.beginPath();
    for(const f of [1/3,2/3]){g.moveTo(x0+(x1-x0)*f,y0);g.lineTo(x0+(x1-x0)*f,y1);g.moveTo(x0,y0+(y1-y0)*f);g.lineTo(x1,y0+(y1-y0)*f)}
    g.stroke();g.globalAlpha=1;g.fillStyle='#fff';
    for(const [x,y] of [[x0,y0],[x1,y0],[x0,y1],[x1,y1]])g.fillRect(x-6,y-6,12,12);
  }
  g.restore();
}
function cropHit(p){
  const c=ED.crop,tol=12/ED.dw,tolv=12/ED.dh;
  const corners=[[c[0],c[1]],[c[2],c[1]],[c[0],c[3]],[c[2],c[3]]];
  for(let i=0;i<4;i++)if(Math.abs(p[0]-corners[i][0])<tol&&Math.abs(p[1]-corners[i][1])<tolv)return {mode:'resize',anchor:corners[3-i]};
  if(p[0]>c[0]&&p[0]<c[2]&&p[1]>c[1]&&p[1]<c[3]&&!isFull(c))return {mode:'move',start:[...c],from:p};
  return {mode:'new',anchor:p};
}
function drawEdStroke(){
  const g=$('edDraw').getContext('2d');g.clearRect(0,0,ED.dw,ED.dh);const dr=ED.drag;
  drawCrop(g);
  if(dr&&dr.tool==='box'){
    const [a,b]=[dr.pts[0],dr.pts[dr.pts.length-1]];
    g.setLineDash([6,4]);g.lineWidth=2;g.strokeStyle='#2246c8';g.fillStyle='rgba(34,70,200,.08)';
    g.fillRect(a[0]*ED.dw,a[1]*ED.dh,(b[0]-a[0])*ED.dw,(b[1]-a[1])*ED.dh);g.strokeRect(a[0]*ED.dw,a[1]*ED.dh,(b[0]-a[0])*ED.dw,(b[1]-a[1])*ED.dh);g.setLineDash([]);
  }else if(dr){
    g.lineCap=g.lineJoin='round';g.lineWidth=+$('edSize').value;g.strokeStyle=dr.tool==='keep'?'rgba(22,121,58,.55)':'rgba(192,38,45,.55)';
    g.beginPath();dr.pts.forEach(([u,v],i)=>i?g.lineTo(u*ED.dw,v*ED.dh):g.moveTo(u*ED.dw,v*ED.dh));
    if(dr.pts.length===1)g.lineTo(dr.pts[0][0]*ED.dw+0.1,dr.pts[0][1]*ED.dh);g.stroke();
  }
  if(!dr&&ED.hover&&(ED.tool==='erase'||ED.tool==='keep')){
    g.lineWidth=1.5;g.strokeStyle=ED.tool==='keep'?'#16793a':'#c0262d';g.beginPath();
    g.arc(ED.hover[0]*ED.dw,ED.hover[1]*ED.dh,+$('edSize').value/2,0,Math.PI*2);g.stroke();
  }
}
const edDraw=$('edDraw');
edDraw.addEventListener('pointerdown',e=>{
  if(ED.busy)return;edDraw.setPointerCapture(e.pointerId);const p=edPt(e);
  ED.drag=ED.tool==='crop'?{tool:'crop',...cropHit(p),pts:[p]}:{tool:ED.tool,pts:[p]};drawEdStroke();
});
edDraw.addEventListener('pointermove',e=>{
  const p=edPt(e);ED.hover=p;
  if(ED.drag&&ED.drag.tool==='crop'){
    const d=ED.drag;
    if(d.mode==='move'){
      const s=d.start,w=s[2]-s[0],h=s[3]-s[1];
      const u0=Math.min(Math.max(s[0]+p[0]-d.from[0],0),1-w),v0=Math.min(Math.max(s[1]+p[1]-d.from[1],0),1-h);
      ED.crop=[u0,v0,u0+w,v0+h];
    }else ED.crop=shapeBox(d.anchor,p);
    $('edStatus').textContent=cropText();drawEdStroke();return;
  }
  if(ED.drag){const pts=ED.drag.pts,last=pts[pts.length-1];
    if(ED.drag.tool==='box')pts[1]=p;else if(Math.hypot(p[0]-last[0],p[1]-last[1])>0.002)pts.push(p)}
  drawEdStroke();
});
edDraw.addEventListener('pointerleave',()=>{ED.hover=null;if(!ED.drag)drawEdStroke()});
edDraw.addEventListener('pointerup',()=>{
  const dr=ED.drag;ED.drag=null;if(!dr)return;
  if(dr.tool==='crop'){commitCrop();drawEdStroke();return}
  if(dr.tool==='box'){
    const [a,b]=[dr.pts[0],dr.pts[dr.pts.length-1]];
    if(Math.abs(b[0]-a[0])<0.02||Math.abs(b[1]-a[1])<0.02){drawEdStroke();$('edStatus').textContent='Drag a bigger box around the subject.';return}
    maskOp({op:'select',rect:[a[0],a[1],b[0],b[1]]});
  }else maskOp({op:'paint',smart:$('edSmart').checked,strokes:[{mode:dr.tool==='keep'?'keep':'erase',points:dr.pts,size:+$('edSize').value/ED.dw}]});
});

/* ---------- slider fill (blue up to the thumb, like macOS) ---------- */
function fillRange(el){const min=+el.min||0,max=+el.max||100,v=+el.value;el.style.setProperty('--p',`${((v-min)/(max-min||1))*100}%`)}
const fillAll=()=>document.querySelectorAll('input[type=range]').forEach(fillRange);
document.addEventListener('input',e=>{if(e.target.type==='range')fillRange(e.target)});
new MutationObserver(fillAll).observe(document.getElementById('params'),{childList:true});

/* ---------- toolbar, drag & drop, shortcuts ---------- */
$('tbEdit').onclick=()=>openEditor('crop');
const canvasEl=document.querySelector('.canvas');
for(const ev of ['dragenter','dragover'])document.addEventListener(ev,e=>{if([...e.dataTransfer?.types||[]].includes('Files')){e.preventDefault();canvasEl.classList.add('dropping')}});
for(const ev of ['dragleave','drop'])document.addEventListener(ev,()=>canvasEl.classList.remove('dropping'));
document.addEventListener('drop',e=>{const f=e.dataTransfer?.files?.[0];if(f){e.preventDefault();openFile(f)}});
document.addEventListener('keydown',e=>{
  if(!(e.metaKey||e.ctrlKey)||$('editor').open||$('sendSheet').open||!$('dashboard').hidden)return;
  const k=e.key.toLowerCase();
  if(k==='o'){e.preventDefault();$('file').click()}
  else if(k==='s'){e.preventDefault();saveNow()}
  else if(k==='e'&&e.shiftKey){e.preventDefault();if(!$('dlG').disabled)$('dlG').click()}
  else if(k==='e'&&!e.shiftKey){e.preventDefault();if(!$('tbEdit').disabled)openEditor('crop')}
  else if(k==='enter'){e.preventDefault();$('sendOpen').click()}
  else if(['1','2','3','4'].includes(k)){e.preventDefault();selectTab(['tab-picture','tab-effect','tab-setup','tab-printer'][+k-1])}
});

/* ---------- boot ---------- */
/* ---------- projects & dashboard ---------- */
const DASH={list:[],sort:'recent',query:'',menuFor:null};
function relTime(t){
  const d=new Date(t*1000),now=new Date(),day=86400000;
  const time=d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
  const start=new Date(now.getFullYear(),now.getMonth(),now.getDate()).getTime();
  if(d.getTime()>=start)return `Today, ${time}`;
  if(d.getTime()>=start-day)return `Yesterday, ${time}`;
  return d.toLocaleDateString([], {day:'numeric',month:'short',year:d.getFullYear()===now.getFullYear()?undefined:'numeric'});
}
const effectLabel=n=>effects.find(e=>e.name===n)?.label||'Wave lines';
async function loadProjects(){
  try{DASH.list=await (await fetch('/api/projects')).json()}catch(_){DASH.list=[]}
  renderDashboard();
}
function renderDashboard(){
  const q=DASH.query.trim().toLowerCase();
  let items=DASH.list.filter(p=>!q||p.name.toLowerCase().includes(q)||(p.source?.name||'').toLowerCase().includes(q));
  items=[...items].sort(DASH.sort==='name'?(a,b)=>a.name.localeCompare(b.name,undefined,{numeric:true}):(a,b)=>b.updated-a.updated);
  $('projCount').textContent=DASH.list.length?`${DASH.list.length} project${DASH.list.length===1?'':'s'}`:'';
  $('projEmpty').hidden=DASH.list.length>0;$('projGrid').hidden=DASH.list.length===0;
  const card=p=>`<div class="card" role="listitem" tabindex="0" data-id="${p.id}" aria-label="${esc(p.name)}">
      <div class="art">${p.has_thumb?`<img src="/api/projects/${p.id}/thumb.png?v=${p.updated}" alt="">`:'<span class="none">Not plotted yet</span>'}
        <button class="more" type="button" aria-label="Actions for ${esc(p.name)}" aria-haspopup="menu" aria-expanded="false">⋯</button></div>
      <div class="name">${esc(p.name)}</div>
      <div class="meta">${relTime(p.updated)} · ${esc(effectLabel(p.effect))}${p.source?.name?` · ${esc(p.source.name)}`:''}</div></div>`;
  $('projGrid').innerHTML=(q?'':`<div class="card new" role="listitem" tabindex="0" data-new="1"><div class="art"><svg viewBox="0 0 28 28"><path d="M14 6v16M6 14h16"/></svg>New Project</div><div class="name">&nbsp;</div></div>`)+
    items.map(card).join('')+(q&&!items.length?`<p class="foot">No projects match “${esc(DASH.query)}”.</p>`:'');
}
async function newProject(state){
  const r=await post('/api/projects',{name:'Untitled',state:state||null});
  const p=await r.json();location.hash=`#/p/${p.id}`;
}
function showDashboard(){
  saveNow();currentProject=null;
  document.querySelector('.app').hidden=true;$('dashboard').hidden=false;document.title='BambuPlot';
  loadProjects();
}
async function openProject(pid){
  if(pid===currentProject){document.querySelector('.app').hidden=false;$('dashboard').hidden=true;return}
  await saveNow();
  let d;
  try{const r=await fetch(`/api/projects/${pid}`);if(!r.ok)throw new Error();d=await r.json()}
  catch(_){location.hash='#/';return}
  currentProject=null; // don't autosave while loading
  const st=d.state||{};
  L=deepMerge(DEF.layout,st.layout);job.prep=deepMerge(DEF.prep,st.prep);
  job.effect=effects.some(e=>e.name===st.effect)?st.effect:'waves';
  job.params=(st.params&&typeof st.params==='object')?st.params:{};
  job.aiModel=typeof st.aiModel==='string'?st.aiModel:null;
  if(job.image?.url)URL.revokeObjectURL(job.image.url);
  job.image=null;plan=null;$('ink').setAttribute('d','');$('travel').setAttribute('d','');
  if(d.image){
    try{
      const blob=await (await fetch(`/api/projects/${pid}/source`)).blob();
      const im=d.image;
      job.image={id:im.id,kind:im.kind,w:im.width,h:im.height,name:im.name||'picture',warnings:im.warnings||[],strokes:im.strokes,fills:im.fills,sel:im.selection||null,url:URL.createObjectURL(blob)};
    }catch(_){job.image=null}
  }
  $('projName').value=d.name;document.title=`${d.name} · BambuPlot`;
  document.querySelector('.app').hidden=false;$('dashboard').hidden=true;
  buildEffects();render();renderThumb();refreshChecks();schedulePlan(0);loadAI();
  if(job.image?.sel?.has_mask)applySel(job.image.sel);
  selectTab(job.image?'tab-effect':'tab-picture');
  currentProject=pid;dirty=false;setDocState(`Saved · ${docInfo()}`);
  if(d.warning)showProblems(d.warning);
}
function route(){
  const m=location.hash.match(/^#\/p\/([a-f0-9]{12})$/);
  if(m)openProject(m[1]);else showDashboard();
}
window.addEventListener('hashchange',route);
$('toProjects').onclick=()=>{location.hash='#/'};
$('projNew').onclick=$('projNewEmpty').onclick=()=>newProject();
$('projSearch').oninput=e=>{DASH.query=e.target.value;renderDashboard()};
document.querySelectorAll('#projSort button').forEach(b=>b.onclick=()=>{
  DASH.sort=b.dataset.sort;document.querySelectorAll('#projSort button').forEach(x=>x.setAttribute('aria-pressed',x===b));renderDashboard();
});
$('projName').addEventListener('input',()=>{document.title=`${$('projName').value||'Untitled'} · BambuPlot`;save()});
$('projName').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key==='Escape'){e.target.blur();saveNow()}});
$('projName').addEventListener('blur',()=>{if(!$('projName').value.trim())$('projName').value='Untitled';saveNow()});

/* cards: click to open, ⋯ for actions */
$('projGrid').addEventListener('click',e=>{
  const c=e.target.closest('.card');if(!c||e.target.closest('.confirm,.rename'))return;
  if(c.dataset.new){newProject();return}
  const more=e.target.closest('.more');
  if(more){
    DASH.menuFor=c.dataset.id;const m=$('cardMenu'),r=more.getBoundingClientRect();
    m.style.top=`${r.bottom+4}px`;m.style.left=`${Math.min(r.left,innerWidth-240)}px`;m.style.right='auto';
    more.setAttribute('aria-expanded','true');m.showPopover();return;
  }
  location.hash=`#/p/${c.dataset.id}`;
});
$('projGrid').addEventListener('keydown',e=>{
  const c=e.target.closest('.card');if(!c||e.target!==c)return;
  if(e.key==='Enter'){c.dataset.new?newProject():location.hash=`#/p/${c.dataset.id}`}
  else if((e.key==='Backspace'||e.key==='Delete')&&!c.dataset.new){e.preventDefault();askDelete(c)}
});
$('cardMenu').addEventListener('toggle',e=>{if(e.newState==='closed')document.querySelectorAll('.more[aria-expanded="true"]').forEach(b=>b.setAttribute('aria-expanded','false'))});
$('cardMenu').addEventListener('click',async e=>{
  const act=e.target.closest('button')?.dataset.act,pid=DASH.menuFor;if(!act||!pid)return;
  $('cardMenu').hidePopover();
  const c=document.querySelector(`.card[data-id="${pid}"]`);
  if(act==='open')location.hash=`#/p/${pid}`;
  else if(act==='duplicate'){await post(`/api/projects/${pid}/duplicate`,{});loadProjects()}
  else if(act==='rename'&&c){
    const nameEl=c.querySelector('.name'),old=DASH.list.find(p=>p.id===pid)?.name||'';
    nameEl.innerHTML=`<input class="rename" value="${esc(old)}" aria-label="Project name">`;
    const inp=nameEl.querySelector('input');inp.focus();inp.select();
    const done=async keep=>{inp.onblur=null;if(keep&&inp.value.trim()&&inp.value.trim()!==old)await fetch(`/api/projects/${pid}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:inp.value})});loadProjects()};
    inp.onkeydown=ev=>{if(ev.key==='Enter')done(true);else if(ev.key==='Escape')done(false)};
    inp.onblur=()=>done(true);
  }
  else if(act==='delete'&&c)askDelete(c);
});
function askDelete(c){
  const pid=c.dataset.id,name=DASH.list.find(p=>p.id===pid)?.name||'this project',art=c.querySelector('.art');
  art.insertAdjacentHTML('beforeend',`<div class="confirm" role="alertdialog" aria-label="Delete ${esc(name)}?"><b>Delete “${esc(name)}”?</b><span>Its picture and settings will be removed. This can’t be undone.</span><div><button class="btn small" type="button" data-no>Cancel</button><button class="btn small danger" type="button" data-yes>Delete</button></div></div>`);
  const box=art.querySelector('.confirm');box.querySelector('[data-no]').focus();
  box.onclick=async ev=>{
    ev.stopPropagation();
    if(ev.target.closest('[data-yes]')){await fetch(`/api/projects/${pid}`,{method:'DELETE'});loadProjects()}
    else if(ev.target.closest('[data-no]'))box.remove();
  };
}

/* ---------- boot ---------- */
(async function boot(){
  try{
    const [defs,effs]=await Promise.all([fetch('/api/defaults').then(r=>r.json()),fetch('/api/effects').then(r=>r.json())]);
    DEF=defs;effects=effs;L=deepMerge(defs.layout,null);job.prep=deepMerge(defs.prep,null);
    loadPrinter();
    // First run with projects: keep the setup from before as a first project.
    const list=await (await fetch('/api/projects')).json();
    const old=restore();
    if(!list.length&&old&&!localStorage.getItem(STORE+'.migrated')){
      const r=await post('/api/projects',{name:'My First Plot',state:{layout:old.layout,effect:old.effect,params:old.params,prep:old.prep,aiModel:old.aiModel}});
      try{localStorage.setItem(STORE+'.migrated','1')}catch(_){}
      if(r.ok&&!location.hash){const p=await r.json();location.hash=`#/p/${p.id}`;return}
    }
    route();
    const v=await fetch('/api/version').then(r=>r.ok?r.json():null).catch(()=>null);
    if(!v||!['rotation','printer','projects'].every(f=>v.features?.includes(f)))
      setStatus('The BambuPlot server is out of date. Stop it (Ctrl+C) and run `uv run bambuplot serve` again.',true);
  }catch(e){setStatus('Could not reach the BambuPlot server. Start it with `bambuplot serve`.',true)}
})();
