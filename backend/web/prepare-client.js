import {workerClient} from './worker-client.js';

// Bound both job count and text size: descriptions can vary by orders of magnitude.
export function* preparationBatches(jobs){
 let batch=[],size=0;
 for(const job of jobs){
  const bytes=JSON.stringify(job).length*2;
  if(batch.length&&(batch.length>=48||size+bytes>512*1024)){yield batch;batch=[];size=0}
  batch.push(job);size+=bytes;
 }
 if(batch.length)yield batch;
}
export async function prepareJobs(jobs,pref,anchor,models,createClient=workerClient,onProgress=()=>{}){
 const result={jobs:[],remoteOnly:false};let first=true;
 const client=createClient('/prepare-worker.js',{timeoutMs:45000,onProgress:({message})=>onProgress(result.jobs.length,jobs.length,message)});
 onProgress(0,jobs.length,'Loading search tools…');
 try{
  for(const batch of preparationBatches(jobs)){
   const prepared=await client.request({jobs:batch,pref,anchor,...(first?{models}:{})});first=false;
   result.jobs.push(...prepared.jobs);result.remoteOnly=prepared.remoteOnly;
   onProgress(result.jobs.length,jobs.length);
  }
  return result;
 }catch(error){
  if(/MemoryError|out of memory|allocation failed/i.test(error.message))throw Error('Your browser ran out of memory while preparing matches. Close unused tabs, then try again. Your draft and downloaded matches are saved.');
  throw error;
 }finally{
  // WebAssembly memory cannot shrink. Release it before opening the search UI,
  // and always start a clean runtime after a failed preparation.
  client.terminate();
 }
}
