// Generate the hosted search from the SAME source as the local compiler. Do not hand edit search.html.
import {readFileSync,writeFileSync,mkdirSync,copyFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../../',import.meta.url)), web=root+'backend/web/';
let html=readFileSync(root+'tools/search.html','utf8');
html=html.replace('</head>','<link rel="stylesheet" href="/search-theme.css"></head>');
html=html.replace('<script>','<script type="module">\nimport {getSearch} from "/storage.js";\nimport {createRenderGuard} from "/render-guard.js";\nvar hoverGuard;\nconst hosted=await getSearch();\nif(!hosted?.jobs?.length){location.replace("/");throw Error("No saved search")}\nwindow.OPEN_JOBS_API=location.origin;\n');
const replacements={__JOBS__:'hosted.jobs',__IDEAL__:'hosted.ideal',__IDEAL_TEXT__:'hosted.text',__GROUPS__:'hosted.groups',__GROUPS3__:'hosted.groups',__PREF__:'hosted.ideal.location',__INIT_LABELS__:'{}',__PREF_REMOTE_ONLY__:'hosted.remoteOnly'};
for(const [a,b] of Object.entries(replacements))html=html.replaceAll(a,b);
html=html.replace("const LS='open-jobs:'+(IDEAL.title||'');", "const LS='open-jobs:hosted:'+hosted.id;");
html=html.replace(/function event\(e\)\{[^\n]+/,`function event(e){e.ts=Date.now();if(e.type==='label'&&e.value===1)parent.postMessage({type:'expand',key:e.key},location.origin)}`);
// First searches prefer fresh eligible jobs; retain the user's subsequent choices through refreshes.
html=html.replace("const boardOf=j=>", "if(!st.hostedFreshInitialized){if(JOBS.some(j=>j.el!==false&&freshness(j)==='🌱 fresh'))sel.fr.add('🌱 fresh');st.hostedFreshInitialized=true;st.hostedFilters=Object.fromEntries(FACETS.map(f=>[f,[...sel[f]]]));save()}\nfor(const [f,values] of Object.entries(st.hostedFilters||{}))if(sel[f])sel[f]=new Set(values);\nconst boardOf=j=>");
html=html.replace('function renderFacets(){', 'function renderFacets(){st.hostedFilters=Object.fromEntries(FACETS.map(f=>[f,[...sel[f]]]));save();');
html=html.replace('st.labels={};st.notes={};', 'st.hostedFilters={};st.hostedFreshInitialized=false;st.labels={};st.notes={};');
// A downloadable export works on browsers without clipboard access too.
html=html.replace("navigator.clipboard?.writeText(s);event({type:'export'", "const link=document.createElement('a');link.href=URL.createObjectURL(new Blob([s],{type:'application/json'}));link.download='open-jobs-search.json';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);event({type:'export'");
html=html.replace('Model, labels and comparisons copied to the clipboard as JSON','Search model, labels and notes exported');
html=html.replace("if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')", "if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT'||e.target.tagName==='TEXTAREA')");
html=html.replace("$('#brand-title').textContent=",readFileSync(web+'search-interaction.js','utf8')+"\n$('#brand-title').textContent=");
html=html.replace('function render(reset=true,reason=null){', 'function render(reset=true,reason=null){if(hoverGuard?.defer(reset,reason))return;');
html=html.replace("label(l===1?undefined:1,false)","label(st.labels[j.k]===1?undefined:1,false)").replace("label(l===0?undefined:0,false)","label(st.labels[j.k]===0?undefined:0,false)");
html=html.replace("if(twQueued){twQueued=false;refit()}};","if(twQueued){twQueued=false;refit()}window.onHostedTasteIdle?.()};");
html=html.replace('</script></body>', readFileSync(web+'search-live.js','utf8')+'\n</script></body>');
if(/__[A-Z_]+__/.test(html))throw Error('Unresolved search template token');
html=html.replace('</body>', '<script type="module" src="/search-accessibility.js"></script></body>');
writeFileSync(web+'search.html',html);
mkdirSync(web+'python',{recursive:true});
for(const name of ['locparse.py','salary.py','seniority.py','city_countries.json'])copyFileSync(root+'tools/'+name,web+'python/'+name);
copyFileSync(web+'prepare.py',web+'python/prepare.py');
console.log('Hosted search and exact local parsing rules built.');

// Serve the pinned runtime on our own origin; search startup must not depend on a third-party CDN.
const runtime=web+'runtime/314.0.6/';
mkdirSync(runtime,{recursive:true});
for(const name of ['pyodide.mjs','pyodide.asm.mjs','pyodide.asm.wasm','python_stdlib.zip','pyodide-lock.json'])copyFileSync(root+'backend/node_modules/pyodide/'+name,runtime+name);
