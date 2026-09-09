// The /data directory uses the same forest palette as the search page and manual.
// Everything is inline in the response: no fonts, scripts, or CSS need to load first.
export const dataIndexStyle = `
:root {
  color-scheme: light;
  --bg: #f9faf7; --paper: #fff; --ink: #263b2e; --text: #445249;
  --soft: #68776c; --rule: #d9e2d6; --panel: #edf3e9;
  --acc: #386d3d; --acc-ink: #2a6438; --forest: #142a20;
  --mint: #d3efb4; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
  --serif: Georgia, "Times New Roman", serif;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; scroll-padding-top: 32px; }
body { margin: 0; background: var(--bg); color: var(--text); font: 16px/1.75 var(--sans); -webkit-font-smoothing: antialiased; }
::selection { background: #d0e6bd; color: #203d25; }
a { color: var(--acc-ink); text-decoration: none; text-underline-offset: 4px; }
a:hover { text-decoration: underline; }
button, input { font: inherit; }
button { cursor: pointer; }
:focus-visible { outline: 3px solid #7caa63; outline-offset: 5px; }
[hidden] { display: none !important; }
.skip { position: fixed; top: 12px; left: 12px; z-index: 10; padding: 9px 18px; color: var(--forest); background: var(--mint); transform: translateY(-180%); }
.skip:focus { transform: none; }
.shell { max-width: 1280px; margin-inline: auto; padding-inline: 48px; }
.masthead { background: var(--forest); color: #ecf1e7; border-bottom: 1px solid #374e39; }
.top { min-height: 92px; display: flex; align-items: center; justify-content: space-between; gap: 24px; border-bottom: 1px solid #354938; }
.brand { display: inline-flex; align-items: center; gap: 11px; color: #f2f4ea; font: 28px/1 var(--serif); letter-spacing: -1px; }
.brand:hover { text-decoration: none; color: var(--mint); }
.brand svg { width: 25px; height: 28px; stroke: var(--mint); stroke-width: 1.4; fill: none; }
.brand em { font-style: normal; color: var(--mint); }
.header-links { display: flex; align-items: center; gap: 27px; font-size: 14px; }
.header-links > a { color: #c6d5c6; }
.gh { display: inline-flex; align-items: center; gap: 9px; padding: 8px 12px; border: 1px solid #4d624b; border-radius: 5px; color: #e9f0e2 !important; white-space: nowrap; font-size: 13px; }
.gh:hover { border-color: #a1be8a; text-decoration: none; background: #233d2b; }
.gh svg { width: 16px; height: 16px; fill: currentColor; }
.gh b { font: 12px var(--mono); color: var(--mint); border-left: 1px solid #4d624b; padding-left: 10px; font-variant-numeric: tabular-nums; }
.hero-head { display: grid; grid-template-columns: 1fr 1fr; gap: 65px; align-items: end; padding: 47px 0 35px; }
.path { display: flex; align-items: center; gap: 13px; color: #b9cfaa; font: 12px/1.5 var(--mono); margin-bottom: 18px; }
.path:before { content: ''; width: 22px; height: 1px; background: #8ca77a; }
h1 { font: 400 clamp(48px,5.3vw,72px)/1.03 var(--serif); letter-spacing: -.055em; margin: 0; color: #f1f3e9; }
.lede { font-size: 17px; line-height: 1.8; color: #bcccbb; margin: 0; max-width: 49ch; }
.access { display: flex; gap: 22px; margin: 19px 0 0; font: 11px/1.6 var(--mono); color: #d0e0c1; letter-spacing: .03em; }
.access span { display: inline-flex; align-items: center; gap: 7px; }
.access span:before { content: ''; width: 4px; height: 4px; border-radius: 50%; background: #a6cb87; }
.stats { display: grid; grid-template-columns: 1.1fr .8fr 1fr 1.35fr; gap: 28px; padding: 24px 0 29px; border-top: 1px solid #354938; }
.stats div { min-width: 0; }
.stats b { display: block; font: 400 25px/1.5 var(--mono); letter-spacing: -.055em; color: #e2eddb; font-variant-numeric: tabular-nums; }
.stats .date-stat { font-size: 20px; letter-spacing: -.04em; padding-top: 5px; }
.stats .built-stat { font-size: 16px; letter-spacing: -.04em; padding-top: 9px; padding-bottom: 4px; }
.stats span { display: block; margin-top: 4px; color: #9eb298; font-size: 12px; }
.layout { display: grid; grid-template-columns: 174px minmax(0,1fr); gap: 56px; align-items: start; padding-top: 52px; }
.contents { position: sticky; top: 30px; font-size: 14px; }
.contents-label { font: 11px/1.6 var(--mono); text-transform: uppercase; letter-spacing: .1em; color: var(--soft); margin-bottom: 16px; }
.contents a { display: flex; gap: 12px; padding: 9px 0; color: var(--soft); }
.contents a:hover, .contents a[aria-current] { color: var(--acc-ink); text-decoration: none; }
.contents a[aria-current] { font-weight: 600; }
.contents a span { font: 11px/24px var(--mono); color: #7a9372; }
.contents .technical-note { border-top: 1px solid var(--rule); margin-top: 23px; padding-top: 18px; font: 11px/1.9 var(--mono); color: var(--soft); }
main { min-width: 0; padding-bottom: 30px; }
section { margin: 0 0 58px; scroll-margin-top: 35px; }
section + section { border-top: 1px solid var(--rule); padding-top: 38px; }
.eyebrow { font: 11px/1.6 var(--mono); letter-spacing: .1em; text-transform: uppercase; color: var(--acc); margin: 0 0 10px; }
h2 { font: 400 37px/1.2 var(--serif); letter-spacing: -.04em; color: var(--ink); margin: 0 0 21px; text-wrap: balance; }
h3 { font-size: 18px; line-height: 1.5; font-weight: 600; color: var(--ink); margin: 31px 0 10px; letter-spacing: -.015em; }
p { margin: 12px 0; max-width: 78ch; }
strong, b { color: var(--ink); }
.soft { color: var(--soft); font-size: 14px; }
code { font: .83em/1.6 var(--mono); background: var(--panel); color: var(--acc-ink); padding: 2px 5px; border-radius: 3px; overflow-wrap: anywhere; }
pre { font: 12px/1.85 var(--mono); color: #dce9d3; background: #192d21; padding: 19px 23px; overflow: auto; margin: 0; max-width: 100%; tab-size: 2; scrollbar-width: thin; }
pre.hero { font-size: 14px; line-height: 2.15; padding: 22px 24px 24px; }
.ln { display: inline-block; width: 2.4em; color: #81997a; user-select: none; }
.code-panel { background: #192d21; border: 1px solid #3a5138; border-radius: 7px; overflow: hidden; margin: 20px 0; }
.code-head { display: flex; justify-content: space-between; align-items: center; min-height: 41px; padding: 8px 15px 8px 23px; color: #a3bf95; font: 10px/1.5 var(--mono); letter-spacing: .1em; text-transform: uppercase; border-bottom: 1px solid #354b33; }
.copy { padding: 4px 9px; border-radius: 4px; border: 1px solid #4c6544; color: #d7e7cb; background: transparent; font: 12px/1.4 var(--sans); letter-spacing: 0; text-transform: none; }
.copy:hover { background: #304b2c; border-color: #8dab74; }
.copy:focus-visible { outline-color: var(--mint); }
.export-note { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px 18px; font: 12px/1.7 var(--mono); color: var(--soft); padding: 0 1px 9px; }
.export-note b { font-weight: 500; color: var(--acc-ink); }
.inc { margin-top: 20px; border: 1px solid var(--rule); border-radius: 6px; background: var(--paper); overflow: hidden; }
.inc details { margin: 0; border: 0; border-radius: 0; background: transparent; }
.inc details + details { border-top: 1px solid var(--rule); }
.inc summary { padding: 16px 19px; font-size: 14px; font-weight: 500; }
.inc .code-panel { margin: 0 15px 15px; }
.inc pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 16px 20px; line-height: 1.85; }
details { margin-top: 20px; background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; }
summary { cursor: pointer; padding: 17px 20px; color: var(--acc-ink); font-size: 14px; list-style: none; display: flex; align-items: baseline; gap: 14px; }
summary::-webkit-details-marker { display: none; }
summary:after { content: '+'; margin-left: auto; font: 17px/1 var(--mono); color: #7d9474; }
details[open] > summary:after { content: '−'; }
summary:hover { background: var(--panel); }
details[open] > summary { border-bottom: 1px solid var(--rule); margin-bottom: 16px; }
.inc details[open] > summary { border: 0; margin: 0; }
details > .code-panel { margin: 16px; }
details > .wrap { border-radius: 0; border-inline: 0; border-bottom: 0; }
.cols { font: 12px/2.05 var(--mono); color: var(--soft); max-width: none; padding: 18px 21px; background: var(--panel); border-left: 2px solid #abc294; border-radius: 0 4px 4px 0; overflow-wrap: anywhere; }
.wrap { overflow: auto; margin-top: 21px; border: 1px solid var(--rule); border-radius: 6px; background: var(--paper); scrollbar-width: thin; }
table { border-collapse: collapse; width: 100%; font-size: 13px; font-variant-numeric: tabular-nums; line-height: 1.65; }
th, td { text-align: left; padding: 13px 15px; border-bottom: 1px solid var(--rule); vertical-align: top; white-space: nowrap; }
th { background: var(--panel); font: 10px/1.5 var(--mono); letter-spacing: .07em; text-transform: uppercase; color: var(--soft); font-weight: 500; }
tr:last-child td { border-bottom: 0; }
tbody tr:nth-child(even) { background: #fafcf8; }
tbody tr:hover { background: #eff5e9; }
td.n, th.n { text-align: right; }
td a { font-family: var(--mono); font-size: 12px; }
.dls { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 8px; margin-top: 14px; }
.dls .dl { display: flex; flex-direction: column; gap: 2px; min-width: 0; padding: 9px 12px; border: 1px solid var(--rule); border-radius: 6px; background: var(--paper); text-decoration: none; color: var(--ink); }
.dls .dl:hover { border-color: var(--acc); text-decoration: none; }
.dls .dl b { font: 500 13px/1.4 var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dls .dl span { font: 11px/1.4 var(--mono); color: var(--soft); white-space: nowrap; }
.dls .dl span:before { content: '↓ '; color: var(--acc); }
td .part-link { display: block; padding-top: 3px; }
.file-size { display: block; font: 11px/1.7 var(--mono); color: var(--soft); margin-bottom: 5px; }
.diff-date { font-family: var(--mono); font-size: 12px; color: var(--ink); }
.diff-date small { display: block; font: 10px/1.7 var(--mono); color: var(--soft); margin-top: 4px; }
.added { color: #377041; }.removed { color: #996e52; }
.latest { display: grid; grid-template-columns: 1fr 1fr; gap: 15px 25px; padding: 21px 24px; margin: 22px 0; background: var(--panel); border: 1px solid var(--rule); border-radius: 6px; font-size: 14px; }
.latest .period { grid-column: 1 / -1; display: flex; justify-content: space-between; gap: 12px; color: var(--soft); font: 11px/1.6 var(--mono); }
.delta-counts { display: flex; flex-wrap: wrap; gap: 9px 23px; grid-column: 1 / -1; }
.delta-counts b { font: 21px/1.5 var(--mono); display: block; color: var(--ink); letter-spacing: -.05em; }
.delta-counts span { color: var(--soft); font-size: 12px; }
.delta-counts .added b { color: var(--acc-ink); }
.feed-head { padding: 14px 18px; border: 1px solid var(--rule); border-radius: 5px; font: 12px/1.8 var(--mono); background: var(--paper); margin: 18px 0; overflow-wrap: anywhere; }
.feed-head .soft { font: 11px/1.8 var(--mono); margin-top: 5px; }
.file-index { margin: 23px 0; border-top: 1px solid var(--rule); }
.file-entry { display: grid; grid-template-columns: minmax(0,1fr) minmax(0,1.35fr); gap: 25px; padding: 19px 0; border-bottom: 1px solid var(--rule); align-items: baseline; }
.file-entry > a, .file-entry > code { font: 13px/1.65 var(--mono); overflow-wrap: anywhere; background: none; padding: 0; }
.file-entry p { margin: 0; font-size: 14px; color: var(--soft); }
.estimators { display: flex; flex-wrap: wrap; gap: 8px; margin: 15px 0 20px; }
.estimators a { border: 1px solid var(--rule); padding: 5px 12px; font-size: 13px; border-radius: 4px; background: var(--paper); }
.estimators a:hover { border-color: #8ea57c; background: var(--panel); text-decoration: none; }
footer { border-top: 1px solid var(--rule); padding: 25px 0 38px; font-size: 13px; color: var(--soft); }
footer p { max-width: none; font-size: 12px; margin: 10px 0; }
.footer-line { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 10px 20px; }
.footer-links { display: flex; flex-wrap: wrap; gap: 22px; }
.empty { padding: 22px; color: var(--soft); font-size: 14px; white-space: normal; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark; --bg: #142018; --paper: #19281d; --ink: #dfe8d8; --text: #c0cebb; --soft: #9aaf93; --rule: #354b33; --panel: #213523; --acc: #b0cf92; --acc-ink: #c0dea4; }
  tbody tr:nth-child(even) { background: #1b2b1f; } tbody tr:hover { background: #283f29; }
  .contents a span { color: #9db78c; }.added { color: #abc994; }.removed { color: #cead90; }
}
@media (max-width: 1050px) {
  .shell { padding-inline: 32px; }.layout { grid-template-columns: 145px minmax(0,1fr); gap: 32px; }
  .hero-head { gap: 35px; }.stats { gap: 18px; }.stats b { font-size: 23px; }.stats .date-stat { font-size: 18px; }.stats .built-stat { font-size: 13px; padding-top: 12px; }
  .contents { font-size: 13px; }.contents a { gap: 8px; }
}
@media (max-width: 780px) {
  .shell { padding-inline: 25px; }.top { min-height: 78px; }.hero-head { grid-template-columns: 1fr; gap: 24px; padding-top: 33px; }
  .lede { max-width: 65ch; font-size: 16px; }.access { margin-top: 15px; }.stats { grid-template-columns: 1fr 1fr; gap: 20px 28px; }
  .stats b { font-size: 24px; }.stats .date-stat { font-size: 20px; padding-top: 0; }.stats .built-stat { font-size: 15px; padding-top: 6px; }
  .layout { display: block; padding-top: 0; }.contents { position: static; display: flex; flex-wrap: wrap; gap: 5px 24px; padding: 19px 0; margin-bottom: 32px; border-bottom: 1px solid var(--rule); }
  .contents-label, .technical-note { display: none; }.contents a { padding: 3px 0; font-size: 13px; }
  h2 { font-size: 34px; }.code-panel { margin-top: 19px; }.header-links { gap: 17px; }
}
@media (max-width: 480px) {
  .shell { padding-inline: 20px; }.brand { font-size: 25px; }.brand svg { width: 22px; }.header-links > .search-link { display: none; }.gh { padding: 7px 9px; font-size: 12px; }
  .hero-head { padding: 29px 0 27px; gap: 22px; }h1 { font-size: 52px; }.path { margin-bottom: 16px; }
  .stats { gap: 18px 17px; }.stats b { font-size: 22px; }.stats .built-stat { font-size: 12px; padding-top: 7px; }.stats span { font-size: 11px; }
  .contents { gap: 5px 17px; }.contents a { font-size: 12px; }.contents a span { font-size: 10px; }
  h2 { font-size: 31px; }h3 { font-size: 17px; }section { margin-bottom: 40px; }section + section { padding-top: 29px; }
  pre.hero { font-size: 12px; padding: 16px; }.ln { width: 2em; }pre { padding: 16px; }.code-head { padding-left: 16px; }
  .export-note { font-size: 11px; }.inc summary { padding: 15px; }.inc .code-panel { margin: 0 10px 10px; }.inc pre { padding: 14px; font-size: 11px; }
  .cols { font-size: 11px; padding: 14px 17px; }.latest { padding: 17px; }.latest .period { flex-direction: column; gap: 3px; }
  .delta-counts { gap: 8px 21px; }.delta-counts b { font-size: 20px; }.file-entry { grid-template-columns: 1fr; gap: 7px; }
  th, td { padding: 11px 12px; }.wrap:before { content: 'Scroll to see all columns →'; display: block; padding: 8px 12px; color: var(--soft); font: 10px/1.6 var(--mono); border-bottom: 1px solid var(--rule); }
}
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
@media print {
  body { background: white; color: #222; font-size: 10pt; }.shell { max-width: none; padding: 0; }.masthead { background: white; color: #222; }
  .top, .contents, .copy, .skip { display: none; }.hero-head { padding: 0 0 20px; }h1 { color: #222; font-size: 35pt; }.lede { color: #444; font-size: 11pt; }
  .path, .access, .stats b, .stats span { color: #444; }.layout { display: block; padding-top: 25px; }h2 { font-size: 23pt; }h2,h3 { break-after: avoid; }
  .code-panel, pre { background: #f0f3ed; color: #222; border-color: #ccc; }pre { white-space: pre-wrap; overflow-wrap: anywhere; }.code-head { color: #444; }
  .wrap { overflow: visible; }.wrap:before { display: none; }table { font-size: 8pt; }td,th { white-space: normal; overflow-wrap: anywhere; padding: 7px; }td a { font-size: 8pt; }
}
`;
