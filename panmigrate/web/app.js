const $ = (sel, root = document) => root.querySelector(sel);
const ROLE_LABEL = { source: "源", target: "目标" };
const STATE_LABEL = {
  pending: "待传", running: "传输中", done: "完成",
  skipped: "跳过", failed: "失败", paused: "已暂停",
};

let S = null;
let providers = [];
const builtFor = { source: "", target: "" };
let sigTasks = "";
let sigLogs = "";
let browse = { role: "source", path: "/", grid: false, extSel: new Set() };
// 目标端只需要选目录，不列文件 —— 单独一套轻量状态
const tdir = { path: "/", items: [] };
let scanPolling = false;

/* ---------------- 本地偏好（刷新后保持） ---------------- */
const LS = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* 隐私模式下忽略 */ }
  },
};
const LS_BROWSE_GRID = "pm.browseGrid";
const LS_FOLD = "pm.panelFold2";       // 用户最后一次的选择（v2：默认折叠）
const LS_FOLD_SET = "pm.panelFoldSet"; // 用户手动点过（之后不再自动收起）
let panelFold = LS.get(LS_FOLD, { source: true, target: true });
let panelFoldSet = LS.get(LS_FOLD_SET, { source: false, target: false });

/* ---------------- 文件类型图标 ---------------- */
const FILE_ICONS = {
  dir: '<svg viewBox="0 0 24 24" fill="none"><path d="M3 7.4A2.4 2.4 0 015.4 5h3.1l1.9 2.1h7.2A2.4 2.4 0 0120 9.5v7.1A2.4 2.4 0 0117.6 19H5.4A2.4 2.4 0 013 16.6V7.4z" fill="currentColor" opacity=".16"/><path d="M3 7.4A2.4 2.4 0 015.4 5h3.1l1.9 2.1h7.2A2.4 2.4 0 0120 9.5v7.1A2.4 2.4 0 0117.6 19H5.4A2.4 2.4 0 013 16.6V7.4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
  image: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="18" height="14" rx="2.4" stroke="currentColor" stroke-width="1.6"/><circle cx="8.6" cy="10" r="1.5" fill="currentColor"/><path d="M4.5 17.5l4.3-4.2a1.6 1.6 0 012.2 0l3 2.9 1.6-1.5a1.6 1.6 0 012.2 0l2.7 2.6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>',
  video: '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5.5" width="18" height="13" rx="2.4" stroke="currentColor" stroke-width="1.6"/><path d="M10.2 9.4l4.6 2.6-4.6 2.6V9.4z" fill="currentColor"/></svg>',
  audio: '<svg viewBox="0 0 24 24" fill="none"><path d="M9 17.5V6.6l9-1.8v10.4" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><circle cx="6.8" cy="17.6" r="2.4" stroke="currentColor" stroke-width="1.6"/><circle cx="15.8" cy="15.8" r="2.4" stroke="currentColor" stroke-width="1.6"/></svg>',
  archive: '<svg viewBox="0 0 24 24" fill="none"><rect x="4" y="3.5" width="16" height="17" rx="2.2" stroke="currentColor" stroke-width="1.6"/><path d="M12 3.5v4M10.4 7.5h3.2M10.4 10h3.2M10.4 12.5h3.2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><rect x="10.4" y="15" width="3.2" height="3" rx=".8" fill="currentColor"/></svg>',
  doc: '<svg viewBox="0 0 24 24" fill="none"><path d="M6 3.5h7.2L19 9.2v11.3H6V3.5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M13 3.8v5.6h5.6" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M8.6 13h6.4M8.6 16h4.4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  file: '<svg viewBox="0 0 24 24" fill="none"><path d="M6 3.5h7.2L19 9.2v11.3H6V3.5z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M13 3.8v5.6h5.6" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>',
};

function kindOf(item) {
  if (item.is_dir === true) return "dir";
  const n = String(item.name || "").toLowerCase();
  if (/\.(jpe?g|png|gif|webp|bmp|svg|heic|avif|tiff?|raw)$/.test(n)) return "image";
  if (/\.(mp4|mkv|mov|avi|flv|wmv|webm|ts|m4v|rmvb|mpg|mpeg|3gp)$/.test(n)) return "video";
  if (/\.(mp3|flac|wav|aac|m4a|ogg|ape|wma|opus|dts)$/.test(n)) return "audio";
  if (/\.(zip|rar|7z|tar|gz|bz2|xz|iso|zst)$/.test(n)) return "archive";
  if (/\.(txt|md|pdf|docx?|xlsx?|pptx?|csv|json|xml|ya?ml|log|srt|ass|ini|conf|epub)$/.test(n)) return "doc";
  return "file";
}

function fileIconHtml(item) {
  const kind = kindOf(item);
  return `<span class="fico" data-k="${kind}" aria-hidden="true">${FILE_ICONS[kind]}</span>`;
}

// 任务表的视图状态：搜索词、状态筛选、排序方式
const view = { q: "", filter: "all", sort: "path" };
const MAX_ROWS = 400;

/* 预设档：把一堆看不懂的并发/限速参数打包成人话选项。
   改高级设置里任何一个数值都会切到「自定义」，不再跟随预设。 */
/* 预设的数值要跟项目默认配置对得上，否则用户一次都没改过却显示「自定义」，会以为哪里坏了。
   这里「均衡」= 引擎默认档（file 2 / part 2 / 10MB / 不限速）。 */
const PRESETS = {
  safe: { file_concurrency: 1, part_concurrency: 1, part_size_mb: 10, bandwidth_kbps: 0,
    verify: true, label: "稳妥", note: "一次搬一个，网络不稳也不容易断" },
  balanced: { file_concurrency: 2, part_concurrency: 2, part_size_mb: 10, bandwidth_kbps: 0,
    verify: true, label: "均衡", note: "同时搬 2 个文件，稳定又快" },
  fast: { file_concurrency: 4, part_concurrency: 6, part_size_mb: 10, bandwidth_kbps: 0,
    verify: true, label: "极速", note: "并发拉满，可能被网盘限速" },
  gentle: { file_concurrency: 2, part_concurrency: 2, part_size_mb: 10, bandwidth_kbps: 512,
    verify: true, label: "省流", note: "限 512 KB/s，不影响正常上网" },
};

// 目标网盘的分片能力：百度/夸克固定 4MB、天翼固定 10MB。
// 这些网盘下「分片大小」设置根本不生效（服务端只认自己的值），
// 所以判断档位时要把这一项排除掉，否则用户切了档位却显示「自定义」。
function chunkOf(role) {
  const side = (S && S.config && S.config[role]) || {};
  const prov = providerOf(side.provider);
  return prov.chunk || {};
}

function presetKeyOf(cfg) {
  const cur = {
    file_concurrency: Number(cfg.file_concurrency || 2),
    part_concurrency: Number(cfg.part_concurrency || 4),
    part_size_mb: Number(cfg.part_size_mb || 8),
    bandwidth_kbps: Number(cfg.bandwidth_kbps || 0),
  };
  const sizeIgnored = !!chunkOf("target").upload_fixed;
  for (const key of Object.keys(PRESETS)) {
    const p = PRESETS[key];
    if (p.file_concurrency === cur.file_concurrency
      && p.part_concurrency === cur.part_concurrency
      && (sizeIgnored || p.part_size_mb === cur.part_size_mb)
      && p.bandwidth_kbps === cur.bandwidth_kbps) return key;
  }
  return "custom";
}

