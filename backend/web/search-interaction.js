// Included before the search's first render. The guard never changes saved ranking evidence.
hoverGuard=createRenderGuard({
 protectedNow:()=>!!document.querySelector('#list .job:hover, #list .job input:focus, #list .job textarea:focus'),
 render:(reset,reason)=>render(reset,reason),
 onDefer:()=>{
  // A yes/no reacts immediately without moving the card out from under the pointer.
  for(const card of document.querySelectorAll('#list .job')){
   const j=ranked[Number(card.dataset.i)];if(!j)continue;
   const value=st.labels[j.k];card.classList.toggle('pos',value===1);card.classList.toggle('neg',value===0);
   card.querySelector('.y').textContent=value===1?'✓ yes':'✓';
   card.querySelector('.n').textContent=value===0?'✗ no':'✗';
  }
 }
});
for(const type of ['pointerout','focusout'])document.querySelector('#list').addEventListener(type,()=>hoverGuard.resume(),{passive:true});
