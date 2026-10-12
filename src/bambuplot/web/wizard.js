/* New Project setup: printer & pen, paper, project. Uses helpers and state from app.js. */
const NW={step:1,machine:null,orient:'portrait',hold:'clips',tape:{left:true,front:false,right:true,back:false},start:'picture',file:null};
const nwLen=id=>fromU(parseFloat($(id).value));
const nwSet=(id,mm)=>{$(id).value=+toU(mm).toFixed(UNITS[unit].inDec)};

async function openWizard(){
  try{NW.machine=await (await fetch('/api/machine')).json()}catch(_){NW.machine=DEF.machine}
  const m=NW.machine||{};
  $('nwPrinter').innerHTML=(DEF.printers||[]).map(p=>`<option value="${p.key}">${esc(p.label)} · ${fmtSize(p.w,p.h)}</option>`).join('')+
    '<option value="custom">Other printer (custom plate)…</option>';
  $('nwPrinter').value=m.bed?.printer||'a1mini';nwSet('nwBedW',m.bed?.w||180);nwSet('nwBedH',m.bed?.h||180);
  const pw=String(m.pen?.width_mm??0.4);
  $('nwPenW').value=[...$('nwPenW').options].some(o=>o.value===pw)?pw:'0.4';
  $('nwHolder').value=(m.z?.press??0.15)>0.5?'spring':'rigid';
  const ox=m.pen?.offset_x||0,oy=m.pen?.offset_y||0;
  nwSet('nwOffY',Math.abs(oy));$('nwOffYDir').value=oy<0?'front':'back';
  nwSet('nwOffX',Math.abs(ox));$('nwOffXDir').value=ox<0?'left':'right';
  $('nwPaper').innerHTML=(DEF.paper_sizes||[]).map(p=>`<option value="${esc(p.key)}">${esc(p.key)} · ${fmtSize(p.w,p.h)}</option>`).join('')+
    '<option value="custom">Custom size…</option>';
  $('nwPaper').value='A5';nwSet('nwPW',148);nwSet('nwPH',210);nwSet('nwMargin',5);nwSet('nwTapeW',18);nwSet('nwTapeOn',8);
  $('nwPlace').value=oy<-5?'front':oy>5?'back':'center';
  Object.assign(NW,{orient:'portrait',hold:'clips',start:'picture',file:null});
  $('nwFileName').textContent='';$('nwName').value='';$('nwName').placeholder='Untitled';
  document.querySelectorAll('.nwu').forEach(e=>e.textContent=unit);
  for(const el of document.querySelectorAll('#newSheet input[type=number]'))el.step=['nwBedW','nwBedH'].includes(el.id)?1:UNITS[unit].step;
  document.querySelectorAll('#newSheet .field>span').forEach(sp=>{if(sp.parentElement.querySelector('input[type=number]'))sp.dataset.unit=unit});
  nwStep(1);syncWizard();$('newSheet').showModal();
}

