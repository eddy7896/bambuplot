const $=id=>document.getElementById(id);
let PAPERS={A5:[148,210],A6:[105,148]}; // replaced by the server's list at boot
const STORE='bambuplot.v1';
const PLAN_KEYS=new Set(['area.w','area.h','pen.width_mm','touch_off']);
let L=null, DEF=null, effects=[];
/* Layers: each has its own picture, effect, settings, tone, placement and pen.
   job.effect / params / prep / image always mean the active layer, so the Picture and Effect tabs edit it. */
const LAYERS={list:[],active:0};
const act=()=>LAYERS.list[LAYERS.active]||LAYERS.list[0];
const job={
  get effect(){return act()?.effect}, set effect(v){act().effect=v},
  get params(){return act()?.params}, set params(v){act().params=v},
  get prep(){return act()?.prep}, set prep(v){act().prep=v},
  get image(){return act()?.image}, set image(v){act().image=v},
  get lastRaster(){return act()?.lastRaster}, set lastRaster(v){act().lastRaster=v},
  extras:{}, aiModel:null,
};
const FULL_PLACE={x:0,y:0,w:1,h:1,rotation:0};
const newLayerId=()=>'l'+Math.random().toString(36).slice(2,9);
function newLayer(over={}){
  return {id:newLayerId(),name:'',visible:true,effect:'waves',params:{},prep:deepMerge(DEF.prep,null),image:null,
          place:{...FULL_PLACE},pen:1,lastRaster:null,...over};
}
/* Video frames and flipbooks. Layers that use the project's video share one picture object whose
   id is "video"; the server swaps in the frame being looked at (or every frame, for a flipbook). */
const FLIP={pid:null,video:null,fb:null,ex:null,info:null,vimg:null,pages:new Map()};
const VIDEO_ID='video';
const frameURL=(i,cut=!!FLIP.video?.background&&act()?.prep?.use_mask!==false)=>`/api/projects/${FLIP.pid}/video/frames/${i}.jpg?v=${FLIP.video?.version||0}${cut?'&cut=1':''}`;
const curFrame=()=>Math.max(0,Math.min(FLIP.fb?.frame||0,(FLIP.video?.count||1)-1));
const usesVideo=()=>LAYERS.list.some(l=>l.visible&&l.image?.video);
const flipOn=()=>!!(FLIP.video&&FLIP.fb?.enabled&&usesVideo());
function videoImage(){
  const v=FLIP.video;if(!v)return null;
  if(!FLIP.vimg)FLIP.vimg={id:VIDEO_ID,kind:'raster',video:true,warnings:[],sel:null};
  Object.assign(FLIP.vimg,{w:v.width,h:v.height,name:v.name,url:frameURL(curFrame())});
  return FLIP.vimg;
}
let plan=null, checks=[], sel='area', drag=null, V=null, snap=true;
let drawing=null, showDims=true; // draw-on-plate tool state; edge dimensions on/off
let lineStr=[], travelStr=[], totalPts=0, planSeq=0;

/* ---------- geometry (printer frame: +Y back, angles CCW in degrees) ---------- */
const R=(x,y,w,h)=>({x,y,w,h});
const x2=r=>r.x+r.w, y2=r=>r.y+r.h;
const grow=(r,m)=>R(r.x-m,r.y-m,r.w+2*m,r.h+2*m);
const bedRect=()=>R(0,0,L?.bed?.w||180,L?.bed?.h||180);
const normAng=a=>{a=((a+180)%360+360)%360-180;return Math.round(a*10)/10||0};
const rotPt=(x,y,deg)=>{const t=deg*Math.PI/180,c=Math.cos(t),s=Math.sin(t);return [x*c-y*s,x*s+y*c]};
function paperBox(){
  let w,h;
  if(L.paper.size==='custom'||!PAPERS[L.paper.size]){w=L.paper.width;h=L.paper.height}
  else{[w,h]=PAPERS[L.paper.size];if(L.paper.orientation==='landscape')[w,h]=[h,w]}
  return {x:L.paper.x,y:L.paper.y,w,h,rot:L.paper.rotation||0};
}
/* Distances from the plate edges to a shape's outer edges; negative means it hangs over. */
function edgeDist(b){
  const c=corners(b),xs=c.map(p=>p[0]),ys=c.map(p=>p[1]);
  const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  const plate=bedRect();return {left:x0,front:y0,right:plate.w-x1,back:plate.h-y1,x0,x1,y0,y1};
}
const areaBox=()=>({...L.area,rot:L.area.rotation||0});
const centerOf=b=>[b.x+b.w/2,b.y+b.h/2];
/* local point (origin at the box's front-left corner, along its edges) → world */
function toWorld(b,lx,ly){const [cx,cy]=centerOf(b),[dx,dy]=rotPt(lx-b.w/2,ly-b.h/2,b.rot);return [cx+dx,cy+dy]}
const corners=b=>[[0,0],[b.w,0],[b.w,b.h],[0,b.h]].map(([u,v])=>toWorld(b,u,v));
const clipRect=c=>R(c.x,c.y,L.clip_w,L.clip_d);
const keepouts=()=>L.clips.map(c=>grow(clipRect(c),L.clip_keep_out));
const reach=()=>R(L.pen.offset_x,L.pen.offset_y,L.bed.w,L.bed.h);
/* Measurement units for page and paper lengths. Everything is stored in mm; this is display and input only. */
const UNITS={
  // dec: decimals in labels; inDec: decimals in input fields
  mm:{f:1,dec:1,inDec:2,step:0.5,snap:1,snapLabel:'1 mm'},
  cm:{f:10,dec:2,inDec:2,step:0.05,snap:1,snapLabel:'1 mm'},
  in:{f:25.4,dec:3,inDec:3,step:0.01,snap:25.4/16,snapLabel:'1/16 in'},
};
let unit=(()=>{try{const u=localStorage.getItem('bambuplot.units');return UNITS[u]?u:'mm'}catch(_){return 'mm'}})();
const toU=mm=>mm/UNITS[unit].f, fromU=v=>v*UNITS[unit].f;
const fmtU=mm=>String(+toU(mm).toFixed(UNITS[unit].dec));
const fmtLen=mm=>`${fmtU(mm)} ${unit}`;
const fmtSize=(w,h)=>`${fmtU(w)} × ${fmtU(h)} ${unit}`;
const snapv=v=>snap?Math.round(v/UNITS[unit].snap)*UNITS[unit].snap:Math.round(v*10)/10;
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
const projectBody=()=>({name:$('projName').value,state:{layout:L,effect:job.effect,params:job.params,prep:job.prep,aiModel:job.aiModel,extras:job.extras,
  layers:LAYERS.list.map(l=>({...layerBody(l),params:l.params})),active:LAYERS.active,flipbook:FLIP.fb},image_id:job.image?.video?null:job.image?.id||null});
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
const layerBody=l=>({id:l.id,name:l.name,visible:l.visible,effect:l.effect,params:l.params[l.effect]||{},prep:l.prep,
  image_id:l.image?.id||null,place:l.place,pen:l.pen});
const planBody=()=>({layout:L,effect:job.effect,params:job.params[job.effect]||{},prep:job.prep,image_id:job.image?.id||null,
  extras:job.extras,layers:LAYERS.list.map(layerBody),project:FLIP.pid,flipbook:FLIP.fb||undefined});
function setStatus(t,err){const s=$('status');s.textContent=t;s.classList.toggle('err',!!err)}

