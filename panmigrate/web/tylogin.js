// 天翼云盘「一键登录」辅助脚本：启动受控 Edge，用户登录官网后自动抓取 cookie。
// 由 Python 后端（server.py）通过 child_process 调用，通过 stdout 返回结果。
//
// 用法：node tylogin.js <edge路径> <profile目录> <调试端口>
// 成功时最后一行输出 JSON：{"ok":true,"cookie":"..."}
// 失败/超时输出：{"ok":false,"error":"..."}
//
// 原理：天翼 cookie 是 v20 app-bound 加密（DPAPI），离线解不开，只能从活着的
// 浏览器进程经 CDP 读取。这里起一个独立 profile 的 Edge，用户在里面登录，
// 脚本轮询 Storage.getCookies 直到拿到 COOKIE_LOGIN_USER（登录会话 cookie）。
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const EDGE = process.argv[2];
const PROFILE = process.argv[3];
const PORT = parseInt(process.argv[4], 10) || 0;
const TARGET_URL = process.argv[5] || 'https://cloud.189.cn/';
const TIMEOUT_MS = parseInt(process.argv[6], 10) || 300000; // 默认 5 分钟

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJson(path) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port: PORT, path }, (res) => {
      let d = '';
      res.on('data', (c) => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { reject(e); } });
    });
    req.on('error', reject);
    req.setTimeout(5000, () => { req.destroy(); reject(new Error('timeout')); });
  });
}

function emit(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

async function main() {
  // 只清理我们这个独立 profile 的残留（不碰用户自己的浏览器）
  try { fs.rmSync(PROFILE, { recursive: true, force: true }); } catch (_) {}
  await sleep(300);

  // 启动受控 Edge（有头模式：用户要在这个可见窗口里手动登录）。
  // 独立 user-data-dir 保证与用户日常用的 Edge 完全隔离。
  const edge = spawn(EDGE, [
    '--remote-debugging-port=' + PORT,
    '--remote-allow-origins=*',
    '--user-data-dir=' + PROFILE,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-sync',
    '--window-size=1000,900',
    TARGET_URL,
  ], { detached: true, stdio: 'ignore' });
  edge.unref();
  const edgePid = edge.pid;

  // 等调试端口就绪
  let up = false;
  for (let i = 0; i < 50; i++) {
    try { await getJson('/json/version'); up = true; break; } catch (_) { await sleep(500); }
  }
  if (!up) { emit({ ok: false, error: '浏览器调试端口未能启动' }); process.exit(1); }
  emit({ phase: 'browser_ready' });

  // 找到目标页面
  let wsUrl = null;
  for (let i = 0; i < 20; i++) {
    try {
      const list = await getJson('/json/list');
      const page = list.find((p) => p.type === 'page' && p.url.includes('cloud.189.cn')) || list.find((p) => p.type === 'page');
      if (page) { wsUrl = page.webSocketDebuggerUrl; break; }
    } catch (_) {}
    await sleep(500);
  }
  if (!wsUrl) { emit({ ok: false, error: '找不到浏览器页面' }); process.exit(1); }

  const WS = global.WebSocket;
  const ws = new WS(wsUrl);
  let msgId = 0;
  const pending = new Map();
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  });
  await new Promise((r) => (ws.onopen = r));
  const send = (method, params) => new Promise((res) => {
    const id = ++msgId;
    pending.set(id, res);
    ws.send(JSON.stringify({ id, method, params }));
  });

  // 通过浏览器级 CDP 优雅关闭（只关我们启动的这个，不误杀用户浏览器）
  let browserWsUrl = null;
  try {
    const ver = await getJson('/json/version');
    browserWsUrl = ver.webSocketDebuggerUrl;
  } catch (_) {}
  async function closeBrowser() {
    if (browserWsUrl) {
      try {
        const bws = new WS(browserWsUrl);
        await new Promise((r) => (bws.onopen = r));
        bws.send(JSON.stringify({ id: 1, method: 'Browser.close' }));
        await sleep(500);
      } catch (_) { /* 忽略关闭失败 */ }
    } else if (edgePid) {
      try { spawn('taskkill', ['/F', '/PID', String(edgePid), '/T'], { stdio: 'ignore' }); } catch (_) {}
    }
  }

  // 轮询 cookie，直到拿到登录会话
  const deadline = Date.now() + TIMEOUT_MS;
  emit({ phase: 'waiting_login' });
  while (Date.now() < deadline) {
    const res = await send('Storage.getCookies', {});
    const cookies = (res.result && res.result.cookies) || [];
    // 天翼登录后关键 cookie：COOKIE_LOGIN_USER（含 cookieUserSession）
    const loginCookie = cookies.find((c) => c.name === 'COOKIE_LOGIN_USER');
    if (loginCookie && loginCookie.value) {
      // 只保留 cloud.189.cn / .189.cn / .dlife.cn 相关的 cookie
      const relevant = cookies.filter((c) =>
        /(\.189\.cn|dlife\.cn)$/.test(c.domain || '') && c.value
      );
      const cookieStr = relevant.map((c) => `${c.name}=${c.value}`).join('; ');
      if (/COOKIE_LOGIN_USER/.test(cookieStr)) {
        emit({ ok: true, cookie: cookieStr, count: relevant.length });
        await closeBrowser();
        process.exit(0);
      }
    }
    await sleep(800);
  }
  emit({ ok: false, error: '等待登录超时（5 分钟未完成登录）' });
  await closeBrowser();
  process.exit(1);
}

main().catch((e) => {
  emit({ ok: false, error: e.message || String(e) });
  process.exit(1);
});