function nwBed(){
  const k=$('nwPrinter').value,p=(DEF.printers||[]).find(x=>x.key===k);
  return p?{printer:k,w:p.w,h:p.h}:{printer:'custom',w:nwLen('nwBedW')||180,h:nwLen('nwBedH')||180};
}
/* Pen tip relative to the nozzle, in the layout's convention: +X right, +Y toward the back. */
function nwOffset(){
  const y=(nwLen('nwOffY')||0)*($('nwOffYDir').value==='front'?-1:1);
  const x=(nwLen('nwOffX')||0)*($('nwOffXDir').value==='left'?-1:1);
  return [x,y];
}
function nwPaperDims(){
  const k=$('nwPaper').value;
  if(k==='custom')return [nwLen('nwPW')||148,nwLen('nwPH')||210];
  let [w,h]=PAPERS[k]||[148,210];if(NW.orient==='landscape')[w,h]=[h,w];return [w,h];
}
function nwPaperXY(){
  const b=nwBed(),[pw,ph]=nwPaperDims(),place=$('nwPlace').value,edge=5;
  const x=(b.w-pw)/2;let y=(b.h-ph)/2;
  if(ph<=b.h-2*edge){if(place==='front')y=edge;else if(place==='back')y=b.h-ph-edge}
  return [x,y];
}
/* Two clips on the front edge and two on the back, where the sheet (or the plate) ends. */
function nwClips(px,py,pw,ph,b){
  if(NW.hold!=='clips'&&NW.hold!=='both')return [];
  const cw=DEF.layout.clip_w,cd=DEF.layout.clip_d,front=Math.max(py,0),back=Math.min(py+ph,b.h);
  const xs=[px+pw*0.2-cw/2,px+pw*0.8-cw/2].map(x=>Math.min(Math.max(x,0),b.w-cw));
  return [...xs.map(x=>({x:Math.round(x),y:Math.round(front-6)})),...xs.map(x=>({x:Math.round(x),y:Math.round(back-cd+6)}))];
}
function nwLayout(){
  const b=nwBed(),[pw,ph]=nwPaperDims(),[px,py]=nwPaperXY(),[ox,oy]=nwOffset(),m=NW.machine||{};
  const lay=deepMerge(DEF.layout,null);
  lay.bed=b;
  lay.pen={...lay.pen,...m.pen,offset_x:Math.round(ox*100)/100,offset_y:Math.round(oy*100)/100,width_mm:parseFloat($('nwPenW').value)};
  lay.z={...lay.z,...m.z};
  const spring=$('nwHolder').value==='spring',wasSpring=(m.z?.press??0.15)>0.5;
  if(spring!==wasSpring)lay.z.press=spring?1.5:0.15; // a sensible starting press for the holder type
  lay.feed={...lay.feed,...m.feed};
  const custom=$('nwPaper').value==='custom';
  lay.paper={...lay.paper,size:custom?'custom':$('nwPaper').value,orientation:custom?(pw>ph?'landscape':'portrait'):NW.orient,
    width:pw,height:ph,x:Math.round(px*10)/10,y:Math.round(py*10)/10,rotation:0,margin:nwLen('nwMargin')||0};
  lay.clips=nwClips(px,py,pw,ph,b);
  const taped=NW.hold==='tape'||NW.hold==='both';
  lay.tape={...lay.tape,left:taped&&NW.tape.left,front:taped&&NW.tape.front,right:taped&&NW.tape.right,back:taped&&NW.tape.back,
    width:nwLen('nwTapeW')||18,on_paper:nwLen('nwTapeOn')||8};
  lay.area={x:px+5,y:py+5,w:Math.max(10,pw-10),h:Math.max(10,ph-10),rotation:0};
  return lay;
}