/* ---------------- 基础 ---------------- */
function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtBytes(n) {
  n = Number(n || 0);
  if (n <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return (v < 10 && i > 0 ? v.toFixed(1) : Math.round(v)) + " " + units[i];
}

function fmtDur(sec) {
  if (sec == null || !isFinite(sec)) return "—";
  if (sec < 60) return sec + " 秒";
  if (sec < 3600) return Math.round(sec / 60) + " 分钟";
  return (sec / 3600).toFixed(1) + " 小时";
}

function toast(msg) {
  const box = $("#toast");
  box.textContent = msg;
  box.classList.add("on");
  clearTimeout(box._t);
  box._t = setTimeout(() => box.classList.remove("on"), 3200);
}

async function api(path, body) {
  const init = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const resp = await fetch(path, init);
  let data = {};
  try { data = await resp.json(); } catch (_) { /* 非 JSON 响应 */ }
  if (!resp.ok || data.ok === false) {
    // 后端有的地方用 error、有的用 msg（engine.start 的两条早退就是 msg），
    // 只认 error 会把真正原因丢掉，用户看到「请求失败 HTTP 200」这种没意义的提示。
    throw new Error(data.error || data.msg || ("请求失败 HTTP " + resp.status));
  }
  return data;
}

function providerOf(id) {
  const hit = providers.find((p) => p.id === id);
  if (hit) return hit;
  // 找不到就如实显示这个 id，别退成 providers[0] —— 否则演示模式（mock）
  // 会把源和目标都标成「百度网盘」，两端一模一样，看不出方向
  return { id, name: id === "mock" ? "本地演示" : id, roles: [], auth_modes: [] };
}

function defaultRedirect(prov) {
  if (prov.id === "baidu") return "oob";
  return "http://" + location.host + "/api/oauth/callback?state=target";
}

/* ---------------- 网盘平台 logo ---------------- */
// 四家网盘用「官方原版 logo 素材」，落在 web/logo/ 下，通过 <img> 引用，不做手绘。
// 来源：
//   百度网盘 = pan.baidu.com 官网 favicon（蓝橙三瓣云朵）
//   阿里云盘 = App Store 官方 App 图标（蓝紫渐变 + 白色圆环）
//   夸克     = pan.quark.cn 官网 favicon（蓝绿渐变云朵）
//   天翼     = cloud.189.cn 官网 logo（青绿渐变 + 白云箭头）
const PROVIDER_LOGO_FILE = {
  baidu: "/logo/baidu.png",
  aliyun: "/logo/aliyun.jpg",
  quark: "/logo/quark.png",
  tianyi: "/logo/tianyi.png",
  pan123: "/logo/pan123.png",
  pan115: "/logo/pan115.png",
};

function providerLogo(id, size) {
  const file = PROVIDER_LOGO_FILE[id];
  if (!file) return "";
  const s = size || 26;
  return `<span class="prov-logo"><img src="${file}" width="${s}" height="${s}" alt=""></span>`;
}

/* ---------------- 面板 ---------------- */
/* 登录板块收起 / 展开：收起后只留标题行（含摘要），把版面让给文件选择 */
function applyFold(role) {
  const card = $("#panel-" + role);
  if (!card) return;
  const folded = !!panelFold[role];
  card.classList.toggle("collapsed", folded);
  const btn = $("#" + role + "-fold");
  if (btn) btn.title = folded ? "展开这个设置面板" : "关闭这个设置面板";
  // 方向条上的入口文案跟着面板状态走（设置 ⇄ 收起）
  if (S) updateDirectionBar();
}

function buildPanels() {
  // 网盘清单还没拿到时不要重建：buildPanel 依赖 providers 生成「网盘」下拉、
  // 「认证方式」下拉和说明文字，清单为空就会把面板清成只剩标题和目录框的空壳
  // （看着像界面坏了）。保留上一次的渲染结果，等数据齐了再重建。
  if (!providers || !providers.length) {
    console.warn("providers 为空，跳过面板重建，保留现有内容");
    return;
  }
  ["source", "target"].forEach((role) => {
    try {
      buildPanel(role);
    } catch (err) {
      console.error("构建面板失败:", role, err);
      const host = document.getElementById("panel-" + role);
      if (host) {
        host.innerHTML = `<h2>${aliasRole(role)}</h2>
          <p class="hint err">面板渲染出错：${esc(err.message)}</p>`;
      }
    }
  });
}

function aliasRole(role) {
  return role === "source" ? "源 · 从这里读取" : "目标 · 写入到这里";
}

function buildPanel(role) {
  const cfg = S.config[role];
  const prov = providerOf(cfg.provider);
  const mode = cfg.auth_mode || prov.auth_modes[0] || "none";
  builtFor[role] = cfg.provider + "|" + mode;

  const host = $("#panel-" + role);
  const isSource = role === "source";
  // 注册表里用的是 source / sink，界面上叫 源 / 目标，这里必须换算，
  // 否则目标面板的下拉会一个选项都没有
  const cap = role === "source" ? "source" : "sink";
  const options = providers
    .filter((p) => (p.roles || []).includes(cap))
    .map((p) => {
      const mark = p.status === "planned" ? "（待接入）" : "";
      return `<option value="${p.id}"${p.id === cfg.provider ? " selected" : ""}>${esc(p.name)}${mark}</option>`;
    })
    .join("");

  // 某些网盘虽在注册表里标了 qr，但扫码接口尚未打通（如天翼），
  // 这里在前端再兜一道，避免下拉里出现点了却扫不出来的「扫码登录」。
  const QR_DISABLED = { tianyi: true };
  const modeOptions = (prov.auth_modes || ["none"])
    .filter((m) => !(m === "qr" && QR_DISABLED[prov.id]))
    .map((m) => {
    const label = { qr: "扫码登录（推荐）", web_qr: "扫码登录（推荐）", oauth: "官方 OAuth", cookie: "网页 Cookie", web_token: "网页 Token", key_pair: "开放平台密钥", session: "SessionKey", api_key: "API Key", none: "无需认证" }[m] || m;
    return `<option value="${m}"${m === mode ? " selected" : ""}>${label}</option>`;
  }).join("");

  let authHtml = "";
  if (prov.status === "planned") {
    authHtml = `<p class="note plan">这个网盘的适配器还在计划中，接入方案已调研清楚，但目前还不能实际传文件。</p>`;
  } else if (mode === "none") {
    authHtml = `<p class="hint">本地演示模式，用两个文件夹模拟两端网盘，不需要任何账号。</p>`;
  } else if (mode === "web_qr") {
    // 弹受控浏览器让用户扫码：token/cookie 由后端从活着的浏览器里取，不用手抄
    const desc = (S.describe || {})[role] || {};
    const logged = desc.authorized;
    const acc = desc.account || "";
    authHtml = `
      <div class="row" style="margin-bottom:8px">
        <button class="primary" id="${role}-weblogin">${logged ? "重新扫码登录" : "扫码登录"}</button>
        <span class="hint" id="${role}-weblogin-stat" style="margin:0;color:${logged ? "var(--ok)" : "var(--muted)"}">
          ${logged ? ("已登录" + (acc ? " " + esc(acc) : "")) : "还没登录"}
        </span>
      </div>
      <p class="hint" style="margin:0">点「扫码登录」会弹出浏览器窗口，用手机扫一下就行 —— 登录态会自动取回，不用手抄 Cookie 或申请密钥。</p>`;
  } else if (mode === "qr") {
    const desc = (S.describe || {})[role] || {};
    const logged = desc.authorized;
    authHtml = `
      <div class="row" style="margin-bottom:8px">
        <button class="primary" id="${role}-qr">${logged ? "重新扫码登录" : "扫码登录"}</button>
        <span class="hint" style="margin:0;color:${logged ? "var(--ok)" : "var(--muted)"}">
          ${logged ? "已登录" : "还没登录"}
        </span>
      </div>
      ${desc.warning ? `<p class="hint warn">${esc(desc.warning)}</p>` : ""}
      <p class="hint">用手机上的${esc(prov.name)} App 扫一下二维码，再在手机上点确认就行，不需要填任何东西。</p>`;
  } else if (mode === "key_pair") {
    // 开放平台「密钥对」：只填 client_id / client_secret，后端自己去换 access_token，
    // 不走浏览器跳转（123 是 client_credentials 模式，没有授权码那一步）
    const desc = (S.describe || {})[role] || {};
    authHtml = `
      <label class="f"><span>client_id</span>
        <input type="text" id="${role}-keyid" value="${esc(cfg.client_id || "")}" placeholder="开放平台创建应用后获得">
      </label>
      <label class="f"><span>client_secret</span>
        <input type="password" id="${role}-keysec" placeholder="${cfg.has_client_secret ? "已保存，留空表示不修改" : "开放平台随邮件下发的密钥"}">
      </label>
      <div class="row">
        <button class="sm primary" id="${role}-savekey">保存凭据</button>
        <span class="hint" id="${role}-keystat" style="margin:0;color:${desc.authorized ? "var(--ok)" : "var(--muted)"}">
          ${desc.authorized ? "凭据已保存" : "还没配置"}
        </span>
      </div>
      <p class="hint warn">到 123pan.com/developer 用手机号 + UID 申请，审核通过后 client_id / client_secret 会发到你邮箱。这是官方接口，凭据长期有效；上传免费，下载直链需要会员。</p>`;
  } else if (mode === "oauth") {
    const redir = cfg.redirect_uri || defaultRedirect(prov);
    authHtml = `
      <label class="f"><span>client_id / AppKey</span>
        <input type="text" id="${role}-cid" value="${esc(cfg.client_id || "")}" placeholder="在开放平台创建应用后获得">
      </label>
      <label class="f"><span>client_secret / SecretKey</span>
        <input type="password" id="${role}-csec" placeholder="${cfg.has_client_secret ? "已保存，留空表示不修改" : "应用密钥"}">
      </label>
      <label class="f"><span>回调地址（必须与平台登记的完全一致）</span>
        <input type="text" id="${role}-redir" value="${esc(redir)}">
      </label>
      <div class="row" style="flex-wrap:wrap">
        <button class="sm" id="${role}-authgo">打开授权页</button>
        <input type="text" id="${role}-code" placeholder="把页面上的 code 粘贴到这里" style="flex:1;min-width:140px">
        <button class="sm primary" id="${role}-exchange">换取令牌</button>
      </div>
      <p class="hint" id="${role}-authstat"></p>`;
  } else {
    // 天翼：只提供「一键登录」（内嵌浏览器自动抓 cookie），不做手动粘贴
    if (prov.id === "tianyi") {
      const desc = (S.describe || {})[role] || {};
      const logged = desc.authorized;
      authHtml = `
        <div class="row" style="margin-bottom:8px">
          <button class="primary" id="${role}-tylogin">${logged ? "重新登录天翼云盘" : "一键登录天翼云盘"}</button>
          <span class="hint" id="${role}-tylogin-stat" style="margin:0;color:${logged ? "var(--ok)" : "var(--muted)"}">
            ${logged ? "已登录" : "未登录"}
          </span>
        </div>
        <p class="hint" style="margin:0">点「一键登录」会弹出浏览器窗口，登录后自动抓取登录态。</p>`;
    } else {
      // 其它网盘的「粘贴凭据」型认证
      const cookieTip = prov.id === "baidu"
        ? "粘贴浏览器里的 BDUSS（F12 → Application → Cookies → pan.baidu.com）。注意：这是非公开接口，凭据会过期，且不符合平台用户协议。"
        : prov.id === "pan115"
          ? "粘贴 115.com 登录后的整条 Cookie（F12 → Network → 任一请求 → Request Headers 里的 Cookie）。当前只支持把它当作「源」读出文件，暂不能作为目标写入。"
          : "粘贴网页登录态 Cookie。";
      const tips = {
        cookie: cookieTip,
        web_token: "粘贴阿里云盘网页端的 refresh_token（F12 → Application → Local Storage → token 里的 refresh_token 字段）。约 2 小时需重新获取。",
        session: "粘贴登录后的 sessionKey。",
        api_key: "填入开放平台申请的 API Key。",
      };
      authHtml = `
        <label class="f"><span>凭据</span>
          <textarea id="${role}-token" placeholder="${esc(tips[mode] || "粘贴凭据")}"></textarea>
        </label>
        <div class="row"><button class="sm" id="${role}-savetoken">保存凭据</button></div>
        <p class="hint warn">${esc(tips[mode] || "")}</p>`;
    }
  }

  // 收起后这行要能替代整个面板说清楚「哪一端、哪个网盘、登录没登录、哪个目录」
  const sideDesc = (S.describe || {})[role] || {};
  const sumText = [
    prov.name,
    sideDesc.authorized ? "已登录" : "未登录",
    cfg.root_path || "/",
  ].join(" · ");

  host.innerHTML = `
    <h2>${providerLogo(cfg.provider, 22)}<span class="h2-txt">${isSource ? "源 · 从这里读取" : "目标 · 写入到这里"}</span> <span class="tag">${esc(prov.name)}</span><span class="sum">${esc(sumText)}</span><button class="side-close" id="${role}-fold" title="关闭这个设置面板">×</button></h2>
    <label class="f"><span>网盘</span>
      <select id="${role}-provider">${options}</select>
    </label>
    ${prov.note ? `<p class="note${prov.status === "planned" ? " plan" : ""}">${esc(prov.note)}</p>` : ""}
    <div class="auth-sec">
      <label class="f"><span>认证方式</span>
        <select id="${role}-mode">${modeOptions}</select>
      </label>
      ${authHtml}
    </div>
    <div class="row" style="margin-top:10px">
      <button class="sm" id="${role}-test" title="探测这个网盘的当前目录能不能读">测试连接</button>
      <span class="hint" style="margin:0">目录在下方「文件与目标」里切换</span>
    </div>`;

  on(role + "-provider", "change", (event) => {
    const nextId = event.target.value;
    const next = providerOf(nextId);
    save(role, {
      provider: nextId,
      auth_mode: (next.auth_modes || ["none"])[0],
      root_path: isSource ? "/" : "/",
    });
  });

  on(role + "-mode", "change", (event) => {
    save(role, { auth_mode: event.target.value });
  });


  on(role + "-fold", "click", (event) => {
    event.stopPropagation();
    panelFold[role] = !panelFold[role];
    // 记下「用户自己动过」，之后不再自动收起，免得刚展开又被收回去
    panelFoldSet[role] = true;
    LS.set(LS_FOLD, panelFold);
    LS.set(LS_FOLD_SET, panelFoldSet);
    applyFold(role);
  });
  applyFold(role);

  on(role + "-test", "click", async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await api("/api/test", { role });
      toast(`连接正常，读到 ${res.count} 项`);
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "测试连接";
    }
  });

  // 注意：这里必须是同一条 if / else if 链。
  // 之前把 qr 分支写成独立 if，qr 模式下会继续走到最后一个 else if，
  // 去绑定只存在于 cookie/web_token 分支的 -savetoken，拿到 null 直接抛错，
  // 结果整个面板构建中断，目标面板一片空白。
  if (mode === "qr") {
    on(role + "-qr", "click", () => openQR(role));
  } else if (mode === "web_qr") {
    on(role + "-weblogin", "click", () => startWebLogin(role));
  } else if (mode === "key_pair") {
    on(role + "-savekey", "click", () => saveKeyPair(role));
  } else if (mode === "oauth") {
    on(role + "-cid", "blur", (event) => {
      if (event.target.value.trim()) save(role, { client_id: event.target.value.trim() });
    });
    on(role + "-csec", "blur", (event) => {
      if (event.target.value.trim()) save(role, { client_secret: event.target.value.trim() });
    });
    on(role + "-redir", "blur", (event) => {
      save(role, { redirect_uri: event.target.value.trim() });
    });
    on(role + "-authgo", "click", () => startOAuth(role));
    on(role + "-exchange", "click", () => exchangeCode(role));
  } else if (mode !== "none" && prov.status !== "planned") {
    if (prov.id === "tianyi") {
      // 天翼只有「一键登录」，没有手动粘贴的 -savetoken/-token
      on(role + "-tylogin", "click", () => startTyLogin(role));
    } else {
      on(role + "-savetoken", "click", () => saveToken(role, mode));
    }
  }
}

/* ---------------- 扫码登录（123 / 115，弹浏览器） ---------------- */
let webloginPolling = null;

async function startWebLogin(role) {
  const btn = $("#" + role + "-weblogin");
  const stat = $("#" + role + "-weblogin-stat");
  try {
    if (btn) { btn.disabled = true; btn.textContent = "正在启动…"; }
    const res = await api("/api/web-login/start", { role });
    if (!res.ok) {
      toast(res.error || "启动失败");
      if (stat) { stat.textContent = res.error || ""; stat.style.color = "var(--danger)"; }
      return;
    }
    if (stat) { stat.textContent = res.hint || "已弹出浏览器窗口，请扫码…"; stat.style.color = "var(--muted)"; }
    pollWebLogin(role);
  } catch (err) {
    toast(err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "扫码登录"; }
  }
}