let planT=0;
function schedulePlan(delay=250){clearTimeout(planT);planT=setTimeout(runPlan,delay);$('inkg').classList.add('stale')}
async function runPlan(){
  const seq=++planSeq;setStatus('Updating preview…');
  try{
    const r=await post('/api/plan',planBody());
    if(seq!==planSeq)return;
    if(!r.ok){
      const detail=(await r.json().catch(()=>({}))).detail||r.statusText;
      if(r.status===404&&/no longer loaded/.test(detail)&&LAYERS.list.some(l=>l.image&&!l.image.video)){
        for(const l of LAYERS.list)if(!l.image?.video)l.image=null;
        renderThumb();buildEffects();renderLayers();setStatus('The server lost the pictures (it was restarted). Choose them again.',true);schedulePlan(0);return;
      }
      throw new Error(typeof detail==='string'?detail:'the server refused it');
    }
    const d=await r.json();if(seq!==planSeq)return;
    plan={groups:d.groups||[{color:'#1d1d1f',lines:d.lines}],stats:d.stats,w:L.area.w,h:L.area.h};checks=d.checks;
    FLIP.info=d.flipbook||null;
    buildInk();renderFlipCells();renderChecks();renderOver();renderStats();if(FLIP.video)renderFlip();
    const visible=LAYERS.list.filter(l=>l.visible).length,fi=FLIP.info;
    if(fi)setStatus(`Flipbook · frame ${fi.frame+1} of ${fi.count}${fi.sheet?` · sheet ${fi.sheet} of ${fi.sheets}, page ${fi.slot+1}`:' · not in the frames to plot'} · ${fi.frames} frame${fi.frames===1?'':'s'} on ${fi.sheets} sheet${fi.sheets===1?'':'s'}`);
    else setStatus(`${visible>1?`${visible} layers`:job.effect} · ${d.stats.points.toLocaleString()} points${d.stats.pens>1?` · ${d.stats.pens} pens`:''}`);
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
    changed(resized?['area.w']:['area.x']);setStatus(mode==='fit'?`Fitted ${fmtSize(d.w,d.h)}`:'Centered');
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
  const pts=[...corners(paperBox()),...corners({...reach(),rot:0}),...corners({...bedRect(),rot:0}),...L.clips.flatMap(c=>corners({...clipRect(c),rot:0}))];
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
  let h=`<defs><mask id="offbed" maskUnits="userSpaceOnUse" x="0" y="0" width="${V.x1-V.x0}" height="${V.y1-V.y0}"><rect x="0" y="0" width="${V.x1-V.x0}" height="${V.y1-V.y0}" fill="white"></rect><rect ${rA(bedRect())} fill="black"></rect></mask></defs>`;
  h+=`<rect class="bed" ${rA(bedRect())} rx="2"></rect>`;
  const bw=L.bed.w,bh=L.bed.h;
  for(let i=10;i<bw;i+=10)h+=`<line class="grid" x1="${X(i)}" y1="${Y(0)}" x2="${X(i)}" y2="${Y(bh)}"></line>`;
  for(let i=10;i<bh;i+=10)h+=`<line class="grid" x1="${X(0)}" y1="${Y(i)}" x2="${X(bw)}" y2="${Y(i)}"></line>`;
  const pl=(DEF.printers||[]).find(p=>p.key===L.bed.printer);
  h+=`<text class="bedlbl" x="${X(bw-2)}" y="${Y(bh-4)}" text-anchor="end">${esc((pl?pl.label:'Custom plate').toUpperCase())} · ${fmtSize(bw,bh).toUpperCase()}</text>`;
  h+=`<g ${rotAttr(p)}><rect class="paper${sel==='paper'?' sel':''}" data-drag="paper" ${rA(pr)}></rect></g>`;
  h+=`<g mask="url(#offbed)"><g ${rotAttr(p)}><rect class="overhang" ${rA(pr)}></rect></g></g>`;
  h+=`<g ${rotAttr(p)}><rect class="pmargin" ${rA(R(p.x+m,p.y+m,p.w-2*m,p.h-2*m))}></rect>`;
  h+=`<text class="paperlbl" x="${X(p.x+2)}" y="${Y(y2(pr)-4.5)}">${L.paper.size==='custom'?'Custom':esc(L.paper.size)} ${fmtSize(p.w,p.h)}${p.rot?` · ${fmt(p.rot)}°`:''}</text></g>`;
  h+=tapeSVG(p);
  keepouts().forEach(k=>h+=`<rect class="keep" ${rA(k)}></rect>`);
  $('under').innerHTML=h;
}
/* CAD-style dimensions from each plate edge to the shape, drawn through its middle. */
function dimsSVG(b){
  const d=edgeDist(b),cx=(d.x0+d.x1)/2,cy=(d.y0+d.y1)/2;let h='';
  const seg=(x1,y1,x2,y2,val,lx,ly,anchor)=>{
    const over=val<-0.05,cls=over?' over':'',t=1.4;
    const vert=Math.abs(x1-x2)<1e-6,tick=vert?`M${X(x1-t)} ${Y(y1)}H${X(x1+t)}M${X(x2-t)} ${Y(y2)}H${X(x2+t)}`:`M${X(x1)} ${Y(y1-t)}V${Y(y1+t)}M${X(x2)} ${Y(y2-t)}V${Y(y2+t)}`;
    h+=`<path class="dimline${cls}" d="M${X(x1)} ${Y(y1)}L${X(x2)} ${Y(y2)}${tick}"></path>`;
    h+=`<text class="dimlbl${cls}" x="${X(lx)}" y="${Y(ly)}" text-anchor="${anchor}">${over?`${fmtLen(-val)} over`:fmtLen(val)}</text>`;
  };
  if(Math.abs(d.left)>0.05)seg(0,cy,d.x0,cy,d.left,(0+d.x0)/2,cy+1.2,'middle');
  const BW=L.bed.w,BH=L.bed.h;
  if(Math.abs(d.right)>0.05)seg(d.x1,cy,BW,cy,d.right,(d.x1+BW)/2,cy+1.2,'middle');
  if(Math.abs(d.front)>0.05)seg(cx,0,cx,d.y0,d.front,cx+1.6,(0+d.y0)/2-1.2,'start');
  if(Math.abs(d.back)>0.05)seg(cx,d.y1,cx,BH,d.back,cx+1.6,(d.y1+BH)/2-1.2,'start');
  if(b.rot)h+=`<rect class="dimext" x="${X(d.x0)}" y="${Y(d.y1)}" width="${(d.x1-d.x0).toFixed(2)}" height="${(d.y1-d.y0).toFixed(2)}"></rect>`;
  return h;
}
/* Masking tape strips along the sheet's own edges, in its rotated frame. */
function tapeSVG(p){
  const t=L.tape,edges=['left','front','right','back'].filter(e=>t[e]);if(!edges.length)return '';
  const o=t.on_paper,out=t.width-t.on_paper,ext=t.overhang??6,k=t.keep_out;
  const rects={left:R(p.x-out,p.y-ext,t.width,p.h+2*ext),right:R(p.x+p.w-o,p.y-ext,t.width,p.h+2*ext),
               front:R(p.x-ext,p.y-out,p.w+2*ext,t.width),back:R(p.x-ext,p.y+p.h-o,p.w+2*ext,t.width)};
  let h=`<g ${rotAttr(p)}>`;
  for(const e of edges){
    const r=rects[e];h+=`<rect class="tape" ${rA(r)} rx=".6"></rect>`;
    if(k>0)h+=`<rect class="tapekeep" ${rA(grow(r,k))}></rect>`;
    const vert=e==='left'||e==='right',cx=r.x+r.w/2,cy=r.y+r.h/2;
    h+=vert?`<text class="tapelbl" x="${X(cx)}" y="${Y(cy)}" text-anchor="middle" transform="rotate(-90 ${X(cx)} ${Y(cy)})">TAPE</text>`
           :`<text class="tapelbl" x="${X(cx)}" y="${(V.y1-cy+1).toFixed(2)}" text-anchor="middle">TAPE</text>`;
  }
  return h+'</g>';
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
  h+=`<text class="dim" x="${X(a.x+a.w/2)}" y="${(V.y1-a.y+5).toFixed(2)}" text-anchor="middle">${fmtSize(a.w,a.h)}</text></g>`;
  [['fl',0,0],['fr',a.w,0],['bl',0,a.h],['br',a.w,a.h]].forEach(([k,u,v])=>{
    const [x,y]=toWorld(a,u,v);
    h+=`<rect class="handle" data-drag="h-${k}" x="${(x-V.x0-2).toFixed(2)}" y="${(V.y1-y-2).toFixed(2)}" width="4" height="4" rx=".6" style="cursor:crosshair"></rect>`;
  });
  const [lx,ly]=localTouch(),[tx,ty]=toWorld(a,lx,ly);
  h+=`<path class="touch" d="M${X(tx-3)} ${Y(ty)}H${X(tx+3)}M${X(tx)} ${Y(ty-3)}V${Y(ty+3)}"></path><circle class="touch" cx="${X(tx)}" cy="${Y(ty)}" r="1.6"></circle>`;
  L.clips.forEach((c,i)=>{const r=clipRect(c);h+=`<rect class="clip${sel==='clip:'+i?' sel':''}" data-drag="clip:${i}" ${rA(r)} rx="1"></rect><text class="cliplbl" x="${X(r.x+r.w/2)}" y="${(V.y1-r.y-r.h/2+1.3).toFixed(2)}" text-anchor="middle">${i+1}</text>`});
  h+=layerFrameSVG();
  if(sel==='area')h+=knob(a,'area');
  if(sel==='paper')h+=knob(paperBox(),'paper');
  if(showDims&&!drawing&&(sel==='paper'||sel==='area'))h+=dimsSVG(sel==='paper'?paperBox():areaBox());
  if(drawing?.rect){const r=drawing.rect;h+=`<rect class="drawrect" ${rA(r)}></rect>`+dimsSVG({...r,rot:0})+
    `<text class="dimlbl" x="${X(r.x+r.w/2)}" y="${Y(r.y+r.h/2)}" text-anchor="middle">${fmtSize(r.w,r.h)}</text>`}
  h+=`<path class="axis" d="M${X(0)} ${Y(0)}m0 7h12M${X(0)} ${Y(0)}m-7 0v-12"></path><path class="axisfill" d="M${X(12)} ${(V.y1+7).toFixed(2)}l-2.4 -1.3v2.6zM${(-V.x0-7).toFixed(2)} ${Y(12)}l-1.3 2.4h2.6z"></path>`;
  h+=`<text class="offlbl" x="${X(14)}" y="${(V.y1+8.2).toFixed(2)}">X</text><text class="offlbl" x="${(-V.x0-8.2).toFixed(2)}" y="${Y(14)}">Y</text><text class="offlbl" x="${X(0)}" y="${(V.y1+12.5).toFixed(2)}" text-anchor="middle">0,0</text>`;
  $('over').innerHTML=h;
}
function placeInk(){
  const a=areaBox(),g=$('inkg'),pw=plan?.w||a.w,ph=plan?.h||a.h,[cx,cy]=centerOf(a);
  g.setAttribute('transform',`translate(${X(cx)} ${Y(cy)}) rotate(${-a.rot}) scale(${a.w/pw} ${a.h/ph}) translate(${-pw/2} ${-ph/2})`);
  const c=$('inkClipRect');c.setAttribute('width',pw);c.setAttribute('height',ph);
  // Flipbooks: the picture sits on the page being looked at.
  const P=flipOn()?layerParent():{x:0,y:0,w:pw,h:ph},bx=P.x,by=P.y,bw=P.w,bh=P.h;
  const pl=act()?.place||FULL_PLACE,fw=pl.w*bw,fh=pl.h*bh,fx=bx+pl.x*bw,fy=ph-(by+(pl.y+pl.h)*bh);
  $('srcframe').setAttribute('transform',`translate(${fx} ${fy}) rotate(${-(pl.rotation||0)} ${fw/2} ${fh/2})`);
  const lf=$('layerframe');lf.setAttribute('width',fw);lf.setAttribute('height',fh);
  lf.setAttribute('visibility',isFullPlace(pl)?'hidden':'visible');
  const raster=srcKind()==='raster',turn=raster?(+job.prep.rotate||0):0,side=turn===90||turn===270;
  const iw=job.image?.w||1,ih=job.image?.h||1,cr=(raster&&job.prep.crop)||[0,0,1,1];
  const sw=side?fh:fw,sh=side?fw:fh,wrap=$('srcwrap'),im=$('srcimg');
  wrap.setAttribute('x',(fw-sw)/2);wrap.setAttribute('y',(fh-sh)/2);wrap.setAttribute('width',sw);wrap.setAttribute('height',sh);
  wrap.setAttribute('viewBox',`${cr[0]*iw} ${cr[1]*ih} ${(cr[2]-cr[0])*iw} ${(cr[3]-cr[1])*ih}`);
  wrap.setAttribute('preserveAspectRatio',!raster||job.prep.fit==='fit'?'xMidYMid meet':'xMidYMid slice');
  im.setAttribute('width',iw);im.setAttribute('height',ih);
  $('srcturn').setAttribute('transform',turn?`rotate(${turn} ${fw/2} ${fh/2})`:'');
  $('srcturn').setAttribute('visibility',job.image&&$('showImg').checked?'visible':'hidden');
  $('inklayers').style.strokeWidth=L.pen.width_mm;
}
const lineD=(l,h,n=l.length)=>{const pts=l.length===1?[l[0],[l[0][0]+0.01,l[0][1]]]:l.slice(0,Math.max(2,n));
  let d='M'+pts[0][0]+' '+(h-pts[0][1]);for(let i=1;i<pts.length;i++)d+='L'+pts[i][0]+' '+(h-pts[i][1]);return d};
function buildInk(){
  const h=plan.h;let prev=null;totalPts=0;travelStr=[];
  for(const g of plan.groups){
    g.strs=g.lines.map(l=>lineD(l,h));
    for(const l of g.lines){totalPts+=l.length;travelStr.push(prev?`M${prev[0]} ${h-prev[1]}L${l[0][0]} ${h-l[0][1]}`:'');prev=l[l.length-1]}
  }
  drawProgress();placeInk();
}
/* Draw the plan up to the replay slider, one path per pen in that pen's colour. */
function drawProgress(){
  if(!plan)return;const f=+$('progress').value/1000,h=plan.h;
  let budget=f>=1?Infinity:Math.round(totalPts*f),k=0,tr='',html='';
  for(const g of plan.groups){
    let d='';
    for(let i=0;i<g.lines.length&&budget>0;i++,k++){
      const l=g.lines[i];tr+=travelStr[k];
      if(l.length<=budget){d+=g.strs[i];budget-=l.length}else{d+=lineD(l,h,budget);budget=0}
    }
    if(d)html+=`<path class="inkpath" d="${d}" stroke="${g.color}"></path>`;
  }
  $('inklayers').innerHTML=html;$('travel').setAttribute('d',$('showTravel').checked?tr:'');
}
function renderChecks(){
  $('checks').innerHTML=checks.map(c=>`<li class="${c.state}"><b>${esc(c.title)}</b><small>${esc(c.detail)}</small></li>`).join('');
  const bad=checks.filter(c=>c.state==='bad').length,warn=checks.filter(c=>c.state==='warn').length,b=$('checksBtn');
  b.className='badge '+(bad?'bad':warn?'warn':'ok');
  b.textContent=bad?`${bad} safety problem${bad>1?'s':''}`:warn?`Ready · ${warn} note${warn>1?'s':''}`:'Ready to plot';
  b.title=bad?'Fix these before downloading or sending':'All safety checks pass';
  $('dlG').disabled=$('dl3mf').disabled=$('dlSmoke').disabled=$('dlPress').disabled=$('dlAlign').disabled=$('alDownload').disabled=$('alSend').disabled=$('sendOpen').disabled=isBad();
}
function renderStats(){
  const s=plan?.stats;if(!s)return;
  $('nInk').textContent=`${s.draw_m} m`;$('nLifts').textContent=s.pen_lifts.toLocaleString();
  $('nTravel').textContent=`${s.travel_m} m`;
  const fi=FLIP.info,mins=fi?fi.minutes_per_page*fi.frames:s.minutes;
  $('nTime').textContent=(fi?'≈ ':'')+fmtMinutes(mins);
  $('nTime').parentElement.title=fi?`About ${fmtMinutes(fi.minutes_per_page)} a page × ${fi.frames} pages, plus your time at each pause`:'Estimate; ignores acceleration';
}
const fmtMinutes=m=>m<60?`${Math.max(1,Math.round(m))} min`:`${Math.floor(m/60)} h ${Math.round(m%60)} m`;
function render(){if(!drag)computeView();renderUnder();renderOver();placeInk();syncInputs();syncExtras();renderPlace();fillAll()}

/* ---------- inputs ---------- */
const getP=(o,p)=>p.split('.').reduce((a,k)=>a?.[k],o);
const setP=(o,p,v)=>{const ks=p.split('.'),l=ks.pop();ks.reduce((a,k)=>a[k],o)[l]=v};
const layoutInputs=[...document.querySelectorAll('[id^="L."]')];
const prepInputs=[...document.querySelectorAll('input[id^="P."],select[id^="P."]')];
function syncInputs(){
  for(const el of layoutInputs){if(el===document.activeElement)continue;const v=getP(L,el.id.slice(2));el.value=typeof v==='number'?(el.hasAttribute('data-len')?+toU(v).toFixed(UNITS[unit].inDec):Math.round(v*100)/100):v}
  for(const el of prepInputs){const v=job.prep[el.id.slice(2)];if(el.type==='range'){const o=$(el.id+'.out');if(o)o.textContent=fmtVal(v,el.step,el.dataset.unit||'')}if(el===document.activeElement)continue;if(el.type==='checkbox')el.checked=!!v;else el.value=v}
  document.querySelectorAll('#orient button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.o===L.paper.orientation));
  document.querySelectorAll('#tapeEdges button').forEach(b=>b.setAttribute('aria-pressed',!!L.tape[b.dataset.edge]));
  $('clearClips').disabled=!L.clips.length;
  $('cliplist').innerHTML=L.clips.map((c,i)=>`<li data-i="${i}" class="${sel==='clip:'+i?'sel':''}"><span>Clip ${i+1} · X ${fmtU(c.x)} Y ${fmtU(c.y)} ${unit}</span><button type="button" aria-label="Remove clip ${i+1}" data-rm="${i}">×</button></li>`).join('');
  $('aspect').disabled=!job.image;
  const custom=L.paper.size==='custom';$('pwField').hidden=$('phField').hidden=!custom;
  renderAlignExpected();
  for(const [kind,b] of [['paper',paperBox()],['area',areaBox()]]){
    const d=edgeDist(b);
    for(const side of ['left','front','right','back']){const el=$(`D.${kind}.${side}`);if(el!==document.activeElement)el.value=+toU(d[side]).toFixed(UNITS[unit].dec)}
  }
}
/* Typing a distance from a plate edge moves the shape so its outer edge lands there. */
document.querySelectorAll('[id^="D."]').forEach(el=>el.addEventListener('input',()=>{
  const raw=parseFloat(el.value);if(!isFinite(raw))return;const v=fromU(raw);
  const [,kind,side]=el.id.split('.'),obj=kind==='paper'?L.paper:L.area,d=edgeDist(kind==='paper'?paperBox():areaBox());
  const delta=v-d[side];
  if(side==='left')obj.x+=delta;else if(side==='right')obj.x-=delta;else if(side==='front')obj.y+=delta;else obj.y-=delta;
  obj.x=Math.round(obj.x*100)/100;obj.y=Math.round(obj.y*100)/100;sel=kind;changed(['moved']);
}));
/* Pen holder, Z heights and speeds belong to the printer: every project shares them. */
let machineT=0;
function saveMachine(){
  clearTimeout(machineT);
  machineT=setTimeout(()=>fetch('/api/machine',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({bed:L.bed,pen:L.pen,z:L.z,feed:L.feed})}).catch(()=>{}),500);
}
function changed(keys){
  if(keys.some(k=>/^(bed|pen|z|feed)\./.test(k)))saveMachine();
  if(keys.some(k=>k==='area.w'||k==='area.h'))reshapeAll();
  render();refreshChecks();save();
  if(keys.some(k=>PLAN_KEYS.has(k)||k.startsWith('feed.')||k.startsWith('z.')))schedulePlan();
}
for(const el of layoutInputs)el.addEventListener('input',()=>{
  const key=el.id.slice(2);let v=el.value;
  if(key==='paper.size'&&v==='custom'&&L.paper.size!=='custom'){const b=paperBox();L.paper.width=b.w;L.paper.height=b.h}
  if(el.type==='number'){v=parseFloat(v);if(!isFinite(v))return;if(el.min!==''&&v<+el.min)return;if(el.max!==''&&v>+el.max)return;if(el.hasAttribute('data-len'))v=fromU(v)}
  if(key.endsWith('rotation'))v=normAng(v);
  setP(L,key,v);changed([key]);
});
for(const el of prepInputs)el.addEventListener('input',()=>{
  const key=el.id.slice(2);let v=el.type==='checkbox'?el.checked:el.dataset.kind==='text'?el.value:parseFloat(el.value);
  if(el.type==='number'&&(!isFinite(v)||(el.min!==''&&v<+el.min)))return;
  job.prep[key]=v;if(el.type==='range'){const o=$(el.id+'.out');if(o)o.textContent=fmtVal(v,el.step,el.dataset.unit||'')}
  if(key==='rotate')keepShape(act());
  if(key==='use_mask'&&job.image?.video){videoImage();$('srcimg').setAttribute('href',job.image.url)}
  save();placeInk();renderOver();renderPlace();schedulePlan(400);
});
document.querySelectorAll('#orient button').forEach(b=>b.onclick=()=>{
  if(L.paper.orientation===b.dataset.o)return;const [cx,cy]=centerOf(paperBox());
  L.paper.orientation=b.dataset.o;
  if(L.paper.size==='custom'&&(b.dataset.o==='landscape')!==(L.paper.width>L.paper.height))[L.paper.width,L.paper.height]=[L.paper.height,L.paper.width];
  const q=paperBox();L.paper.x=snapv(cx-q.w/2);L.paper.y=snapv(cy-q.h/2);changed(['paper']);
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
document.querySelectorAll('#tapeEdges button').forEach(b=>b.onclick=()=>{L.tape[b.dataset.edge]=!L.tape[b.dataset.edge];changed(['tape'])});
$('tapeAll').onclick=()=>{for(const e of ['left','front','right','back'])L.tape[e]=true;changed(['tape'])};
$('tapeNone').onclick=()=>{for(const e of ['left','front','right','back'])L.tape[e]=false;changed(['tape'])};
$('clearClips').onclick=()=>{L.clips=[];sel='area';changed(['clips'])};
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
$('travelBtn').onclick=()=>{const c=$('showTravel');c.checked=!c.checked;$('travelBtn').setAttribute('aria-pressed',c.checked);drawProgress()};

/* The picture under the plot: on/off and how strong, remembered on this computer. */
const VIEW=(()=>{try{return {img:true,op:22,...JSON.parse(localStorage.getItem('bambuplot.view')||'{}')}}catch(_){return {img:true,op:22}}})();
function applyView(){
  $('showImg').checked=VIEW.img;$('viewImg').checked=VIEW.img;$('imgBtn').setAttribute('aria-pressed',VIEW.img);
  $('viewImgOp').value=VIEW.op;$('viewImgOp.out').textContent=`${VIEW.op}%`;$('viewImgOpRow').hidden=!VIEW.img||!job.image;
  document.documentElement.style.setProperty('--src-op',VIEW.op/100);
  if(typeof fillRange==='function')fillRange($('viewImgOp'));
  try{localStorage.setItem('bambuplot.view',JSON.stringify(VIEW))}catch(_){}
  if(L)placeInk();
}
function toggleBaseImage(){VIEW.img=!VIEW.img;applyView();setStatus(VIEW.img?'Picture shown under the plot.':'Picture hidden. The preview shows only the lines.')}
$('imgBtn').onclick=toggleBaseImage;
$('viewImg').addEventListener('input',()=>{VIEW.img=$('viewImg').checked;applyView()});
$('viewImgOp').addEventListener('input',()=>{VIEW.op=+$('viewImgOp').value;applyView()});
applyView();
function selectTab(id){
  document.querySelectorAll('.tabs [role=tab]').forEach(x=>{const on=x.id===id;x.setAttribute('aria-selected',on);$(x.getAttribute('aria-controls')).hidden=!on});
}
document.querySelectorAll('.tabs [role=tab]').forEach(t=>t.onclick=()=>selectTab(t.id));

/* effect + params */
const GROUP_ORDER=['Trace to vector lines','Single continuous line','Hatching & engraving','Dots','Vector file','Calibration'];
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
$('effect').onchange=e=>{job.effect=e.target.value;buildParams();renderLayers();save();schedulePlan(0)};

/* image */
function renderThumb(){
  const t=$('thumb'),n=$('srcNotes'),im=job.image;
  n.hidden=!(im?.warnings?.length);n.innerHTML=(im?.warnings||[]).map(w=>`<li>${esc(w)}</li>`).join('');
  $('tbEdit').disabled=im?.kind!=='raster';
  $('viewImg').closest('.srow').hidden=!im;$('viewImgOpRow').hidden=!im||!VIEW.img;if(currentProject)setDocState(`${dirty?'Edited':'Saved'} · ${docInfo()}`);
  if(!im){t.innerHTML='<label class="drop" for="file">Drop a PNG, JPG or SVG here, or click to choose.<br>The preview uses a sample sphere until then.</label>';$('srcimg').setAttribute('href','');}
  else{
    const info=im.video?`Video · ${FLIP.video?.count||0} frames · ${im.w} × ${im.h} px`:im.kind==='svg'
      ?`SVG · ${im.strokes} line${im.strokes===1?'':'s'}, ${im.fills} filled shape${im.fills===1?'':'s'} · ${im.w} × ${im.h} mm`
      :`${im.w} × ${im.h} px`;
    t.innerHTML=`<img src="${im.url}" alt=""><span>${esc(im.name)}<br><small>${info}</small></span><button class="btn small" type="button" id="clearSrc">Remove</button>`;
    $('srcimg').setAttribute('href',im.url);
    $('clearSrc').onclick=()=>{if(!im.video)URL.revokeObjectURL(im.url);job.image=null;job.prep.crop=null;$('file').value='';renderThumb();buildEffects();renderLayers();renderFlip();save();schedulePlan(0)};
  }
  $('selBox').hidden=im?.kind!=='raster';$('selBox').classList.toggle('video',!!im?.video);renderCropInfo();
  if(im?.kind==='raster'){$('selInfo').textContent=selText(im.sel);$('selOpts').hidden=!im.sel?.has_mask;if(im.sel?.has_mask)$('srcimg').setAttribute('href',im.sel.preview)}
  placeInk();syncInputs();if(L&&LAYERS.list.length){renderOver();renderPlace()}
}
$('file').onchange=e=>{const f=e.target.files[0];if(f)openFile(f)};
const isVideoFile=f=>/^video\//.test(f.type)||/\.(mp4|mov|m4v|webm|avi|mkv|gif)$/i.test(f.name);
async function openFile(f){
  if(isVideoFile(f))return openVideo(f);
  setStatus('Reading file…');
  const fd=new FormData();fd.append('file',f);
  try{
    const r=await fetch('/api/image',{method:'POST',body:fd});const d=await r.json();
    if(!r.ok)throw new Error(d.detail||r.statusText);
    if(job.image?.url)URL.revokeObjectURL(job.image.url);
    job.prep.crop=null;
    job.image={id:d.id,kind:d.kind,w:d.width,h:d.height,name:f.name,warnings:d.warnings||[],strokes:d.strokes,fills:d.fills,sel:d.selection||null,url:URL.createObjectURL(f)};
    keepShape(act(),true);
    renderThumb();buildEffects();renderLayers();save();schedulePlan(0);selectTab('tab-effect');
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
for(const id of ['dlG','dl3mf','dlSmoke','dlPress','dlAlign'])$(id).addEventListener('click',()=>$('dlMenu').hidePopover?.());
$('dlG').onclick=async()=>{
  if(!await flipReady(setStatus))return;
  download('/api/gcode',planBody(),flipOn()?'bambuplot-flipbook.gcode':`bambuplot-${job.effect}.gcode`);
};
const jobTitle=()=>$('projName').value||'BambuPlot plot';
$('dl3mf').onclick=async()=>{
  $('problems').hidden=true;
  if(!await flipReady(setStatus))return;
  try{
    const r=await post('/api/package',{...planBody(),kind:'art',title:jobTitle()});
    if(!r.ok){const e=await r.json().catch(()=>({}));showProblems(r.status===404?'The server is out of date. Restart it.':e.detail);return}
    const name=(r.headers.get('Content-Disposition')||'').match(/filename="([^"]+)"/)?.[1]||'bambuplot.gcode.3mf';
    const a=document.createElement('a');a.href=URL.createObjectURL(await r.blob());a.download=name;document.body.appendChild(a);a.click();a.remove();
    setTimeout(()=>URL.revokeObjectURL(a.href),4000);setStatus(`Saved ${name}`);
  }catch(_){showProblems('Could not reach the BambuPlot server.')}
};
$('dlSmoke').onclick=()=>download('/api/calibrate/smoke',L,'bambuplot-smoke.gcode');
$('dlPress').onclick=()=>download('/api/calibrate/press',L,'bambuplot-press.gcode');
$('dlAlign').onclick=$('alDownload').onclick=()=>download('/api/calibrate/align',L,'bambuplot-alignment.gcode');
$('alSend').onclick=()=>{$('sendOpen').click();$('send-kind').value='align'};

/* ---------- dragging ---------- */
function toMM(e){const pt=svg.createSVGPoint();pt.x=e.clientX;pt.y=e.clientY;const q=pt.matrixTransform(svg.getScreenCTM().inverse());return {x:q.x+V.x0,y:V.y1-q.y}}
function startDraw(kind){
  drawing={kind,rect:null};svg.classList.add('drawing');
  $('drawPaper').classList.toggle('on',kind==='paper');$('drawArea').classList.toggle('on',kind==='area');
  setStatus(`Drag on the plate to draw the ${kind==='paper'?'paper':'drawing area'}. Esc cancels.`);
}
function stopDraw(){drawing=null;svg.classList.remove('drawing');$('drawPaper').classList.remove('on');$('drawArea').classList.remove('on');renderOver()}
$('drawPaper').onclick=()=>drawing?.kind==='paper'?stopDraw():startDraw('paper');
$('drawArea').onclick=()=>drawing?.kind==='area'?stopDraw():startDraw('area');
$('centerPaper').onclick=()=>{const b=paperBox();L.paper.x=snapv(L.bed.w/2-b.w/2);L.paper.y=snapv(L.bed.h/2-b.h/2);sel='paper';changed(['moved'])};
$('dimsBtn').onclick=()=>{showDims=!showDims;$('dimsBtn').setAttribute('aria-pressed',showDims);renderOver()};
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drawing){stopDraw();setStatus('Drawing cancelled.')}});
svg.addEventListener('pointerdown',e=>{
  if(drawing){const m=toMM(e);drawing.start={x:snapv(m.x),y:snapv(m.y)};drawing.rect=null;svg.setPointerCapture(e.pointerId);e.preventDefault();return}
  const t=e.target.closest('[data-drag]');if(!t)return;
  const k=t.dataset.drag;drag={k,m:toMM(e),paper:{...L.paper},area:{...L.area},clips:L.clips.map(c=>({...c})),lbox:layerBox()};
  sel=k.startsWith('h-')||k==='rot-area'?'area':k==='rot-paper'?'paper':k==='layer'||k.startsWith('lh-')?'layer':k;
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
  if(drawing?.start){
    const m=toMM(e),a=drawing.start,bx=snapv(m.x),by=snapv(m.y);
    drawing.rect=R(Math.min(a.x,bx),Math.min(a.y,by),Math.abs(bx-a.x),Math.abs(by-a.y));
    if(!raf)raf=requestAnimationFrame(()=>{raf=0;renderOver()});return;
  }
  if(!drag)return;const m=toMM(e),dx=m.x-drag.m.x,dy=m.y-drag.m.y,k=drag.k;
  if(k==='layer'||k.startsWith('lh-')){
    const a=areaLocal(m),o=areaLocal(drag.m),P=layerParent(),ob=drag.lbox;
    const nb=k==='layer'?{...ob,x:clampN(ob.x+a.x-o.x,P.x,P.x+P.w-ob.w),y:clampN(ob.y+a.y-o.y,P.y,P.y+P.h-ob.h)}:resizeLayer(ob,k.slice(3),a.x-o.x,a.y-o.y,P);
    setLayerBox(nb);
    if(!raf)raf=requestAnimationFrame(()=>{raf=0;placeInk();renderOver();renderPlace()});return;
  }
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
const end=()=>{
  if(drawing?.start){
    const r=drawing.rect,kind=drawing.kind,min=kind==='paper'?20:10;stopDraw();
    if(!r||r.w<min||r.h<min){setStatus(`Too small: draw at least ${fmtSize(min,min)}.`,true);return}
    if(kind==='paper'){Object.assign(L.paper,{size:'custom',x:r.x,y:r.y,width:r.w,height:r.h,rotation:0,orientation:r.w>r.h?'landscape':'portrait'});sel='paper'}
    else{L.area={...L.area,x:r.x,y:r.y,w:r.w,h:r.h,rotation:0};sel='area'}
    changed(kind==='area'?['area.w']:['paper']);setStatus(`${kind==='paper'?'Paper':'Drawing area'} set to ${fmtSize(r.w,r.h)}.`);return;
  }
  if(!drag)return;const resized=drag.k.startsWith('h-'),pic=drag.k==='layer'||drag.k.startsWith('lh-');drag=null;
  if(pic){syncLayerSettings();renderOver();renderPlace();save();schedulePlan(0);return}
  changed(resized?['area.w']:['moved'])};
svg.addEventListener('pointerup',end);svg.addEventListener('pointercancel',end);
svg.addEventListener('keydown',e=>{
  if(e.key==='['||e.key===']'||e.key==='{'||e.key==='}'){
    const t=sel==='paper'?'paper':sel==='area'?'area':null;if(!t)return;e.preventDefault();
    const step=e.shiftKey?15:1;rotate(t,String(e.key==='['||e.key==='{'?step:-step));return;
  }
  const d={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,1],ArrowDown:[0,-1]}[e.key];if(!d)return;e.preventDefault();
  const s=e.shiftKey?10:1,ax=d[0]*s,ay=d[1]*s;
  if(sel==='layer'&&act()?.image){
    const P=layerParent(),b=layerBox();setLayerBox({...b,x:clampN(b.x+ax,P.x,P.x+P.w-b.w),y:clampN(b.y+ay,P.y,P.y+P.h-b.h)});
    placeInk();renderOver();renderPlace();syncLayerSettings();save();schedulePlan(300);return;
  }
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
  $('tpl-info').textContent=!t?'No template yet.':t.error?t.error:
    `Template ready: sliced for the ${t.printer_model||'unknown printer'}, plots go into ${t.plate}.${t.warning?' '+t.warning:''}`;
  $('tpl-remove').disabled=!t;
  $('pf-mode').value=c.project_file||'builtin';$('pf-thumb').value=c.thumbnail||'both';
  $('sc-progress').checked=c.screen_progress!==false;$('sc-status').checked=c.screen_status!==false;
  const opts='<option value="">No change</option>'+(DEF.screen_messages||[]).map(m=>`<option value="${m.code}">${esc(m.label)}</option>`).join('');
  $('sc-pause').innerHTML=opts;$('sc-pen').innerHTML=opts;$('sc-sheet').innerHTML=opts;
  $('sc-sheet').value=c.claim_sheet==null?'':String(c.claim_sheet);
  $('sc-pause').value=c.claim_pause==null?'':String(c.claim_pause);$('sc-pen').value=c.claim_pen==null?'':String(c.claim_pen);
  syncProjectFileUI();
}
async function loadPrinter(){
  try{const r=await fetch('/api/printer');
    if(r.status===404){msg('pr-msg','The server is out of date. Stop it (Ctrl+C) and run `uv run bambuplot serve` again.','err');return}
    renderPrinter(await r.json())}catch(_){}
}
async function savePrinter(){
  const code=$('pr-code').value.trim();
  const r=await post('/api/printer',{ip:$('pr-ip').value,serial:$('pr-serial').value,url_prefix:$('pr-url').value||'file:///sdcard/',access_code:code?code:null,
    project_file:$('pf-mode').value,thumbnail:$('pf-thumb').value,screen_progress:$('sc-progress').checked,screen_status:$('sc-status').checked,
    claim_pause:$('sc-pause').value===''?null:+$('sc-pause').value,claim_pen:$('sc-pen').value===''?null:+$('sc-pen').value,
    claim_sheet:$('sc-sheet').value===''?null:+$('sc-sheet').value});
  if(!r.ok){msg('pr-msg',r.status===404?'The server is out of date. Stop it (Ctrl+C) and run `uv run bambuplot serve` again.':'Could not save the settings.','err');return false}
  renderPrinter(await r.json());return true;
}
function syncProjectFileUI(){
  const tpl=$('pf-mode').value==='template';$('tplBox').hidden=!tpl;$('sc-msgs').hidden=!$('sc-status').checked;
  $('pf-note').textContent=tpl?'Your sliced template is copied and the plot swapped in; its own thumbnail and settings are kept.'
    :'BambuPlot writes the whole project file: the plot, a thumbnail, the printer model, the time estimate and the pause count. No slicer needed.';
  $('pf-thumb').closest('.srow').hidden=tpl;
}
for(const id of ['pf-mode','pf-thumb','sc-progress','sc-status','sc-pause','sc-pen','sc-sheet'])$(id).addEventListener('input',async()=>{syncProjectFileUI();await savePrinter()});
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
  const step=$('sendSheetStep'),fi=FLIP.info;step.hidden=!(flipOn()&&fi&&fi.sheets>1);
  if(!step.hidden)step.innerHTML=`<b>New sheet</b> ${fi.sheets-1} time${fi.sheets>2?'s':''}: the pen lifts high and the plate comes forward. Swap in the next sheet exactly where the last one was, then Resume. An L of tape on the plate at one corner makes this easy.`;
  $('sendSheet').showModal();
};
$('send-cancel').onclick=()=>$('sendSheet').close();
$('send-ack').onchange=e=>{$('send-start').disabled=$('send-connect').disabled=!e.target.checked};
async function sendJob(start){
  const kind=$('send-kind').value;
  if(isBad()){msg('send-msg','Fix the failing safety checks first.','err');return}
  if(printerCfg&&(!printerCfg.ip||!printerCfg.has_access_code)){msg('send-msg','Add the printer IP and access code in the Printer tab first.','err');return}
  $('send-upload').disabled=$('send-start').disabled=true;
  if(!await flipReady((t,err)=>msg('send-msg',t,err?'err':''))){$('send-upload').disabled=false;$('send-start').disabled=!$('send-ack').checked;return}
  msg('send-msg',start?'Uploading and starting…':'Uploading…');
  try{
    const r=await post('/api/send',{...planBody(),kind,start,title:jobTitle()}),d=await r.json();
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
  $('send-connect').disabled=true;
  if(!await flipReady((t,err)=>msg('send-msg',t,err?'err':''))){$('send-connect').disabled=!$('send-ack').checked;return}
  msg('send-msg','Packaging for Bambu Connect…');
  try{
    const r=await post('/api/connect',{...planBody(),kind:$('send-kind').value,title:jobTitle()}),d=await r.json();
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
  $('editor').classList.toggle('video',!!job.image.video);
  if(job.image.video)tool='crop'; // a crop applies to every frame; selections don't
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
  $('vb-model').innerHTML=sel.innerHTML;$('vb-model').value=job.aiModel;if(FLIP.video)renderVideoBg();
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
  keepShape(act());
  renderCropInfo();save();placeInk();renderOver();renderPlace();schedulePlan(0);$('edStatus').textContent=cropText();
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
  changed(['area.h']);$('edStatus').textContent=`Drawing area reshaped to ${fmtSize(a.w,a.h)} to match the crop. Check the safety checks.`;
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

/* ---------- finishing: border and signature ---------- */
const extrasInputs=[...document.querySelectorAll('input[id^="X."],select[id^="X."]')];
function syncExtras(){
  for(const el of extrasInputs){
    const k=el.id.slice(2),v=job.extras[k];
    if(el.type==='checkbox')el.checked=!!v;else if(el!==document.activeElement)el.value=v;
    if(el.type==='range'){const o=$(el.id+'.out');if(o)o.textContent=fmtVal(v,el.step,el.dataset.unit||'')}
  }
  $('borderOpts').hidden=!job.extras.border;$('sigOpts').hidden=!job.extras.signature;
}
for(const el of extrasInputs)el.addEventListener('input',()=>{
  const k=el.id.slice(2);let v=el.type==='checkbox'?el.checked:el.dataset.kind==='text'?el.value:parseFloat(el.value);
  if(el.type==='range'&&!isFinite(v))return;
  job.extras[k]=v;syncExtras();save();schedulePlan(el.type==='range'?350:0);
  if(k==='signature'&&v&&!SIG.strokes.length)openSignature();
});

/* Signature: drawn once, shared by all projects. Strokes are stored normalized, y up. */
const SIG={strokes:[],pad:[],drawing:null};
function sigPath(strokes,flip=true){
  const pts=strokes.flat();if(!pts.length)return {d:'',vb:'0 0 1 1'};
  const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]),x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  const d=strokes.map(s=>'M'+s.map(([x,y])=>`${(x-x0).toFixed(3)} ${(flip?y1-y:y-y0).toFixed(3)}`).join('L')).join('');
  return {d,vb:`-0.05 -0.05 ${(x1-x0+.1).toFixed(3)} ${(y1-y0+.1).toFixed(3)}`};
}
function renderSigPreview(){
  const box=$('sigPrev');
  if(!SIG.strokes.length){box.innerHTML='No signature yet. Press Draw Signature.';$('sigEdit').textContent='Draw Signature…';return}
  const {d,vb}=sigPath(SIG.strokes);box.innerHTML=`<svg viewBox="${vb}" preserveAspectRatio="xMidYMid meet"><path d="${d}"/></svg>`;
  $('sigEdit').textContent='Edit Signature…';
}
async function loadSignature(){try{SIG.strokes=(await (await fetch('/api/signature')).json()).strokes||[]}catch(_){SIG.strokes=[]}renderSigPreview()}
function padCanvas(){const c=$('sigPad'),r=c.getBoundingClientRect(),dpr=devicePixelRatio||1;c.width=r.width*dpr;c.height=r.height*dpr;const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);return {c,g,w:r.width,h:r.height}}
function drawPad(){
  const {g,w,h}=padCanvas();g.clearRect(0,0,w,h);g.lineCap=g.lineJoin='round';g.lineWidth=2.6;g.strokeStyle='#1c2c8a';
  for(const s of SIG.pad.concat(SIG.drawing?[SIG.drawing]:[])){
    g.beginPath();s.forEach(([x,y],i)=>i?g.lineTo(x,y):g.moveTo(x,y));if(s.length===1)g.lineTo(s[0][0]+.1,s[0][1]);g.stroke();
  }
}
/* Fit saved (normalized, y-up) strokes onto the pad. */
function strokesToPad(strokes){
  const {w,h}=padCanvas(),pts=strokes.flat();if(!pts.length)return [];
  const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]),sw=Math.max(...xs)-Math.min(...xs),sh=Math.max(...ys)-Math.min(...ys)||1;
  const s=Math.min((w-60)/(sw||1),(h-90)/sh),ox=(w-sw*s)/2-Math.min(...xs)*s,base=h-56;
  return strokes.map(st=>st.map(([x,y])=>[ox+x*s,base-(y-Math.min(...ys))*s]));
}
function openSignature(){
  $('sigSheet').showModal();msg('sigMsg','');
  requestAnimationFrame(()=>{SIG.pad=strokesToPad(SIG.strokes);drawPad()});
}
$('sigEdit').onclick=openSignature;
$('sigCancel').onclick=()=>$('sigSheet').close();
$('sigClear').onclick=()=>{SIG.pad=[];drawPad()};
$('sigUndo').onclick=()=>{SIG.pad.pop();drawPad()};
const pad=$('sigPad');
pad.addEventListener('pointerdown',e=>{const r=pad.getBoundingClientRect();pad.setPointerCapture(e.pointerId);SIG.drawing=[[e.clientX-r.left,e.clientY-r.top]];drawPad()});
pad.addEventListener('pointermove',e=>{
  if(!SIG.drawing)return;const r=pad.getBoundingClientRect(),p=[e.clientX-r.left,e.clientY-r.top],l=SIG.drawing[SIG.drawing.length-1];
  if(Math.hypot(p[0]-l[0],p[1]-l[1])>1.2){SIG.drawing.push(p);drawPad()}
});
const padEnd=()=>{if(SIG.drawing){SIG.pad.push(SIG.drawing);SIG.drawing=null;drawPad()}};
pad.addEventListener('pointerup',padEnd);pad.addEventListener('pointercancel',padEnd);
/* One pass of corner cutting so mouse-drawn strokes plot smoothly. */
const smoothStroke=s=>s.length<3?s:[s[0],...s.slice(0,-1).flatMap((p,i)=>{const q=s[i+1];return [[.75*p[0]+.25*q[0],.75*p[1]+.25*q[1]],[.25*p[0]+.75*q[0],.25*p[1]+.75*q[1]]]}),s[s.length-1]];
$('sigSave').onclick=async()=>{
  if(!SIG.pad.length){msg('sigMsg','Sign in the box first.','err');return}
  const strokes=SIG.pad.map(smoothStroke).map(s=>s.map(([x,y])=>[x,-y])); // canvas y points down; stored y points up
  const r=await fetch('/api/signature',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({strokes})});
  const d=await r.json();if(!r.ok){msg('sigMsg',typeof d.detail==='string'?d.detail:'Could not save the signature.','err');return}
  SIG.strokes=d.strokes;renderSigPreview();$('sigSheet').close();
  if(!job.extras.signature){job.extras.signature=true;syncExtras();save()}
  schedulePlan(0);setStatus('Signature saved.');
};
$('sigFile').onchange=async e=>{
  const f=e.target.files[0];e.target.value='';if(!f)return;msg('sigMsg','Tracing your signature…');
  const fd=new FormData();fd.append('file',f);
  try{
    const r=await fetch('/api/signature/trace',{method:'POST',body:fd}),d=await r.json();
    if(!r.ok){msg('sigMsg',typeof d.detail==='string'?d.detail:'Could not trace that picture.','err');return}
    SIG.pad=strokesToPad(d.strokes);drawPad();msg('sigMsg','Traced. Check it, tidy up with Undo or draw more, then Save.','ok');
  }catch(_){msg('sigMsg','Could not reach the BambuPlot server.','err')}
};

