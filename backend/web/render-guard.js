// Coalesce background reorders while a person is pointing at or editing a result.
export function createRenderGuard({protectedNow,render,onDefer=()=>{},delay=120,schedule=setTimeout,cancel=clearTimeout}) {
 let pending=null,timer=null;
 const flush=()=>{
  timer=null;
  if(!pending||protectedNow())return;
  const next=pending;pending=null;render(next.reset,next.reason);
 };
 return {
  defer(reset,reason){
   // Explicit search/filter/order changes take effect immediately.
   if(['search','filter','order'].includes(reason)){pending=null;return false}
   if(!protectedNow()){pending=null;return false}
   pending={reset:!!reset||!!pending?.reset,reason:reason||pending?.reason};onDefer();return true;
  },
  resume(){if(timer)cancel(timer);timer=schedule(flush,delay)},
  flush,
 };
}
