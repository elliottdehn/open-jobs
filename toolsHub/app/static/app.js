const JOBS=__JOBS__, IDEAL=__IDEAL__, IDEAL_TEXT=__IDEAL_TEXT__, GROUPS=__GROUPS__, GROUPS3=__GROUPS3__, PREF=__PREF__;
const $=s=>document.querySelector(s);
function b64f32(s){const b=atob(s),u=new Uint8Array(b.length);for(let i=0;i<b.length;i++)u[i]=b.charCodeAt(i);return new Float32Array(u.buffer)}
function norm(v){let s=0;for(const x of v)s+=x*x;s=Math.sqrt(s)||1;return Float32Array.from(v,x=>x/s)}
function dot(a,b){let s=0;for(let i=0;i<a.length;i++)s+=a[i]*b[i];return s}
function esc(s){return String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
const _ENT={amp:'&',nbsp:' ',lt:'<',gt:'>',quot:'"',apos:"'",rsquo:'’',lsquo:'‘',rdquo:'”',ldquo:'“',ndash:'–',mdash:'—',hellip:'…',bull:'•',middot:'·',trade:'™',reg:'®',copy:'©',eacute:'é',egrave:'è',uuml:'ü',ouml:'ö',auml:'ä',ccedil:'ç',ntilde:'ñ'};
function jdText(t){return String(t||'').replace(/&(#x?[0-9a-f]+|[a-z]+);/gi,(m,e)=>{if(e[0]==='#'){const n=e[1]==='x'||e[1]==='X'?parseInt(e.slice(2),16):parseInt(e.slice(1),10);return isNaN(n)?m:String.fromCodePoint(n)}return _ENT[e.toLowerCase()]??m}).replace(/[ \t]+\n/g,'\n')}
const ideal=norm(IDEAL.vector);
const TITLE_TERMS=(IDEAL.title||'').toLowerCase().split(/[^a-z0-9+#]+/).filter(t=>t.length>2&&!['and','the','engineer','senior','software'].includes(t));
function titleFit(j){if(!TITLE_TERMS.length)return 0;const t=(j.t||'').toLowerCase();let n=0;for(const w of TITLE_TERMS)if(t.includes(w))n++;return 0.03*n}
for(const j of JOBS){j.vec=b64f32(j.v);delete j.v;j.pre=j.sim+titleFit(j);j.score=j.pre}
// local centering: this slice is a narrow cone around the ideal JD (every posting shares a big common component),
// so cosines are compressed into a thin high band. Subtracting the slice mean and renormalizing spreads the
// differences the taste model and neighbours actually care about (benchmarked: +0.5-1 AUC point, never worse).
const MEAN=new Float32Array(JOBS[0].vec.length);for(const j of JOBS)for(let i=0;i<MEAN.length;i++)MEAN[i]+=j.vec[i]/JOBS.length;
function centered(v){const o=new Float32Array(v.length);let n=0;for(let i=0;i<v.length;i++){o[i]=v[i]-MEAN[i];n+=o[i]*o[i]}n=Math.sqrt(n)||1;for(let i=0;i<o.length;i++)o[i]/=n;return o}
for(const j of JOBS)j.cvec=centered(j.vec);
const cideal=centered(ideal);
let prior=Float32Array.from(cideal);
// ---- taste worker: fitting + rescoring off the UI thread (the kNN rescore is ~1B ops at 10k+ jobs) ----
let TW=null,twSeq=0,twBusy=false,twQueued=false;
try{
  TW=new Worker('/static/worker.js');
  const cv=new Float32Array(JOBS.length*1536);JOBS.forEach((j,i)=>cv.set(j.cvec,i*1536));
  TW.postMessage({type:'init',cv:cv.buffer,n:JOBS.length,dims:1536,cideal:Array.from(cideal)},[cv.buffer]);
  TW.onmessage=e=>{const m=e.data;twBusy=false;
    if(m.seq!==twSeq){if(twQueued){twQueued=false;refit()}return}
    const scores=new Float32Array(m.scores);JOBS.forEach((j,i)=>j.score=scores[i]);
    u=new Float32Array(m.u);w=u;b=m.b;committee=m.committee.map(x=>new Float32Array(x));KNN_L=m.KNN_L;looAcc=m.looAcc;
    render(false,'refit');
    if(twQueued){twQueued=false;refit()}};
  TW.onerror=()=>{TW=null;twBusy=false;twQueued=false;window.onHostedTasteIdle?.()};
}catch(e){TW=null}
function updatePrior(){const P=JOBS.filter(j=>st.labels[j.k]===1),N=JOBS.filter(j=>st.labels[j.k]===0);if(!P.length&&!N.length){prior=Float32Array.from(cideal);return}
  const v=Float32Array.from(cideal);for(const j of P)for(let i=0;i<v.length;i++)v[i]+=0.6*j.cvec[i]/P.length;for(const j of N)for(let i=0;i<v.length;i++)v[i]-=0.4*j.cvec[i]/N.length;prior=norm(v)}
// ---- state
const INIT_LABELS=__INIT_LABELS__;
const LS='open-jobs:'+(IDEAL.title||'');  // stable per search; the slice grows (unions, expansion) without losing state
let st={labels:{},notes:{},opened:{},enrich:{},companies:{},hideCo:{}};try{st=Object.assign(st,JSON.parse(localStorage.getItem(LS)||'{}'))}catch{}
try{ // migrate state from the old 'title:jobcount' keys (the count changed on every recompile)
  for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);
    if(k&&k.startsWith(LS+':')&&/:[0-9]+$/.test(k)){const o=JSON.parse(localStorage.getItem(k)||'{}');
      for(const f of ['labels','notes','opened','enrich','companies','hideCo','kw'])if(o[f])st[f]=Object.assign({},o[f],st[f]||{});
      if(o.compares)st.compares=(st.compares||[]).concat(o.compares);if(o.exclGroups)st.exclGroups=[...new Set([...(st.exclGroups||[]),...o.exclGroups])]}}
}catch{}
try{for(const[k,v] of Object.entries(INIT_LABELS||{}))if(st.labels[k]===undefined&&(v===0||v===1))st.labels[k]=v}catch{} // labels serve recorded: survive any storage loss
save();
if(new URLSearchParams(location.search).get('reset')==='1'){st.labels={};st.notes={};st.opened={};st.compares=[];st.exclGroups=[];st.kw={t:{},b:{}};st.hideCo={};try{localStorage.setItem(LS,JSON.stringify(st))}catch{};history.replaceState(null,'',location.pathname)}
function save(){try{localStorage.setItem(LS,JSON.stringify(st))}catch{}}
function event(e){e.ts=Date.now();try{fetch('/event',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(e)}).catch(()=>{})}catch{}}
// ---- one taste model. Evidence: pairwise comparisons (Bradley-Terry: P(a>b)=σ(u·(va−vb))) and yes/no labels
// (pointwise: P(yes)=σ(u·v+b)), fit jointly from the Rocchio prior (ideal shifted by the yes/no means) with L2 back
// toward that prior. On top, a local kNN term (max cosine to any yes minus max cosine to any no) captures "more like
// *that* one" and multi-cluster taste, which a single direction can't. Its weight λ is picked by leave-one-out on labels.
st.compares=st.compares||[];
const byKey=Object.fromEntries(JOBS.map(j=>[j.k,j]));
let u=Float32Array.from(cideal),b=0,w=u,taste=false,KNN_L=1.0,looAcc=null;
const K_COMMITTEE=5;let committee=[];
function pairsFrom(list){return list.map(c=>[byKey[c.a],byKey[c.b],c.win]).filter(c=>c[0]&&c[1])}
function labelItems(exclude){const out=[];for(const j of JOBS){const y=st.labels[j.k];if((y===1||y===0)&&j.k!==exclude)out.push([j,y])}return out}
function fitOn(C,L,iters=60){const w=Float32Array.from(prior);let bb=0;if(!C.length&&!L.length)return [w,0];const lr=.5,l2=.02,n=Math.max(1,C.length+L.length);
  // centre the pointwise term at the prior's own score so b starts near 0
  let c0=0;if(L.length){for(const [j] of L)c0+=dot(prior,j.cvec);c0/=L.length}
  for(let ep=0;ep<iters;ep++){
    for(const [a,bj,win] of C){let s=0;for(let i=0;i<w.length;i++)s+=w[i]*(a.cvec[i]-bj.cvec[i]);const y=win==='a'?1:0,p=1/(1+Math.exp(-s)),g=p-y;for(let i=0;i<w.length;i++)w[i]-=lr*(g*(a.cvec[i]-bj.cvec[i])+l2*(w[i]-prior[i])/n)}
    for(const [j,y] of L){const s=dot(w,j.cvec)-c0+bb,p=1/(1+Math.exp(-s)),g=p-y;for(let i=0;i<w.length;i++)w[i]-=lr*(g*j.cvec[i]+l2*(w[i]-prior[i])/n);bb-=lr*g}}
  return [w,bb-c0]}
function knnTerm(j,items){let best=-1,worst=-1;for(const [x,y] of items){if(x===j)continue;const c=dot(j.cvec,x.cvec);if(y===1){if(c>best)best=c}else if(c>worst)worst=c}
  return (best<0?0:best)-(worst<0?0:worst)}
function chooseLambda(){  // leave-one-out over labels: which λ predicts held-out labels best (ties → smaller λ)
  const L=labelItems();const ys=L.filter(x=>x[1]===1).length,ns=L.length-ys;if(ys<2||ns<2||L.length<6){KNN_L=1.0;looAcc=null;return}
  const C=pairsFrom(st.compares);const cands=[0,0.5,1,2,3];const hits=cands.map(()=>0);
  for(const [j,y] of L){const rest=labelItems(j.k);const [wj,bj]=fitOn(C,rest,40);const lin=dot(wj,j.cvec)+bj;const kn=knnTerm(j,rest);
    cands.forEach((lam,i)=>{if(((lin+lam*kn)>0)===(y===1))hits[i]++})}
  let bi=0;for(let i=1;i<cands.length;i++)if(hits[i]>hits[bi])bi=i;KNN_L=cands[bi];looAcc=hits[bi]/L.length}
function fitAll(){updatePrior();const C=pairsFrom(st.compares),L=labelItems();[u,b]=fitOn(C,L);w=u;
  committee=[];for(let k=0;k<K_COMMITTEE;k++){const R=[];for(let i=0;i<C.length;i++)R.push(C[Math.floor(Math.random()*C.length)]);const RL=[];for(let i=0;i<L.length;i++)RL.push(L[Math.floor(Math.random()*L.length)]);committee.push(fitOn(R,RL,40)[0])}
  taste=C.length>0||L.length>0;chooseLambda();rescore()}
function rescore(){const L=labelItems();if(!taste){for(const j of JOBS)j.score=j.pre;return}
  for(const j of JOBS){const lin=dot(u,j.cvec)+b;const kn=L.length?knnTerm(j,L):0;j.score=1/(1+Math.exp(-(lin+KNN_L*kn)))}}
function refit(){
  const C0=pairsFrom(st.compares),L0=labelItems();
  taste=C0.length>0||L0.length>0;
  if(!taste){for(const j of JOBS)j.score=j.pre;return}
  if(!TW){fitAll();return}
  if(twBusy){twQueued=true;return}
  twBusy=true;twSeq++;
  const ki={};JOBS.forEach((j,i)=>ki[j.k]=i);
  TW.postMessage({seq:twSeq,K:K_COMMITTEE,
    L:L0.map(([j,y])=>[ki[j.k],y]),
    C:C0.map(([a,bj,win])=>[ki[a.k],ki[bj.k],win==='a'?1:0])})}
function fitPairs(){fitAll()}
function predictsRecent(k=10){if(st.compares.length<k+3)return 0;const [wv]=fitOn(pairsFrom(st.compares.slice(0,-k)),labelItems());let ok=0,n=0;
  for(const c of st.compares.slice(-k)){const a=byKey[c.a],bb=byKey[c.b];if(!a||!bb)continue;n++;if((dot(wv,a.cvec)-dot(wv,bb.cvec)>0)===(c.win==='a'))ok++}return n?ok/n:0}
function topSetStability(N=100){if(committee.length<2)return 0;const tops=committee.map(w=>new Set(JOBS.map(j=>[dot(w,j.cvec),j.k]).sort((x,y)=>y[0]-x[0]).slice(0,N).map(x=>x[1])));let s=0,n=0;
  for(let i=0;i<tops.length;i++)for(let j=i+1;j<tops.length;j++){let inter=0;for(const k of tops[i])if(tops[j].has(k))inter++;s+=inter/(2*N-inter);n++}return s/n}
function tasteScore(j){return dot(u,j.cvec)}
let cmpPair=null,cmpAcc=0,cmpStab=0;
const PAIR_MODE='committee';
function nextPair(){const pool=JOBS.filter(j=>st.labels[j.k]!==0&&passes(j)).sort((x,y)=>(taste||st.compares.length?tasteScore(y)-tasteScore(x):y.sim-x.sim)).slice(0,300);
  if(pool.length<2)return null;const seen=new Set(st.compares.map(c=>c.a+'|'+c.b));let best=null,bestS=PAIR_MODE==='v1'?Infinity:-Infinity;
  for(let t=0;t<400;t++){const a=pool[Math.floor(Math.random()*pool.length)],b=pool[Math.floor(Math.random()*pool.length)];
    if(a===b||(a.c===b.c&&a.t===b.t)||seen.has(a.k+'|'+b.k)||seen.has(b.k+'|'+a.k))continue;const cos=dot(a.cvec,b.cvec);
    if(PAIR_MODE==='v1'){const sc=Math.abs(dot(u,a.cvec)-dot(u,b.cvec))+0.15*cos;if(sc<bestS){bestS=sc;best=[a,b]}}
    else{if(cos>0.95)continue;let mean=0,m2=0;const ds=[];for(const w of committee){const d=dot(w,a.cvec)-dot(w,b.cvec);ds.push(d);mean+=d}mean/=committee.length;for(const d of ds)m2+=(d-mean)*(d-mean);m2/=committee.length;
      const sc=(Math.sqrt(m2)+0.05)*Math.sqrt(Math.max(0,2-2*cos))*(0.5+Math.exp(-Math.abs(mean)*8));if(sc>bestS){bestS=sc;best=[a,b]}}}
  return best}
const MIN=12,MAX=25;
function showPair(){cmpPair=nextPair();if(!cmpPair){stopCmp();return}
  const n=st.compares.length;$('#cmpn').textContent=`${n+1} / ${MAX}`;cmpAcc=predictsRecent();cmpStab=topSetStability();
  const conf=Math.min(1,Math.max(n/MAX,n>=MIN?Math.max(cmpStab/0.75,cmpAcc/0.8):n/MIN*0.6));$('#cmpbar').style.width=`${conf*100}%`;
  let txt;if(n<MIN)txt=`Getting a feel for your taste · ${MIN-n} more before it can stop`;else{txt=`Your ranking is ${Math.round(cmpStab*100)}% settled`;if(n>=13)txt+=` · guessed ${Math.round(cmpAcc*10)} of your last 10`;txt+=` · stops by ${MAX}`}
  $('#cmpc').textContent=txt;
  const [A,B]=cmpPair;const dm=dot(u,A.cvec)-dot(u,B.cvec);const pA=1/(1+Math.exp(-dm*4));
  const votesA=committee.filter(w=>dot(w,A.cvec)-dot(w,B.cvec)>0).length,agree=Math.max(votesA,committee.length-votesA)/(committee.length||1);
  const guess=st.compares.length<3?null:(pA>=0.5?'a':'b');const gp=Math.round(100*Math.max(pA,1-pA));
  for(const [el,j,side] of [[$('#optA'),A,'a'],[$('#optB'),B,'b']])
    el.innerHTML=`<div class="guess">${guess===side?`<b>▲ its guess</b> · ${gp}% · ${Math.round(agree*100)}% of the committee agree`:(guess?'&nbsp;':'no guess yet')}</div><div class="t">${esc(j.t)}</div><div class="m">${esc(j.c)}${j.l?' · '+esc(j.l):''}</div><div class="badges">${badges(j,true)}</div>${j.e&&j.e.summary?'<div class="sum">'+esc(j.e.summary)+'</div>':''}<div class="jdx">${esc(jdText(j.jd).slice(0,1400))}</div>`}
function pick(win){if(!cmpPair)return;const [a,b]=cmpPair;st.compares.push({a:a.k,b:b.k,win});save();
  const guessed=st.compares.length>=4?((dot(u,a.cvec)-dot(u,b.cvec)>=0)?'a':'b'):null;
  event({type:'compare',a:a.k,b:b.k,win,aTitle:a.t,aCompany:a.c,bTitle:b.t,bCompany:b.c,n:st.compares.length,guess:guessed,surprised:guessed!==null&&guessed!==win});
  fitPairs();const n=st.compares.length;if(n>=25||(n>=12&&(topSetStability()>=0.75||predictsRecent()>=0.8))){stopCmp();return}showPair()}
// ---- Sort flow UI
function step(n){for(const s of document.querySelectorAll('#steps span')){const k=+s.dataset.s;s.className=k===n?'on':k<n?'done':''}
  for(const [id,on] of [['#intro',n===0],['#scope',n===1],['#what',n===2],['#cmpmain',n===3]])$(id).style.display=on?'flex':'none';
  $('#steps').style.visibility=n===0?'hidden':'visible';$('#cmp-title').textContent=n===0?'Sort by my taste':n===1?'Where would you work?':n===2?'What kind of job?':'Which would you rather have?'}
for(const id of ['#intro','#scope','#what','#cmpmain'])$(id).style.flexDirection='column';
function renderScope(){for(const [f,el,max] of [['rm',$('#sc-rm'),6],['co',$('#sc-co'),12],['rg',$('#sc-rg'),14]]){el.innerHTML='';const counts={};for(const j of JOBS)for(const k of keysOf[f](j))counts[k]=(counts[k]||0)+1;
    Object.entries(counts).filter(([k])=>k!=='unknown'&&k!=='(unknown)').sort((a,b)=>b[1]-a[1]).slice(0,max).forEach(([k,n])=>{const b=document.createElement('button');b.innerHTML=`${esc(k)}<span class="n">${n}</span>`;b.className='chip'+(sel[f].has(k)?' on':'');
      b.onclick=()=>{sel[f].has(k)?sel[f].delete(k):sel[f].add(k);b.className='chip'+(sel[f].has(k)?' on':'');scopeCount()};el.appendChild(b)})}scopeCount()}
function scopeCount(){$('#sc-n').textContent=`${JOBS.filter(passes).length.toLocaleString()} jobs in scope`}
function startCmp(){fitPairs();$('#cmp').classList.add('on');renderScope();step(st.sortIntroSeen?1:0);event({type:'sort_start',compares:st.compares.length})}
let spreadSeed=0;
function inScopeJobs(){return JOBS.filter(j=>{if(kwHidden(j))return false;if(st.eligOnly&&j.el===false)return false;for(const f in sel)if(sel[f].size&&!keysOf[f](j).some(k=>sel[f].has(k)))return false;return true})}
function spread(list,k){if(list.length<=k)return list.slice();const byS=[...list].sort((a,b)=>b.sim-a.sim);const out=[byS[Math.min(spreadSeed*3,byS.length-1)]];const d=new Float32Array(list.length).fill(Infinity);
  for(let r=0;r<k-1;r++){const last=out[out.length-1];let bi=-1,bd=-Infinity;for(let i=0;i<list.length;i++){const j=list[i];if(out.includes(j))continue;const dist=1-dot(j.cvec,last.cvec);if(dist<d[i])d[i]=dist;const sc=d[i]+0.12*j.sim;if(sc>bd){bd=sc;bi=i}}if(bi<0)break;out.push(list[bi])}return out}
function renderWhat(){const list=inScopeJobs().filter(j=>st.labels[j.k]===undefined);const el=$('#wh-cards');el.innerHTML='';
  for(const j of spread(list,24)){const d=document.createElement('div');d.className='gcard';
    d.innerHTML=`<div class="t">${esc(j.t)}</div><div class="m">${esc(j.c)}${j.l?' · '+esc(j.l.slice(0,40)):''}</div><div class="badges">${badges(j,true)}</div><div class="jdx">${esc((j.e&&j.e.summary)||jdText(j.jd).slice(0,360))}</div><div class="btns"><button class="btn ok">More like this</button><button class="btn no">Less</button></div>`;
    const [bm,bl]=d.querySelectorAll('button');
    bm.onclick=()=>{st.labels[j.k]=st.labels[j.k]===1?undefined:1;if(st.labels[j.k]===undefined)delete st.labels[j.k];d.className='gcard'+(st.labels[j.k]===1?' more':'');save();event({type:'seed',key:j.k,value:st.labels[j.k]??null,title:j.t,company:j.c});whatCount()};
    bl.onclick=()=>{st.labels[j.k]=st.labels[j.k]===0?undefined:0;if(st.labels[j.k]===undefined)delete st.labels[j.k];d.className='gcard'+(st.labels[j.k]===0?' less':'');save();event({type:'seed',key:j.k,value:st.labels[j.k]??null,title:j.t,company:j.c});whatCount()};
    el.appendChild(d)}whatCount()}
function whatCount(){const P=JOBS.filter(j=>st.labels[j.k]===1).length,N=JOBS.filter(j=>st.labels[j.k]===0).length;$('#wh-n').textContent=`${P} more · ${N} less · ${inScopeJobs().length.toLocaleString()} in scope`}
function toWhat(){event({type:'scope',rm:[...sel.rm],co:[...sel.co],rg:[...sel.rg],n:JOBS.filter(passes).length});step(2);renderWhat()}
function beginPairs(){event({type:'seed_done',more:JOBS.filter(j=>st.labels[j.k]===1).map(j=>j.k),less:JOBS.filter(j=>st.labels[j.k]===0).map(j=>j.k),n:JOBS.filter(passes).length});
  refit();renderFacets();render(true,'seed');step(3);showPair()}
function stopCmp(){$('#cmp').classList.remove('on');fitAll();if(taste)$('#sort').value='model';
  event({type:'sort_done',compares:st.compares.length,accuracy:predictsRecent(),stability:topSetStability()});render(true,'sort')}
function median(){const v=JOBS.map(j=>dot(u,j.cvec)).sort((a,b)=>a-b);return v[Math.floor(v.length/2)]||0}
$('#intro-go').onclick=()=>{if($('#intro-skip').checked){st.sortIntroSeen=true;save()}step(1)};
$('#sc-help').onclick=()=>step(0);$('#sc-go').onclick=toWhat;$('#sc-any').onclick=()=>{sel.rm.clear();sel.co.clear();sel.rg.clear();toWhat()};
$('#wh-back').onclick=()=>step(1);$('#wh-more').onclick=()=>{spreadSeed++;renderWhat()};$('#wh-go').onclick=beginPairs;
$('#cmpback').onclick=()=>{step(2);renderWhat()};$('#cmpstop').onclick=stopCmp;$('#cmpclose').onclick=()=>{$('#cmp').classList.remove('on')};
$('#sortbtn').onclick=startCmp;$('#optA').onclick=()=>pick('a');$('#optB').onclick=()=>pick('b');
// ---- enrichment
async function enrichTop(n=300){const btn=$('#enrichbtn');btn.disabled=true;const list=JOBS.filter(passes).sort((a,b)=>fscore(b)-fscore(a)).slice(0,n).filter(j=>!j.e);let spent=0,done=0;
  if(!list.length){toast('Everything in view is already enriched');btn.disabled=false;return}
  for(let i=0;i<list.length;i+=100){const batch=list.slice(i,i+100);btn.textContent=`⚡ Enriching ${Math.min(i+100,list.length)}/${list.length}…`;
    let r;try{r=await fetch((window.OPEN_JOBS_API||'https://backend.dehnbostele.workers.dev')+'/enrich',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({jobs:batch.map(j=>{const [ats,rest]=j.k.split(/\/(.+)/);const [slug,id]=rest.split(/#(.+)/);return{ats,slug,id}})})})}catch(e){btn.textContent='⚡ Enrich (network error)';btn.disabled=false;return}
    const res=await r.json().catch(()=>({}));
    for(const [name,b] of Object.entries(res.boards||{}))if(b.company)st.companies[name]={name:b.company.name,website:b.company.website,industry:b.company.industry,size:b.company.size_bucket,hq:(b.company.hq_location||{}).country_code,staffing:b.company.is_staffing_agency,desc:b.company.description};
    for(const [k,x] of Object.entries(res.jobs||{}))if(x.status==='done'&&x.enrichment){st.enrich[k]=x.enrichment.data;done++}
    applyEnrich();save();
    if(r.status===429){btn.textContent=`⚡ Rate limited (retry in ${res.retryAfterSeconds}s)`;event({type:'enrich',n:done,rateLimited:true});btn.disabled=false;rebuildFacets();renderFacets();render(false,'enrich');return}
    spent+=(res.cost||{}).thisCallUsd||0}
  event({type:'enrich',n:done,usd:spent});btn.innerHTML=`⚡ Enrich <span class="lbl-long">top 300</span>`;btn.disabled=false;rebuildFacets();renderFacets();render(false,'enrich');toast(`Enriched ${done} jobs for $${spent.toFixed(2)} · new facets and badges`);loadBudget()}
async function loadBudget(){try{const r=await fetch((window.OPEN_JOBS_API||'https://backend.dehnbostele.workers.dev')+'/enrich/budget');const d=await r.json();const h=d.hour||d.hourly||{},dd=d.day||d.daily||{};const left=v=>v==null?'?':'$'+Number(v).toFixed(2);
  $('#budgetline').innerHTML=`<span>Your budget left</span><span>${left(h.remainingUsd??h.remaining)} this hour · ${left(dd.remainingUsd??dd.remaining)} today</span>`}catch{}}
const PREF_REMOTE_ONLY=__PREF_REMOTE_ONLY__;  // computed by jobs.py: every OR-clause of the preference is remote-only
function applyEnrich(){for(const j of JOBS){const e=st.enrich[j.k];if(e){j.e=e;if(!j.sn&&e.seniority&&e.seniority!=='unspecified'){j.sn=({intern:'intern',entry:'junior',junior:'junior',mid:'mid',senior:'senior',staff:'staff',principal:'staff',lead:'lead',manager:'manager',senior_manager:'manager',director:'director',vp:'executive',c_level:'executive'})[e.seniority]||e.seniority;j.sne=null}if(e.work_arrangement&&e.work_arrangement!=='unspecified'){j.rm=e.work_arrangement;if(PREF_REMOTE_ONLY){if((j.rm==='onsite'||j.rm==='hybrid')&&j.el!==false){j.el=false;j.elr=j.rm+' (you asked for remote)'}else if(j.rm==='remote'&&j.elr==='not labelled remote'){j.el=true;j.elr='remote (enrichment)'}}}}const c=st.companies[j.k.split('#')[0]];if(c){j.co_=c;if(c.name&&c.name!==j.c){j.c=c.name;delete j.searchText}}}}
$('#enrichbtn').onclick=()=>enrichTop(300);$('#enrichbtn').addEventListener('mouseenter',loadBudget,{once:true});
// ---- facets
const FACETS=['el','rm','sn','rf','et','sa','fr','ag','in','cs','co','rg','ci','g','c','a'];const facets={};const sel={};for(const f of FACETS){facets[f]={};sel[f]=new Set()}
st.exclGroups=st.exclGroups||[];const exclG=new Set(st.exclGroups);st.kw=st.kw||{t:{},b:{}};
if(st.eligOnly===undefined)st.eligOnly=!!PREF;
function termRe(t){return new RegExp('(^|[^a-z0-9+#.])'+t.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?![a-z0-9+#])','i')}
const kwCache=new Map();function hasTerm(text,t){let r=kwCache.get(t);if(!r){r=termRe(t);kwCache.set(t,r)}return r.test(text)}
const boardOf=j=>j.k.split('#')[0];
function coHidden(j){return !!(st.hideCo&&st.hideCo[boardOf(j)])}
function kwHidden(j){if(coHidden(j))return true;for(const [t,v] of Object.entries(st.kw.t))if(v<0&&hasTerm(j.t,t))return true;for(const [t,v] of Object.entries(st.kw.b))if(v<0&&hasTerm(j.jd||'',t))return true;return false}
function kwBoost(j){let b=0;for(const [t,v] of Object.entries(st.kw.t))if(v>0&&hasTerm(j.t,t))b+=0.08;for(const [t,v] of Object.entries(st.kw.b))if(v>0&&hasTerm(j.jd||'',t))b+=0.04;return Math.min(b,0.25)}
const NAMES={US:'United States',GB:'United Kingdom',CA:'Canada',DE:'Germany',FR:'France',IN:'India',NL:'Netherlands',ES:'Spain',IT:'Italy',PL:'Poland',PT:'Portugal',IE:'Ireland',SE:'Sweden',CH:'Switzerland',AU:'Australia',SG:'Singapore',JP:'Japan',BR:'Brazil',MX:'Mexico',IL:'Israel',AE:'UAE',AT:'Austria',BE:'Belgium',DK:'Denmark',NO:'Norway',FI:'Finland',CZ:'Czechia',GR:'Greece',KR:'South Korea',HK:'Hong Kong',TW:'Taiwan',NZ:'New Zealand',AR:'Argentina',CO:'Colombia',CL:'Chile',ZA:'South Africa',NG:'Nigeria',KE:'Kenya',PH:'Philippines',UA:'Ukraine',TR:'Türkiye',RO:'Romania',HU:'Hungary'};
function fmtSal(e){if(!e)return'';const lo=e.salary_min,hi=e.salary_max;if(lo==null&&hi==null)return e.salary_raw?String(e.salary_raw).replace(/^.*?(?=[$€£]|\d)/,'').slice(0,40):'';const cur=e.salary_currency||'',sym=cur==='USD'?'$':cur==='EUR'?'€':cur==='GBP'?'£':cur?cur+' ':'';const f=v=>v>=1000?sym+Math.round(v/1000)+'k':sym+Math.round(v);const per=e.salary_period&&e.salary_period!=='year'?' /'+e.salary_period:'';return (lo!=null&&hi!=null&&lo!==hi?f(lo)+'–'+f(hi):f(lo!=null?lo:hi))+per}
function salaryOf(j){const e=j.e;if(e&&(e.salary_min!=null||e.salary_max!=null)){const m=e.salary_period==='hour'?2080:e.salary_period==='month'?12:e.salary_period==='week'?52:e.salary_period==='day'?260:1;return{lo:(e.salary_min||e.salary_max)*m,hi:(e.salary_max||e.salary_min)*m,cur:e.salary_currency||'',raw:e.salary_raw||'',src:'llm'}}if(j.sal)return{lo:j.sal.annual_min,hi:j.sal.annual_max,cur:j.sal.currency,raw:j.sal.raw,src:'text'};if(j.est)return{lo:j.est.lo,hi:j.est.hi,mid:j.est.mid,cur:'USD',raw:'',src:'est'};return null}
const band=v=>v>=250000?'250k+':v>=200000?'200–250k':v>=150000?'150–200k':v>=100000?'100–150k':v>=60000?'60–100k':'<60k';
const salaryBucket=j=>{const s=salaryOf(j);if(!s)return['(unknown)'];if(s.src==='est')return['≈ '+band(s.mid)+' (est.)'];return[(s.cur&&s.cur!=='USD'?s.cur+' ':'')+band(s.hi)]};
const fmtk=v=>'$'+Math.round(v/1000)+'k';
function effAge(j){if(!j.p&&!j.s)return null;const now=Date.now();return Math.max(j.p?(now-j.p):0,j.s?(now-j.s):0)/864e5}
function isBumped(j){return !!(j.p&&j.s&&j.p-j.s>7*864e5)}  // posting claims to be newer than when our crawler first saw it
function freshness(j){const d=effAge(j);if(d==null||!j.p)return 'unknown';if(d>365)return '👻 ghost risk';if(j.agp==null)return 'unknown';return d<=j.agp?'🌱 fresh':'🥀 stale'}
function ageBucket(j){if(!j.p)return 'unknown';const d=effAge(j);return d<=7?'this week':d<=31?'this month':d<=93?'≤ 3 months':d<=365?'≤ 1 year':'over 1 year 👻'}
const keysOf={fr:j=>[freshness(j)],ag:j=>[ageBucket(j)],el:j=>[j.el===true?'eligible':j.el===false?'ineligible':'unknown'],rm:j=>[j.rm&&j.rm!=='unknown'?j.rm:j.rme?j.rme.v+' (est.)':'unknown'],sn:j=>[j.sn?j.sn:j.sne?j.sne.v+' (est.)':'unknown'],rf:j=>j.e?[j.e.role_family||'other']:['(not enriched)'],et:j=>j.e?[j.e.employment_type||'unspecified']:['(not enriched)'],sa:salaryBucket,in:j=>j.co_?[j.co_.industry||'?']:['(not enriched)'],cs:j=>j.co_?[j.co_.size||'?']:['(not enriched)'],co:j=>j.co&&j.co.length?j.co.map(c=>NAMES[c]||c):j.coe?[(NAMES[j.coe.country]||j.coe.country)+' (est.)']:['(unknown)'],rg:j=>j.rg||[],ci:j=>j.ci||[],g:j=>['group '+j.g],c:j=>[j.c||'?'],a:j=>[j.k.split('/')[0]]};
function rebuildFacets(){for(const f of FACETS)facets[f]={};for(const j of JOBS)for(const f of FACETS)for(const k of keysOf[f](j))facets[f][k]=(facets[f][k]||0)+1}
const facetShow={};
function renderFacets(){for(const f of FACETS){const el=$('#f-'+f);el.innerHTML='';const entries=Object.entries(facets[f]).sort((a,b)=>b[1]-a[1]);const cap=facetShow[f]||(f==='g'?12:f==='rm'||f==='el'||f==='sn'?12:8);
    entries.slice(0,cap).forEach(([k,n])=>{const l=document.createElement('label');l.className='facet';const est=/\(est\.\)$/.test(k);const cls=est?'est':f==='el'&&k==='ineligible'?'no':f==='el'&&k==='eligible'?'ok':'';
      l.innerHTML=`<input type="checkbox" ${sel[f].has(k)?'checked':''}><span class="fl ${cls}" title="${esc(k)}">${esc(est?k.replace(/^≈ /,'').replace(/ \(est\.\)$/,''):k)}</span><span class="fn">${n.toLocaleString()}</span>`;
      l.querySelector('input').onchange=e=>{e.target.checked?sel[f].add(k):sel[f].delete(k);event({type:'filter',facet:f,value:k,on:e.target.checked});renderFacets();render(true,'filter')};el.appendChild(l)});
    if(entries.length>cap){const m=document.createElement('div');m.className='facet-more';m.textContent=`+ ${entries.length-cap} more`;m.onclick=()=>{facetShow[f]=cap+25;renderFacets()};el.appendChild(m)}}
  const cnt=(fs)=>{const n=fs.reduce((s,f)=>s+sel[f].size,0);return n?`${n} active`:''};$('#cnt-fit').textContent=cnt(['el','fr']);$('#cnt-role').textContent=cnt(['sn','rm','sa','et','rf','ag']);$('#cnt-loc').textContent=cnt(['co','rg','ci']);$('#cnt-co').textContent=cnt(['c','in','cs','a']);$('#cnt-g').textContent=cnt(['g']);
  renderChips()}
function renderChips(){const el=$('#chips');el.innerHTML='';
  if(PREF){const b=document.createElement('button');const hidden=JOBS.filter(j=>j.el===false).length;b.className='chip'+(st.eligOnly?' on':'');b.innerHTML=`✓ eligible for ${esc(PREF)}${st.eligOnly?` <span class="n">hiding ${hidden}</span>`:''}`;b.title='Jobs restricted to other countries/regions, or not labelled remote when you asked for remote, are hidden while this is on';b.onclick=()=>{st.eligOnly=!st.eligOnly;save();event({type:'filter',facet:'eligible',value:PREF,on:st.eligOnly});renderFacets();render(true,'filter')};el.appendChild(b)}
  const chips=[['rm','remote','Remote']];for(const [k] of Object.entries(facets.co).sort((a,b)=>b[1]-a[1]).slice(0,3))chips.push(['co',k,k]);
  for(const [f,k,label] of chips){const b=document.createElement('button');b.textContent=label;b.className='chip'+(sel[f].has(k)?' on':'');b.onclick=()=>{sel[f].has(k)?sel[f].delete(k):sel[f].add(k);event({type:'filter',facet:f,value:k,on:sel[f].has(k)});renderFacets();render(true,'filter')};el.appendChild(b)}
  if(st.simTo&&byKey[st.simTo]){const x=document.createElement('button');x.className='chip on';x.textContent='◉ similar to: '+byKey[st.simTo].t.slice(0,28)+' ✕';x.title='Click to go back to the normal ranking';x.onclick=()=>{st.simTo=null;save();event({type:'similar',on:false});renderChips();render(true,'similar')};el.appendChild(x)}
  for(const [b,name] of Object.entries(st.hideCo||{})){const x=document.createElement('button');x.className='chip dis';x.textContent='✖ '+name;x.title='Hidden company · click to show again';x.onclick=()=>{delete st.hideCo[b];save();event({type:'hide_company',board:b,company:name,on:false});renderFacets();render(true,'filter')};el.appendChild(x)}
  for(const kind of ['t','b'])for(const [t,v] of Object.entries(st.kw[kind])){if(v>=0)continue;const b=document.createElement('button');b.className='chip dis';b.textContent='✖ '+t+(kind==='b'?' (body)':'');b.title='Click to un-ban';b.onclick=()=>{delete st.kw[kind][t];save();event({type:'ban',where:kind,term:t,on:false});renderFacets();render(true,'filter')};el.appendChild(b)}}
$('#clearf').onclick=()=>{for(const f of FACETS)sel[f].clear();renderFacets();render(true,'filter')};
// ---- list
let ranked=[],cur=0,shown=0,prevTop=null,CHIP=j=>[fscore(j),'Model score'];
function passes(j){const q=$('#q').value.trim().toLowerCase();if(q&&!(j.searchText??=(j.t+' '+j.c+' '+j.l+' '+j.jd).toLowerCase()).includes(q))return false;
  if(exclG.has(j.g3)||kwHidden(j))return false;
  if(st.eligOnly&&j.el===false&&!(j.rme&&sel.rm.has(j.rme.v+' (est.)')&&String(j.elr||'').startsWith('not labelled remote'))&&!(j.coe&&sel.co.has((NAMES[j.coe.country]||j.coe.country)+' (est.)')))return false;
  for(const f in sel)if(sel[f].size&&!keysOf[f](j).some(k=>sel[f].has(k)))return false;
  const s=$('#show').value,l=st.labels[j.k];if(s==='unlabeled'&&l!==undefined)return false;if(s==='yes'&&l!==1)return false;if(s==='no'&&l!==0)return false;return true}
function fscore(j){return j.score+kwBoost(j)}
function nc0(){return (st.compares||[]).length}
const REASON={similar:'similarity to a job you liked',label:'your label',compare:'your comparisons',seed:'More / Less picks',sort:'your taste model',enrich:'new enrichment data',filter:'filters',search:'search',order:'sort order'};
function render(reset=true,reason=null){const _y=window.scrollY;const sort=$('#sort').value,show=$('#show').value;const ref=st.simTo&&byKey[st.simTo];const simv=ref?Object.fromEntries(JOBS.map(j=>[j.k,dot(j.vec,ref.vec)])):null;  // Show similar is pure raw cosine to that job: no taste model, no boosts, no centering
  const fitScores=new Map();const cachedFit=j=>{if(!fitScores.has(j.k))fitScores.set(j.k,fscore(j));return fitScores.get(j.k)};
  const cmp=(a,b)=>simv?simv[b.k]-simv[a.k]:sort==='sim'?b.sim-a.sim:sort==='date'?b.s-a.s:sort==='company'?(a.c||'').localeCompare(b.c||''):cachedFit(b)-cachedFit(a);
  CHIP=simv?j=>[simv[j.k],'Cosine similarity to “'+byKey[st.simTo].t.slice(0,40)+'”']:sort==='sim'?j=>[j.sim,'Cosine similarity to your ideal JD']:j=>[fscore(j),'Model score'];
  const vis=JOBS.filter(passes);
  // Yes: in the order you said yes, never re-ordered. Unlabelled: ranked. No: collapsed, in the order you said no.
  const order=new Map(Object.keys(st.labels).map((k,i)=>[k,i]));const at=k=>order.get(k)??1e9;
  const yes=vis.filter(j=>st.labels[j.k]===1).sort((a,b)=>at(a.k)-at(b.k)),no=vis.filter(j=>st.labels[j.k]===0).sort((a,b)=>at(a.k)-at(b.k)),un=vis.filter(j=>st.labels[j.k]===undefined).sort(cmp);
  const showNo=!!st.showNo||show==='no';
  ranked=show==='yes'?yes:show==='no'?no:show==='unlabeled'?un:[...yes,...un,...(showNo?no:[])];
  const newTop=un.slice(0,20).map(j=>j.k);let movedIn=[];
  if(prevTop&&reason&&['label','compare','seed','sort','enrich','similar'].includes(reason)){const was=new Set(prevTop);movedIn=newTop.filter(k=>!was.has(k))}
  if(reset){shown=0;cur=Math.min(cur,Math.max(0,ranked.length-1))}const el=$('#list');el.innerHTML='';
  if(!ranked.length){const d=document.createElement('div');d.className='empty';d.innerHTML='No jobs match. <a href="#">Clear filters</a>';el.appendChild(d);d.querySelector('a').onclick=e=>{e.preventDefault();$('#clearf').click()}}
  const head=(cls,txt,n,extra)=>{const h=document.createElement('div');h.className='sec-h '+cls;h.innerHTML=`${txt} <span class="n">${n.toLocaleString()}</span>${extra?' <span class="hint2">'+extra+'</span>':''}`;return h};
  const secAll=show==='all';const N=Math.min(secAll?yes.length+un.length:ranked.length,Math.max(shown,60));let i=0;
  if(secAll&&yes.length){el.appendChild(head('yes','✓ Yes',yes.length,'kept in the order you picked them'));}
  for(;i<N&&i<(secAll?yes.length:(show==='yes'?ranked.length:0));i++)el.appendChild(jobEl(ranked[i],i));
  if(secAll){const simple=sort==='sim';
    const rh=head('',un.length?'Ranked':'Nothing left to rank',un.length,un.length?(ref?'by similarity to “'+esc(ref.t.slice(0,50))+'”':(simple?'by cosine similarity to your ideal JD':(taste||st.compares.length?'by your taste':(yes.length&&no.length)?'by your yes/no labels':'by fit to your ideal JD'))+' · only this section re-orders'):'');
    {const tb=document.createElement('button');tb.className='btn ghost sm';tb.textContent=simple?'Switch to advanced ranking':'Switch to simple ranking';
      tb.title=simple?'Back to the learned model (your labels, comparisons and kNN term)':'Plain cosine similarity to your ideal JD; ignores everything the model has learned'+(ref?' (also clears the similar-to focus)':'');
      tb.onclick=e=>{e.stopPropagation();if(st.simTo){st.simTo=null;save()}$('#sort').value=simple?'model':'sim';event({type:'sort',value:$('#sort').value,via:'section-toggle'});renderChips();render(true,'order')};rh.appendChild(tb)}
    el.appendChild(rh);
    for(;i<N&&i<yes.length+un.length;i++){const d=jobEl(ranked[i],i);if(movedIn.includes(ranked[i].k))d.classList.add('moved');el.appendChild(d)}
    if(no.length){const h=head('no',(showNo?'▾':'▸')+' ✗ No',no.length,showNo?'click to collapse':'click to show');h.onclick=()=>{st.showNo=!st.showNo;save();render(false)};el.appendChild(h)}
    if(showNo)for(let k=yes.length+un.length;k<ranked.length;k++)el.appendChild(jobEl(ranked[k],k))}
  else for(;i<N;i++){const d=jobEl(ranked[i],i);if(movedIn.includes(ranked[i].k))d.classList.add('moved');el.appendChild(d)}
  shown=N;
  if((secAll?yes.length+un.length:ranked.length)>N){const m=document.createElement('button');m.className='btn ghost';m.id='more';m.textContent=`Show more (${((secAll?yes.length+un.length:ranked.length)-N).toLocaleString()} left)`;m.onclick=()=>{shown+=60;render(false)};el.appendChild(m)}
  const nc=(st.compares||[]).length;const personalized=taste||nc||yes.length||no.length;
  $('#stats').innerHTML=`<b>${vis.length.toLocaleString()}</b> of ${JOBS.length.toLocaleString()} jobs · `+(personalized?`ranked by <b>your taste</b> · ${yes.length} yes · ${no.length} no · ${nc} comparisons${looAcc!=null?` · <span title="Leave-one-out: fit without each label, predict it. λ=${KNN_L} for the similar-to-a-yes/no term">predicts your labels ${Math.round(looAcc*100)}% held-out</span>`:''}`:`ranked by <b>fit to your ideal JD</b>${IDEAL.title?' (“'+esc(IDEAL.title)+'”)':''} · label a few or press ✨ Sort to personalize`);
  if(reason&&['label','compare','seed','sort','enrich','similar'].includes(reason)){const what=movedIn.length?`${movedIn.length} job${movedIn.length>1?'s':''} moved into the top 20`:'order refreshed';$('#reranktxt').textContent=`Re-ranked from ${REASON[reason]} · ${what}`;$('#rerank').classList.add('show');clearTimeout(render._t);render._t=setTimeout(()=>$('#rerank').classList.remove('show'),4000)}
  prevTop=newTop;window.scrollTo(0,Math.min(_y,document.body.scrollHeight));hi()}
function badges(j,compact=false){const out=[];
  if(j.el===false)out.push(`<span class="b no" title="${esc(j.elr)}">⛔ ${esc(j.elr.length>34?j.elr.slice(0,34)+'…':j.elr)}</span>`);
  if(j.rm&&j.rm!=='unknown')out.push(`<span class="b">${esc(j.rm)}</span>`);else if(j.rme)out.push(`<span class="b est" title="${j.rme.p?'Estimated from the posting\'s embedding; the posting itself doesn\'t say':'Default: the posting names a place and nothing in it says remote or hybrid'}">${esc(j.rme.v)}${j.rme.p?' '+Math.round(j.rme.p*100)+'%':''}</span>`);
  if(j.sn)out.push(`<span class="b">${esc(j.sn)}</span>`);else if(j.sne)out.push(`<span class="b est" title="${j.sne.p?'Seniority estimated from the posting\'s embedding; the title doesn\'t say':'Default: the title states no level and the model has no strong signal'}">${esc(j.sne.v)}${j.sne.p?' '+Math.round(j.sne.p*100)+'%':''}</span>`);
  const s=salaryOf(j);if(s){if(s.src==='est')out.push(`<span class="b est" title="Estimated from similar postings with stated pay; not from this posting">${fmtk(s.lo)}–${fmtk(s.hi)}</span>`);else out.push(`<span class="b ok" title="${esc(s.raw)}">💰 ${s.src==='llm'?esc(fmtSal(j.e)):esc(j.sal.raw.length>26?fmtk(s.lo)+'–'+fmtk(s.hi):j.sal.raw)+(j.sal&&j.sal.currency!=='USD'?' '+esc(j.sal.currency):'')}</span>`)}
  if(j.coe&&!(j.co&&j.co.length))out.push(`<span class="b est" title="Country estimated from similar location strings; the posting doesn't state one">${esc(NAMES[j.coe.country]||j.coe.country)}</span>`);
  if(j.p){const d=effAge(j);const bump=isBumped(j);const t=d<1?'today':d<30?Math.round(d)+'d ago':d<365?Math.round(d/30)+'mo ago':(d/365).toFixed(1)+'y ago';
    const bumpNote=bump?` Claims posted ${new Date(j.p).toISOString().slice(0,10)}, but our crawler first saw it ${new Date(j.s).toISOString().slice(0,10)} — the date was re-stamped; age shown is from our own first sighting, which can't be bumped.`:` Posted ${new Date(j.p).toISOString().slice(0,10)}.`;
    if(d>365)out.push(`<span class="b no" title="${bumpNote} Open for over a year; may be a ghost listing that is never filled">👻 ${bump?'🔁 ':''}${t}</span>`);
    else if(j.agp!=null){const fresh=d<=j.agp;const ttl=`${bumpNote} Postings with this content are typically ~${j.agp<2?j.agp.toFixed(1):Math.round(j.agp)} days old — this one is ${fresh?'younger (the market moves fast: fresh means apply soon)':'older (an outlier survivor: hard-to-fill, reposted, or possibly never filled)'}`;
      out.push(`<span class="b ${fresh?'ok':'est'}" title="${ttl}">${fresh?'🌱 Fresh':'🥀 Stale'}${bump?' 🔁':''} · ${t}</span>`)}
    else out.push(`<span class="b" title="${bumpNote}">${bump?'🔁 ':''}${t}</span>`)}
  if(!compact&&j.e&&j.e.visa_sponsorship==='yes')out.push('<span class="b acc">visa ✓</span>');
  if(!compact&&j.co_&&j.co_.staffing)out.push('<span class="b warn">staffing agency</span>');
  return out.join('')}
function jobEl(j,i){const d=document.createElement('div');const l=st.labels[j.k];d.className='job'+(l===1?' pos':l===0?' neg':'')+(st.opened[j.k]?' open':'');d.dataset.i=i;
  d.innerHTML=`<div class="jh"><div class="jt"><b>${esc(j.t)}</b><div class="jc">${esc(j.c)}${j.l?' · '+esc(j.l):''}</div></div><div class="jr"><span class="lbl">${l===1?`<button class="btn ghost sm simbtn${st.simTo===j.k?' on':''}" title="Re-rank the unlabelled jobs by similarity to this one">${st.simTo===j.k?'◉ similar':'Show similar'}</button>`:''}<button class="btn ghost sm y" title="Yes, more like this (J)">${l===1?'✓ yes':'✓'}</button><button class="btn ghost sm n" title="No, less like this (K)">${l===0?'✗ no':'✗'}</button></span><span class="score" title="${esc(CHIP(j)[1])} ${CHIP(j)[0].toFixed(2)} · model ${fscore(j).toFixed(2)} · similarity to ideal JD ${j.sim.toFixed(2)}${kwBoost(j)?' · keyword boost':''}">${CHIP(j)[0].toFixed(2)}</span></div></div>
  <div class="badges">${badges(j)}</div>${j.e&&j.e.summary?`<div class="jsum">${esc(j.e.summary)}</div>`:''}
  ${st.opened[j.k]?`<div class="jdetail"><div class="meta">${j.s?new Date(j.s).toISOString().slice(0,10)+' · ':''}${esc(j.k.split('/')[0])} · group ${j.g} · <a href="${esc(j.u)}" target="_blank" rel="noopener">open posting ↗</a></div>
  ${j.e?`<div class="meta">${[j.e.seniority,j.e.role_family,j.e.employment_type,(j.e.skills_required||[]).slice(0,8).join(', ')].filter(Boolean).map(esc).join(' · ')}</div>`:''}
  ${j.co_?`<div class="meta">🏢 ${esc(j.co_.name)}${j.co_.industry?' · '+esc(j.co_.industry):''}${j.co_.size?' · '+esc(j.co_.size)+' people':''}${j.co_.hq?' · HQ '+esc(j.co_.hq):''}${j.co_.website?' · <a href="'+esc(j.co_.website)+'" target="_blank" rel="noopener">site ↗</a>':''}</div>`:''}
  <div class="jd">${esc(jdText(j.jd)||'(no description)')}</div>
  <div class="jactions"><input class="note" type="text" placeholder="Note to self…" value="${esc(st.notes[j.k]||'')}"><button class="btn ghost sm hideco" title="Hide every posting from this company; kept across re-compiles">✖ Never show ${esc((j.c||'').slice(0,22))} again</button></div></div>`:''}`;
  d.onclick=e=>{if(e.target.closest('a')){event({type:'open',key:j.k});return}if(e.target.closest('input,button'))return;cur=i;toggle(j,d)};
  const sb=d.querySelector('.simbtn');if(sb)sb.onclick=e=>{e.stopPropagation();st.simTo=st.simTo===j.k?null:j.k;save();event({type:'similar',key:j.k,title:j.t,on:!!st.simTo});renderChips();render(true,'similar');if(st.simTo)toast(`Ranked section now ordered by similarity to “${j.t.slice(0,40)}”`)};
  d.querySelector('.y').onclick=e=>{e.stopPropagation();cur=i;label(l===1?undefined:1,false)};d.querySelector('.n').onclick=e=>{e.stopPropagation();cur=i;label(l===0?undefined:0,false)};
  if(st.opened[j.k]){d.querySelector('.note').onchange=e=>{st.notes[j.k]=e.target.value;save();event({type:'note',key:j.k,text:e.target.value})};d.querySelector('.note').onkeydown=e=>e.stopPropagation();
  d.querySelector('.hideco').onclick=e=>{e.stopPropagation();st.hideCo[boardOf(j)]=j.c||boardOf(j);save();event({type:'hide_company',board:boardOf(j),company:j.c,on:true});renderFacets();render(true,'filter');toast(`Hidden ${j.c}. Undo from the ✖ chip in the toolbar.`)}};return d}
function toggle(j,d){st.opened[j.k]=!st.opened[j.k];save();d.replaceWith(jobEl(j,Number(d.dataset.i)));if(st.opened[j.k])event({type:'view',key:j.k,title:j.t,company:j.c});hi(true)}
function label(v,scroll=true){const j=ranked[cur];if(!j)return;const nxt=ranked.slice(cur+1).find(x=>st.labels[x.k]===undefined&&x!==j);if(v===undefined)delete st.labels[j.k];else st.labels[j.k]=v;if(st.simTo===j.k&&v!==1)st.simTo=null;save();event({type:'label',key:j.k,value:v??null,title:j.t,company:j.c,location:j.l,score:j.score});refit();render(false,'label');
  const tgt=v===undefined?j:(nxt||j);const ni=ranked.indexOf(tgt);cur=ni>=0?ni:Math.min(cur,ranked.length-1);hi(scroll)}
function hi(scroll=false){document.querySelectorAll('.job').forEach((e,i)=>e.classList.toggle('cur',+e.dataset.i===cur));if(!scroll)return;const e=document.querySelector(`.job[data-i="${cur}"]`);e&&e.scrollIntoView({block:'nearest'})}
function toast(msg){$('#toasttxt').textContent=msg;$('#toast').classList.add('show');clearTimeout(toast._t);toast._t=setTimeout(()=>$('#toast').classList.remove('show'),3200)}
document.addEventListener('keydown',e=>{
  if($('#help').classList.contains('on')){if(e.key==='Escape'||e.key==='?')$('#help').classList.remove('on');return}
  if($('#cmp').classList.contains('on')){const vis=id=>$(id).style.display!=='none';
    if(vis('#intro')){if(e.key==='Escape')$('#cmp').classList.remove('on');else if(e.key==='Enter')$('#intro-go').click();return}
    if(vis('#scope')){if(e.key==='Escape')$('#cmp').classList.remove('on');else if(e.key==='Enter')toWhat();return}
    if(vis('#what')){if(e.key==='Escape')$('#cmp').classList.remove('on');else if(e.key==='Enter')beginPairs();return}
    if(e.key==='ArrowLeft')pick('a');else if(e.key==='ArrowRight')pick('b');else if(e.key==='s'||e.key==='S')showPair();else if(e.key==='Escape')stopCmp();return}
  if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'){if(e.key==='Escape')e.target.blur();return}const j=ranked[cur];
  if(e.key==='j'||e.key==='J')label(1);else if(e.key==='k'||e.key==='K')label(0);else if(e.key===' '){e.preventDefault();if(j)toggle(j,document.querySelector(`.job[data-i="${cur}"]`))}
  else if(e.key==='ArrowDown'){e.preventDefault();cur=Math.min(cur+1,ranked.length-1);hi(true)}else if(e.key==='ArrowUp'){e.preventDefault();cur=Math.max(0,cur-1);hi(true)}else if((e.key==='o'||e.key==='O')&&j){window.open(j.u,'_blank');event({type:'open',key:j.k})}
  else if(e.key==='/'){e.preventDefault();$('#q').focus()}else if(e.key==='?'){$('#help').classList.add('on')}});
let qt;$('#q').oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{event({type:'search',q:$('#q').value});render(true,'search')},250)};
$('#sort').onchange=()=>{event({type:'sort',value:$('#sort').value});render(true,'order')};$('#show').onchange=()=>render(true,'filter');
$('#toggleIdeal').onclick=()=>{const el=$('#ideal');el.textContent=IDEAL_TEXT||'(ideal JD text not available)';el.style.display=el.style.display==='block'?'none':'block'};
$('#export').onclick=()=>{const m={recipe:IDEAL.recipe,space:'centered',mean:Array.from(MEAN,x=>+x.toFixed(6)),w:Array.from(u,x=>+x.toFixed(5)),b:+b.toFixed(5),taste:Array.from(u,x=>+x.toFixed(5)),knn:{lambda:KNN_L,yes:Object.keys(st.labels).filter(k=>st.labels[k]===1),no:Object.keys(st.labels).filter(k=>st.labels[k]===0)},loo_accuracy:looAcc,labels:st.labels,notes:st.notes,compares:st.compares,exported_at:Date.now()};const s=JSON.stringify(m);navigator.clipboard?.writeText(s);event({type:'export',labels:Object.keys(st.labels).length});toast('Model, labels and comparisons copied to the clipboard as JSON')};
$('#reset').onclick=()=>{if(!confirm('Wipe labels, comparisons and notes for this search? Enrichment is kept.'))return;location.search='?reset=1'};
$('#helpbtn').onclick=()=>$('#help').classList.add('on');$('#helpclose').onclick=$('#helpok').onclick=()=>$('#help').classList.remove('on');
$('#filtersbtn').onclick=()=>document.body.classList.toggle('filters');$('#closef').onclick=()=>document.body.classList.remove('filters');
for(const m of ['#cmp','#help'])$(m).addEventListener('click',e=>{if(e.target===$(m)){$(m).classList.remove('on')}});
$('#brand-title').textContent=IDEAL.title?'· '+IDEAL.title:'';
st.enrich=st.enrich||{};st.companies=st.companies||{};applyEnrich();if(TW){for(const j of JOBS)j.score=j.pre;refit()}else{fitAll()};rebuildFacets();renderFacets();render();event({type:'session',jobs:JOBS.length,title:IDEAL.title,compares:st.compares.length});