/* ---------- layers ---------- */
const isFullPlace=p=>!p||(Math.abs(p.x)<1e-9&&Math.abs(p.y)<1e-9&&Math.abs(p.w-1)<1e-9&&Math.abs(p.h-1)<1e-9&&!p.rotation);
const penInfo=n=>(DEF.pens||[]).find(p=>p.pen===n)||{pen:n,label:`Pen ${n}`,colour:'',color:'#1d1d1f'};
function layerTitle(l){return l.name||effects.find(e=>e.name===l.effect)?.label||l.effect}
function layerSub(l){return `${l.image?(l.image.video?`${l.image.name} (video)`:l.image.name):'Sample picture'} · ${penInfo(l.pen).label}`}
const EYE_ON='<svg viewBox="0 0 20 20"><path d="M2 10s3-5.5 8-5.5S18 10 18 10s-3 5.5-8 5.5S2 10 2 10Z"/><circle cx="10" cy="10" r="2.4"/></svg>';
const EYE_OFF='<svg viewBox="0 0 20 20"><path d="M3 3l14 14M7.5 5.2A8 8 0 0 1 10 4.5c5 0 8 5.5 8 5.5a14 14 0 0 1-2.4 3M12.5 14.8a8 8 0 0 1-2.5.7c-5 0-8-5.5-8-5.5a14 14 0 0 1 2.4-3"/></svg>';
function renderLayers(){
  // Top of the list = drawn last, like layers in other apps. Each layer is plotted in full before the next.
  const order=new Map();let n=0;LAYERS.list.forEach((l,i)=>{if(l.visible)order.set(i,++n)});
  const step=i=>order.has(i)&&n>1?`${order.get(i)}. `:'';
  $('layerList').innerHTML=LAYERS.list.map((l,i)=>({l,i})).reverse().map(({l,i})=>`
    <li role="option" data-i="${i}" aria-selected="${i===LAYERS.active}" class="${l.visible?'':'hidden'}">
      <button class="eye" type="button" data-eye="${i}" aria-label="${l.visible?'Hide':'Show'} layer" title="${l.visible?'Hide':'Show'} layer">${l.visible?EYE_ON:EYE_OFF}</button>
      <span class="dot" style="background:${penInfo(l.pen).color}" title="${esc(penInfo(l.pen).label)}"></span>
      <span class="txt"><b>${esc(layerTitle(l))}</b><span class="sub" title="${order.has(i)?`Plotted ${order.get(i)} of ${n}`:'Hidden: not plotted'}">${step(i)}${esc(layerSub(l))}</span></span>
      <button class="more" type="button" data-more="${i}" aria-label="Layer actions" aria-haspopup="menu">⋯</button>
    </li>`).join('');
  syncLayerSettings();if(FLIP.fb)renderFlip();
}
function selectLayer(i){
  LAYERS.active=i;if(sel==='layer')sel='area';renderLayers();buildEffects();renderThumb();syncInputs();placeInk();renderOver();renderPlace();fillAll();
}
function layersChanged(){renderLayers();save();schedulePlan(0)}
$('layerList').addEventListener('click',e=>{
  const eye=e.target.closest('[data-eye]');
  if(eye){const l=LAYERS.list[+eye.dataset.eye];l.visible=!l.visible;layersChanged();return}
  const more=e.target.closest('[data-more]');
  if(more){
    LAYERS.menuFor=+more.dataset.more;const m=$('layerItemMenu'),r=more.getBoundingClientRect();
    m.style.top=`${r.bottom+4}px`;m.style.left=`${Math.max(8,r.right-220)}px`;m.style.right='auto';m.showPopover();return;
  }
  const li=e.target.closest('li');if(li)selectLayer(+li.dataset.i);
});
$('layerItemMenu').addEventListener('click',e=>{
  const act_=e.target.closest('button')?.dataset.act,i=LAYERS.menuFor;if(act_==null||i==null)return;
  $('layerItemMenu').hidePopover();const list=LAYERS.list;
  if(act_==='duplicate'){
    const src=list[i],copy=newLayer({...JSON.parse(JSON.stringify({...src,image:null,id:''})),id:newLayerId(),image:src.image,name:src.name?`${src.name} copy`:''});
    list.splice(i+1,0,copy);LAYERS.active=i+1;
  }else if(act_==='up'&&i<list.length-1){[list[i],list[i+1]]=[list[i+1],list[i]];LAYERS.active=i+1}
  else if(act_==='down'&&i>0){[list[i],list[i-1]]=[list[i-1],list[i]];LAYERS.active=i-1}
  else if(act_==='delete'){
    if(list.length===1){setStatus('A project needs at least one layer.',true);return}
    list.splice(i,1);LAYERS.active=Math.min(LAYERS.active>=i?Math.max(0,LAYERS.active-1):LAYERS.active,list.length-1);
  }
  selectLayer(LAYERS.active);save();schedulePlan(0);
});
$('layerMenu').addEventListener('click',e=>{
  const kind=e.target.closest('button')?.dataset.add;if(!kind)return;$('layerMenu').hidePopover();
  const cur=act();
  // A stacked effect starts with the next pen, and a trace if the current layer is a tone effect.
  const layer=kind==='same'
    ?newLayer({image:cur.image,prep:JSON.parse(JSON.stringify(cur.prep)),place:{...cur.place},pen:Math.min(6,cur.pen+1),
      effect:cur.image?.kind==='svg'?'svg':(cur.effect==='centerline'?'flow':'centerline')})
    :newLayer({pen:Math.min(6,cur.pen+1)});
  LAYERS.list.push(layer);selectLayer(LAYERS.list.length-1);selectTab(kind==='picture'?'tab-picture':'tab-effect');
  save();schedulePlan(0);
  if(kind==='picture')$('file').click();
});