async function pollWebLogin(role) {
  if (webloginPolling) { clearTimeout(webloginPolling); webloginPolling = null; }
  const stat = $("#" + role + "-weblogin-stat");
  try {
    const res = await api("/api/web-login/status", {});
    if (res.done) {
      const who = res.account ? " " + res.account : "";
      if (stat) { stat.textContent = "已登录" + who; stat.style.color = "var(--ok)"; }
      toast("扫码登录成功，凭据已自动保存");
      await refresh(true);
      return;
    }
    if (res.error) {
      if (stat) { stat.textContent = res.error; stat.style.color = "var(--danger)"; }
      toast(res.error);
      return;
    }
    if (res.active) {
      if (stat) { stat.textContent = "等待扫码…（在浏览器窗口里完成登录）"; stat.style.color = "var(--muted)"; }
      webloginPolling = setTimeout(() => pollWebLogin(role), 1500);
    } else if (stat) {
      stat.textContent = "";
    }
  } catch (err) {
    toast(err.message);
  }
}

/* ---------------- 天翼一键登录 ---------------- */
let tyloginPolling = null;

function resetTyButton(role) {
  const btn = $("#" + role + "-tylogin");
  if (!btn) return;
  btn.dataset.cancel = "";
  btn.disabled = false;
  btn.textContent = "一键登录天翼云盘";
}

async function startTyLogin(role) {
  const btn = $("#" + role + "-tylogin");
  const stat = $("#" + role + "-tylogin-stat");
  // 正在等待登录时，这个按钮变成「取消登录」——否则中途想放弃只能干等 5 分钟
  if (btn && btn.dataset.cancel === "1") {
    btn.disabled = true;
    try {
      await api("/api/tylogin/cancel", {});
    } catch (err) { /* 取消失败也不影响后面重置按钮 */ }
    if (tyloginPolling) { clearTimeout(tyloginPolling); tyloginPolling = null; }
    resetTyButton(role);
    if (stat) { stat.textContent = "已取消登录"; stat.style.color = "var(--muted)"; }
    return;
  }
  try {
    if (btn) { btn.disabled = true; btn.textContent = "正在启动…"; }
    const res = await api("/api/tylogin/start", { role });
    if (!res.ok) {
      toast(res.error || "启动失败");
      if (stat) stat.textContent = res.error || "";
      resetTyButton(role);
      return;
    }
    if (stat) {
      stat.textContent = res.hint || "已弹出浏览器窗口，请在里面登录天翼云盘…";
      stat.style.color = "var(--muted)";
    }
    if (btn) { btn.disabled = false; btn.textContent = "取消登录"; btn.dataset.cancel = "1"; }
    pollTyLogin(role);
  } catch (err) {
    toast(err.message);
    resetTyButton(role);
  }
}

async function pollTyLogin(role) {
  if (tyloginPolling) { clearTimeout(tyloginPolling); tyloginPolling = null; }
  const stat = $("#" + role + "-tylogin-stat");
  try {
    const res = await api("/api/tylogin/status", {});
    if (res.done) {
      resetTyButton(role);
      if (stat) { stat.textContent = "已登录成功"; stat.style.color = "var(--ok)"; }
      toast("天翼云盘登录成功，凭据已自动保存");
      await refresh(true);
      // 成功反馈展示 2 秒后自动收起提示文字，避免一直挂着
      setTimeout(() => {
        const s = $("#" + role + "-tylogin-stat");
        if (s && s.textContent === "已登录成功") { s.textContent = ""; }
      }, 2000);
      return;
    }
    if (res.error) {
      resetTyButton(role);
      if (stat) { stat.textContent = res.error; stat.style.color = "var(--danger)"; }
      toast(res.error);
      return;
    }
    if (res.active) {
      if (stat) { stat.textContent = "等待登录…（在浏览器窗口里完成登录）"; stat.style.color = "var(--muted)"; }
      tyloginPolling = setTimeout(() => pollTyLogin(role), 1500);
    } else {
      // 既非 done 也非 error 也非 active：登录窗口被关闭/流程结束，收掉提示
      resetTyButton(role);
      if (stat) { stat.textContent = ""; }
    }
  } catch (err) {
    toast(err.message);
  }
}

function on(id, event, handler) {
  const node = document.getElementById(id);
  if (node) node.addEventListener(event, handler);
  else console.warn("绑定失败，元素不存在：", id);
}

/* ---------------- 动作 ---------------- */
async function save(role, patch) {
  try {
    await api("/api/config", { [role]: patch });
    await refresh(true);
  } catch (err) {
    toast(err.message);
  }
}

async function saveToken(role, mode) {
  const raw = $("#" + role + "-token").value.trim();
  if (!raw) return toast("先把凭据粘贴进去");
  const patch = mode === "cookie" ? { cookie: raw }
    : mode === "web_token" ? { refresh_token: raw }
      : mode === "session" ? { access_token: raw }
        : { api_key: raw };
  patch.auth_mode = mode;
  $("#" + role + "-token").value = "";
  await save(role, patch);
  toast("凭据已保存");
}

async function saveKeyPair(role) {
  const cid = ($("#" + role + "-keyid").value || "").trim();
  const csec = ($("#" + role + "-keysec").value || "").trim();
  if (!cid) return toast("先填 client_id");
  const patch = { client_id: cid, auth_mode: "key_pair" };
  // 留空表示「不修改已保存的密钥」，和 OAuth 分支的行为保持一致
  if (csec) patch.client_secret = csec;
  $("#" + role + "-keysec").value = "";
  await save(role, patch);
  const stat = $("#" + role + "-keystat");
  if (stat) {
    stat.textContent = "凭据已保存";
    stat.style.color = "var(--ok)";
  }
  toast("凭据已保存");
}

async function startOAuth(role) {
  try {
    const redir = ($("#" + role + "-redir").value || "").trim();
    await api("/api/config", { [role]: { redirect_uri: redir } });
    const res = await api("/api/oauth/start", { role, redirect_uri: redir });
    window.open(res.url, "_blank", "noopener");
    toast(redir === "oob" ? "授权页已打开，把 code 复制回来粘贴" : "授权页已打开，同意后会自动回跳");
  } catch (err) {
    toast(err.message);
  }
}

async function exchangeCode(role) {
  const code = ($("#" + role + "-code").value || "").trim();
  if (!code) return toast("先粘贴 code");
  const redir = ($("#" + role + "-redir").value || "").trim();
  try {
    await api("/api/oauth/exchange", { role, code, redirect_uri: redir });
    $("#" + role + "-code").value = "";
    await refresh(true);
    toast("授权成功");
  } catch (err) {
    toast(err.message);
    $("#" + role + "-authstat").textContent = err.message;
  }
}

async function doScan(thenStart) {
  if (scanPolling) return;
  try {
    await api("/api/scan", {});
  } catch (err) {
    return toast(err.message);
  }
  scanPolling = true;
  $("#scan-hint").textContent = "正在扫描…";
  const tick = async () => {
    try {
      const res = await api("/api/scan_status", {});
      const phase = res.phase || {};
      if (res.running) {
        $("#scan-hint").textContent =
          `${phase.phase || "扫描中"} · 已发现 ${phase.files || 0} 个文件 · ${fmtBytes(phase.bytes || 0)}` +
          (phase.current ? ` · ${phase.current}` : "");
        setTimeout(tick, 900);
        return;
      }
      scanPolling = false;
      $("#scan-hint").textContent = "";
      if (res.error) { toast(res.error); return; }
      await refresh(true);
      const r = res.result || {};
      toast(`扫描完成：${r.files || 0} 个文件，${r.skip || 0} 个已存在`);
      if (thenStart) await doStart();
    } catch (err) {
      scanPolling = false;
      toast(err.message);
    }
  };
  setTimeout(tick, 600);
}

async function doStart() {
  try {
    const res = await api("/api/start", {});
    if (res.ok === false) toast(res.msg || "无法开始");
    await refresh(true);
  } catch (err) {
    toast(err.message);
  }
}

/* ---------------- 渲染 ---------------- */
/* 源/目标组合的硬限制提示：哪些能传、哪些要多花代价、哪些直接不行 */
function updateCompatHint() {
  const box = $("#compat-hint");
  if (!box) return;
  const cfg = (S && S.config) || {};
  const src = providerOf((cfg.source || {}).provider);
  const dst = providerOf((cfg.target || {}).provider);
  const tips = [];

  const full = [];   // 悬停时看的完整说明
  if (dst.needs_sha1) {
    const has = (src.digests || []).includes("sha1");
    if (!has) {
      tips.push("115 每文件<em>多读一遍</em>算 SHA1");
      full.push(`115 的预上传要求整文件 SHA1，而${src.name || "源盘"}只提供 MD5，`
        + "所以每个文件会额外完整读一遍源来计算，会多消耗一份源盘下载流量。");
    }
    if (dst.max_file_mb) {
      tips.push(`115 <em>单文件 ≤${dst.max_file_mb}MB</em>`);
      full.push(`115 是整文件上传（不分片），单个文件最大 ${dst.max_file_mb}MB，`
        + "超出的文件会在扫描时自动跳过。");
    }
  }
  if (src.id === "pan115" && dst.id && dst.id !== src.id) {
    tips.push("115 往外读<em>免费账号限速</em>");
    full.push("从 115 往外读会走它的 CDN，免费账号有速度限制，"
      + "遇到传不动可以把「档位」调到「稳妥」。");
  }

  if (!tips.length) {
    box.style.display = "none";
    box.innerHTML = "";
    return;
  }
  box.innerHTML = tips.map((t) => `<span class="compat-line">${t}</span>`).join("");
  box.title = full.join("\n");     // 悬停看完整说明
  box.style.display = "";
}

async function refresh(forcePanels) {
  try {
    S = await api("/api/state");
  } catch (err) {
    return;
  }
  // 接口偶发返回不全时别把已有清单清空 —— 清空会导致面板被重建成空壳
  if (Array.isArray(S.providers) && S.providers.length) {
    providers = S.providers;
  } else if (!providers.length) {
    providers = S.providers || [];
  }
  const needBuild = forcePanels ||
    builtFor.source !== (S.config.source.provider + "|" + (S.config.source.auth_mode || "")) ||
    builtFor.target !== (S.config.target.provider + "|" + (S.config.target.auth_mode || ""));
  if (needBuild) buildPanels();
  updateStatus();
  updatePresets();
  updateProgress();
  updateQuota();
  updateTasks();
  updateLogs();
  updateCompatHint();
}

function updateQuota() {
  const node = $("#quota-info");
  if (!node) return;
  const q = S.quota || {};
  if (!q.total) {
    node.textContent = q.error ? `目标容量：${q.error}` : "";
    node.style.color = "var(--faint)";
    return;
  }
  const need = Number((S.engine || {}).remaining || 0);
  const short = need > 0 && need > q.free;
  node.innerHTML = `目标剩余 <b>${fmtBytes(q.free)}</b> / 共 ${fmtBytes(q.total)}` +
    (need > 0 ? ` · 本次还需 ${fmtBytes(need)}` : "") +
    (short ? ` · <b>空间不足，先清点目标盘</b>` : "");
  node.style.color = short ? "var(--danger)" : "var(--faint)";
}

function sideBadge(role) {
  const cfg = S.config[role];
  const prov = providerOf(cfg.provider);
  const desc = (S.describe || {})[role] || {};
  if (prov.status === "planned") return { dot: "warn", text: `${prov.name} · 待接入` };
  if (desc.error) return { dot: "err", text: `${prov.name} · 异常` };
  if (prov.id === "mock" || desc.authorized) return { dot: "ok", text: prov.name };
  return { dot: "warn", text: `${prov.name} · 未授权` };
}

/* ---------------- 方向（交换源/目标） ---------------- */
// 箭头按钮：真正交换源/目标的配置（网盘、目录、凭据整体对调），
// 面板位置不动，里面的内容左变右、右变左。
function applyDirection() {
  // 方向条上的网盘名和目录，来自当前配置
  updateDirectionBar();
}

