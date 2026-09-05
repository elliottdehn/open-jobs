// Included in the generated search module so incoming jobs use its existing taste model.
let incomingSearch = null, applyingSearch = false;
async function applyIncomingSearch() {
  if (!incomingSearch || applyingSearch || twBusy || document.querySelector('.modal.on') || document.querySelector('#list .job:hover, #list .job input:focus, #list .job textarea:focus') ||
      document.activeElement?.matches('input,textarea,select')) return;
  applyingSearch = true;
  try {
    const snapshot = incomingSearch;
    incomingSearch = null;
    if (snapshot.id !== hosted.id || snapshot.ideal.recipe !== IDEAL.recipe) return;
    const added = snapshot.jobs.filter(j => !byKey[j.k]);
    if (!added.length) return;
    const selected = ranked[cur]?.k;
    for (const j of added) {
      j.vec = b64f32(j.v); delete j.v;
      j.pre = j.sim + titleFit(j); j.score = j.pre;
      // Keep the original centering space: learned weights and comparisons remain valid.
      j.cvec = centered(j.vec);
      JOBS.push(j); byKey[j.k] = j;
    }
    Object.assign(GROUPS, snapshot.groups);
    Object.assign(GROUPS3, snapshot.groups);
    if (TW) {
      const cv = new Float32Array(added.length * MEAN.length);
      added.forEach((j, i) => cv.set(j.cvec, i * MEAN.length));
      TW.postMessage({type:'append',cv:cv.buffer,n:added.length},[cv.buffer]);
    }
    applyEnrich(); rebuildFacets(); renderFacets(); refit(); render(false);
    const index = ranked.findIndex(j => j.k === selected);
    if (index >= 0) {cur = index; hi()}
    toast(`${added.length.toLocaleString()} new matches added`);
  } finally {applyingSearch = false}
}
window.addEventListener('message', async e => {
  if (e.origin !== location.origin || e.source !== parent || e.data?.type !== 'matches-updated') return;
  const snapshot=e.data.snapshot||await getSearch();
  // Merge coalesced deltas so a hovered card can hold multiple arriving batches safely.
  if(incomingSearch&&incomingSearch.id===snapshot.id){
   const jobs=new Map(incomingSearch.jobs.map(j=>[j.k,j]));for(const j of snapshot.jobs)jobs.set(j.k,j);
   incomingSearch={...snapshot,jobs:[...jobs.values()],groups:{...incomingSearch.groups,...snapshot.groups}};
  }else incomingSearch=snapshot;
  scheduleIncomingSearch();
});
// Updates wait for typing/comparisons to finish, then appear without a reload or confirmation.
let incomingTimer;
function scheduleIncomingSearch(){clearTimeout(incomingTimer);incomingTimer=setTimeout(applyIncomingSearch,140)}
for(const type of ['pointerout','focusout'])document.querySelector('#list').addEventListener(type,scheduleIncomingSearch,{passive:true});
window.onHostedTasteIdle=scheduleIncomingSearch;
for(const modal of document.querySelectorAll('.modal'))new MutationObserver(scheduleIncomingSearch).observe(modal,{attributes:true,attributeFilter:['class']});

// Reveal cached results as you approach the bottom, then fetch the next neighbourhood.
let observedEnd = null, lastExpansionView = null;
const moreObserver = new IntersectionObserver(entries => {
  if (!entries.some(e => e.isIntersecting) || document.querySelector('.modal.on') || document.activeElement?.matches('input,textarea,select')) return;
  const more = document.querySelector('#more');
  if (more) {more.click(); return}
  const view = JSON.stringify([Math.round(scrollY / 100), $('#q').value, $('#show').value,
    Object.fromEntries(FACETS.map(f => [f,[...sel[f]]]))]);
  if (view === lastExpansionView) return;
  lastExpansionView = view;
  parent.postMessage({type:'expand-more'}, location.origin);
}, {rootMargin:'0px 0px 600px 0px'});
function observeEnd() {
  const end = document.querySelector('#more') || document.querySelector('#list')?.lastElementChild;
  if (end === observedEnd) return;
  moreObserver.disconnect(); observedEnd = end;
  if (end) moreObserver.observe(end);
}
new MutationObserver(observeEnd).observe(document.querySelector('#list'), {childList:true});
observeEnd();