/* Layer settings (Effect tab) */
function syncLayerSettings(){
  const l=act();if(!l)return;
  if(document.activeElement!==$('lyName'))$('lyName').value=l.name;$('lyName').placeholder=layerTitle({...l,name:''});
  $('lyPen').innerHTML=(DEF.pens||[]).map(p=>`<option value="${p.pen}">${esc(p.label)} · ${esc(p.colour)}</option>`).join('');$('lyPen').value=String(l.pen);
  $('lyBand').hidden=srcKind()==='svg';
  for(const [id,k] of [['lyLo','band_lo'],['lyHi','band_hi']]){const v=l.prep[k]??(k==='band_lo'?0:1);if(document.activeElement!==$(id))$(id).value=v;$(id+'.out').textContent=`${Math.round(v*100)}%`}
  const full=isFullPlace(l.place);$('lyFull').checked=full;$('lyPlace').hidden=full;
  for(const [id,k,f] of [['lyX','x',100],['lyY','y',100],['lyW','w',100],['lyH','h',100],['lyR','rotation',1]])
    if(document.activeElement!==$(id))$(id).value=Math.round((l.place[k]||0)*f*10)/10;
  $('layerGroup').querySelector('h2').textContent=LAYERS.list.length>1?`Layer · ${layerTitle(l)}`:'Layer';
}
$('lyName').addEventListener('input',()=>{act().name=$('lyName').value.slice(0,60);renderLayers();save()});
$('lyPen').addEventListener('input',()=>{act().pen=+$('lyPen').value;renderLayers();save();schedulePlan(0)});
for(const [id,k] of [['lyLo','band_lo'],['lyHi','band_hi']])$(id).addEventListener('input',()=>{
  let v=parseFloat($(id).value);const p=act().prep;
  if(k==='band_lo')v=Math.min(v,(p.band_hi??1)-0.02);else v=Math.max(v,(p.band_lo??0)+0.02);
  p[k]=v;$(id).value=v;$(id+'.out').textContent=`${Math.round(v*100)}%`;fillRange($(id));save();schedulePlan(350);
});
$('lyFull').addEventListener('input',()=>{
  const l=act();l.place=$('lyFull').checked?{...FULL_PLACE}:{x:0.25,y:0.25,w:0.5,h:0.5,rotation:0};
  syncLayerSettings();placeInk();renderOver();renderPlace();save();schedulePlan(0);
});
for(const [id,k,f] of [['lyX','x',100],['lyY','y',100],['lyW','w',100],['lyH','h',100],['lyR','rotation',1]])$(id).addEventListener('input',()=>{
  const v=parseFloat($(id).value);if(!isFinite(v))return;if((k==='w'||k==='h')&&v<2)return;
  act().place[k]=k==='rotation'?normAng(v):v/f;placeInk();renderOver();renderPlace();save();schedulePlan(300);
});