async function toggleDirection() {
  if ((S.engine || {}).running) {
    toast("迁移进行中，先暂停再交换源与目标");
    return;
  }
  const btn = $("#btn-swap");
  if (btn) { btn.disabled = true; btn.classList.add("busy"); }
  try {
    const res = await api("/api/swap", {});
    await refresh(true);
    // 交换后两端都换了，两个浏览区都要按新配置重新加载
    await focusSourceBrowser();
    await loadTargetDir(S.config.target.root_path || "/");
    const from = (providerOf(S.config.source.provider) || {}).name || "源";
    const to = (providerOf(S.config.target.provider) || {}).name || "目标";
    if (res && res.cleared > 0) {
      toast(`已交换：现在从「${from}」传到「${to}」。旧清单 ${res.cleared} 项已清空，请重新勾选`);
    } else {
      toast(`已交换：现在从「${from}」传到「${to}」`);
    }
  } catch (err) {
    toast(err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.classList.remove("busy"); }
  }
}

/* ---------------- 设置预设 ---------------- */
function updatePresets() {
  const cfg = S.config.transfer || {};
  const key = presetKeyOf(cfg);
  document.querySelectorAll("#presets .preset").forEach((btn) => {
    btn.classList.toggle("on", btn.dataset.preset === key);
  });
  // 折叠态标题里显示当前档位（展开时 CSS 会隐藏这个角标，因为按钮就在眼前）
  const nowEl = $("#preset-now");
  if (nowEl) {
    nowEl.textContent = key === "custom"
      ? "自定义"
      : (PRESETS[key] || {}).label || "—";
  }
  const note = $("#preset-note");
  if (!note) return;
  let text;
  if (key === "custom") {
    text = "自定义：点上面的档位可一键回到推荐值";
    note.style.color = "var(--muted)";
  } else {
    text = PRESETS[key].note;
    note.style.color = "var(--faint)";
  }

  // 把档位落到「这对网盘」上的实际值算出来给用户看。
  // 不同网盘的上传分片和下载并发是硬约束，档位调到再高也会被压回来，
  // 不显示出来用户就会以为「档位没生效」。
  const tc = chunkOf("target");
  const sc = chunkOf("source");
  const srcName = providerOf((S.config.source || {}).provider).name;
  const tgtName = providerOf((S.config.target || {}).provider).name;

  const effPartMb = tc.upload_fixed ? tc.upload_mb : Number(cfg.part_size_mb || 10);
  const wantConc = Number(cfg.part_concurrency || 2);
  const effConc = sc.conn ? Math.min(wantConc, sc.conn) : wantConc;

  text += `。实际生效：分片 ${effPartMb}MB × ${effConc} 并发`;
  if (tc.upload_fixed && effPartMb !== Number(cfg.part_size_mb || 10)) {
    text += `（${tgtName}上传分片固定 ${tc.upload_mb}MB，设置里的分片大小对它无效）`;
  }
  if (sc.conn && wantConc > sc.conn) {
    text += `（${srcName}最多 ${sc.conn} 条连接，已自动收敛）`;
  }
  note.textContent = text;

  const customBtn = document.querySelector('#presets .preset.custom');
  if (customBtn) customBtn.classList.toggle("on", key === "custom");
}

function applyPreset(key) {
  const p = PRESETS[key];
  if (!p) return;
  const patch = {
    file_concurrency: p.file_concurrency,
    part_concurrency: p.part_concurrency,
    part_size_mb: p.part_size_mb,
    bandwidth_kbps: p.bandwidth_kbps,
    verify: p.verify,
  };
  api("/api/config", { transfer: patch })
    .then(async () => {
      toast(`已切到「${p.label}」`);
      await refresh(false);
    })
    .catch((err) => toast(err.message));
}

function updateStatus() {
  const eng = S.engine || {};
  // 源/目标状态移到方向条上显示了，品牌栏只保留运行状态
  const dotRun = $("#dot-run");
  if (dotRun) dotRun.className = "dot " + (eng.running ? "run" : "");
  $("#txt-run").textContent = eng.running
    ? "运行中"
    : (eng.fatal ? "已中止" : (eng.paused ? "已暂停" : "空闲"));

  // 换网盘后，浏览器 tab 上的网盘名要跟着变
  if (typeof updateDirectionBar === "function") updateDirectionBar();
  // 版本号：确认浏览器里跑的是不是刚打包的那一版
  const verEl = $("#app-ver");
  if (verEl && S.version) {
    verEl.textContent = "v" + S.version;
    // 悬停能看启动时间：如果不是你刚双击那次，说明这是旧实例的页面
    verEl.title = `版本 ${S.version}｜服务启动于 ${S.started_at || "?"}｜进程 ${S.pid || "?"}`;
  }

  // 登录成功后自动收起登录板块，把版面让给下面的文件明细 / 选文件。
  // 只在用户从没手动点过收起/展开时做一次，否则会出现「我刚展开又被收起来」。
  ["source", "target"].forEach((role) => {
    const desc = (S.describe || {})[role] || {};
    if (desc.authorized && !panelFoldSet[role] && !panelFold[role]) {
      panelFold[role] = true;
      panelFoldSet[role] = true;
      LS.set(LS_FOLD, panelFold);
      LS.set(LS_FOLD_SET, panelFoldSet);
      applyFold(role);
    }
  });

  // 用户已经点过「暂停」时，即使引擎还在收尾也要让「开始迁移」可点。
  // 否则暂停后想换批文件重来，按钮会灰几十秒（等当前大分片传完），
  // 而且这时点它还会被「已经在跑了」顶回来，看着就像卡死。
  const settling = !!eng.running && !!eng.pause_requested;
  const startBtn = $("#btn-start");
  if (startBtn) {
    startBtn.disabled = !!eng.running && !settling;
    startBtn.title = settling ? "正在收尾上一个分片，点一下即可继续" : "";
  }
  $("#btn-pause").disabled = !eng.running;
  // 停止：跑着或暂停着都该能点。暂停时 running 已经是 false，
  // 只判断 running 会让「暂停后想放弃这批任务」没有出口（实测 picked 一直是 101）。
  const stopRunBtn = $("#btn-stop");
  if (stopRunBtn) stopRunBtn.disabled = !(eng.running || eng.paused);
  const scanning = !!eng.scan_running || scanPolling;
  $("#btn-scan").disabled = (!!eng.running && !settling) || scanning;
  const stopBtn = $("#btn-scan-stop");
  if (stopBtn) stopBtn.style.display = scanning ? "" : "none";

  // 迁移进行中锁定「交换方向」和「网盘/目录」这类结构性改动，避免引擎适配器错乱。
  // 注意用 running && !pause_requested：用户一点「暂停」，worker 可能还在收尾当前
  // 分片（百度慢速下要几十秒），但用户已经决定停了，按钮应立刻可用，别让人干等。
  const locked = !!eng.running && !eng.pause_requested;
  const swapBtn = $("#btn-swap");
  if (swapBtn) {
    swapBtn.disabled = locked;
    swapBtn.title = locked
      ? "迁移进行中，先暂停再交换"
      : "把源和目标整体对调：网盘、登录状态、目录全部互换";
  }
  ["source", "target"].forEach((role) => {
    const sel = $("#" + role + "-provider");
    if (sel) sel.disabled = locked;
    const browseBtn = $("#" + role + "-browse");
    if (browseBtn) browseBtn.disabled = locked;
  });

  const cfg = S.config.transfer || {};
  const setVal = (id, val) => { const node = $(id); if (node && document.activeElement !== node) node.value = val; };
  setVal("#cfg-file-conc", cfg.file_concurrency || 2);
  setVal("#cfg-part-conc", cfg.part_concurrency || 4);
  setVal("#cfg-bandwidth", cfg.bandwidth_kbps || 0);

  // 登录浏览器：Safari 只在 macOS（且有 safaridriver）时才出现，Windows 用户看不到这一项，
  // 免得误选。配置里是 safari 但当前平台不支持时，服务端会按「自动」处理，这里也回落到自动显示。
  const lcfg = (S.config.login || {}).browser || "auto";
  const lcap = S.login || {};
  const lbSel = $("#cfg-login-browser");
  if (lbSel) {
    const safariOpt = lbSel.querySelector('option[value="safari"]');
    const safariOk = !!lcap.safari_supported;
    if (safariOpt) {
      safariOpt.hidden = !safariOk;
      safariOpt.disabled = !safariOk;
    }
    setVal("#cfg-login-browser", lcfg === "safari" && !safariOk ? "auto" : lcfg);
    lbSel.title = safariOk
      ? (lcap.safari_hint || "Safari 需要先在「开发」菜单里允许远程自动化")
      : "这台机器上没有可用的 Safari 自动化通道";
  }

  // 分片大小：目标网盘固定分片时（百度/夸克 4MB、天翼 10MB），填多少都不作数，
  // 直接显示服务端真实值并锁住，避免「改了没反应」的困惑。
  const tc = chunkOf("target");
  const sizeInput = $("#cfg-part-size");
  const sizeTip = $("#chunk-tip");
  if (sizeInput) {
    if (tc.upload_fixed) {
      sizeInput.value = tc.upload_mb;
      sizeInput.disabled = true;
      sizeInput.title = "该网盘分片大小由服务端固定，无法修改";
      if (sizeTip) sizeTip.textContent = `（${providerOf((S.config.target || {}).provider).name}固定 ${tc.upload_mb}MB）`;
    } else {
      sizeInput.disabled = false;
      sizeInput.title = "";
      if (sizeTip) sizeTip.textContent = "";
      setVal("#cfg-part-size", cfg.part_size_mb || 8);
    }
  }
  setVal("#cfg-conflict", cfg.conflict || "auto_rename");
  setVal("#cfg-structure", cfg.structure || "keep");
  const chk = $("#cfg-verify");
  if (chk) chk.checked = cfg.verify !== false;
}

function updateProgress() {
  const eng = S.engine || {};
  const s = eng.summary || {};
  const total = Number(s.total_bytes || 0);
  // 夹在 [0, total] 内：后端已经保证不超，这里再兜一道，
  // 免得任何口径误差又显示成「已传 395MB / 总 263MB」这种自相矛盾的数字
  const done = Math.min(total || Infinity, Math.max(0, Number(eng.done_bytes || s.done_bytes || 0)));
  const pct = total > 0 ? Math.min(100, (done / total) * 100) : 0;
  const bar = $("#prog");
  bar.classList.toggle("done", pct >= 100 && total > 0);
  // 跑起来时给进度条加流动条纹，停下来就静默
  bar.classList.toggle("running", !!eng.running);
  bar.querySelector("i").style.width = pct.toFixed(1) + "%";

  $("#prog-label").textContent = total > 0
    ? `已传输 ${fmtBytes(done)} / ${fmtBytes(total)}（${pct.toFixed(1)}%）`
    : "还没有任务";
  $("#prog-right").textContent = eng.running
    ? `${fmtBytes(eng.speed || 0)}/s · 已运行 ${fmtDur(eng.elapsed)}`
    : "";

  // 慢速网盘（百度非会员）一个大分片要好几分钟，只看整体百分比会以为卡死。
  // 这里把「正在传哪个文件、第几片」直接写出来。
  const activeBox = $("#prog-active");
  if (activeBox) {
    if (eng.fatal) {
      activeBox.textContent = `⛔ ${eng.fatal} —— 处理后点「开始迁移」继续（已传分片保留）`;
      activeBox.style.color = "var(--danger)";
    } else if (eng.running && (eng.active || []).length) {
      const items = eng.active.slice(0, 2).map((a) => {
        const part = a.total_parts
          ? ` · 第 ${Math.min(a.done_parts + 1, a.total_parts)}/${a.total_parts} 片`
          : "";
        return `${a.rel}${part}`;
      });
      const more = eng.active.length > 2 ? ` 等 ${eng.active.length} 个` : "";
      activeBox.textContent = `正在传：${items.join("；")}${more}`;
      activeBox.style.color = "var(--muted)";
    } else {
      activeBox.textContent = "";
    }
  }

  $("#st-done").textContent = s.done || 0;
  $("#st-skip").textContent = s.skipped || 0;
  $("#st-fail").textContent = s.failed || 0;
  $("#st-total").textContent = s.total || 0;
  const stBytes = $("#st-bytes");
  if (stBytes) stBytes.textContent = `${fmtBytes(done)} / ${fmtBytes(total)}`;
  $("#st-speed").textContent = eng.running ? fmtBytes(eng.speed || 0) + "/s" : "—";
  $("#st-eta").textContent = eng.running ? fmtDur(eng.eta) : "—";

  const tag = $("#run-tag");
  if (tag) {
    // 必须把 running 算进来：之前只数 pending/paused/failed，正在传的时候
    // 这三个都是 0，于是明明还在传却显示「已全部处理完」，自相矛盾。
    const running = Number(s.running || 0);
    const waiting = Number(s.pending || 0) + Number(s.paused || 0) + Number(s.failed || 0);
    if (running) {
      tag.textContent = `正在处理 ${running} 个 · 剩 ${fmtBytes(eng.remaining || 0)}`;
    } else if (waiting) {
      tag.textContent = `待处理 ${waiting} 个 · ${fmtBytes(eng.remaining || 0)}`;
    } else {
      tag.textContent = Number(s.total || 0) ? "已全部处理完" : "";
    }
  }
}

