export function decode(s){const raw=atob(s),bytes=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)bytes[i]=raw.charCodeAt(i);return new Float32Array(bytes.buffer)}
export function dot(a,b){let s=0;for(let i=0;i<a.length;i++)s+=a[i]*b[i];return s}
export function norm(v){const n=Math.sqrt(dot(v,v))||1;return Float32Array.from(v,x=>x/n)}
export function half(h){const s=h&32768?-1:1,e=h>>10&31,m=h&1023;return e===0?s*m*2**-24:e===31?(m?NaN:s*Infinity):s*(1+m/1024)*2**(e-15)}
function probabilities(m,v){if(!m)return {};const z=m.W.map((w,i)=>dot(w,v)+m.b[i]),mx=Math.max(...z),p=z.map(x=>Math.exp(x-mx)),sum=p.reduce((a,b)=>a+b,0);return Object.fromEntries(m.classes.map((c,i)=>[c,p[i]/sum]))}
export function estimate(j,models){
 const v=norm(decode(j.v)),sm=models.salary,am=models.arrangement,sn=models.seniority,age=models.age;
 if(sm){const mid=Math.exp(dot(sm.w,v)+sm.b),k=Math.exp(sm.sigma),round=x=>Math.round(x/1000)*1000;j.est={mid:round(mid),lo:round(mid/k),hi:round(mid*k)}}
 if(age){let z;if(age.kind==='mlp'){const h=Float32Array.from(age.b1);for(let i=0;i<v.length;i++)for(let k=0;k<h.length;k++)h[k]+=v[i]*age.W1[i][k];z=dot(Float32Array.from(h,x=>Math.max(0,x)),age.W2)+age.b2}else z=dot(v,age.w)+age.b;j.agp=Math.max(0,Math.expm1(z))}
 if(!j.sn){const p=probabilities(sn,v),best=Object.entries(p).sort((a,b)=>b[1]-a[1])[0];j.sne=best&&best[1]>=Math.max(sn.threshold||.7,.85)&&!/member of (?:the )?technical staff|\bMTS\b/i.test(j.t)?{v:best[0],p:best[1]}:{v:'mid',p:null}}
 if(j.rm==='unknown'){const p=probabilities(am,v);if(p.remote>=(am?.threshold||.7))j.rme={v:'remote',p:p.remote};else if(p.hybrid>=Math.max(am?.threshold||.7,.85))j.rme={v:'hybrid',p:p.hybrid};else if(j.ci.length||j.rg.length)j.rme={v:'onsite',p:null}}
 // Never allow executable URLs from a raw posting into the shared search page.
 try {if(!['https:','http:'].includes(new URL(j.u).protocol))j.u=''}catch{j.u=''}
 return j;
}
export async function jsonFetch(url,options={}){const r=await fetch(url,{...options,signal:options.signal||AbortSignal.timeout(90000)});const d=await r.json().catch(()=>({}));if(!r.ok)throw Error((d.error||`Unable to load data (${r.status}).`)+(d.retryAfterSeconds?` Try again in ${d.retryAfterSeconds} seconds.`:''));return d}