/* ---------- print area: where a layer's picture prints, keeping its shape ---------- */
const clampN=(v,a,b)=>Math.min(Math.max(v,a),Math.max(a,b));
/* Width ÷ height of what the layer prints: the crop, turned if the picture is turned. */
function pictureAspect(l=act()){
  const im=l?.image;if(!im||!im.w||!im.h)return null;
  if(im.kind==='svg')return im.w/im.h;
  const c=l.prep.crop||[0,0,1,1],a=((c[2]-c[0])*im.w)/((c[3]-c[1])*im.h),t=+l.prep.rotate||0;
  return t===90||t===270?1/a:a;
}
/* The box (drawing-area mm, y up) a layer's placement refers to: the drawing area, or on a flipbook
   the picture part of the page being looked at. Matches the server's page grid. */
function flipSlot(){const per=FLIP.fb.cols*FLIP.fb.rows,fr=frameRange(),i=curFrame(),k=fr.indexOf(i);return (k>=0?k:i)%per}
function layerParent(){
  const a=L.area;if(!flipOn())return {x:0,y:0,w:a.w,h:a.h};
  const fb=FLIP.fb,per=fb.cols*fb.rows,pw=a.w/fb.cols,ph=a.h/fb.rows,inset=per>1?fb.gap/2:0,k=flipSlot();
  const r=Math.floor(k/fb.cols),c=k%fb.cols,w=pw-2*inset,h=ph-2*inset,b=Math.min(fb.binding,w*0.6);
  return {x:c*pw+inset+b,y:a.h-(r+1)*ph+inset,w:w-b,h};
}
/* Fill mode: the picture covers the whole area and is cut to its shape (how BambuPlot used to work). */
const fillMode=l=>isFullPlace(l.place)&&l.prep.fit!=='fit';
function layerBox(l=act()){const P=layerParent(),p=l?.place||FULL_PLACE;return {x:P.x+p.x*P.w,y:P.y+p.y*P.h,w:p.w*P.w,h:p.h*P.h,rotation:p.rotation||0}}
function setLayerBox(b,l=act()){
  const P=layerParent(),r=v=>Math.round(v*10000)/10000;
  l.place={x:r((b.x-P.x)/P.w),y:r((b.y-P.y)/P.h),w:r(b.w/P.w),h:r(b.h/P.h),rotation:l.place?.rotation||0};
}
/* The largest box of the picture's shape that fits, scaled and centered where asked (parent mm). */
function fitBox(l,{scale=1,center=null,area=null}={}){
  const asp=pictureAspect(l),P=layerParent();
  let mw=P.w,mh=mw/asp;if(mh>P.h){mh=P.h;mw=mh*asp}
  let w=mw*scale,h=mh*scale;
  if(area){w=Math.sqrt(area*asp);h=w/asp;const k=Math.min(1,mw/w);w*=k;h*=k}
  w=Math.max(Math.min(5,mw),w);h=w/asp;
  const cx=clampN(center?center[0]:P.x+P.w/2,P.x+w/2,P.x+P.w-w/2),cy=clampN(center?center[1]:P.y+P.h/2,P.y+h/2,P.y+P.h-h/2);
  return {x:cx-w/2,y:cy-h/2,w,h,rotation:l.place?.rotation||0};
}
/* After the picture's shape changes (new picture, crop, turn): the print area takes the new shape,
   keeping its middle and size. A new picture, or one that filled the area, is fitted whole. */