function updatePickInfo() {
  const sel = S.selection || {};
  const total = Number(sel.total || 0);
  const picked = Number(sel.picked || 0);
  const info = $("#pick-info");
  if (!info) return;
  info.textContent = total
    ? `已选 ${picked} / 共 ${total} 个 · ${fmtBytes(sel.picked_bytes || 0)}`
    : "";
  const all = $("#chk-all");
  if (all) all.checked = total > 0 && picked === total;
}

async function setSelection(payload) {
  try {
    await api("/api/select", payload);
    sigTasks = "";
    await refresh(false);
  } catch (err) {
    toast(err.message);
  }
}

async function onRowChk(event) {
  const node = event.currentTarget;
  const id = Number(node.dataset.id);
  const row = node.closest("tr");
  if (row) row.style.opacity = node.checked ? "1" : ".45";
  await setSelection({ ids: [id], selected: node.checked });
}

async function onRowDelete(event) {
  const id = Number(event.currentTarget.dataset.id);
  try {
    await api("/api/remove", { ids: [id] });
    sigTasks = "";
    await refresh(false);
    toast("已从清单移除（网盘上的文件没动）");
  } catch (err) {
    toast(err.message);
  }
}

function matchFilter(task) {
  switch (view.filter) {
    case "open": return task.state !== "done" && task.state !== "skipped";
    case "failed": return task.state === "failed";
    case "done": return task.state === "done" || task.state === "skipped";
    case "pending": return task.state === "pending";
    default: return true;
  }
}

function visibleTasks(tasks) {
  const q = view.q.trim().toLowerCase();
  const out = tasks.filter((t) =>
    matchFilter(t) && (!q || String(t.rel_path || "").toLowerCase().includes(q)));
  const sorters = {
    path: (a, b) => String(a.rel_path).localeCompare(String(b.rel_path), "zh"),
    size: (a, b) => Number(b.size || 0) - Number(a.size || 0),
    state: (a, b) =>
      String(a.state).localeCompare(String(b.state)) ||
      String(a.rel_path).localeCompare(String(b.rel_path), "zh"),
  };
  const cmp = sorters[view.sort];
  return cmp ? out.slice().sort(cmp) : out;
}

function updateTasks() {
  if (!S) return;
  const tasks = S.tasks || [];
  const shownAll = visibleTasks(tasks);
  const sig = tasks.length + "|" + view.q + "|" + view.filter + "|" + view.sort + "|" +
    tasks.map((t) => t.id + t.state + t.done_bytes + t.selected).join(",");
  const info = $("#list-info");
  if (info) {
    info.textContent = tasks.length
      ? `显示 ${Math.min(shownAll.length, MAX_ROWS)} / ${shownAll.length} 项（清单共 ${tasks.length}）`
      : "";
  }
  if (sig === sigTasks) {
    updatePickInfo();
    return;
  }
  sigTasks = sig;

  $("#task-count").textContent = tasks.length ? `共 ${tasks.length} 项` : "";
  const body = $("#task-body");
  if (!shownAll.length) {
    body.innerHTML = `<tr><td colspan="7" class="hint" style="padding:16px 10px">${
      tasks.length ? "没有符合筛选条件的文件" : "暂无任务，先点「扫描整个目录」或「浏览」勾选文件"
    }</td></tr>`;
    updatePickInfo();
    return;
  }

  const shown = shownAll.slice(0, MAX_ROWS);
  body.innerHTML = shown.map((t) => {
    const size = Number(t.size || 0);
    const done = Number(t.done_bytes || 0);
    const checked = Number(t.selected) === 1;
    const locked = t.state === "done" || t.state === "skipped";
    const pct = size > 0 ? Math.min(100, (done / size) * 100) : (t.state === "done" ? 100 : 0);
    // skip_reason 是「这条链路上注定传不了」（空文件/名字超长），属于提示不是报错，
    // 用橙色；error 才是真失败，用红色。
    const reason = t.skip_reason || "";
    const note = reason
      ? `<span style="color:var(--warn)" title="${esc(reason)}">${esc(reason)}</span>`
      : (t.error
        ? `<span style="color:var(--danger)" title="${esc(t.error)}">${esc(t.error)}</span>`
        : "");
    return `<tr style="${checked ? "" : "opacity:.45"}">
      <td><input type="checkbox" class="row-chk" data-id="${t.id}"
           ${checked ? "checked" : ""} ${locked ? "disabled" : ""} style="width:auto"></td>
      <td class="name" title="${esc(t.rel_path)}">${esc(t.rel_path)}</td>
      <td class="num">${fmtBytes(size)}</td>
      <td><span class="pill ${t.state}">${STATE_LABEL[t.state] || t.state}</span></td>
      <td>${pct > 0 || t.state === "done" || t.state === "running"
        // 还没开始的文件不画进度条：一排全是 0% 的空槽，看着像表格里多了 6 条横线
        ? `<div class="progress ${t.state === "done" ? "done" : ""}" style="height:5px"><i style="width:${pct.toFixed(1)}%"></i></div>`
        : `<span class="hint" style="margin:0;font-size:11.5px">—</span>`}</td>
      <td style="font-size:11.5px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${note}</td>
      <td style="white-space:nowrap"><button class="sm ghost row-del" data-id="${t.id}" title="只从清单里移除，不动网盘上的文件">移除</button></td>
    </tr>`;
  }).join("");

  if (shownAll.length > shown.length) {
    body.innerHTML += `<tr><td colspan="7" class="hint" style="padding:10px">还有 ${shownAll.length - shown.length} 项没显示，用搜索或筛选缩小范围</td></tr>`;
  }
  body.querySelectorAll(".row-chk").forEach((node) => {
    node.addEventListener("change", onRowChk);
  });
  body.querySelectorAll(".row-del").forEach((node) => {
    node.addEventListener("click", onRowDelete);
  });
  updatePickInfo();
}

function updateLogs() {
  const logs = (S.engine || {}).logs || [];
  const sig = logs.length + "|" + (logs[logs.length - 1] || {}).t + (logs[logs.length - 1] || {}).msg;
  if (sig === sigLogs) return;
  sigLogs = sig;
  const box = $("#logbox");
  if (!logs.length) { box.innerHTML = `<div class="hint">等待操作…</div>`; return; }
  box.innerHTML = logs.map((l) =>
    `<div class="lv-${l.level}">${esc(l.t)}  ${esc(l.msg)}</div>`).join("");
  box.scrollTop = box.scrollHeight;
}

/* ---------------- 扫码登录 ---------------- */
const qrState = {
  sid: null, timer: null, ticker: null, role: "source",
  startedAt: 0, provider: "", message: "", status: "waiting",
};

function stopQRPoll() {
  if (qrState.timer) { clearTimeout(qrState.timer); qrState.timer = null; }
  if (qrState.ticker) { clearInterval(qrState.ticker); qrState.ticker = null; }
  qrState.sid = null;
}

function renderQrHint() {
  const node = $("#qr-hint");
  if (!node) return;
  const secs = Math.max(0, Math.round((Date.now() - qrState.startedAt) / 1000));
  const base = qrState.message || "等待扫码…";
  node.textContent = qrState.status === "ok" ? base : base + " · 已等待 " + secs + " 秒";
  node.style.color = qrState.status === "ok" ? "var(--ok)"
    : qrState.status === "scanned" ? "var(--warn)"
      : qrState.status === "err" ? "var(--danger)"
        : "var(--muted)";
}

function setQrState(message, status) {
  qrState.message = message || "";
  qrState.status = status || "waiting";
  renderQrHint();
}

// 正常等待时不显示上游细节，只有出现异常状态才亮出来，方便排查
function qrRawText(raw) {
  if (!raw || !Object.keys(raw).length) return "";
  const err = raw.err || "";
  const status = raw.qrCodeStatus || "";
  const state = raw.state;
  const normal =
    err === "TimeoutError" ||
    err === "(空响应)" ||
    status === "NEW" ||
    state === 0;
  if (normal) return "";
  return "上游返回：" + JSON.stringify(raw);
}

async function openQR(role) {
  qrState.role = role;
  qrState.provider = S.config[role].provider;
  const prov = providerOf(qrState.provider);
  $("#qr-title").textContent = "扫码登录 · " + prov.name;
  $("#qr-mask").classList.add("on");
  await startQR();
}

async function startQR() {
  stopQRPoll();
  qrState.startedAt = Date.now();
  qrState.provider = S.config[qrState.role].provider;
  $("#qr-box").innerHTML = `<span class="hint">正在获取二维码…</span>`;
  $("#qr-raw").textContent = "";
  setQrState("正在获取二维码…", "waiting");
  try {
    const res = await api("/api/qr/start", { role: qrState.role });
    qrState.sid = res.sid;
    if (res.image) {
      $("#qr-box").innerHTML =
        `<img src="${res.image}" alt="登录二维码" style="width:240px;height:240px">`;
    } else {
      $("#qr-box").innerHTML =
        `<p class="hint err">二维码图片没能加载出来，点「换个二维码」重试</p>`;
    }
    setQrState(res.hint || "等待扫码…", "waiting");
    qrState.ticker = setInterval(renderQrHint, 1000);
    qrState.timer = setTimeout(pollQR, 300);
  } catch (err) {
    $("#qr-box").innerHTML = "";
    setQrState(err.message, "err");
  }
}

async function pollQR() {
  if (!qrState.sid) return;
  let res;
  try {
    res = await api("/api/qr/poll", { sid: qrState.sid });
  } catch (err) {
    setQrState(err.message, "err");
    $("#qr-raw").textContent = "";
    stopQRPoll();
    return;
  }

  if (res.raw) {
    $("#qr-raw").textContent = qrRawText(res.raw);
  }

  if (res.done) {
    setQrState(res.message || "登录成功", "ok");
    if (qrState.ticker) { clearInterval(qrState.ticker); qrState.ticker = null; }
    qrState.sid = null;
    $("#qr-mask").classList.remove("on");
    toast(res.message || "登录成功");
    await refresh(true);
    return;
  }

  if (res.status === "scanned") {
    setQrState(res.message, "scanned");
  } else if (res.status === "expired" || res.status === "canceled") {
    setQrState(res.message, "err");
    stopQRPoll();
    return;
  } else {
    setQrState(res.message, "waiting");
  }

  // 百度是长轮询，扫到就立即返回；接力要快，避免空档漏掉事件。
  // 阿里是普通轮询，间隔稍长避免给对方压力。
  const gap = qrState.provider === "baidu" ? 150 : 1500;
  if (qrState.sid) qrState.timer = setTimeout(pollQR, gap);
}

/* ---------------- 目录浏览 / 挑选文件 ---------------- */
browse.items = [];
browse.sel = new Set();
browse.extSel = new Set();

// 后缀筛选：支持 ".jpg,.mp4" 或 "jpg mp4"，只影响文件，文件夹始终可见（方便继续往下钻）
/* 当前选中的格式（由下拉候选里勾选，不再手输） */
function extTokens() {
  return [...(browse.extSel || [])];
}

