import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(new URL('../web/search-live.js',import.meta.url),'utf8');
let hovered=false,focused=false,modal=false,workerBusy=false,rendered=0,workerInit=0,interval,intersection;
const old={k:'old',vec:new Float32Array([1,0]),cvec:new Float32Array([.5,-.5])};
const jobs=[old],byKey={old},sent=[];
let more=null;
const listeners={};const document={activeElement:{matches:()=>focused},querySelectorAll:()=>[],querySelector:s=>s.includes('.job:hover')?hovered:s==='.modal.on'?modal:s==='#more'?more:s==='#list'?{lastElementChild:{},addEventListener:()=>{}}:null};
const context=vm.createContext({
 document,console,Float32Array,Object,JSON,Math,
 hosted:{id:'search'},IDEAL:{recipe:'recipe'},JOBS:jobs,byKey,ranked:[old],cur:0,
 GROUPS:{},GROUPS3:{},MEAN:new Float32Array([.5,.5]),cideal:[1,0],
 b64f32:()=>new Float32Array([0,1]),titleFit:()=>.02,centered:()=>new Float32Array([-.5,.5]),
 TW:{postMessage:m=>{assert.equal(m.type,'append');assert.equal(m.cv.byteLength,m.n*2*4);workerInit++}},
 applyEnrich:()=>{},rebuildFacets:()=>{},renderFacets:()=>{},refit:()=>{},render:()=>rendered++,hi:()=>{},toast:()=>{},
 window:{addEventListener:(type,fn)=>listeners[type]=fn},parent:{postMessage:m=>sent.push(m)},location:{origin:'https://example.test'},
 setTimeout:()=>1,clearTimeout:()=>{},scrollY:0,FACETS:[],sel:{},$:()=>({value:''}),
 IntersectionObserver:class{constructor(fn){intersection=fn}observe(){}disconnect(){}},
 MutationObserver:class{observe(){}},
});
Object.defineProperty(context,'twBusy',{get:()=>workerBusy});
vm.runInContext(source,context);interval=()=>vm.runInContext('applyIncomingSearch()',context);
const snapshot={id:'search',ideal:{recipe:'recipe'},jobs:[{k:'old'},{k:'new',v:'fake',sim:.8}],groups:{}};
context.snapshot=snapshot;
vm.runInContext('incomingSearch=snapshot',context);
hovered=true;await interval();assert.equal(jobs.length,1,'defer incoming matches while hovering');hovered=false;
focused=true;await interval();assert.equal(jobs.length,1,'do not disturb typing');
focused=false;modal=true;await interval();assert.equal(jobs.length,1,'do not interrupt comparisons');
modal=false;workerBusy=true;await interval();assert.equal(jobs.length,1,'wait for pending taste scores');
workerBusy=false;await interval();assert.equal(jobs.length,2);assert.equal(jobs[0],old,'retain existing job state');assert.equal(rendered,1);assert.equal(workerInit,1);assert.equal(byKey.new,jobs[1]);
vm.runInContext('incomingSearch=snapshot',context);await interval();assert.equal(jobs.length,2,'deduplicate repeated updates');assert.equal(rendered,1);
let revealed=0;more={click:()=>revealed++};intersection([{isIntersecting:true}]);assert.equal(revealed,1,'reveal cached results automatically');
more=null;intersection([{isIntersecting:true}]);intersection([{isIntersecting:true}]);assert.equal(sent.length,1,'avoid repeated downloads for the same view');assert.equal(sent[0].type,'expand-more');
context.scrollY=1200;intersection([{isIntersecting:true}]);assert.equal(sent.length,2,'continue expanding as browsing advances');
context.snapshot={...snapshot,jobs:[{k:'third',v:'fake',sim:.5}]};
await listeners.message({origin:context.location.origin,source:context.parent,data:{type:'matches-updated',snapshot:context.snapshot}});
await listeners.message({origin:context.location.origin,source:context.parent,data:{type:'matches-updated',snapshot:{...context.snapshot,jobs:[{k:'fourth',v:'fake',sim:.4}]}}});
await interval();assert.equal(jobs.length,4,'coalesced deltas retain every arriving batch');
console.log('PASS: automatic live merge, deduplication, typing/comparison protection, taste worker refresh, infinite reveal and bounded expansion.');
