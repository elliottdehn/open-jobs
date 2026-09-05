import {getSearch,setSearch} from './storage.js';
import {decode,dot,norm,jsonFetch} from './search-data.js';
import {cachedData,chooseGroups} from './data-cache.js';
import {workerClient} from './worker-client.js';
const $=s=>document.querySelector(s), draftKey='open-jobs:conversation:v1';
let conversation={messages:[],title:'',location:'',jd:'',versions:[],id:crypto.randomUUID()},current=null,busy=false,expanding=false,chatBusy=false,manifest=null,indexClient=null,indexBuild=null,indexChecked=0,models=null,parser=null,parserModels=null,sequence=0;
try{const saved=JSON.parse(localStorage.getItem(draftKey));if(saved?.id)conversation={...conversation,...saved}}catch{}
function persist(){try{localStorage.setItem(draftKey,JSON.stringify(conversation))}catch{$('#chat-status').textContent='Your browser could not save the draft. Keep this tab open.'}}
function syncDraft(){conversation.title=$('#title').value.trim();conversation.location=$('#loc').value.trim();conversation.jd=$('#draft').value;clearTimeout(syncDraft.timer);syncDraft.timer=setTimeout(persist,250);$('#find').disabled=busy||chatBusy||expanding||conversation.jd.trim().length<40||!conversation.title||!conversation.location}
function populate(){for(const [id,key] of [['title','title'],['loc','location'],['draft','jd']])$('#'+id).value=conversation[key]||'';syncDraft()}
function appendMessage(m){const a=document.createElement('article');a.className='message '+m.role;const d=document.createElement('div'),b=document.createElement('b'),p=document.createElement('p');b.textContent=m.role==='user'?'You':'Your job assistant';p.textContent=m.content;d.append(b,p);if(m.role==='assistant'){const icon=document.createElement('span');icon.className='avatar';icon.textContent='↗';a.append(icon)}a.append(d);$('#messages').append(a);$('#messages').scrollTop=$('#messages').scrollHeight}
conversation.messages.forEach(appendMessage);populate();if(conversation.messages.length)$('#suggestions').hidden=true;
for(const id of ['title','loc','draft'])$('#'+id).oninput=syncDraft;
const starts=['I want to build things. Help me figure out what kind of role fits.','I’m changing careers. Help me describe the work I want to move into.','I know my next role. Help me turn it into a great job description.'];
$('#suggestions').querySelectorAll('button').forEach((b,i)=>b.onclick=()=>{$('#message').value=starts[i];$('#message').focus()});
$('#message').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#chat-form').requestSubmit()}};
$('#chat-form').onsubmit=async e=>{
 e.preventDefault();if(chatBusy||busy)return;const content=$('#message').value.trim();if(!content)return;
 chatBusy=true;$('#send').disabled=true;syncDraft();const messages=[...conversation.messages,{role:'user',content}];appendMessage(messages.at(-1));$('#message').value='';$('#suggestions').hidden=true;$('#chat-status').textContent='Thinking about your next role…';
 for(const id of ['title','loc','draft'])$('#'+id).readOnly=true;
 try{
  const reply=await jsonFetch('/chat',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({messages:messages.slice(-29),draft:`Target role: ${conversation.title}\nLocation: ${conversation.location}\n\n${conversation.jd}`})});
  if(typeof reply.message!=='string'||typeof reply.jd!=='string')throw Error('The assistant returned an incomplete draft. Please retry.');
  if(conversation.jd)conversation.versions.push({jd:conversation.jd,title:conversation.title,location:conversation.location,at:Date.now()});
  conversation.messages=[...messages,{role:'assistant',content:reply.message}];Object.assign(conversation,{title:reply.title,location:reply.location,jd:reply.jd});appendMessage(conversation.messages.at(-1));populate();$('#draft-state').textContent=reply.ready?'Ready for your review':'Draft in progress';$('#chat-status').textContent='';
 }catch(e){$('#messages').lastElementChild?.remove();$('#message').value=content;$('#chat-status').textContent=e.message}
 finally{chatBusy=false;$('#send').disabled=false;for(const id of ['title','loc','draft'])$('#'+id).readOnly=false;syncDraft();if(expansionQueue.length)expand(expansionQueue.shift())}
};
async function loadIndex(){
 // A HEAD request detects a new daily build without downloading the large manifest again.
 if(manifest&&indexBuild===manifest.built_at&&Date.now()-indexChecked<60000)return manifest;
 let m=manifest;
 if(manifest){
  const head=await fetch('/data/manifest.json',{method:'HEAD',cache:'no-cache',signal:AbortSignal.timeout(30000)});
  if(!head.ok||!manifest.etag||head.headers.get('etag')!==manifest.etag)m=null;
 }
 if(!m){
  const r=await fetch('/data/manifest.json',{cache:'no-cache',signal:AbortSignal.timeout(90000)});
  if(!r.ok)throw Error('The job index is unavailable. Please retry.');
  m=await r.json();m.etag=r.headers.get('etag');
 }
 if(!m.tree?.length||!m.dims)throw Error('The job index is being prepared. Please try again shortly.');
 if(indexBuild!==m.built_at){
  const r=await cachedData('/data/centroids.bin',m.built_at,{validate:async r=>{if((await r.arrayBuffer()).byteLength!==m.nodes*m.dims*2)throw Error('The index is updating. Please retry.')}});
  const bytes=await r.arrayBuffer();
  if(bytes.byteLength!==m.nodes*m.dims*2)throw Error('The index is updating. Please retry.');
  if(indexClient)indexClient.terminate();indexClient=workerClient('/index-worker.js');
  await indexClient.request({type:'init',bytes,dims:m.dims,leaves:m.tree.filter(n=>!n.children.length).map(n=>({id:n.id}))},[bytes]);
  indexBuild=m.built_at;models=null;
 }
 manifest=m;indexChecked=Date.now();
 $('#corpus').textContent=`${m.jobs.toLocaleString()} open jobs · Updated ${new Date(m.built_at).toLocaleDateString()}`;
 if(!models){
  const entries=await Promise.all(['salary','arrangement','seniority','age','location'].map(async name=>{
   try{return[name,await(await cachedData('/data/'+(name==='location'?'location-countries':name+'-model')+'.json',m.built_at)).json()]}catch{return[name,null]}
  }));
  models=Object.fromEntries(entries.map(([name,model])=>[name,model?.recipe&&model.recipe!==m.recipe?null:model]));
 }
 return m;
}
async function nearest(vector,excluded=[],advance=false){
 const {ranked}=await indexClient.request({type:'rank',vector});
 return chooseGroups(ranked.map(id=>manifest.tree[id]),excluded,{advance});
}
async function parseJobs(jobs,pref,anchor){
 if(!parser)parser=new Worker('/prepare-worker.js',{type:'module'});const worker=parser,id=++sequence;
 return new Promise((resolve,reject)=>{const cleanup=()=>{clearTimeout(timer);worker.removeEventListener('message',receive);worker.removeEventListener('error',fail)};const fail=e=>{cleanup();worker.terminate();if(parser===worker)parser=null;parserModels=null;reject(Error(e.message||'Search preparation could not load. Check your connection and retry.'))};const receive=({data})=>{if(data.id!==id)return;cleanup();data.error?reject(Error(data.error)):resolve(data)};const timer=setTimeout(()=>fail({message:'Search preparation timed out. Please retry.'}),180000);worker.addEventListener('message',receive);worker.addEventListener('error',fail);worker.postMessage({id,jobs,pref,anchor,...(parserModels===models?{}:{models})});parserModels=models});
}
async function fetchSlice(vector,ideal,previous,progress,text=conversation.jd,advance=false){
 const sameBuild=previous?.builtAt===manifest.built_at;
 const nodes=await nearest(vector,sameBuild?(previous.fetched||[]):[],advance),raw=[],fetched=[];let completed=0;
 // Bounded downloads keep both the network and memory usable on small devices.
 const queue=[...nodes];const failures=[];const anchor=norm(ideal.vector);
 const sameIdeal=previous&&JSON.stringify(previous.ideal.vector)===JSON.stringify(ideal.vector);
 if(!nodes.length&&previous&&sameIdeal&&previous.ideal.location===ideal.location)return previous;
 await Promise.all(Array.from({length:3},async()=>{while(queue.length){const n=queue.shift();try{const g=await(await cachedData(`/data/groups/${n.id}.json`,manifest.built_at,{validate:async r=>{const g=await r.json();if(!Array.isArray(g.jobs)||g.leaf!==n.id||g.jobs.some(j=>typeof j.v!=='string'||j.v.length!==4*Math.ceil(manifest.dims*4/3)))throw Error('The group is incomplete. Please retry.')}})).json();if(g.leaf!==undefined&&g.leaf!==n.id)throw Error('Index changed');for(const j of g.jobs)raw.push({...j,leaf:n.id});fetched.push(n.id)}catch(e){failures.push(n.id)}progress(++completed,nodes.length)}}));
 if(!raw.length&&nodes.length)throw Error('Could not download matching jobs. Please retry; your previous search is safe.');
 if(nodes.length&&manifest.etag){const check=await fetch('/data/manifest.json',{method:'HEAD',cache:'no-cache'});if(!check.ok||check.headers.get('etag')!==manifest.etag){indexChecked=0;throw Error('The daily index was updated during your search. Please retry.')}}
 progress(nodes.length,nodes.length,'Preparing location, salary and freshness filters…');
 const prepared=raw.length?await parseJobs(raw,ideal.location,anchor):{jobs:[],remoteOnly:previous?.remoteOnly||false};
 const union=new Map((previous?.jobs||[]).map(j=>[j.k,j]));
 // Re-apply eligibility to the old slice when the person revises their location preference.
 if(previous&&previous.ideal.location!==ideal.location){const oldRaw=previous.jobs.map(j=>{const [ats,rest]=j.k.split(/\/(.+)/),[slug,id]=rest.split(/#(.+)/);return{ats,slug,id,title:j.t,company:j.c,location:j.l,url:j.u,seen:j.s,pub:j.p,jd:j.jd,leaf:j.g,v:j.v,sim:sameIdeal?j.sim:dot(anchor,decode(j.v))}});const old=await parseJobs(oldRaw,ideal.location,anchor);for(const j of old.jobs)union.set(j.k,j)}
 for(const j of prepared.jobs)union.set(j.k,j);
 if(previous&&!sameIdeal)for(const j of union.values())j.sim=dot(anchor,decode(j.v));
 const groups={...(previous?.groups||{})};for(const n of nodes)groups[n.id]={label:n.label,medoid:n.medoid,size:n.size,exemplars:n.exemplars};
 return{id:conversation.id,ideal,text,jobs:[...union.values()],groups,remoteOnly:prepared.remoteOnly,builtAt:manifest.built_at,fetched:[...new Set([...(sameBuild?previous.fetched:[]),...fetched])],failures};
}
function showResults(reload=true){document.body.classList.add('results-mode');$('#workspace').hidden=true;$('#results').hidden=false;$('#resume-search').hidden=false;document.querySelectorAll('.nav-step').forEach((e,i)=>e.classList.toggle('active',i===1));if(reload)$('#search-frame').src='/search';history.replaceState(null,'','#matches')}
$('#edit-search').onclick=()=>{document.body.classList.remove('results-mode');$('#workspace').hidden=false;$('#results').hidden=true;document.querySelectorAll('.nav-step').forEach((e,i)=>e.classList.toggle('active',i===0));history.replaceState(null,'','#draft')};
$('#resume-search').onclick=()=>showResults(false);
$('#find').onclick=async()=>{
 if(busy||chatBusy||expanding)return;syncDraft();busy=true;$('#send').disabled=true;for(const id of ['title','loc','draft'])$('#'+id).readOnly=true;syncDraft();$('#progress').hidden=false;$('#search-status').textContent='Finding the right corner of the job market…';
 try{
  conversation.versions.push({jd:conversation.jd,title:conversation.title,location:conversation.location,at:Date.now()});persist();
  await loadIndex();const signature=JSON.stringify([conversation.jd,conversation.title,conversation.location,manifest.recipe]);let ideal;
  if(conversation.embedding?.signature===signature)ideal=conversation.embedding.ideal;
  else{const e=await jsonFetch('/embed',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({text:conversation.jd,title:conversation.title,location:conversation.location})});if(e.recipe!==manifest.recipe||e.vector?.length!==manifest.dims)throw Error('The search index is updating. Please try again shortly.');ideal={...e,title:conversation.title,location:conversation.location};conversation.embedding={signature,ideal};persist()}
  const next=await fetchSlice(norm(ideal.vector),ideal,current,(done,total,msg)=>{$('#progress').max=total||12;$('#progress').value=done;$('#search-status').textContent=msg||`Gathering your matches · ${done} of ${total}`});
  if(!next.jobs.length)throw Error('No matching jobs were available. Try a broader description.');
  await setSearch(next);current=next;showResults();$('#expand-status').textContent=next.failures.length?'Some matches could not load. The search will try again as you browse.':'';
 }catch(e){$('#search-status').textContent=e.message}
 finally{busy=false;$('#send').disabled=false;for(const id of ['title','loc','draft'])$('#'+id).readOnly=false;$('#progress').hidden=true;syncDraft();if(expansionQueue.length)expand(expansionQueue.shift())}
};
let expansionQueue=[];
async function expand(key=null){
 if(!current)return;
 if(busy||chatBusy||expanding){if(!expansionQueue.includes(key))expansionQueue.push(key);return}
 expanding=true;syncDraft();$('#expand-status').textContent=key?'Finding more jobs like the one you saved…':'Finding more matches…';
 try {
  await loadIndex();
  if(manifest.recipe!==current.ideal.recipe)throw Error('The search index has changed. Refine your ideal role to update your search.');
  const j=current.jobs.find(j=>j.k===key);
  const next=await fetchSlice(j?norm(decode(j.v)):norm(current.ideal.vector),current.ideal,current,
   (d,t,msg)=>{$('#expand-status').textContent=msg||`Finding more matches · ${d} of ${t}`},current.text,key===null);
  const known=new Set(current.jobs.map(j=>j.k));
  const addedJobs=next.jobs.filter(j=>!known.has(j.k));
  const added=addedJobs.length;
  if(next!==current)await setSearch(next);current=next;
  $('#expand-status').textContent=added?`${added.toLocaleString()} new matches added`:'';
  if(added)$('#search-frame').contentWindow.postMessage({type:'matches-updated',snapshot:{id:next.id,ideal:{recipe:next.ideal.recipe},jobs:addedJobs,groups:next.groups}},location.origin);
 } catch(e){$('#expand-status').textContent=e.message}
 finally {
  expanding=false;syncDraft();
  if(expansionQueue.length)expand(expansionQueue.shift());
 }
}
window.addEventListener('message',e=>{
 if(e.origin!==location.origin||e.source!==$('#search-frame').contentWindow)return;
 if(e.data?.type==='expand'&&typeof e.data.key==='string')expand(e.data.key);
 else if(e.data?.type==='expand-more')expand();
});
getSearch().then(s=>{if(s?.jobs?.length){current=s;conversation.id=s.id;persist();$('#resume-search').hidden=false;$('#search-frame').src='/search';if(location.hash!=='#draft')showResults(false)}}).catch(e=>{$('#search-status').textContent=e.message});

window.addEventListener('pagehide',persist);