/* 候选项从「当前目录的文件」自动提取：格式 + 出现次数，按数量降序 */
function extOptions() {
  const map = new Map();
  (browse.items || []).forEach((item) => {
    if (item.is_dir === true) return;
    const m = String(item.name || "").toLowerCase().match(/\.([a-z0-9]+)$/);
    const key = m ? "." + m[1] : "(无后缀)";
    map.set(key, (map.get(key) || 0) + 1);
  });
  return [...map.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function updateExtLabel() {
  const label = $("#br-ext-label");
  const trigger = $("#br-ext-trigger");
  const n = (browse.extSel || new Set()).size;
  if (label) label.textContent = n ? `已选 ${n} 种格式` : "全部格式";
  if (trigger) trigger.classList.toggle("on", n > 0);
}

function renderExtMenu() {
  const list = $("#br-ext-list");
  if (!list) return;
  const qInput = $("#br-ext-q");
  const q = String(qInput ? qInput.value : "").trim().toLowerCase().replace(/^\./, "");
  const all = extOptions();
  const opts = q ? all.filter(([ext]) => ext.replace(/^\./, "").includes(q)) : all;
  const clear = (browse.extSel || new Set()).size
    ? `<button class="ext-clear" id="ext-clear-btn">清空筛选</button>`
    : "";
  if (!opts.length) {
    list.innerHTML = `<div class="ext-empty">${
      all.length ? "没有匹配的格式" : "当前目录没有可筛选的格式"
    }</div>${clear}`;
    return;
  }
  list.innerHTML = opts.map(([ext, n]) => `
    <button class="ext-item ${browse.extSel.has(ext) ? "on" : ""}" data-ext="${esc(ext)}">
      <span class="ext-name">${esc(ext)}</span>
      <span class="ext-count">${n}</span>
    </button>`).join("") + clear;
}

function toggleExtMenu(force) {
  const menu = $("#br-ext-menu");
  const trigger = $("#br-ext-trigger");
  if (!menu || !trigger) return;
  const open = force === undefined ? menu.hidden : !!force;
  menu.hidden = !open;
  trigger.classList.toggle("open", open);
  if (open) {
    renderExtMenu();
    const qInput = $("#br-ext-q");
    if (qInput) {
      qInput.value = "";
      setTimeout(() => qInput.focus(), 30);
    }
  }
}

function closeExtMenu() {
  toggleExtMenu(false);
}

function visibleItems() {
  const exts = extTokens();
  return (browse.items || []).filter((item) => {
    if (item.is_dir === true || !exts.length) return true;
    const name = String(item.name || "").toLowerCase();
    return exts.some((ext) => name.endsWith(ext));
  });
}

function findBrowseRow(item) {
  return Array.from(document.querySelectorAll("#br-list .it"))
    .find((node) => node.dataset.path === item.path) || null;
}

/* 底部操作条：只服务源端（目标端的目录选择在右边那张卡里）。 */
function updatePickBar() {
  const add = $("#br-add");
  const info = $("#br-pickinfo");
  const picked = (browse.items || []).filter((item) => browse.sel.has(item.path));
  const files = picked.filter((item) => item.is_dir !== true).length;
  const dirs = picked.length - files;
  if (add) {
    add.disabled = picked.length === 0;
    add.textContent = picked.length
      ? `加入清单（${files} 个文件${dirs ? ` + ${dirs} 个文件夹` : ""}）`
      : "加入清单";
  }
  if (info) {
    info.textContent = picked.length
      ? `已勾选 ${picked.length} 项`
      : (extTokens().length ? `筛选后 ${visibleItems().length} 项` : "");
  }
}

function applyPickVisual(item, row) {
  const on = browse.sel.has(item.path);
  if (!row) return;
  row.classList.toggle("picked", on);
  const box = row.querySelector(".pick");
  if (box) box.checked = on;
}

function setPick(item, on) {
  if (on) browse.sel.add(item.path);
  else browse.sel.delete(item.path);
  applyPickVisual(item, findBrowseRow(item));
  updatePickBar();
}

function togglePick(item) {
  setPick(item, !browse.sel.has(item.path));
}

/* 图标 / 列表两种查看方式：同一份 DOM，只切一个 class。
   图标视图适合看图片、视频这类「靠缩略图认」的文件；
   列表视图信息密度高，适合按名字、大小快速挑。 */
function applyBrowseView() {
  const list = $("#br-list");
  if (!list) return;
  const grid = !!browse.grid;
  list.classList.toggle("grid", grid);
  const g = $("#br-view-grid");
  const l = $("#br-view-list");
  if (g) g.classList.toggle("on", grid);
  if (l) l.classList.toggle("on", !grid);
}

function setBrowseView(grid) {
  browse.grid = !!grid;
  LS.set(LS_BROWSE_GRID, browse.grid);
  applyBrowseView();
}

function renderBrowseRows() {
  const list = $("#br-list");
  if (!list) return;
  const items = visibleItems();
  if (!items.length) {
    list.innerHTML = `<div class="hint" style="padding:10px;grid-column:1/-1">${
      extTokens().length ? "没有符合格式的文件（换个格式试试，或清空筛选）" : "这里是空的"
    }</div>`;
    return;
  }
  list.innerHTML = items.map((item) => {
    const isDir = item.is_dir === true;
    const pickable = browse.role === "source";
    const checked = pickable && browse.sel.has(item.path);
    const lead = pickable
      ? `<input type="checkbox" class="pick" ${checked ? "checked" : ""}>`
      : "";
    // 目标端：文件夹行加一个「选择」按钮，点一下直接设为目录，不用先进去再点「用当前目录」
    const pickDirBtn = (!pickable && isDir)
      ? `<button class="sm pick-dir" data-path="${esc(item.path)}" title="把这个文件夹设为目标目录">选择</button>`
      : "";
    return `<div class="it ${isDir ? "dir" : "file"}${pickable ? " pickable" : ""}${checked ? " picked" : ""}" data-path="${esc(item.path)}">
      ${lead}
      ${fileIconHtml(item)}
      <span class="nm" title="${esc(item.name)}">${esc(item.name)}${isDir ? "/" : ""}</span>
      <span class="hint" style="margin:0">${isDir ? "文件夹" : fmtBytes(item.size)}</span>
      ${pickDirBtn}
    </div>`;
  }).join("");
}

/* 目录导航栏：当前目录下的子文件夹，点一下直接进去。
   这是主流网盘「左树右列表」的轻量版 —— 不做递归懒加载树（对本工具太重，
   每展开一层都要额外请求），纵向定位交给面包屑，横向移动靠这一栏。 */
function renderNav() {
  const nav = $("#br-nav");
  if (!nav) return;
  const dirs = (browse.items || []).filter((item) => item.is_dir === true);
  if (!dirs.length) {
    nav.innerHTML = `<div class="hint" style="padding:8px 10px;font-size:12px">这里没有子文件夹</div>`;
    return;
  }
  nav.innerHTML = dirs.map((dir) => `
    <button class="nav-item" data-path="${esc(dir.path)}" title="${esc(dir.name)}">
      ${fileIconHtml(dir)}<span>${esc(dir.name)}</span>
    </button>`).join("");
}

function parentPath(path) {
  const clean = String(path || "/").replace(/\/+$/, "");
  if (!clean) return "/";
  const cut = clean.lastIndexOf("/");
  return cut <= 0 ? "/" : clean.slice(0, cut);
}

/* 聚焦源端文件浏览器：滚进视野并刷新当前目录。
   目标端不再共用这个浏览器（它只需要选目录，见 loadTargetDir）。 */
async function focusSourceBrowser() {
  // 兜底：不管从哪儿调进来，都保证卡片是展开的（否则滚过去只有一条折叠条）
  if (isFolded("browser")) setFoldGroup("browser", false);
  browse.role = "source";
  browse.sel = new Set();
  browse.extSel = new Set();
  const extBox = $("#br-ext");
  if (extBox) extBox.value = "";
  const pathBox = $("#br-pathbox");
  if (pathBox) pathBox.style.display = "none";
  applyBrowseView();
  updatePickBar();
  requestAnimationFrame(() => {
    const browser = $("#browser");
    if (browser && browser.scrollIntoView) {
      browser.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  });
  await loadBrowse(S.config.source.root_path || "/");
}

/* 方向条：两端的网盘、目录、登录状态都在这条上，交换按钮就在中间。 */
function updateDirectionBar() {
  if (!S) return;
  const srcProv = providerOf(S.config.source.provider) || {};
  const dstProv = providerOf(S.config.target.provider) || {};
  const desc = S.describe || {};
  const put = (id, text) => {
    const el = $(id);
    if (el) el.textContent = text == null ? "" : String(text);
  };

  put("#dir-src-name", srcProv.name || "未选择");
  put("#dir-dst-name", dstProv.name || "未选择");
  // 目录只在「文件与目标」标题条上显示一份，顶部方向条不再重复

  // 目录不在这里显示：源端看文件列表的面包屑，目标端看「写入位置」

  // 折叠态：卡片标题旁显示「当前用的是哪个网盘」（展开时这两个胶囊被 CSS 隐藏）
  put("#br-src-name", srcProv.name || "");
  put("#tg-dst-name", dstProv.name || "");

  // 登录状态做成一个带色点的小标签；没登录时「设置」变成醒目的「去登录」
  // side = DOM 后缀（src/dst），folderRole = 折叠状态里的键（source/target）——
  // 两者必须分开传，之前混用导致「已展开」判断永远为真，按钮一直显示「收起」
  const setSide = (side, folderRole, sideDesc) => {
    const logged = !!(sideDesc || {}).authorized;
    const stateEl = $("#dir-" + side + "-state");
    if (stateEl) {
      stateEl.textContent = logged ? "已登录" : "未登录";
      stateEl.classList.toggle("on", logged);
      stateEl.classList.toggle("off", !logged);
    }
    const setBtn = $("#dir-" + side + "-set");
    if (setBtn) {
      // 面板开着的时候，同一个按钮就是「收起」——不用再去面板里找收起按钮
      const opened = !panelFold[folderRole];
      if (opened) {
        setBtn.textContent = "收起";
        setBtn.classList.remove("need");
        setBtn.classList.add("open");
        setBtn.title = "收起这端的设置面板";
      } else {
        setBtn.textContent = logged ? "设置" : "去登录";
        setBtn.classList.toggle("need", !logged);
        setBtn.classList.remove("open");
        setBtn.title = logged
          ? "展开这端的网盘与登录设置"
          : "这端还没登录，点这里去登录";
      }
    }
  };
  setSide("src", "source", desc.source);
  setSide("dst", "target", desc.target);

  // 目录栏前缀：标明这一栏是哪个网盘、哪个账号（账号拿不到就只显示网盘名）
  const setProv = (sel, role) => {
    const el = $(sel);
    if (!el) return;
    const prov = providerOf((S.config[role] || {}).provider) || {};
    const sideDesc = desc[role] || {};
    const acct = sideDesc.account || sideDesc.user || "";
    el.innerHTML = esc(prov.name || "未选择")
      + (acct ? `<span class="acct">${esc(acct)}</span>` : "");
  };
  setProv("#br-prov", "source");
  setProv("#tg-prov", "target");

  const btn = $("#btn-swap");
  if (btn) {
    const busy = !!(S.engine || {}).running;
    btn.disabled = busy;
    btn.title = busy
      ? "迁移进行中，先暂停再交换"
      : "把源和目标整体对调：网盘、登录状态、目录全部互换";
  }
}

/* 点方向条上的入口：开着就收起，关着就展开。
   面板是浮层（absolute），展开不会把下面的文件列表和作业区往下挤。 */
function toggleSidePanel(role) {
  panelFold[role] = !panelFold[role];
  panelFoldSet[role] = true;
  LS.set(LS_FOLD, panelFold);
  LS.set(LS_FOLD_SET, panelFoldSet);
  applyFold(role);
  updateDirectionBar();
}

function closeSidePanels(exceptRole) {
  let changed = false;
  ["source", "target"].forEach((role) => {
    if (role === exceptRole || panelFold[role]) return;
    panelFold[role] = true;
    changed = true;
    applyFold(role);
  });
  if (changed) {
    LS.set(LS_FOLD, panelFold);
    updateDirectionBar();
  }
}

/* ---------------- 板块折叠：折叠后只剩标题行 + 一个「展开」按钮 ---------------- */
const CARD_FOLDS = ["browser", "tdir", "job", "picked", "settings", "log"];

function applyCardFold(key, folded) {
  const body = document.querySelector(`[data-fold-body="${key}"]`);
  if (body) {
    // 已选清单特殊：收起时仍要露三行文件，所以不整体隐藏，交给 CSS 收窄
    body.style.display = (folded && key === "picked") ? "" : (folded ? "none" : "");
  }
  const btn = document.querySelector(`[data-fold-btn="${key}"]`);
  if (btn) {
    const grouped = foldGroupOf(key).length > 1;
    // 箭头不再用字符（⌄ / ⌃ 在不同字体下形状和位置都不一样，看着很跳），
    // 改由 CSS 的 ::before 画一个固定尺寸的 chevron，随状态翻转。
    btn.textContent = folded ? "展开" : "收起";
    // 联动的一组用「这一排」措辞，点一次两边一起动，避免让人以为要各点一次
    btn.title = grouped
      ? (folded ? "展开这一排（左右同时展开）" : "收起这一排（左右同时收起）")
      : (folded ? "展开这个板块" : "收起这个板块");
  }
  const card = document.querySelector(`[data-fold="${key}"]`);
  if (card) card.classList.toggle("is-folded", folded);
  // 标题条跟着变色：CSS 的 :has(+ .card.is-folded) 只对「紧跟卡片」的板块生效，
  // 而 browser / settings 后面跟的是 .panels / .bottom-grid，会漏掉 → 由 JS 统一打标
  const head = document.querySelector(`[data-fold-btn="${key}"]`);
  const headBar = head ? head.closest(".row-head") : null;
  if (headBar) headBar.classList.toggle("is-collapsed", folded);
  if (typeof syncRowVisibility === "function") syncRowVisibility();
}

/* 同一排里的两块必须同步收放，否则一高一矮、栅格行高被撑乱。
   主区一排：browser（左） ⇄ tdir（右）；底部一排：settings（左） ⇄ log（右）。 */
const FOLD_GROUPS = [
  ["browser", "tdir"],
  ["settings", "log"],
];

function foldGroupOf(key) {
  const group = FOLD_GROUPS.find((list) => list.includes(key));
  return group || [key];
}

function isFolded(key) {
  // 以卡片的 .is-folded 为准。不能看 body 的 display：
  // 已选清单收起时 body 仍然显示（要露三行表格），display 不是 "none"，
  // 会让 isFolded 永远返回 false → 再点一次又变成「收起」，按钮永远展不开。
  const card = document.querySelector(`[data-fold="${key}"]`);
  if (card) return card.classList.contains("is-folded");
  const body = document.querySelector(`[data-fold-body="${key}"]`);
  return !!body && body.style.display === "none";
}

function setFoldGroup(key, folded) {
  foldGroupOf(key).forEach((each) => {
    applyCardFold(each, folded);
    LS.set("pm.fold2." + each, folded);
  });
}

/* 折叠后卡片仍保留标题与功能按钮，容器不需要隐藏。
   保留这个函数是为了让 applyCardFold 的调用点保持稳定。 */
function syncRowVisibility() {}

function initCardFolds() {
  // 先按存储恢复，再把同组的状态拉齐（历史上可能只折叠了其中一个）
  CARD_FOLDS.forEach((key) => {
    applyCardFold(key, !!LS.get("pm.fold2." + key, false));
  });
  FOLD_GROUPS.forEach(([first, ...rest]) => {
    const folded = isFolded(first);
    rest.forEach((key) => {
      if (isFolded(key) !== folded) applyCardFold(key, folded);
    });
  });

  CARD_FOLDS.forEach((key) => {
    const btn = document.querySelector(`[data-fold-btn="${key}"]`);
    if (btn) {
      btn.addEventListener("click", (event) => {
        event.stopPropagation();
        setFoldGroup(key, !isFolded(key));
      });
    }

    // 折叠之后只靠右边那个小按钮才能展开，整条标题行看着可点其实点不动，
    // 手一歪就点在空白处、毫无反应。这里让「整条」都能点回来：
    //   · 标题条（.row-head）
    //   · 卡片折叠后残留的标题带（.br-head / .tdir-head / .card-head）
    // 只在折叠态接管，展开时完全不干扰正常操作；
    // 点在按钮、输入框、表格上时让开，避免抢掉本该属于控件的点击。
    const targets = [
      btn ? btn.closest(".row-head") : null,
      document.querySelector(`[data-fold="${key}"]`),
    ].filter(Boolean);

    targets.forEach((node) => {
      if (node.dataset.foldToggleBound) return;
      node.dataset.foldToggleBound = "1";
      node.addEventListener("click", (event) => {
        if (!isFolded(key)) return;
        // 只让开真正的控件；不要再排除 table / .list-ops ——
        // 已选清单折叠时仍然露着工具栏和三行表格，那是它可见面积的绝大部分，
        // 一起排掉就等于这块「点哪儿都没反应」。
        if (event.target.closest("button, a, input, select, textarea, label")) return;
        setFoldGroup(key, false);
      });
    });
  });
}

/* ---------------- 目标端：写入位置选择器 ---------------- */
function renderTargetCrumbs(path) {
  const parts = String(path || "/").split("/").filter(Boolean);
  let acc = "";
  let html = `<a data-nav="/">根目录</a>`;
  parts.forEach((part) => {
    acc += "/" + part;
    html += `<span class="sep">›</span><a data-nav="${esc(acc)}">${esc(part)}</a>`;
  });
  const box = $("#tg-crumbs");
  if (box) {
    box.innerHTML = html;
    box.querySelectorAll("a").forEach((node) => {
      node.addEventListener("click", () => loadTargetDir(node.dataset.nav));
    });
  }
  const up = $("#tg-up");
  if (up) up.disabled = !path || path === "/";
  const cur = $("#tg-path");
  if (cur) cur.textContent = path || "/";
}

function renderTargetDir() {
  const list = $("#tg-list");
  if (!list) return;
  if (!tdir.items.length) {
    list.innerHTML = `<div class="tdir-empty">这里没有子文件夹 —— 可以直接点「就用这个目录」</div>`;
    return;
  }
  list.innerHTML = tdir.items.map((dir) => `
    <button class="nav-item" data-path="${esc(dir.path)}" title="进入 ${esc(dir.name)}">
      ${fileIconHtml(dir)}<span>${esc(dir.name)}</span>
    </button>`).join("");
}

/* 目标端只列文件夹：选的是「写到哪」，不需要看文件 */
async function loadTargetDir(path) {
  tdir.path = path || "/";
  renderTargetCrumbs(tdir.path);
  const list = $("#tg-list");
  if (list) list.innerHTML = `<div class="tdir-empty">读取中…</div>`;
  try {
    const res = await api("/api/browse", { role: "target", path: tdir.path });
    tdir.items = (res.entries || []).filter((item) => item.is_dir === true);
    renderTargetDir();
  } catch (err) {
    tdir.items = [];
    if (list) list.innerHTML = `<div class="tdir-empty">${esc(err.message)}</div>`;
  }
}

async function loadBrowse(path) {
  browse.path = path;
  renderCrumbs(path);
  $("#br-list").innerHTML = `<div class="hint" style="padding:10px;grid-column:1/-1">读取中…</div>`;
  const nav = $("#br-nav");
  if (nav) nav.innerHTML = "";
  try {
    const res = await api("/api/browse", { role: browse.role, path });
    browse.items = res.entries || [];
    renderBrowseRows();
    renderNav();
  } catch (err) {
    browse.items = [];
    if (nav) nav.innerHTML = "";
    $("#br-list").innerHTML =
      `<div class="hint err" style="padding:10px">${esc(err.message)}</div>
       <div class="hint" style="padding:0 10px">目录不存在时迁移会自动创建；也可以先返回上级选现有目录。</div>`;
  }
  updatePickBar();
}

/* 面包屑：纵向定位。根目录显示「我的文件」，每级可点。 */
function renderCrumbs(path) {
  const parts = String(path || "/").split("/").filter(Boolean);
  let acc = "";
  let html = `<a data-nav="/">我的文件</a>`;
  parts.forEach((part) => {
    acc += "/" + part;
    html += `<span class="sep">›</span><a data-nav="${esc(acc)}">${esc(part)}</a>`;
  });
  const box = $("#br-crumbs");
  if (box) {
    box.innerHTML = html;
    box.querySelectorAll("a").forEach((node) => {
      node.addEventListener("click", () => loadBrowse(node.dataset.nav));
    });
  }
  const up = $("#br-up");
  if (up) up.disabled = !path || path === "/";
}

/* ---------------- 绑定 ---------------- */
function bind() {
  $("#btn-scan").addEventListener("click", () => doScan(false));
  $("#btn-start").addEventListener("click", doStart);

  $("#btn-pause").addEventListener("click", async () => {
    try { await api("/api/pause", {}); toast("已暂停"); } catch (err) { toast(err.message); }
  });

  // 停止：中止当前迁移 + 取消所有勾选（「清空任务」已移除 —— 它和清单里的
  // 「全部移除」后端都是 db.clear()，功能完全重复，留着只会让人分不清）
  $("#btn-stop").addEventListener("click", async () => {
    const btn = $("#btn-stop");
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "停止中…";
    try {
      const res = await api("/api/stop", {});
      sigTasks = "";
      await refresh(false);
      if (res.stopped) {
        toast(`已停止迁移，并取消 ${res.cleared || 0} 项勾选（已传进度保留）`);
      } else {
        toast(`已取消 ${res.cleared || 0} 项勾选`);
      }
    } catch (err) {
      toast(err.message);
    } finally {
      btn.textContent = label;
      updateStatus();
    }
  });

  const saveTransfer = () => {
    const patch = {
      file_concurrency: Number($("#cfg-file-conc").value) || 2,
      part_concurrency: Number($("#cfg-part-conc").value) || 4,
      bandwidth_kbps: Math.max(0, Number($("#cfg-bandwidth").value) || 0),
      conflict: $("#cfg-conflict").value,
      structure: $("#cfg-structure").value,
      verify: $("#cfg-verify").checked,
    };
    // 分片输入框被锁住时（目标网盘固定分片），框里显示的是服务端真实值，
    // 不能回写配置 —— 否则会把用户为阿里云设的 8/10MB 悄悄改成 4MB。
    const sizeInput = $("#cfg-part-size");
    if (sizeInput && !sizeInput.disabled) {
      patch.part_size_mb = Number(sizeInput.value) || 8;
    }
    api("/api/config", { transfer: patch })
      .then(() => toast("设置已保存，下一轮生效"))
      .catch((err) => toast(err.message));
  };
  ["#cfg-file-conc", "#cfg-part-conc", "#cfg-part-size", "#cfg-bandwidth",
   "#cfg-conflict", "#cfg-structure"].forEach((sel) => {
    $(sel).addEventListener("change", saveTransfer);
  });
  $("#cfg-verify").addEventListener("change", saveTransfer);

  // 登录浏览器单独存（属于 login 段，不是 transfer 段）
  const lbSel = $("#cfg-login-browser");
  if (lbSel) {
    lbSel.addEventListener("change", () => {
      api("/api/config", { login: { browser: lbSel.value } })
        .then(() => toast("登录浏览器已保存，下次登录生效"))
        .catch((err) => toast(err.message));
    });
  }

  // 退出程序：双击 .app 启动时没有终端，这里是唯一的正常退出入口
  const quitBtn = $("#btn-quit");
  if (quitBtn) {
    quitBtn.addEventListener("click", () => {
      if (!window.confirm("退出搬山后台进程？迁移中的任务会先被拒绝，请确认当前没有在传文件。")) return;
      quitBtn.disabled = true;
      api("/api/quit", {})
        .then(() => {
          document.body.innerHTML =
            '<div style="padding:56px 24px;text-align:center;font:15px/1.9 system-ui">' +
            '<p style="font-size:20px;margin:0 0 12px">搬山已退出</p>' +
            '<p style="color:#8a8f99;margin:0">可以关掉这个标签页了。下次双击「搬山」会重新启动并自动打开界面。</p>' +
            "</div>";
        })
        .catch((err) => { quitBtn.disabled = false; toast(err.message); });
    });
  }

  // 方向箭头
  const swapBtn = $("#btn-swap");
  if (swapBtn) swapBtn.addEventListener("click", toggleDirection);

  // 预设档
  document.querySelectorAll("#presets .preset").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.classList.contains("custom")) return; // 自定义只是个状态显示，不可点
      applyPreset(btn.dataset.preset);
    });
  });

  $("#br-view-grid").addEventListener("click", () => setBrowseView(true));
  $("#br-view-list").addEventListener("click", () => setBrowseView(false));

  $("#br-list").addEventListener("click", (event) => {
    // 目标端「选择」按钮：直接设为目录，不再进入
    if (event.target.classList.contains("pick-dir")) {
      const path = event.target.dataset.path;
      const role = browse.role;
      save(role, { root_path: path }).then(() => toast(`已选择：${path}`));
      return;
    }
    if (event.target.classList.contains("pick")) return; // 勾选框交给 change 处理
    const row = event.target.closest(".it");
    if (!row) return;
    const item = (browse.items || []).find((it) => it.path === row.dataset.path);
    if (!item) return;
    if (item.is_dir === true) {
      const base = browse.path === "/" ? "" : browse.path;
      loadBrowse(base + "/" + item.name);
      return;
    }
    if (browse.role !== "source") return;
    togglePick(item);
  });

  $("#br-list").addEventListener("change", (event) => {
    if (!event.target.classList.contains("pick")) return;
    const row = event.target.closest(".it");
    const item = (browse.items || []).find((it) => it.path === row.dataset.path);
    if (item) setPick(item, event.target.checked);
  });

  // ---- 格式筛选：候选项来自当前目录，点选即可，不用手输 ----
  $("#br-ext-trigger").addEventListener("click", (event) => {
    event.stopPropagation();
    toggleExtMenu();
  });

  $("#br-ext-q").addEventListener("input", renderExtMenu);

  $("#br-ext-list").addEventListener("click", (event) => {
    if (event.target.id === "ext-clear-btn") {
      browse.extSel = new Set();
      updateExtLabel();
      renderExtMenu();
      renderBrowseRows();
      updatePickBar();
      return;
    }
    const item = event.target.closest(".ext-item");
    if (!item) return;
    const ext = item.dataset.ext;
    if (browse.extSel.has(ext)) browse.extSel.delete(ext);
    else browse.extSel.add(ext);
    updateExtLabel();
    renderExtMenu();
    renderBrowseRows();
    updatePickBar();
  });

  // 点别处收起候选面板
  document.addEventListener("click", (event) => {
    const box = $("#br-ext-filter");
    if (box && !box.contains(event.target)) closeExtMenu();
  });

  $("#br-pickall").addEventListener("click", () => {
    const files = visibleItems().filter((item) => item.is_dir !== true);
    if (!files.length) return toast("当前范围没有可勾选的文件");
    const allOn = files.every((item) => browse.sel.has(item.path));
    files.forEach((item) => {
      if (allOn) browse.sel.delete(item.path);
      else browse.sel.add(item.path);
    });
    renderBrowseRows();
    updatePickBar();
    // 这个按钮只勾文件、不勾文件夹。不明确说一句，用户会以为「全选」= 整个目录都选了，
    // 结果子目录里的文件全没进清单（实测漏掉 107 个）。
    const dirCount = visibleItems().filter((item) => item.is_dir === true).length;
    if (!allOn && dirCount > 0) {
      toast(`已选 ${files.length} 个文件；文件夹未选（勾文件夹会连带里面的文件一起加入）`);
    }
  });

  $("#br-picknone").addEventListener("click", () => {
    browse.sel.clear();
    renderBrowseRows();
    updatePickBar();
  });

  $("#br-paste").addEventListener("click", () => {
    const box = $("#br-pathbox");
    const open = box.style.display === "none";
    box.style.display = open ? "" : "none";
    if (open) $("#br-paths").focus();
  });

  $("#br-addpaths").addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    const paths = ($("#br-paths").value || "")
      .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
    if (!paths.length) return toast("先粘贴至少一个路径");
    btn.disabled = true;
    btn.textContent = "加入中…";
    try {
      const res = await api("/api/pick_paths", { paths });
      $("#br-paths").value = "";
      toast(res.message || `已加入 ${res.count || 0} 个文件`);
      sigTasks = "";
      await refresh(false);
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "按路径加入清单";
    }
  });

  // 勾选的文件 / 文件夹一起提交；文件夹由服务端递归展开
  $("#br-add").addEventListener("click", async (event) => {
    const picked = (browse.items || []).filter((item) => browse.sel.has(item.path));
    if (!picked.length) return toast("先勾选文件或文件夹");
    const btn = event.currentTarget;
    const label = btn.textContent;
    btn.disabled = true;
    btn.textContent = "正在加入…";
    try {
      const res = await api("/api/pick", { items: picked });
      toast(res.message || `已加入 ${res.count || picked.length} 个文件`);
      browse.sel.clear();
      renderBrowseRows();
      updatePickBar();
      sigTasks = "";
      await refresh(false);
      // 内联浏览器常驻，加完接着挑下一个目录即可
    } catch (err) {
      toast(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
      updatePickBar();
    }
  });

  $("#btn-scan-stop").addEventListener("click", async () => {
    try {
      await api("/api/scan_cancel", {});
      toast("已请求停止扫描");
    } catch (err) {
      toast(err.message);
    }
  });

  // 全选只作用于「当前搜索/筛选范围内」的行，避免误勾没显示出来的文件
  $("#chk-all").addEventListener("change", (event) => {
    const ids = visibleTasks(S.tasks || [])
      .filter((t) => t.state !== "done" && t.state !== "skipped")
      .map((t) => t.id);
    if (!ids.length) {
      toast("当前范围内没有可勾选的文件");
      event.target.checked = false;
      return;
    }
    setSelection({ ids, selected: event.target.checked });
  });
  $("#btn-pick-none").addEventListener("click", () => setSelection({ all: false }));
  $("#btn-pick-open").addEventListener("click", () => setSelection({ all: true, only_open: true }));

  $("#task-search").addEventListener("input", (event) => {
    view.q = event.target.value;
    sigTasks = "";
    updateTasks();
  });
  $("#task-filter").addEventListener("change", (event) => {
    view.filter = event.target.value;
    sigTasks = "";
    updateTasks();
  });
  $("#task-sort").addEventListener("change", (event) => {
    view.sort = event.target.value;
    sigTasks = "";
    updateTasks();
  });

  $("#btn-retry").addEventListener("click", async () => {
    try {
      const res = await api("/api/retry", {});
      sigTasks = "";
      await refresh(false);
      toast(res.changed ? `已重新排队 ${res.changed} 个任务，点「开始迁移」继续` : "没有失败或中断的任务");
    } catch (err) { toast(err.message); }
  });

  $("#btn-report").addEventListener("click", () => {
    window.location.href = "/api/report.csv";
  });

  $("#btn-remove-all").addEventListener("click", async () => {
    const total = (S.tasks || []).length;
    if (!total) return toast("清单已经是空的");
    if (!confirm(`确定把清单里的 ${total} 个文件全部移除？\n（只从清单移除，网盘上的文件不会动）`)) return;
    try {
      await api("/api/remove", { ids: [] });
      sigTasks = "";
      await refresh(false);
      toast(`已移除 ${total} 项`);
    } catch (err) { toast(err.message); }
  });

  // ---- 源端文件浏览器 ----
  $("#br-up").addEventListener("click", () => loadBrowse(parentPath(browse.path)));

  // 目录导航：点子文件夹直接进入
  $("#br-nav").addEventListener("click", (event) => {
    const btn = event.target.closest(".nav-item");
    if (!btn) return;
    loadBrowse(btn.dataset.path);
  });

  // ---- 方向条上的「设置 / 去登录 / 收起」：同一个入口来回切 ----
  // 必须 stopPropagation：否则这次点击会继续冒泡到下面的 document 监听，
  // 刚展开就被「点外部收起」的逻辑立刻收回去，表现为点了没反应。
  $("#dir-src-set").addEventListener("click", (event) => {
    event.stopPropagation();
    toggleSidePanel("source");
  });
  $("#dir-dst-set").addEventListener("click", (event) => {
    event.stopPropagation();
    toggleSidePanel("target");
  });

  // 点面板以外的地方自动收起，不用专门找关闭按钮
  document.addEventListener("click", (event) => {
    ["source", "target"].forEach((role) => {
      if (panelFold[role]) return;              // 本来就是收起状态
      const card = $("#panel-" + role);
      const entry = $("#dir-" + role + "-set");
      if (card && card.contains(event.target)) return;
      if (entry && entry.contains(event.target)) return;
      closeSidePanels(null);
    });
  });

  // ---- 目标端：写入位置 ----
  $("#tg-up").addEventListener("click", () => loadTargetDir(parentPath(tdir.path)));

  $("#tg-list").addEventListener("click", (event) => {
    const btn = event.target.closest(".nav-item");
    if (!btn) return;
    loadTargetDir(btn.dataset.path);
  });

  $("#tg-pick").addEventListener("click", async () => {
    await save("target", { root_path: tdir.path });
    toast(`目标目录已设为：${tdir.path}`);
  });

  $("#tg-mkdir").addEventListener("click", async () => {
    const name = (prompt("新文件夹名称") || "").trim();
    if (!name) return;
    const base = tdir.path === "/" ? "" : tdir.path;
    const full = base + "/" + name;
    try {
      await api("/api/mkdir", { role: "target", path: full });
      await loadTargetDir(full);
      toast("已创建：" + full);
    } catch (err) {
      toast(err.message);
    }
  });

  $("#qr-close").addEventListener("click", () => { stopQRPoll(); $("#qr-mask").classList.remove("on"); });
  $("#qr-cancel").addEventListener("click", () => { stopQRPoll(); $("#qr-mask").classList.remove("on"); });
  $("#qr-refresh").addEventListener("click", startQR);
  $("#qr-mask").addEventListener("click", (event) => {
    if (event.target.id === "qr-mask") { stopQRPoll(); $("#qr-mask").classList.remove("on"); }
  });
}