/* Top view: nozzle in the middle, pen dot where the offset puts it. */
function drawOffDiag(){
  const [x,y]=nwOffset(),s=Math.min(1.6,30/Math.max(Math.abs(x),Math.abs(y),1)),cx=60,cy=55,px=cx+x*s,py=cy-y*s;
  $('nwOffDiag').innerHTML=`<text x="60" y="12" text-anchor="middle" font-size="8" fill="currentColor" opacity=".55">BACK</text>
    <text x="60" y="105" text-anchor="middle" font-size="8" fill="currentColor" opacity=".55">FRONT</text>
    <circle cx="${cx}" cy="${cy}" r="6" fill="#8a8a8e"/><text x="${cx}" y="${cy-9}" text-anchor="middle" font-size="7" fill="currentColor" opacity=".7">nozzle</text>
    <line x1="${cx}" y1="${cy}" x2="${px}" y2="${py}" stroke="#007aff" stroke-width="1" stroke-dasharray="2 2"/>
    <circle cx="${px}" cy="${py}" r="4" fill="#1c2c8a"/><text x="${px+(px>=cx?7:-7)}" y="${py+2.5}" text-anchor="${px>=cx?'start':'end'}" font-size="7" fill="currentColor" opacity=".7">pen</text>`;
}
function drawWizPreview(){
  const lay=nwLayout(),b=lay.bed,p=lay.paper,pad=8,vw=b.w+2*pad,vh=b.h+2*pad,Yp=y=>vh-pad-y,Xp=x=>x+pad;
  const r=(x,y,w,h,st)=>`<rect x="${Xp(x)}" y="${Yp(y+h)}" width="${w}" height="${h}" style="${st}"/>`;
  let h=r(0,0,b.w,b.h,'fill:#2c2d29')+r(p.x,p.y,p.width,p.height,'fill:#fdfdf9;stroke:#bbb;stroke-width:.5');
  const t=lay.tape,out=t.width-t.on_paper,tape='fill:#e7d9a6;opacity:.85';
  if(t.left)h+=r(p.x-out,p.y-4,t.width,p.height+8,tape);
  if(t.right)h+=r(p.x+p.width-t.on_paper,p.y-4,t.width,p.height+8,tape);
  if(t.front)h+=r(p.x-4,p.y-out,p.width+8,t.width,tape);
  if(t.back)h+=r(p.x-4,p.y+p.height-t.on_paper,p.width+8,t.width,tape);
  for(const c of lay.clips)h+=r(c.x,c.y,lay.clip_w,lay.clip_d,'fill:#5a636b');
  const [ox,oy]=[lay.pen.offset_x,lay.pen.offset_y];
  if(ox||oy)h+=r(ox,oy,b.w,b.h,'fill:none;stroke:#ff9500;stroke-width:.8;stroke-dasharray:3 2');
  h+=r(p.x+p.margin,p.y+p.margin,Math.max(0,p.width-2*p.margin),Math.max(0,p.height-2*p.margin),'fill:none;stroke:#007aff;stroke-width:.8;stroke-dasharray:2 1.5');
  $('nwPreview').setAttribute('viewBox',`0 0 ${vw} ${vh}`);$('nwPreview').innerHTML=h;
  const over=Math.max(0,-p.y,p.y+p.height-b.h,-p.x,p.x+p.width-b.w);
  $('nwPreviewCap').innerHTML=`${esc(p.size==='custom'?'Custom':p.size)} ${fmtSize(p.width,p.height)} on the ${fmtSize(b.w,b.h)} plate.`+
    (over>0.5?` It hangs over the plate by up to ${fmtLen(over)}; that part can't be drawn on.`:'')+
    (ox||oy?' The orange box is where the pen can reach.':'');
}
function renderSummary(){
  const lay=nwLayout(),b=lay.bed,pl=(DEF.printers||[]).find(p=>p.key===b.printer),ox=lay.pen.offset_x,oy=lay.pen.offset_y;
  const side=(v,pos,neg)=>v?`${fmtLen(Math.abs(v))} ${v>0?pos:neg}`:'';
  const parts=[side(oy,'behind','in front of'),side(ox,'to the right of','to the left of')].filter(Boolean);
  const off=parts.length?`Tip ${parts.join(' and ')} the nozzle`:'Tip directly under the nozzle';
  const taped=NW.hold==='tape'||NW.hold==='both';
  const hold={clips:'Clips',tape:'Tape',both:'Clips and tape',none:'Nothing'}[NW.hold]+
    (taped?` (${['left','front','right','back'].filter(e=>NW.tape[e]).join(', ')||'no edges'})`:'');
  const rows=[
    ['Printer',`${pl?pl.label:'Custom printer'} · ${fmtSize(b.w,b.h)} plate`],
    ['Pen',`${lay.pen.width_mm} mm tip, ${$('nwHolder').value==='spring'?'spring-loaded':'rigid'} holder`],
    ['Pen position',off],
    ['Paper',`${lay.paper.size==='custom'?'Custom':lay.paper.size} ${fmtSize(lay.paper.width,lay.paper.height)}, ${$('nwPlace').selectedOptions[0].textContent.toLowerCase()}`],
    ['Held down with',hold],
    ['Drawing area','Fitted to the paper automatically'],
  ];
  $('nwSummary').innerHTML=rows.map(([k,v])=>`<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
}
function syncWizard(){
  $('nwBedCustom').hidden=$('nwPrinter').value!=='custom';
  $('nwPaperCustom').hidden=$('nwPaper').value!=='custom';
  document.querySelectorAll('#nwOrient button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.o===NW.orient));
  document.querySelectorAll('#nwHold button').forEach(b=>b.setAttribute('aria-pressed',b.dataset.h===NW.hold));
  document.querySelectorAll('#nwTapeEdges button').forEach(b=>b.setAttribute('aria-pressed',!!NW.tape[b.dataset.edge]));
  $('nwTapeBox').hidden=!(NW.hold==='tape'||NW.hold==='both');
  document.querySelectorAll('#nwStart button').forEach(b=>b.setAttribute('aria-checked',b.dataset.start===NW.start));
  const [,oy]=nwOffset();
  $('nwPaperNote').textContent=oy<-5&&$('nwPlace').value!=='front'
    ?`Your pen sits ${fmtLen(-oy)} in front of the nozzle, so it can't reach the back ${fmtLen(-oy)} of the plate. "Toward the front" leaves the most room.`:'';
  drawOffDiag();drawWizPreview();renderSummary();
}
function nwStep(n){
  NW.step=n;
  document.querySelectorAll('#newSheet .wiz-step').forEach(s=>s.hidden=+s.dataset.step!==n);
  document.querySelectorAll('.wiz-steps li').forEach(li=>{
    const k=+li.dataset.s;if(k===n)li.setAttribute('aria-current','step');else li.removeAttribute('aria-current');li.classList.toggle('done',k<n);
  });
  $('nwBack').hidden=n===1;$('nwNext').textContent=n===3?'Create Project':'Continue';msg('nwMsg','');
}
function nwValid(){
  if(NW.step===1){
    const b=nwBed();if(!(b.w>=50&&b.h>=50))return 'Enter the plate size.';
    if(!isFinite(nwLen('nwOffY'))||!isFinite(nwLen('nwOffX')))return 'Enter how far the pen tip is from the nozzle (0 if it lines up).';
  }
  if(NW.step===2){
    const [pw,ph]=nwPaperDims(),b=nwBed();
    if(!(pw>=10&&ph>=10))return 'Enter the paper size.';
    if(pw>b.w+120||ph>b.h+120)return 'That paper is far bigger than the plate.';
  }
  if(NW.step===3&&NW.start==='picture'&&!NW.file)return 'Choose a picture, or start with the sample.';
  return '';
}
async function nwCreate(){
  const btn=$('nwNext');btn.disabled=true;msg('nwMsg','Setting up…');
  try{
    const lay=nwLayout();
    await fetch('/api/machine',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({bed:lay.bed,pen:lay.pen,z:lay.z,feed:lay.feed})});
    const fit=await post('/api/fit/fit',lay);
    if(fit.ok)lay.area={...lay.area,...await fit.json()};
    const isSvg=NW.file&&/svg/i.test(NW.file.type+NW.file.name);
    const name=$('nwName').value.trim()||(NW.start==='align'?'Alignment test':NW.file&&NW.start==='picture'?NW.file.name.replace(/\.[^.]+$/,''):'Untitled');
    const effect=NW.start==='align'?'alignment':NW.start==='picture'&&isSvg?'svg':'waves';
    const r=await post('/api/projects',{name,state:{layout:lay,effect,params:{},prep:DEF.prep,extras:DEF.extras}});
    if(!r.ok)throw new Error();
    const p=await r.json(),file=NW.start==='picture'?NW.file:null;
    DEF.machine={bed:lay.bed,pen:lay.pen,z:lay.z,feed:lay.feed};
    $('newSheet').close();location.hash=`#/p/${p.id}`;
    if(file){ // load the picture once the new project is open
      const wait=setInterval(()=>{if(currentProject===p.id){clearInterval(wait);openFile(file)}},100);
      setTimeout(()=>clearInterval(wait),10000);
    }
  }catch(_){msg('nwMsg','Could not create the project. Is the BambuPlot server running?','err')}
  finally{btn.disabled=false}
}

