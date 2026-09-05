// Only public index resources belong here. Never cache chat, drafts, labels, or notes.
export function createDataCache({fetcher=fetch,storage=globalThis.caches,origin=globalThis.location?.origin||'https://open-jobs.invalid'}={}) {
  const pending=new Map();
  let opened;
  const cache=()=>opened||=(storage?.open('open-jobs-public-v1').catch(()=>null)||Promise.resolve(null));
  return async function cachedData(path,build,{validate}={}) {
    const url=new URL(path,origin);url.searchParams.set('v',String(build));
    const key=url.href;
    if(pending.has(key))return (await pending.get(key)).clone();
    const request=(async()=>{
      const c=await cache();
      const hit=await c?.match(key).catch(()=>null);
      if(hit)return hit;
      // The build key separates group IDs across daily rebuilds. Only validated successes persist.
      const response=await fetcher(key,{signal:AbortSignal.timeout(90000)});
      if(!response.ok)throw Error(`Could not load search data (${response.status}). Please retry.`);
      if(validate)await validate(response.clone());
      if(c)await c.put(key,response.clone()).catch(()=>{});
      return response;
    })();
    pending.set(key,request);
    try{return (await request).clone()}finally{pending.delete(key)}
  };
}
export const cachedData=createDataCache();

// Saves explore the top neighbourhood once. Scrolling deliberately advances past it.
export function chooseGroups(ranked,downloaded,{advance=false,count=12}={}) {
  const loaded=new Set(downloaded);
  return advance?ranked.filter(n=>!loaded.has(n.id)).slice(0,count):ranked.slice(0,count).filter(n=>!loaded.has(n.id));
}

// Compression may change a strong ETag into its weak form without changing the index.
export function sameETag(a,b){return !!a&&!!b&&a.replace(/^W\//,'')===b.replace(/^W\//,'')}
