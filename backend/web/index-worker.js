import {half,dot} from './search-data.js';
let centroids,leaves,dims;
self.onmessage=({data:m})=>{
 try {
  if(m.type==='init'){
   dims=m.dims;leaves=m.leaves;
   centroids=Float32Array.from(new Uint16Array(m.bytes),half);
   self.postMessage({id:m.id,ready:true});return;
  }
  const ranked=leaves.map(n=>({id:n.id,score:dot(m.vector,centroids.subarray(n.id*dims,(n.id+1)*dims))}));
  ranked.sort((a,b)=>b.score-a.score);
  self.postMessage({id:m.id,ranked:ranked.map(n=>n.id)});
 }catch(e){self.postMessage({id:m.id,error:e.message})}
};