/* ---------------- 主题切换 ----------------
   两套皮肤是两份 CSS：style.css（浅色基础）+ theme-dark.css（暗色覆盖）。
   切换靠 <link id="theme-dark"> 的 media 开关，而不是在同一张表里靠权重打架。
   注意：首次打开时主题已经在 head 的内联脚本里定好了（避免闪白），
   这里只负责把按钮状态同步过去、以及响应点击。 */
function applyTheme(theme, persist) {
  const t = theme === "dark" ? "dark" : "light";
  document.documentElement.setAttribute("data-theme", t);
  // 两份皮肤互为开关：启用一份就关掉另一份，避免两边规则互相覆盖
  const dark = document.getElementById("theme-dark");
  const light = document.getElementById("theme-light");
  if (dark) dark.setAttribute("media", t === "dark" ? "all" : "not all");
  if (light) light.setAttribute("media", t === "dark" ? "not all" : "all");
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.title = t === "dark" ? "切换到浅色主题" : "切换到深色主题";
  if (persist) {
    try { localStorage.setItem("pm.theme", t); } catch (e) {}
  }
}

function initTheme() {
  const btn = document.getElementById("theme-toggle");
  // 同步一次，保证 head 脚本设定的主题和按钮图标一致
  applyTheme(document.documentElement.getAttribute("data-theme"), false);
  if (!btn) return;
  btn.addEventListener("click", () => {
    const now = document.documentElement.getAttribute("data-theme");
    applyTheme(now === "dark" ? "light" : "dark", true);
  });
}

bind();
initTheme();
initCardFolds();
applyDirection();
refresh(true).then(async () => {
  await focusSourceBrowser();
  await loadTargetDir(S.config.target.root_path || "/");
});
setInterval(() => refresh(false), 1200);
