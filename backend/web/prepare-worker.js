import {loadPyodide} from 'https://cdn.jsdelivr.net/pyodide/v314.0.6/full/pyodide.mjs';
import {decode, dot, estimate} from './search-data.js';
let ready,models;
async function init(){
  const py = await loadPyodide();
  await Promise.all(['locparse.py','salary.py','seniority.py','city_countries.json','prepare.py'].map(async name=>{
    const r=await fetch('/python/'+name);if(!r.ok)throw Error('Could not load search rules. Please retry.');
    py.FS.writeFile('/home/pyodide/'+name,await r.text());
  }));
  py.runPython('from prepare import prepare\nimport json');return py;
}
self.onmessage=async({data})=>{
  try{
    const py=await(ready||=init());
    if(data.models)models=data.models;
    for(const j of data.jobs)if(!Number.isFinite(j.sim))j.sim=dot(data.anchor,decode(j.v));
    py.globals.set('raw_json',JSON.stringify(data.jobs));py.globals.set('pref',data.pref);py.globals.set('table_json',JSON.stringify(models.location?.table||{}));
    const result=JSON.parse(py.runPython('prepare(json.loads(raw_json), pref, json.loads(table_json))'));
    for(const j of result.jobs) estimate(j,models);
    self.postMessage({id:data.id,...result});
  }catch(e){ready=null;self.postMessage({id:data.id,error:e.message||String(e)})}
};