function keepShape(l=act(),fresh=false){
  if(!l?.image||!pictureAspect(l))return;
  if(fresh||fillMode(l)){l.prep.fit='fit';setLayerBox(fitBox(l),l);return}
  const b=layerBox(l);setLayerBox(fitBox(l,{center:[b.x+b.w/2,b.y+b.h/2],area:b.w*b.h}),l);
}
/* After the drawing area (or flipbook page) changes size: same shape, same share of the space. */
function reshapeAll(){
  for(const l of LAYERS.list){
    if(!l.image||fillMode(l)||!pictureAspect(l))continue;
    const P=layerParent(),p=l.place,asp=pictureAspect(l);
    let mw=P.w,mh=mw/asp;if(mh>P.h){mh=P.h;mw=mh*asp}
    const scale=Math.min(1,Math.max(p.w*P.w/mw,p.h*P.h/mh));
    setLayerBox(fitBox(l,{scale,center:[P.x+(p.x+p.w/2)*P.w,P.y+(p.y+p.h/2)*P.h]}),l);
  }
}
/* Resize from a corner in the drawing area's frame: the opposite corner stays put and the shape is kept. */
function resizeLayer(o,corner,dx,dy,P){
  const b={...o,rot:o.rotation||0},left=corner[1]==='l',front=corner[0]==='f';
  const [ox,oy]=toWorld(b,left?b.w:0,front?b.h:0),[mx,my]=toWorld(b,left?0:b.w,front?0:b.h);
  const [lu,lv]=rotPt(mx+dx-ox,my+dy-oy,-b.rot);
  let s=Math.max((left?-lu:lu)/o.w,(front?-lv:lv)/o.h);
  s=Math.max(s,5/Math.min(o.w,o.h));s=Math.min(s,P.w/o.w,P.h/o.h);
  const w=o.w*s,h=o.h*s,[cu,cv]=rotPt((left?-w:w)/2,(front?-h:h)/2,b.rot);
  return {...o,w,h,x:clampN(ox+cu-w/2,P.x,P.x+P.w-w),y:clampN(oy+cv-h/2,P.y,P.y+P.h-h)};
}
/* Plate point → drawing-area mm. */
function areaLocal(m){const a=areaBox(),[cx,cy]=centerOf(a),[u,v]=rotPt(m.x-cx,m.y-cy,-a.rot);return {x:u+a.w/2,y:v+a.h/2}}
function layerFrameSVG(){
  const l=act();if(!l?.image||fillMode(l)||drawing||!plan)return '';
  const b={...layerBox(l)};b.rot=b.rotation;const A=areaBox();
  const W=(u,v)=>{const [lx,ly]=toWorld(b,u,v);return toWorld(A,lx,ly)};
  const pts=[[0,0],[b.w,0],[b.w,b.h],[0,b.h]].map(([u,v])=>W(u,v));
  let h=`<polygon class="picframe${sel==='layer'?' sel':''}" data-drag="layer" points="${pts.map(([x,y])=>`${X(x)},${Y(y)}`).join(' ')}"><title>Picture: drag to move it</title></polygon>`;
  for(const [k,u,v] of [['fl',0,0],['fr',b.w,0],['bl',0,b.h],['br',b.w,b.h]]){
    const [x,y]=W(u,v);h+=`<circle class="pichandle" data-drag="lh-${k}" cx="${X(x)}" cy="${Y(y)}" r="1.8"><title>Drag to resize the picture</title></circle>`;
  }
  if(sel==='layer'){const [x,y]=W(b.w/2,b.h);h+=`<text class="piclbl" x="${X(x)}" y="${(V.y1-y-1.6).toFixed(2)}" text-anchor="middle">${fmtSize(b.w,b.h)}</text>`}
  return h;
}
function renderPlace(){
  const l=act(),g=$('placeGroup');if(!g)return;g.hidden=!l?.image||!pictureAspect(l);if(g.hidden)return;
  const fill=fillMode(l),b=layerBox(l),asp=pictureAspect(l),P=layerParent();
  let mw=P.w,mh=mw/asp;if(mh>P.h){mh=P.h;mw=mh*asp}
  $('placeInfo').textContent=fill
    ?'The picture covers the whole drawing area, so any part that doesn’t match the area’s shape is cut off. Press Fit to Area to print all of what you chose.'
    :`Prints ${fmtSize(b.w,b.h)}: all of the ${l.prep.crop?'cropped part':'picture'}, in its own shape. Drag the purple frame on the plate to move it, or a corner to resize it.`;
  const pct=fill?100:Math.round(100*Math.min(1,Math.max(b.w/mw,b.h/mh)));
  if(document.activeElement!==$('plSize'))$('plSize').value=pct;$('plSize.out').textContent=fill?'Fills':`${pct}%`;fillRange($('plSize'));
  $('plFill').setAttribute('aria-pressed',fill);$('plCenter').disabled=fill;
}
function placeChanged(delay=0){placeInk();renderOver();renderPlace();syncLayerSettings();save();schedulePlan(delay)}
$('plFit').onclick=()=>{const l=act();l.prep.fit='fit';setLayerBox(fitBox(l),l);sel='layer';placeChanged()};
$('plCenter').onclick=()=>{const l=act(),b=layerBox(l);setLayerBox(fitBox(l,{area:b.w*b.h}),l);placeChanged()};
$('plFill').onclick=()=>{const l=act();l.place={...FULL_PLACE,rotation:0};l.prep.fit='fill';if(sel==='layer')sel='area';placeChanged()};
$('plSize').addEventListener('input',()=>{
  const l=act();if(fillMode(l))l.prep.fit='fit';
  const b=fillMode(l)?null:layerBox(l),s=+$('plSize').value/100;
  setLayerBox(fitBox(l,{scale:s,center:b?[b.x+b.w/2,b.y+b.h/2]:null}),l);sel='layer';placeChanged(300);
});

/* ---------- background removal for every frame of a video ---------- */
let bgPoll=0;
async function startVideoBg(method){
  const body={method};if(method==='ai')body.model=$('vb-model').value||job.aiModel;
  $('vb-msg').textContent=method==='ai'?'Starting the AI model (the first use downloads it)…':'Starting…';
  try{
    const r=await post(`/api/projects/${FLIP.pid}/video/background`,body),d=await r.json();
    if(!r.ok){$('vb-msg').textContent=errText(d.detail);return}
    FLIP.bg=d;renderVideoBg();pollVideoBg();
  }catch(_){$('vb-msg').textContent='Could not reach the BambuPlot server.'}
}
async function pollVideoBg(){
  clearTimeout(bgPoll);const pid=FLIP.pid;if(!pid||!FLIP.video)return;
  try{
    const r=await fetch(`/api/projects/${pid}/video/background`);if(!r.ok||pid!==FLIP.pid)return;
    const d=await r.json();FLIP.bg=d;
    if(d.running)bgPoll=setTimeout(pollVideoBg,600);
    else if(d.video&&d.video.version!==FLIP.video?.version){
      setVideo(d.video);for(const l of LAYERS.list)if(l.image?.video)l.prep.use_mask=true;
      videoImage();renderThumb();renderFlip();save();schedulePlan(0);
    }
  }catch(_){}
  renderVideoBg();
}
function renderVideoBg(){
  if(!FLIP.video)return;
  const d=FLIP.bg,v=FLIP.video,run=!!d?.running,bg=v.background,l=act();
  $('vb-auto').disabled=$('vb-ai').disabled=run;$('vb-cancel').hidden=!run;$('vb-clear').hidden=!bg||run;
  $('vb-ai').hidden=$('vb-modelRow').hidden=!AI.available;
  $('vb-use').closest('.srow').hidden=!bg||!l?.image?.video;$('vb-use').checked=l?.prep?.use_mask!==false;
  $('vb-auto').textContent=bg?'Remove Again':'Remove Background';
  $('vb-msg').textContent=run?`Removing the background: frame ${d.done} of ${d.total}…`
    :d?.error?`Background removal stopped: ${d.error}`
    :bg?`Background removed from ${v.count-(bg.failed||0)} of ${v.count} frames (${bg.method==='auto:moving'?'kept what moves':bg.method.startsWith('ai')?'AI':'automatic'}).${bg.failed?` No subject found in ${bg.failed}; those are drawn whole.`:''}`
    :'Removes the background from every frame, so only the subject is drawn. With a steady camera it keeps whatever moves.';
}
$('vb-auto').onclick=()=>startVideoBg('auto');
$('vb-ai').onclick=()=>startVideoBg('ai');
$('vb-cancel').onclick=async()=>{await post(`/api/projects/${FLIP.pid}/video/background/cancel`,{});pollVideoBg()};
$('vb-clear').onclick=async()=>{
  const r=await fetch(`/api/projects/${FLIP.pid}/video/background`,{method:'DELETE'}),d=await r.json();
  if(!r.ok){$('vb-msg').textContent=errText(d.detail);return}
  setVideo(d);videoImage();FLIP.bg=null;renderThumb();renderFlip();renderVideoBg();save();schedulePlan(0);
};
$('vb-model').addEventListener('input',()=>{job.aiModel=$('vb-model').value;save()});
$('vb-use').addEventListener('input',()=>{
  act().prep.use_mask=$('vb-use').checked;videoImage();
  if(job.image?.video){$('srcimg').setAttribute('href',job.image.url);const t=$('thumb').querySelector('img');if(t)t.src=job.image.url}
  save();schedulePlan(0);
});

/* ---------- video & flipbook ---------- */
async function openVideo(f){
  if(!FLIP.pid){setStatus('Open a project first.',true);return}
  setStatus(`Uploading ${f.name} (${(f.size/1048576).toFixed(1)} MB), then taking frames…`);
  const fd=new FormData();fd.append('file',f);
  try{
    const r=await fetch(`/api/projects/${FLIP.pid}/video`,{method:'POST',body:fd}),d=await r.json().catch(()=>({}));
    if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:r.status===404?'the server is out of date. Restart it.':r.statusText);
    setVideo(d);Object.assign(FLIP.fb,{frame:0,first:0,last:null,enabled:true});
    if(job.image&&!job.image.video&&job.image.url)URL.revokeObjectURL(job.image.url);
    job.prep.crop=null;job.image=videoImage();keepShape(act(),true);
    renderThumb();buildEffects();renderLayers();renderFlip();render();save();schedulePlan(0);
    setStatus(`Took ${d.count} frames from ${d.name}. Scrub with the slider under the plate, or press , and .`);
  }catch(err){setStatus('Video failed: '+err.message,true)}
}
/* New frames (or none): every layer on the video follows. */
function setVideo(v){
  FLIP.video=v;FLIP.pages.clear();FLIP.info=null;
  if(v){
    FLIP.ex=deepMerge(DEF.extract||{},v.extract);videoImage();
    for(const l of LAYERS.list)if(l.image?.video)l.image=FLIP.vimg;
    const n=v.count;FLIP.fb.frame=Math.min(FLIP.fb.frame,n-1);
    if(FLIP.fb.first>n-1)FLIP.fb.first=0;if(FLIP.fb.last!=null&&FLIP.fb.last>n-1)FLIP.fb.last=null;
  }else{for(const l of LAYERS.list)if(l.image?.video)l.image=null;FLIP.vimg=null}
}
function setFrame(i){
  if(!FLIP.video)return;
  i=Math.max(0,Math.min(i,FLIP.video.count-1));if(i===FLIP.fb.frame&&$('frSlider').value==i)return;
  FLIP.fb.frame=i;const im=videoImage();
  if(job.image?.video){$('srcimg').setAttribute('href',im.url);const t=$('thumb').querySelector('img');if(t)t.src=im.url}
  syncScrubber();save();schedulePlan(120);
}
function syncScrubber(){
  const v=FLIP.video,show=!!v&&usesVideo();
  $('scrubber').hidden=!show;document.querySelector('.canvas').classList.toggle('has-video',show);
  if(!show)return;
  const i=curFrame(),t=v.times?.[i];
  $('frSlider').max=v.count-1;if(document.activeElement!==$('frSlider'))$('frSlider').value=i;fillRange($('frSlider'));
  const inRange=!FLIP.fb.enabled||(i>=FLIP.fb.first&&i<=(FLIP.fb.last??v.count-1));
  $('frLabel').textContent=`Frame ${i+1} of ${v.count}${t!=null?` · ${t.toFixed(2)} s`:''}${inRange?'':' · skipped'}`;
  $('frPlay').hidden=!flipOn();
}
$('frSlider').addEventListener('input',()=>setFrame(+$('frSlider').value));
$('frPrev').onclick=()=>setFrame(curFrame()-1);
$('frNext').onclick=()=>setFrame(curFrame()+1);
$('frPlay').onclick=()=>openFlipPlayer();

