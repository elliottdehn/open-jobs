import {strict as assert} from 'node:assert';
import {prepareJobs,preparationBatches} from '../web/prepare-client.js';
const jobs=Array.from({length:4800},(_,id)=>({id,jd:'Meaningful work. '.repeat(id%3?500:3000),v:'x'.repeat(8192)}));
let peak=0,calls=0,active=0,terminated=0,modelCalls=0;
const createClient=()=>({
 async request(message){
  assert.equal(++active,1,'prepare sequentially, never queue the whole slice');
  const bytes=JSON.stringify(message.jobs).length*2;peak=Math.max(peak,bytes);
  assert.ok(bytes<520*1024,'bound the amount transferred into the runtime');
  if(message.models)modelCalls++;
  calls++;await Promise.resolve();active--;
  return {jobs:message.jobs.map(j=>({k:String(j.id)})),remoteOnly:true};
 },terminate(){terminated++}
});
const result=await prepareJobs(jobs,'Remote',[],{},createClient);
assert.equal(result.jobs.length,jobs.length);assert.equal(new Set(result.jobs.map(j=>j.k)).size,jobs.length);
assert.equal(result.remoteOnly,true);assert.equal(modelCalls,1);assert.equal(terminated,1);assert.ok(calls>100);
await assert.rejects(prepareJobs(jobs,'Remote',[],{},()=>({request:async()=>{throw Error('MemoryError')},terminate(){terminated++}})),/Your draft and downloaded matches are saved/);
assert.equal(terminated,2,'release failed runtime as well');
const oversized={jd:'a'.repeat(600000)};
assert.deepEqual([...preparationBatches([jobs[0],oversized,jobs[1]])].map(b=>b.length),[1,1,1],'an oversized posting is isolated without truncating it');
console.log(`PASS: 4,800 long postings processed in ${calls} sequential batches, peak transfer ${(peak/1024).toFixed(0)} KiB; runtime released on success and failure.`);
