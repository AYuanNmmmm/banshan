// 把海报 HTML 里每一张 <section class="poster"> 导出成 PNG
//
//   用法：npx electron --no-sandbox posters/shot.js
//   可选：posters/shot.js --html poster.html --out . --size 1080x1440
//
// 页数自动探测（数 section.poster 的数量），增删页不用改脚本。

const path = require('path');
const fs = require('fs');

let electron;
try {
  electron = require('electron');
} catch (e) {
  console.error('[错误] 没找到 electron。请用 `npx electron --no-sandbox <本脚本>` 运行，');
  console.error('       并确保当前项目能解析到 electron（本地 node_modules 里有，或允许 npx 临时拉取）。');
  process.exit(1);
}
const { app, BrowserWindow } = electron;

// ---- 解析命令行参数 ----
const argv = process.argv.slice(2);
function arg(name, def) {
  const i = argv.indexOf('--' + name);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : def;
}
const HTML = path.resolve(arg('html', path.join(__dirname, 'poster.html')));
const OUT = path.resolve(arg('out', __dirname));
const [W, H] = arg('size', '1080x1440').split('x').map(Number);

if (!fs.existsSync(HTML)) {
  console.error('[错误] 找不到 HTML：' + HTML);
  process.exit(1);
}
if (!fs.existsSync(OUT)) fs.mkdirSync(OUT, { recursive: true });

app.disableHardwareAcceleration();
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
app.commandLine.appendSwitch('in-process-gpu');

app.whenReady().then(async () => {
  const w = new BrowserWindow({
    width: W,
    height: H + 120,          // 留余量，实际尺寸靠 clip 精确控制
    show: false,
    backgroundColor: '#070B14',
    webPreferences: { deviceScaleFactor: 1, offscreen: true },
  });

  await w.loadFile(HTML);
  await new Promise((r) => setTimeout(r, 800));

  // capturePage 受视口高度限制（实测上限 1400），改用 CDP 突破视口
  try { w.webContents.debugger.attach('1.3'); } catch (e) { /* 已 attach */ }

  const n = await w.webContents.executeJavaScript(
    `document.querySelectorAll('section.poster').length`
  );
  if (!n) { console.error('[错误] HTML 里没有 <section class="poster">'); app.exit(1); }
  console.log(`共 ${n} 张，画布 ${W}x${H}`);

  for (let i = 1; i <= n; i++) {
    const ok = await w.webContents.executeJavaScript(
      `(function(){ if (typeof show === 'function') { show(${i}); return true; }
         // 没有 show() 就自己切：只保留第 i 个
         var ps = document.querySelectorAll('section.poster');
         for (var k=0;k<ps.length;k++) ps[k].classList.toggle('on', k === ${i} - 1);
         return true; })()`
    );
    if (!ok) throw new Error('切换第 ' + i + ' 张失败');
    await new Promise((r) => setTimeout(r, 400));

    const { data } = await w.webContents.debugger.sendCommand('Page.captureScreenshot', {
      format: 'png',
      captureBeyondViewport: true,
      clip: { x: 0, y: 0, width: W, height: H, scale: 1 },
    });
    const buf = Buffer.from(data, 'base64');
    const file = path.join(OUT, `poster-${i}.png`);
    fs.writeFileSync(file, buf);
    console.log(`  poster-${i}.png  ${W}x${H}  ${(buf.length / 1024).toFixed(0)} KB`);
  }

  console.log('导出完成 → ' + OUT);
  app.quit();
});
