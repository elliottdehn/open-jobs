(function(){
  let CV=null,N=0,D=0,cideal=null;
  const dot=(a,o,b)=>{let s=0;for(let i=0;i<D;i++)s+=a[o+i]*b[i];return s};
  function fitOn(C,L,prior,iters){const w=Float32Array.from(prior);let bb=0;if(!C.length&&!L.length)return [w,0];const lr=.5,l2=.02,n=Math.max(1,C.length+L.length);
    let c0=0;if(L.length){for(const [ji] of L)c0+=dot(CV,ji*D,prior);c0/=L.length}
    for(let ep=0;ep<iters;ep++){
      for(const [ai,bi,winA] of C){let sc=0;for(let i=0;i<D;i++)sc+=w[i]*(CV[ai*D+i]-CV[bi*D+i]);const y=winA?1:0,pp=1/(1+Math.exp(-sc)),g=pp-y;for(let i=0;i<D;i++)w[i]-=lr*(g*(CV[ai*D+i]-CV[bi*D+i])+l2*(w[i]-prior[i])/n)}
      for(const [ji,y] of L){const sc=dot(CV,ji*D,w)-c0+bb,pp=1/(1+Math.exp(-sc)),g=pp-y;for(let i=0;i<D;i++)w[i]-=lr*(g*CV[ji*D+i]+l2*(w[i]-prior[i])/n);bb-=lr*g}}
    return [w,bb-c0]}
  function knnTermIdx(ji,L){let best=-1,worst=-1;for(const [xi,y] of L){if(xi===ji)continue;let c=0;for(let i=0;i<D;i++)c+=CV[ji*D+i]*CV[xi*D+i];if(y===1){if(c>best)best=c}else if(c>worst)worst=c}
    return (best<0?0:best)-(worst<0?0:worst)}
  onmessage=e=>{const m=e.data;
    if(m.type==='init'){CV=new Float32Array(m.cv);N=m.n;D=m.dims;cideal=Float32Array.from(m.cideal);return}
    if(m.type==='append'){const added=new Float32Array(m.cv),needed=(N+m.n)*D;if(needed>CV.length){const grown=new Float32Array(Math.max(needed,Math.ceil(CV.length*1.5)));grown.set(CV.subarray(0,N*D));CV=grown}CV.set(added,N*D);N+=m.n;return}
    const {seq,L,C,K}=m;  // L=[[idx,y]], C=[[ia,ib,winA]]
    const prior=Float32Array.from(cideal);
    {const P=L.filter(x=>x[1]===1),Ng=L.filter(x=>x[1]===0);
     for(const [ji] of P)for(let i=0;i<D;i++)prior[i]+=0.6*CV[ji*D+i]/P.length;
     for(const [ji] of Ng)for(let i=0;i<D;i++)prior[i]-=0.4*CV[ji*D+i]/Ng.length;
     let nr=0;for(let i=0;i<D;i++)nr+=prior[i]*prior[i];nr=Math.sqrt(nr)+1e-9;for(let i=0;i<D;i++)prior[i]/=nr}
    const [u,b]=fitOn(C,L,prior,60);
    const committee=[];for(let k=0;k<K;k++){const R=[];for(let i=0;i<C.length;i++)R.push(C[Math.floor(Math.random()*C.length)]);const RL=[];for(let i=0;i<L.length;i++)RL.push(L[Math.floor(Math.random()*L.length)]);committee.push(fitOn(R,RL,prior,40)[0])}
    let KNN_L=1.0,looAcc=null;
    const ys=L.filter(x=>x[1]===1).length,ns=L.length-ys;
    if(ys>=2&&ns>=2&&L.length>=6){const cands=[0,0.5,1,2,3];const hits=cands.map(()=>0);
      for(const [ji,y] of L){const rest=L.filter(x=>x[0]!==ji);const [wj,bj]=fitOn(C,rest,prior,40);const lin=dot(CV,ji*D,wj)+bj;const kn=knnTermIdx(ji,rest);
        cands.forEach((lam,i)=>{if(((lin+lam*kn)>0)===(y===1))hits[i]++})}
      let bi=0;for(let i=1;i<cands.length;i++)if(hits[i]>hits[bi])bi=i;KNN_L=cands[bi];looAcc=hits[bi]/L.length}
    const scores=new Float32Array(N);
    for(let ji=0;ji<N;ji++){const lin=dot(CV,ji*D,u)+b;const kn=L.length?knnTermIdx(ji,L):0;scores[ji]=1/(1+Math.exp(-(lin+KNN_L*kn)))}
    postMessage({seq,scores:scores.buffer,u:u.buffer,b,committee:committee.map(w=>w.buffer),KNN_L,looAcc},[scores.buffer,u.buffer,...committee.map(w=>w.buffer)])}
})();