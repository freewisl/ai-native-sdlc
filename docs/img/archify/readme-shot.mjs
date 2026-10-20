// README picture of a delivered Archify page: the diagram only (no viewer buttons, dock or legend), cropped to what
// is drawn, at 2x. The delivered HTML is not changed; the hiding is done in the browser for this capture.
// usage: node readme-shot.mjs <page.html> <out.png> [viewportWidth=1440] [viewportHeight=900] [pad=28]
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { pathToFileURL } from 'node:url';

const archify = process.env.ARCHIFY_HOME || path.join(os.homedir(), '.agents/skills/archify');
const { ChromeVisualBrowser, findChrome } = await import(pathToFileURL(path.join(archify, 'bin/visual-check.mjs')).href);

const [html, out, w = '1440', h = '900', pad = '28'] = process.argv.slice(2);
if (!html || !out) { console.error('usage: node readme-shot.mjs <page.html> <out.png> [w] [h] [pad]'); process.exit(2); }

const chrome = findChrome();
const browser = new ChromeVisualBrowser(chrome.path || chrome);
const cdp = browser.cdp;
async function evaluate(sessionId, expression) {
  const r = await cdp.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sessionId);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result?.value;
}
try {
  const sessionId = await browser.sessionPromise;
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: +w, height: +h, deviceScaleFactor: 2, mobile: false }, sessionId);
  const url = new URL(pathToFileURL(path.resolve(html)).href);
  url.searchParams.set('theme', 'light');
  const loaded = cdp.waitFor('Page.loadEventFired', sessionId);
  await cdp.send('Page.navigate', { url: url.href }, sessionId);
  await loaded;
  const clip = await evaluate(sessionId, `(async function () {
    document.documentElement.setAttribute('data-motion', 'still');
    var panel = document.querySelector('.diagram-container');
    if (panel) panel.setAttribute('data-detail-level', 'read');
    if (document.fonts && document.fonts.ready) await document.fonts.ready.catch(function () {});
    var L = window.Archify || {};
    for (var i = 0; i < 2; i++) {
      if (L.readerLayout && L.readerLayout.whenStable) await L.readerLayout.whenStable();
      if (L.viewerChromeLayout && L.viewerChromeLayout.whenStable) await L.viewerChromeLayout.whenStable();
    }
    var style = document.createElement('style');
    style.textContent = '.toolbar,.diagram-nav,.no-print,[data-legend]{display:none!important}';
    document.head.appendChild(style);
    await new Promise(function (r) { requestAnimationFrame(function () { requestAnimationFrame(r); }); });
    var svg = panel.querySelector('svg');
    var box = svg.getBoundingClientRect();
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    svg.querySelectorAll('rect, path, line, polyline, polygon, text, circle, ellipse, image, use').forEach(function (el) {
      if (el.closest('[data-legend]') || el.closest('defs') || el.closest('pattern') || el.closest('marker')) return;
      var r = el.getBoundingClientRect();
      if (!r.width && !r.height) return;
      if (r.width >= box.width * 0.98 && r.height >= box.height * 0.98) return;   // the background grid
      var cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return;
      x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top); x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
    });
    var p = ${+pad};
    return { x: Math.max(0, x0 - p), y: Math.max(0, y0 - p + window.scrollY), width: (x1 - x0) + 2 * p, height: (y1 - y0) + 2 * p, scale: 1 };
  })()`);
  const shot = await cdp.send('Page.captureScreenshot', { format: 'png', clip, captureBeyondViewport: true }, sessionId);
  fs.writeFileSync(out, Buffer.from(shot.data, 'base64'));
  console.log(JSON.stringify({ out, clip }));
} finally {
  await browser.close();
}