/* The panel: which frames to take, and how they become pages. */
const FB_SWITCHES=['enabled','numbers','cut_marks','check_each_sheet'],FB_SLIDERS=['gap','binding','number_size'];
function extractCount(){
  const v=FLIP.video,e=FLIP.ex;if(!v||!e)return 0;
  const end=Math.min(e.end??v.clip.duration,v.clip.duration),span=Math.max(0,end-Math.min(e.start,end));
  return Math.max(1,Math.min(e.max_frames,Math.ceil(span*e.fps-1e-9)));
}
function frameRange(){
  const n=FLIP.video?.count||0;if(!n)return [];
  const last=Math.min(FLIP.fb.last??n-1,n-1),first=Math.min(FLIP.fb.first,last),out=[];
  for(let i=first;i<=last;i++)out.push(i);return out;
}
function renderFlip(){
  const v=FLIP.video,g=$('flipGroup');g.hidden=!v;syncScrubber();
  if(!v)return;
  const c=v.clip,e=FLIP.ex,fb=FLIP.fb;
  $('vidInfo').textContent=`${v.name} · ${c.duration.toFixed(1)} s · ${c.width} × ${c.height} · ${Math.round(c.fps*10)/10} frames/s. ${v.count} frames taken${usesVideo()?'':'. Choose the video as a layer\'s picture to use them'}.`;
  for(const [id,val] of [['vx-start',e.start],['vx-end',e.end??c.duration]])if(document.activeElement!==$(id))$(id).value=Math.round(val*10)/10;
  $('vx-start').max=$('vx-end').max=c.duration;
  $('vx-fps').max=Math.max(1,Math.round(c.fps));$('vx-fps').value=e.fps;$('vx-fps.out').textContent=`${e.fps}`;
  $('vx-max').value=e.max_frames;$('vx-max.out').textContent=`${e.max_frames} frames`;
  const want=extractCount(),same=JSON.stringify(e)===JSON.stringify(deepMerge(DEF.extract||{},v.extract));
  $('vx-go').textContent=same?`Take Frames Again`:`Take ${want} Frames`;$('vx-go').disabled=same;
  for(const k of FB_SWITCHES)$('fb-'+k).checked=!!fb[k];
  for(const k of FB_SLIDERS){$('fb-'+k).value=fb[k];$(`fb-${k}.out`).textContent=fmtVal(fb[k],$('fb-'+k).step,'mm')}
  const nums=n=>Array.from({length:8},(_,i)=>`<option value="${i+1}">${i+1}</option>`).join('');
  if(!$('fb-cols').options.length){$('fb-cols').innerHTML=nums();$('fb-rows').innerHTML=nums()}
  $('fb-cols').value=fb.cols;$('fb-rows').value=fb.rows;
  $('fbOpts').hidden=!fb.enabled;$('fbNumSize').hidden=!fb.numbers;
  for(const id of ['fb-first','fb-last']){$(id).max=v.count}
  if(document.activeElement!==$('fb-first'))$('fb-first').value=fb.first+1;
  if(document.activeElement!==$('fb-last'))$('fb-last').value=(fb.last??v.count-1)+1;
  const per=fb.cols*fb.rows,a=L.area,inset=per>1?fb.gap:0,pw=a.w/fb.cols-inset,ph=a.h/fb.rows-inset;
  $('fb-pageinfo').textContent=per===1?'One frame per sheet, filling the drawing area.'
    :`${per} pages per sheet, each ${fmtSize(Math.max(0,pw),Math.max(0,ph))}${fb.binding?` (${fmtSize(Math.max(0,pw-fb.binding),Math.max(0,ph))} of picture)`:''}.`;
  const fr=frameRange(),sheets=Math.ceil(fr.length/per),fi=FLIP.info;
  $('fb-summary').textContent=`${fr.length} frame${fr.length===1?'':'s'} on ${sheets} sheet${sheets===1?'':'s'}${fi?`, about ${fmtMinutes(fi.minutes_per_page*fr.length)} of plotting`:''}.${sheets>1?` The printer pauses for each new sheet.`:''}`;
  $('fb-preview').disabled=!flipOn();renderVideoBg();
}
for(const [id,k] of [['vx-start','start'],['vx-end','end']])$(id).addEventListener('input',()=>{
  const v=parseFloat($(id).value);if(!isFinite(v)||v<0)return;FLIP.ex[k]=Math.min(v,FLIP.video.clip.duration);renderFlip();
});
$('vx-fps').addEventListener('input',()=>{FLIP.ex.fps=+$('vx-fps').value;renderFlip()});
$('vx-max').addEventListener('input',()=>{FLIP.ex.max_frames=+$('vx-max').value;renderFlip()});
$('vx-go').onclick=async()=>{
  const e=FLIP.ex;if(e.end!=null&&e.end<=e.start){$('vx-msg').textContent='“To” must be after “From”.';return}
  $('vx-go').disabled=true;$('vx-msg').textContent=`Taking ${extractCount()} frames…`;
  try{
    const r=await post(`/api/projects/${FLIP.pid}/video/extract`,{...e,end:e.end!=null&&e.end>=FLIP.video.clip.duration-0.05?null:e.end}),d=await r.json();
    if(!r.ok)throw new Error(typeof d.detail==='string'?d.detail:'that didn’t work');
    setVideo(d);$('vx-msg').textContent=`${d.count} frames ready.`;
    renderThumb();renderFlip();save();schedulePlan(0);
  }catch(err){$('vx-msg').textContent='Couldn’t take frames: '+err.message}
  finally{renderFlip()}
};
let removeArmed=0;
$('vx-remove').onclick=async()=>{
  if(!removeArmed){$('vx-remove').textContent='Click Again to Remove';removeArmed=setTimeout(()=>{removeArmed=0;$('vx-remove').textContent='Remove Video'},3000);return}
  clearTimeout(removeArmed);removeArmed=0;$('vx-remove').textContent='Remove Video';
  await fetch(`/api/projects/${FLIP.pid}/video`,{method:'DELETE'});
  setVideo(null);renderThumb();buildEffects();renderLayers();renderFlip();save();schedulePlan(0);setStatus('Video removed from the project.');
};
for(const k of FB_SWITCHES)$('fb-'+k).addEventListener('input',()=>{FLIP.fb[k]=$('fb-'+k).checked;flipChanged()});
for(const k of FB_SLIDERS)$('fb-'+k).addEventListener('input',()=>{FLIP.fb[k]=+$('fb-'+k).value;flipChanged(350)});
for(const k of ['cols','rows'])$('fb-'+k).addEventListener('input',()=>{FLIP.fb[k]=+$('fb-'+k).value;flipChanged()});
$('fb-first').addEventListener('input',()=>{const v=parseInt($('fb-first').value);if(!(v>=1))return;FLIP.fb.first=Math.min(v,FLIP.video.count)-1;flipChanged()});
$('fb-last').addEventListener('input',()=>{const v=parseInt($('fb-last').value);if(!(v>=1))return;const n=FLIP.video.count;FLIP.fb.last=v>=n?null:Math.max(v-1,FLIP.fb.first);flipChanged()});
function flipChanged(delay=0){reshapeAll();renderFlip();save();schedulePlan(delay);placeInk();renderOver();renderPlace()}
$('fb-preview').onclick=()=>openFlipPlayer();

/* Page outlines on the plate, in drawing-area mm (y up), with the frame each page will get. */
function renderFlipCells(){
  const g=$('flipcells'),fi=FLIP.info;
  if(!fi||!flipOn()){g.innerHTML='';placeInk();return}
  const h=L.area.h,per=fi.per_sheet,start=fi.sheet?fi.first+(fi.sheet-1)*per:null;let out='';
  fi.cells.forEach((c,k)=>{
    const [x,y,w,ch]=c.page,[ax]=c.art,top=h-(y+ch);
    if(per>1||ax>x)out+=`<rect class="fbcell${k===fi.slot?' cur':''}" x="${x}" y="${top}" width="${w}" height="${ch}"></rect>`;
    if(ax>x)out+=`<rect class="fbstrip" x="${x}" y="${top}" width="${ax-x}" height="${ch}"></rect>`;
    const n=start!=null&&start+k<=fi.last?start+k+1:null;
    if(per>1&&k!==fi.slot&&n)out+=`<text class="fblbl" x="${ax+2}" y="${top+4.5}">${n}</text>`;
  });
  g.innerHTML=out;placeInk();
}

/* Before a flipbook job: draw every page (each is cached on the server), showing progress. */
const pageKey=(body,i)=>JSON.stringify({...body,flipbook:{...body.flipbook,frame:0}})+'#'+i;
async function loadPage(body,i){
  const key=pageKey(body,i);if(FLIP.pages.has(key))return FLIP.pages.get(key);
  const r=await post('/api/flipbook/frame',{...body,index:i});
  if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(errText(e.detail))}
  const d=await r.json();FLIP.pages.set(key,d);return d;
}
async function prepareFlipbook(say){
  const fr=frameRange(),body=planBody();let done=0,next=0;
  const worker=async()=>{while(next<fr.length){const i=fr[next++];await loadPage(body,i);done++;say(`Drawing the flipbook: frame ${done} of ${fr.length}…`)}};
  await Promise.all([worker(),worker(),worker()]);
  say(`Putting ${fr.length} frames into one job…`);
}
async function flipReady(say){
  if(!flipOn())return true;
  try{await prepareFlipbook(say);return true}catch(e){say('Flipbook failed: '+e.message,true);return false}
}

/* ---------- flipbook preview player ---------- */
const PLAYER={frames:[],at:0,timer:0,body:null,img:new Map(),loading:false};
function stopFlipPlayer(){clearInterval(PLAYER.timer);PLAYER.timer=0;$('flPlayIcon')?.setAttribute('d','M7 5v10l8-5z')}
async function openFlipPlayer(){
  if(!flipOn())return;
  PLAYER.frames=frameRange();PLAYER.body=planBody();PLAYER.at=Math.max(0,PLAYER.frames.indexOf(curFrame()));
  $('flFrame').max=PLAYER.frames.length-1;$('flFrame').value=PLAYER.at;
  const fps=Math.round(FLIP.video.extract?.fps||12);$('flFps').value=Math.min(30,fps);$('flFps.out').textContent=`${$('flFps').value} frames/s`;
  $('flipSheet').showModal();fillAll();drawPlayer();
  PLAYER.loading=true;let done=0;const body=PLAYER.body;
  try{
    let next=0;
    const worker=async()=>{while(next<PLAYER.frames.length&&$('flipSheet').open&&PLAYER.body===body){
      const i=PLAYER.frames[next++];await loadPage(body,i);done++;
      msg('flMsg',done<PLAYER.frames.length?`Drawing frames: ${done} of ${PLAYER.frames.length}…`:`All ${done} frames drawn.`);
      if(i===PLAYER.frames[PLAYER.at])drawPlayer();
    }};
    await Promise.all([worker(),worker(),worker()]);
  }catch(e){msg('flMsg','Preview failed: '+e.message,'err')}
  PLAYER.loading=false;drawPlayer();
}
function playerImage(i){
  if(PLAYER.img.has(frameURL(i)))return PLAYER.img.get(frameURL(i));
  const im=new Image();im.onload=()=>{if(PLAYER.frames[PLAYER.at]===i)drawPlayer()};im.src=frameURL(i);PLAYER.img.set(frameURL(i),im);return im;
}
function drawPlayer(){
  const i=PLAYER.frames[PLAYER.at];if(i==null)return;
  const page=FLIP.pages.get(pageKey(PLAYER.body,i)),c=$('flipCanvas'),stage=c.parentElement.getBoundingClientRect();
  const cell=FLIP.info?.cells?.[0]?.art,w=page?.w||cell?.[2]||L.area.w,h=page?.h||cell?.[3]||L.area.h;
  const s=Math.min((stage.width-24)/w,(stage.height-24)/h),dpr=devicePixelRatio||1;
  c.style.width=`${w*s}px`;c.style.height=`${h*s}px`;c.width=Math.round(w*s*dpr);c.height=Math.round(h*s*dpr);
  const g=c.getContext('2d');g.setTransform(s*dpr,0,0,s*dpr,0,0);g.fillStyle='#fdfdfb';g.fillRect(0,0,w,h);
  if($('flPic').checked){const im=playerImage(i);if(im.complete&&im.naturalWidth){
    const k=Math.max(w/im.naturalWidth,h/im.naturalHeight),iw=im.naturalWidth*k,ih=im.naturalHeight*k;
    g.globalAlpha=.3;g.drawImage(im,(w-iw)/2,(h-ih)/2,iw,ih);g.globalAlpha=1}}
  if(page){
    g.lineCap=g.lineJoin='round';g.lineWidth=L.pen.width_mm;
    for(const grp of page.groups){g.strokeStyle=grp.color;g.beginPath();
      for(const l of grp.lines){g.moveTo(l[0][0],h-l[0][1]);if(l.length===1)g.lineTo(l[0][0]+.01,h-l[0][1]);for(let k=1;k<l.length;k++)g.lineTo(l[k][0],h-l[k][1])}
      g.stroke()}
  }else{g.fillStyle='#8e8e93';g.font='4px -apple-system,sans-serif';g.textAlign='center';g.fillText('Drawing…',w/2,h/2)}
  $('flLabel').textContent=`Frame ${i+1} · ${PLAYER.at+1} of ${PLAYER.frames.length}`;
  if(document.activeElement!==$('flFrame'))$('flFrame').value=PLAYER.at;fillRange($('flFrame'));
}
function playerStep(){
  if(!PLAYER.frames.length)return;
  let n=(PLAYER.at+1)%PLAYER.frames.length;
  // Don't run ahead of the frames still being drawn.
  if(!FLIP.pages.has(pageKey(PLAYER.body,PLAYER.frames[n]))&&PLAYER.loading)return;
  PLAYER.at=n;drawPlayer();
}
function startPlayer(){stopFlipPlayer();PLAYER.timer=setInterval(playerStep,1000/+$('flFps').value);$('flPlayIcon').setAttribute('d','M6 5h3v10H6zM11 5h3v10h-3z')}
$('flPlay').onclick=()=>PLAYER.timer?stopFlipPlayer():startPlayer();
$('flFrame').addEventListener('input',()=>{PLAYER.at=+$('flFrame').value;drawPlayer()});
$('flFps').addEventListener('input',()=>{$('flFps.out').textContent=`${$('flFps').value} frames/s`;if(PLAYER.timer)startPlayer()});
$('flPic').addEventListener('input',drawPlayer);
$('flClose').onclick=()=>$('flipSheet').close();
$('flipSheet').addEventListener('close',()=>{stopFlipPlayer();PLAYER.body=null});
$('flipSheet').addEventListener('keydown',e=>{
  if(e.key===' '){e.preventDefault();$('flPlay').click()}
  else if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();stopFlipPlayer();PLAYER.at=(PLAYER.at+(e.key==='ArrowRight'?1:-1)+PLAYER.frames.length)%PLAYER.frames.length;drawPlayer()}
});

