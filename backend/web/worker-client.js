export function workerClient(url) {
 const worker=new Worker(url,{type:'module'}),requests=new Map();let seq=0;
 worker.onmessage=({data})=>{const r=requests.get(data.id);if(!r)return;requests.delete(data.id);clearTimeout(r.timer);data.error?r.reject(Error(data.error)):r.resolve(data)};
 worker.onerror=()=>{for(const r of requests.values()){clearTimeout(r.timer);r.reject(Error('Search processing failed. Please reload and retry.'))}requests.clear()};
 return {request(message,transfer=[]){return new Promise((resolve,reject)=>{const id=++seq,timer=setTimeout(()=>{requests.delete(id);reject(Error('Search processing timed out. Please retry.'))},180000);requests.set(id,{resolve,reject,timer});worker.postMessage({...message,id},transfer)})},terminate(){worker.terminate()}};
}