$('nwNext').onclick=()=>{const e=nwValid();if(e){msg('nwMsg',e,'err');return}NW.step<3?nwStep(NW.step+1):nwCreate()};
$('nwBack').onclick=()=>nwStep(Math.max(1,NW.step-1));
$('nwCancel').onclick=()=>$('newSheet').close();
for(const el of document.querySelectorAll('#newSheet input,#newSheet select'))if(el.type!=='file')el.addEventListener('input',syncWizard);
document.querySelectorAll('#nwOrient button').forEach(b=>b.onclick=()=>{NW.orient=b.dataset.o;syncWizard()});
document.querySelectorAll('#nwHold button').forEach(b=>b.onclick=()=>{NW.hold=b.dataset.h;syncWizard()});
document.querySelectorAll('#nwTapeEdges button').forEach(b=>b.onclick=()=>{NW.tape[b.dataset.edge]=!NW.tape[b.dataset.edge];syncWizard()});
document.querySelectorAll('#nwStart button').forEach(b=>b.onclick=()=>{NW.start=b.dataset.start;syncWizard();if(NW.start==='picture'&&!NW.file)$('nwFile').click()});
$('nwFile').onchange=e=>{
  NW.file=e.target.files[0]||null;$('nwFileName').textContent=NW.file?`Picture: ${NW.file.name}`:'';
  if(NW.file)$('nwName').placeholder=NW.file.name.replace(/\.[^.]+$/,'');syncWizard();
};
