import { chromium } from 'playwright';
const url = process.argv[2] || 'http://127.0.0.1:9000/?api=127.0.0.1:8000&token=localdev#/live';
const out = process.argv[3] || 'shot.png';
const theme = process.argv[4];
const openPanel = process.argv[5];
const b = await chromium.launch();
const ctx = await b.newContext({ viewport: { width: 1440, height: 810 } });
await ctx.addInitScript(() => { try { localStorage.setItem('ms_help_seen','1'); localStorage.setItem('ms_page','live'); } catch(e){} });
const p = await ctx.newPage();
p.on('dialog', d => d.dismiss().catch(()=>{}));
await p.goto(url, { waitUntil: 'domcontentloaded' });
await p.evaluate(() => { const h=document.getElementById('help'); if(h){h.hidden=true;h.style.display='none';} });
if (theme) await p.evaluate(t => window.__msSetTheme && window.__msSetTheme(t), theme);
await p.waitForTimeout(4200);
if (openPanel) { await p.click('#'+openPanel).catch(()=>{}); await p.waitForTimeout(400); }
else await p.evaluate(() => document.querySelectorAll('.ind-panel').forEach(x => { x.hidden=true; x.style.display='none'; }));
await p.screenshot({ path: out });
await b.close(); console.log('shot ->', out);