/* ---------- units ---------- */
function paperOptions(){
  if(!DEF?.paper_sizes)return;
  const sel=$('L.paper.size'),cur=L?.paper?.size||sel.value||'A5';
  sel.innerHTML=DEF.paper_sizes.map(p=>`<option value="${esc(p.key)}">${esc(p.key)} · ${fmtSize(p.w,p.h)}</option>`).join('')+'<option value="custom">Custom size…</option>';
  sel.value=cur;
}
function applyUnits(prev){
  const u=UNITS[unit];
  document.querySelectorAll('input[data-len]').forEach(el=>{
    if(el.dataset.minMm===undefined){el.dataset.minMm=el.min;el.dataset.maxMm=el.max}
    el.step=u.step;
    el.min=el.dataset.minMm===''?'':+(+el.dataset.minMm/u.f).toFixed(u.dec+1);
    el.max=el.dataset.maxMm===''?'':+(+el.dataset.maxMm/u.f).toFixed(u.dec+1);
    const label=el.closest('.field')?.querySelector(':scope>span');if(label)label.dataset.unit=unit;
    // Typed-in measurements that aren't part of the layout: convert them in place.
    if(prev&&!el.id.startsWith('L.')&&!el.id.startsWith('D.')&&el.value!==''){
      const mm=parseFloat(el.value)*UNITS[prev].f;el.value=+(mm/u.f).toFixed(u.inDec);
    }
  });
  $('snapLabel').textContent=`Snap to ${u.snapLabel}`;
  document.querySelectorAll('#units button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.u===unit));
  paperOptions();
}
document.querySelectorAll('#units button').forEach(b=>b.onclick=()=>{
  if(b.dataset.u===unit)return;const prev=unit;unit=b.dataset.u;
  try{localStorage.setItem('bambuplot.units',unit)}catch(_){}
  applyUnits(prev);if(currentProject){render();refreshChecks();updateAlignment()}
});

/* ---------- alignment test ---------- */
/* Where the cross should land, measured from the sheet's own left and front edges. */
function alignExpected(){
  const p=paperBox(),[pcx,pcy]=centerOf(p),[acx,acy]=centerOf(L.area);
  const [lx,ly]=rotPt(acx-pcx,acy-pcy,-p.rot);
  return [lx+p.w/2,ly+p.h/2];
}
let alT=0,alLast=null;
function renderAlignExpected(){
  const [l,f]=alignExpected();$('alExpL').textContent=fmtLen(l);$('alExpF').textContent=fmtLen(f);
}
function dirWords(v,pos,neg){return `${fmtLen(Math.abs(v))} ${v>0?pos:neg}`}
async function updateAlignment(){
  const l=fromU(parseFloat($('alMeasL').value)),f=fromU(parseFloat($('alMeasF').value)),box=$('alResult');
  if(!isFinite(l)||!isFinite(f)){box.hidden=true;$('alPaper').disabled=$('alPen').disabled=true;alLast=null;return}
  try{
    const c=await (await post('/api/alignment',{layout:L,left:l,front:f})).json();alLast=c;
    const err=Math.hypot(c.error_x,c.error_y),ok=err<0.3;
    let html=ok?`<span class="big">On target</span>The cross landed within ${fmtLen(err)} of where it should. No correction needed.`
      :`<span class="big">${dirWords(c.error_bed_x,'right','left')}, ${dirWords(c.error_bed_y,'toward the back','toward the front')}</span>`+
       `The drawing landed that far from where it should on the sheet. Either <b>move the sheet ${dirWords(c.move_sheet[0],'right','left')} and ${dirWords(c.move_sheet[1],'back','forward')}</b> next time, or apply a fix below.`;
    // Optional size check: the circle's outside size is its diameter plus the pen width.
    // The Download/Send test always uses a 25 mm circle; an alignment project uses its own setting.
    const r=job.effect==='alignment'?+((job.params.alignment||{}).radius??25):25,want=2*r+L.pen.width_mm,sz=[];
    for(const [id,axis] of [['alW','X'],['alH','Y']]){
      const m=fromU(parseFloat($(id).value));if(!isFinite(m))continue;
      const pct=(m/want-1)*100;
      sz.push(Math.abs(pct)<0.4?`${axis} size is right (${fmtLen(m)} of ${fmtLen(want)}).`:`${axis} is ${Math.abs(pct).toFixed(1)}% too ${pct>0?'long':'short'} (${fmtLen(m)} vs ${fmtLen(want)}): check the belt tension on that axis.`);
    }
    if(sz.length)html+=`<br>${sz.join(' ')}`;
    box.innerHTML=html;box.className='alignresult '+(ok?'ok':'off');box.hidden=false;
    $('alPaper').disabled=$('alPen').disabled=ok;
  }catch(_){box.hidden=false;box.className='alignresult';box.textContent='Could not reach the BambuPlot server.'}
}
for(const id of ['alMeasL','alMeasF','alW','alH'])$(id).addEventListener('input',()=>{clearTimeout(alT);alT=setTimeout(updateAlignment,200)});
function clearAlign(msg){for(const id of ['alMeasL','alMeasF','alW','alH'])$(id).value='';const b=$('alResult');b.className='alignresult ok';b.innerHTML=msg;b.hidden=false;$('alPaper').disabled=$('alPen').disabled=true;alLast=null}
$('alPaper').onclick=()=>{
  if(!alLast)return;[L.paper.x,L.paper.y]=alLast.paper_xy.map(v=>Math.round(v*100)/100);sel='paper';changed(['paper']);
  clearAlign('<span class="big">Paper position updated</span>The layout now matches where the sheet really is. If the safety badge turns red, press <b>Fit to Free Space</b> or <b>Center</b> under Drawing Area. Then plot the test again to confirm.');
};
$('alPen').onclick=()=>{
  if(!alLast)return;[L.pen.offset_x,L.pen.offset_y]=alLast.pen_offset.map(v=>Math.round(v*100)/100);changed(['pen.offset_x']);
  clearAlign(`<span class="big">Pen offset corrected</span>Now X ${fmtLen(L.pen.offset_x)}, Y ${fmtLen(L.pen.offset_y)}. Plot the test again to confirm.`);
};

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
  if((e.key===','||e.key==='.')&&!e.metaKey&&!e.ctrlKey&&FLIP.video&&usesVideo()&&!/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement?.tagName||'')&&!document.querySelector('dialog[open]')){
    e.preventDefault();setFrame(curFrame()+(e.key===','?-1:1));return;
  }
  if(!(e.metaKey||e.ctrlKey)||$('editor').open||$('sendSheet').open||$('sigSheet').open||$('newSheet').open||!$('dashboard').hidden)return;
  const k=e.key.toLowerCase();
  if(k==='o'){e.preventDefault();$('file').click()}
  else if(k==='p'&&e.shiftKey){e.preventDefault();toggleBaseImage()}
  else if(k==='s'){e.preventDefault();saveNow()}
  else if(k==='e'&&e.shiftKey){e.preventDefault();if(!$('dlG').disabled)$('dlG').click()}
  else if(k==='e'&&!e.shiftKey){e.preventDefault();if(!$('tbEdit').disabled)openEditor('crop')}
  else if(k==='enter'){e.preventDefault();$('sendOpen').click()}
  else if(['1','2','3','4'].includes(k)){e.preventDefault();selectTab(['tab-picture','tab-effect','tab-setup','tab-printer'][+k-1])}
});

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
  saveNow();currentProject=null;stopFlipPlayer();
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
  L=deepMerge(DEF.layout,st.layout);
  try{ // the printer's shared holder settings win over whatever an older project saved
    const m=await (await fetch('/api/machine')).json();
    L.bed=deepMerge(DEF.layout.bed,m.bed);L.pen=deepMerge(DEF.layout.pen,m.pen);L.z=deepMerge(DEF.layout.z,m.z);L.feed=deepMerge(DEF.layout.feed,m.feed);
  }catch(_){}
  job.aiModel=typeof st.aiModel==='string'?st.aiModel:null;
  job.extras=deepMerge(DEF.extras||{},st.extras);
  for(const l of LAYERS.list)if(l.image?.url)URL.revokeObjectURL(l.image.url);
  plan=null;$('inklayers').innerHTML='';$('travel').setAttribute('d','');$('flipcells').innerHTML='';
  FLIP.pid=pid;FLIP.fb=deepMerge(DEF.flipbook||{},st.flipbook);FLIP.info=null;FLIP.pages.clear();FLIP.vimg=null;
  FLIP.video=d.video||null;FLIP.ex=deepMerge(DEF.extract||{},FLIP.video?.extract);if(FLIP.video)videoImage();
  // Pictures: one shared object per image id, so layers on the same picture share its selection.
  const pics={};
  for(const [iid,im] of Object.entries(d.images||{})){
    try{
      const blob=await (await fetch(`/api/projects/${pid}/sources/${iid}`)).blob();
      pics[iid]={id:im.id,kind:im.kind,w:im.width,h:im.height,name:im.name||'picture',warnings:im.warnings||[],strokes:im.strokes,fills:im.fills,sel:im.selection||null,url:URL.createObjectURL(blob)};
    }catch(_){}
  }
  const okEffect=e=>effects.some(x=>x.name===e)?e:'waves';
  if(Array.isArray(st.layers)&&st.layers.length){
    LAYERS.list=st.layers.map(l=>newLayer({id:l.id||newLayerId(),name:l.name||'',visible:l.visible!==false,effect:okEffect(l.effect),
      params:(l.params&&typeof l.params==='object')?l.params:{},prep:deepMerge(DEF.prep,l.prep),image:l.image_id===VIDEO_ID?videoImage():l.image_id?pics[l.image_id]||null:null,
      place:{...FULL_PLACE,...(l.place||{})},pen:l.pen||1}));
    LAYERS.active=Math.min(Math.max(0,st.active|0),LAYERS.list.length-1);
  }else{ // a project from before layers: one layer
    LAYERS.list=[newLayer({effect:okEffect(st.effect),params:(st.params&&typeof st.params==='object')?st.params:{},
      prep:deepMerge(DEF.prep,st.prep),image:d.image?pics[d.image.id]||null:null})];
    LAYERS.active=0;
  }
  $('projName').value=d.name;document.title=`${d.name} · BambuPlot`;
  document.querySelector('.app').hidden=false;$('dashboard').hidden=true;
  buildEffects();render();renderThumb();renderLayers();renderFlip();refreshChecks();schedulePlan(0);loadAI();loadSignature();
  if(job.image?.sel?.has_mask)applySel(job.image.sel);
  selectTab(job.effect==='alignment'?'tab-setup':job.image||LAYERS.list.length>1?'tab-effect':'tab-picture');
  $('alignGroup').open=job.effect==='alignment';for(const id of ['alMeasL','alMeasF','alW','alH'])$(id).value='';$('alResult').hidden=true;
  currentProject=pid;dirty=false;setDocState(`Saved · ${docInfo()}`);
  if(d.warning)showProblems(d.warning);
  FLIP.bg=null;if(FLIP.video)pollVideoBg();
}
function route(){
  const m=location.hash.match(/^#\/p\/([a-f0-9]{12})$/);
  if(m)openProject(m[1]);else showDashboard();
}
window.addEventListener('hashchange',route);
$('toProjects').onclick=()=>{location.hash='#/'};
$('projNew').onclick=$('projNewEmpty').onclick=()=>openWizard();
$('projAlign').onclick=async()=>{
  let layout=null;const last=DASH.list[0];
  if(last){try{layout=(await (await fetch(`/api/projects/${last.id}`)).json()).state?.layout||null}catch(_){}}
  const r=await post('/api/projects',{name:'Alignment test',state:{layout:layout||DEF.layout,effect:'alignment',params:{},prep:DEF.prep}});
  const p=await r.json();location.hash=`#/p/${p.id}`;
};
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
  if(c.dataset.new){openWizard();return}
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
  if(e.key==='Enter'){c.dataset.new?openWizard():location.hash=`#/p/${c.dataset.id}`}
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
    DEF=defs;effects=effs;L=deepMerge(defs.layout,null);LAYERS.list=[newLayer()];LAYERS.active=0;
    if(defs.paper_sizes){
      PAPERS=Object.fromEntries(defs.paper_sizes.map(p=>[p.key,[p.w,p.h]]));
    }
    applyUnits();
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
