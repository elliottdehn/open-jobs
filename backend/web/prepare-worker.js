import {loadPyodide} from './runtime/314.0.6/pyodide.mjs';
import {decode, dot, estimate} from './search-data.js';
let ready,models;
async function init(){
  const py = await loadPyodide();
  await Promise.all(['locparse.py','salary.py','seniority.py','city_countries.json','prepare.py'].map(async name=>{
    const r=await fetch('/python/'+name,{signal:AbortSignal.timeout(20000)});if(!r.ok)throw Error('Could not load search rules. Please retry.');
    py.FS.writeFile('/home/pyodide/'+name,await r.text());
  }));
  py.runPython('from prepare import prepare\nimport json');return py;
}
self.onmessage=async({data})=>{
  try{
    if(!ready)self.postMessage({id:data.id,type:'progress',message:'Loading search tools…'});
    const py=await(ready||=init());
    self.postMessage({id:data.id,type:'progress',message:'Checking location, salary and freshness…'});
    if(data.models)models=data.models;
    for(const j of data.jobs)if(!Number.isFinite(j.sim))j.sim=dot(data.anchor,decode(j.v));
    // Python only needs text to extract facets; vectors and posting bodies never
    // make a round trip through its output JSON. Only pass relevant table entries.
    const raw=data.jobs.map(j=>({title:j.title||'',location:j.location||'',jd:j.jd||''})),table={};
    for(const location of [...raw.map(j=>j.location),...data.pref.split(/\bor\b|;|\||\//i)])for(const key of [location.trim(),location.trim().toLowerCase()])if(models.location?.table?.[key])table[key]=models.location.table[key];
    py.globals.set('raw_json',JSON.stringify(raw));py.globals.set('pref',data.pref);py.globals.set('table_json',JSON.stringify(table));
    let result;
    try{result=JSON.parse(py.runPython('prepare(json.loads(raw_json), pref, json.loads(table_json))'))}
    finally{py.globals.delete('raw_json');py.globals.delete('table_json')}
    result.jobs=result.jobs.map((metadata,i)=>{
      const j=data.jobs[i];
      return estimate({k:`${j.ats}/${j.slug}#${j.id}`,t:j.title||'',c:j.company||'',l:j.location||'',u:j.url||'',s:j.first_seen||j.seen,p:j.pub,jd:j.jd||'',g:j.leaf,g3:j.leaf,v:j.v,sim:j.sim,...metadata,e:null,co_:null},models);
    });
    self.postMessage({id:data.id,...result});
  }catch(e){self.postMessage({id:data.id,error:e.message||String(e)})}
};
