const { JSDOM } = require("jsdom");
const html = require("fs").readFileSync("/Users/egd/projects/open-jobs/work/search.html", "utf8");
const errors = [];
const dom = new JSDOM(html, { runScripts: "dangerously", url: "http://127.0.0.1:8765/search.html", beforeParse(w) { w.fetch = () => Promise.resolve({ ok: true, json: async () => ({}) }); w.confirm = () => true; w.scrollIntoView = () => {}; w.HTMLElement.prototype.scrollIntoView = () => {}; } });
dom.window.addEventListener("error", e => errors.push(e.message + " @ " + (e.error && e.error.stack ? e.error.stack.split("\n")[1] : "")));
setTimeout(() => {
  const d = dom.window.document;
  console.log("errors:", errors.length ? errors : "none");
  console.log("job rows rendered:", d.querySelectorAll(".job").length, "| stats:", d.querySelector("#stats").textContent);
  process.exit(0);
}, 3000);
