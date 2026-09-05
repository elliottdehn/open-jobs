// Make the shared dialogs keyboard accessible. The search toolbar scrolls with the page.
let lastFocus=null;
for(const modal of document.querySelectorAll('.modal')){
 modal.setAttribute('aria-modal','true');modal.setAttribute('aria-label',modal.querySelector('h2')?.textContent||'Search options');modal.inert=!modal.classList.contains('on');
 new MutationObserver(()=>{const open=modal.classList.contains('on');if(open===!modal.inert)return;modal.inert=!open;if(open){lastFocus=document.activeElement;modal.querySelector('button')?.focus()}else lastFocus?.focus()}).observe(modal,{attributes:true,attributeFilter:['class']});
 modal.addEventListener('keydown',e=>{if(e.key!=='Tab')return;const items=[...modal.querySelectorAll('button,input,select,textarea,[tabindex="0"]')].filter(x=>!x.disabled&&x.getClientRects().length);if(!items.length)return;const first=items[0],last=items.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus()}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus()}});
}
