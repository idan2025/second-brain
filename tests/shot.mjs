#!/usr/bin/env node
// Screenshot the brain UI with headless Chromium over CDP (no playwright needed).
// usage: node shot.mjs <url> <out.png> [width] [height] [actions...]
//   actions: wait:MS  type:TEXT  click:SELECTOR  key:KEY  eval:JS  hover:SELECTOR
import { spawn } from 'node:child_process';
import { writeFileSync, readFileSync } from 'node:fs';

const CHROME = process.env.CHROME || `${process.env.HOME}/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome`;
const [url, out, w = '1400', h = '860', ...actions] = process.argv.slice(2);
if (!url || !out) { console.error('usage: shot.mjs URL OUT.png [w h] [actions]'); process.exit(2); }
const port = 9300 + Math.floor(Math.random() * 500);
const chrome = spawn(CHROME, ['--headless=new', '--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--no-sandbox', '--disable-gpu-sandbox',
  '--hide-scrollbars', '--force-device-scale-factor=1', `--window-size=${w},${h}`, `--remote-debugging-port=${port}`, '--user-data-dir=' + (process.env.TMPDIR || '/tmp') + '/brain-shot-' + port, 'about:blank'],
  { stdio: ['ignore', 'ignore', 'pipe'] });
let errlog = ''; chrome.stderr.on('data', d => errlog += d);
const sleep = ms => new Promise(r => setTimeout(r, ms));
let ws, id = 0; const waiting = new Map(); const logs = [];
async function connect() {
  for (let i = 0; i < 50; i++) {
    try { const r = await fetch(`http://127.0.0.1:${port}/json/list`); const tabs = await r.json(); const t = tabs.find(t => t.type === 'page'); if (t) return t.webSocketDebuggerUrl; } catch { }
    await sleep(100);
  }
  throw new Error('chrome did not come up\n' + errlog);
}
function send(method, params = {}) {
  return new Promise((res, rej) => { const i = ++id; waiting.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
}
try {
  ws = new WebSocket(await connect());
  await new Promise(r => ws.onopen = r);
  ws.onmessage = m => {
    const d = JSON.parse(m.data);
    if (d.id && waiting.has(d.id)) { const { res, rej } = waiting.get(d.id); waiting.delete(d.id); d.error ? rej(new Error(JSON.stringify(d.error))) : res(d.result); }
    else if (d.method === 'Runtime.consoleAPICalled') logs.push(`console.${d.params.type}: ` + d.params.args.map(a => a.value ?? a.description ?? '').join(' '));
    else if (d.method === 'Runtime.exceptionThrown') logs.push('exception: ' + (d.params.exceptionDetails.exception?.description || d.params.exceptionDetails.text));
    else if (d.method === 'Log.entryAdded') logs.push(`log.${d.params.entry.level}: ${d.params.entry.text} ${d.params.entry.url || ''}`);
  };
  await send('Runtime.enable'); await send('Log.enable'); await send('Page.enable'); await send('Network.enable');
  // the LAN password, if one is set: read here, sent as a header, never printed
  try { const pw = readFileSync(`${process.env.HOME}/.config/brain/pw`, 'utf8').trim(); await send('Network.setExtraHTTPHeaders', { headers: { Authorization: 'Basic ' + Buffer.from('brain:' + pw).toString('base64') } }); } catch { }
  await send('Emulation.setDeviceMetricsOverride', { width: +w, height: +h, deviceScaleFactor: 1, mobile: +w < 700 });
  await send('Page.navigate', { url });
  await sleep(1500);
  const evalJs = js => send('Runtime.evaluate', { expression: js, awaitPromise: true, returnByValue: true }).then(r => r.result.value);
  for (const a of actions) {
    const [k, ...rest] = a.split(':'); const v = rest.join(':');
    if (k === 'wait') await sleep(+v);
    else if (k === 'type') { await evalJs(`document.getElementById('search').focus()`); await send('Input.insertText', { text: v }); await evalJs(`document.getElementById('search').dispatchEvent(new Event('input',{bubbles:true}))`); }
    else if (k === 'click') await evalJs(`document.querySelector(${JSON.stringify(v)}).click()`);
    else if (k === 'key') { await send('Input.dispatchKeyEvent', { type: 'keyDown', key: v, code: v, windowsVirtualKeyCode: v === 'Escape' ? 27 : v === 'Enter' ? 13 : 0 }); await send('Input.dispatchKeyEvent', { type: 'keyUp', key: v, code: v }); }
    else if (k === 'hover') { const r = await evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(v)});const b=e.getBoundingClientRect();return [b.x+b.width/2,b.y+b.height/2]})()`); await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x: r[0], y: r[1] }); }
    else if (k === 'mouse') { const [x, y] = v.split(',').map(Number); await send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y }); }
    else if (k === 'eval') logs.push('eval: ' + JSON.stringify(await evalJs(v)));
  }
  const shot = await send('Page.captureScreenshot', { format: 'png' });
  writeFileSync(out, Buffer.from(shot.data, 'base64'));
  console.log('wrote', out); logs.forEach(l => console.log(' ', l.slice(0, 300)));
} catch (e) { console.error(e); process.exitCode = 1; }
finally { chrome.kill('SIGKILL'); }
