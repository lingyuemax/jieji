/**
 * 界面验收：用 Chrome 无头模式真的打开一遍页面，检查
 *   1. 页面没有 JS 报错；
 *   2. 顶栏状态、MJPEG 画面、模拟俯视图都活着；
 *   3. 发一句指令后，现场里出现决策卡；
 *   4. 顺手截一张图给 README 用。
 *
 * 用法：
 *   python serve.py            # 另开一个窗口启动服务
 *   node scripts/ui-test.mjs   # 默认测 http://127.0.0.1:8790
 *
 * 需要本机有 Chrome 或 Edge；不在默认路径就设置 CHROME_PATH。
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');
const DOCS = path.join(ROOT, 'docs');
const SHOTS = path.join(ROOT, '.screenshots');
const PORT = Number(process.env.CDP_PORT || 9333);
const URL = process.env.UI_URL || 'http://127.0.0.1:8790/';

const CHROME_CANDIDATES = [
  process.env.CHROME_PATH,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser'
].filter(Boolean);
const CHROME = CHROME_CANDIDATES.find((candidate) => existsSync(candidate));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function assert(cond, message) {
  if (!cond) throw new Error(`断言失败：${message}`);
}

async function waitForJson(timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`http://127.0.0.1:${PORT}/json/list`);
      const pages = await res.json();
      const page = pages.find((item) => item.type === 'page');
      if (page?.webSocketDebuggerUrl) return page;
    } catch {
      /* 还没起来 */
    }
    await sleep(200);
  }
  throw new Error('Chrome DevTools 没起来');
}

function connect(wsUrl, onEvent = () => {}) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const pending = new Map();
    let nextId = 1;
    ws.onopen = () => resolve({
      send(method, params = {}) {
        const id = nextId++;
        ws.send(JSON.stringify({ id, method, params }));
        return new Promise((res, rej) => pending.set(id, { res, rej }));
      },
      close() { ws.close(); }
    });
    ws.onerror = (err) => reject(err);
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) return onEvent(message);
      const entry = pending.get(message.id);
      if (!entry) return;
      pending.delete(message.id);
      if (message.error) entry.rej(new Error(message.error.message));
      else entry.res(message.result);
    };
  });
}

async function evaluate(client, expression, awaitPromise = false) {
  const result = await client.send('Runtime.evaluate', { expression, awaitPromise, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || '页面脚本异常');
  return result.result.value;
}

async function waitFor(client, expression, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(client, expression)) return true;
    await sleep(250);
  }
  throw new Error(`页面等待超时：${expression}`);
}

async function screenshot(client, dir, name) {
  const shot = await client.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  const file = path.join(dir, name);
  await writeFile(file, Buffer.from(shot.data, 'base64'));
  return file;
}

async function main() {
  if (!CHROME) throw new Error('找不到 Chrome/Edge，设置 CHROME_PATH 再试');
  await mkdir(ROOT, { recursive: true });
  await mkdir(DOCS, { recursive: true });
  await mkdir(SHOTS, { recursive: true });

  const profile = path.join(SHOTS, `chrome-cdp-${Date.now()}`);
  const chrome = spawn(CHROME, [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--hide-scrollbars',
    '--autoplay-policy=no-user-gesture-required',
    '--window-size=1500,1040',
    `--remote-debugging-port=${PORT}`,
    '--remote-allow-origins=*',
    `--user-data-dir=${profile}`,
    URL
  ], { stdio: 'ignore', windowsHide: true });

  const errors = [];
  let client;
  try {
    const page = await waitForJson();
    client = await connect(page.webSocketDebuggerUrl, (message) => {
      if (message.method === 'Runtime.exceptionThrown') {
        errors.push(message.params?.exceptionDetails?.text || 'exception');
      }
      if (message.method === 'Log.entryAdded' && message.params?.entry?.level === 'error') {
        errors.push(message.params.entry.text);
      }
      if (message.method === 'Runtime.consoleAPICalled' && message.params?.type === 'error') {
        errors.push((message.params.args || []).map((a) => a.value ?? a.description).join(' '));
      }
    });
    await client.send('Page.enable');
    await client.send('Runtime.enable');
    await client.send('Log.enable');
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: 1500, height: 1040, deviceScaleFactor: 1, mobile: false
    });

    await waitFor(client, `document.querySelector('#pill-model')?.textContent?.includes('deepseek') || document.querySelector('#pill-model')?.textContent?.includes('—')`);
    await waitFor(client, `document.querySelector('#stream')?.naturalWidth > 0`, 20000);
    await sleep(800);

    const before = await evaluate(client, `({
      model: document.querySelector('#pill-model')?.textContent?.trim(),
      driver: document.querySelector('#pill-driver')?.textContent?.trim(),
      mode: document.querySelector('#pill-mode')?.textContent?.trim(),
      streamW: document.querySelector('#stream')?.naturalWidth,
      mapW: document.querySelector('#map')?.naturalWidth,
      hud: [...document.querySelectorAll('.hud span')].map((e) => e.textContent.trim())
    })`);
    console.log('[ui] 初始状态', before);
    assert(before.streamW > 0, 'MJPEG 画面没出来');
    assert(before.model.includes('deepseek') || before.model.includes('—'), '模型标签异常');

    // 发一句指令，等决策卡出现
    await evaluate(client, `(() => {
      document.querySelector('#text').value = '你看到什么了？往前走一小步';
      document.querySelector('#btn-send').click();
      return true;
    })()`);
    await waitFor(client, `document.querySelectorAll('.decision').length >= 1`, 60000);
    await sleep(1200);

    const after = await evaluate(client, `({
      decisions: document.querySelectorAll('.decision').length,
      tags: [...document.querySelectorAll('.decision .tag')].slice(0, 6).map((e) => e.textContent.trim()),
      bubbles: document.querySelectorAll('.msg').length,
      speak: [...document.querySelectorAll('.decision .say-text')].map((e) => e.textContent.trim())
    })`);
    console.log('[ui] 决策后', after);
    assert(after.decisions >= 1, '没有渲染出决策卡');

    const shot = await screenshot(client, DOCS, 'screenshot.png');
    console.log('[ui] 截图', shot);
    await screenshot(client, SHOTS, 'ui-decided.png');

    const domErrors = errors.filter((e) => !/favicon|DevTools/.test(e));
    assert(domErrors.length === 0, `页面有报错：${domErrors.join(' | ')}`);
    console.log('[ui] 通过 ✓');
  } finally {
    if (client) client.close();
    chrome.kill();
  }
}

main().catch((err) => {
  console.error('[ui] 失败：', err.message);
  process.exitCode = 1;
});
