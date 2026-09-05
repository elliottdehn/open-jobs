import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {workerClient} from '../web/worker-client.js';
import {prepareJobs} from '../web/prepare-client.js';
let terminated=0;
globalThis.Worker=class {
 postMessage(data){queueMicrotask(()=>{this.onmessage({data:{id:data.id,type:'progress',message:'Loading search tools…'}});setTimeout(()=>this.onmessage({data:{id:data.id,jobs:[{k:'one'}],remoteOnly:true}}),5)})}
 terminate(){terminated++}
};
const progress=[];
const prepared=await prepareJobs([{id:1}], 'Remote', [], {}, workerClient,(...p)=>progress.push(p));
assert.equal(prepared.jobs[0].k,'one','progress messages must not resolve pending batches');
assert.ok(progress.some(p=>p[2]==='Loading search tools…'));assert.deepEqual(progress.at(-1).slice(0,2),[1,1]);assert.equal(terminated,1);
globalThis.Worker=class {postMessage(){} terminate(){terminated++}};
const stuck=workerClient('/prepare-worker.js',{timeoutMs:15});
await assert.rejects(stuck.request({}),/timed out/);stuck.terminate();

// Run the actual slice orchestration: show one group first, then reuse it while extending coverage.
const app=readFileSync(new URL('../web/app.js',import.meta.url),'utf8');
const src=app.slice(app.indexOf('async function fetchSlice('),app.indexOf('function showResults('));
const nodes=Array.from({length:20},(_,id)=>({id,label:`Group ${id}`}));
const downloads=[];
const context={manifest:{built_at:1,dims:1},conversation:{jd:'Test role',id:'session'},norm:v=>v,decode:()=>[1],dot:()=>1,
 nearest:async(v,excluded)=>nodes.filter(n=>!excluded.includes(n.id)).slice(0,12),
 cachedData:async path=>{const id=Number(path.match(/(\d+)\.json/)[1]);downloads.push(id);return {json:async()=>({leaf:id,jobs:[{id,v:'AAAAAA=='}]})}},
 parseJobs:async raw=>({jobs:raw.map(j=>({k:String(j.id),g:j.leaf})),remoteOnly:true}),
};
vm.createContext(context);vm.runInContext(src,context);
const ideal={vector:[1],location:'Remote'};
const first=await context.fetchSlice([1],ideal,null,()=>{});
assert.equal(first.jobs.length,1);assert.equal(downloads.length,1,'open the first group without waiting for twelve');
const more=await context.fetchSlice([1],ideal,first,()=>{},'Test role',true);
assert.equal(more.jobs.length,13);assert.equal(new Set(downloads).size,downloads.length,'background expansion reuses the initial group');
assert.equal(more.jobs[0].k,first.jobs[0].k,'keep the first results when more arrive');
console.log('PASS: startup stage/count updates, stalled-worker timeout, first-group search, and automatic expansion without duplicate downloads.');
