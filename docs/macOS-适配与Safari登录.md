# macOS 适配与 Safari 登录（v1.26.0，2026-10-27）

本机环境：Apple Silicon / macOS 26 / Python 3.13.12 / Safari 27.0.1 / Edge 与 Chrome 均在 `/Applications`。

---

## 一、这轮改了什么

| 项 | 内容 | 影响范围 |
|---|---|---|
| 页面注入清理 | 从资料库取回的 `index.html` 带 282 个 `data-page-node-id`、123 处 pnid 注释和一行指向 `/page/page_comm/inject.js` 的脚本（本地服务里必然 404）。已全部清除 | 全平台（体积 34KB → 17.5KB） |
| CSS 死规则归并 | `style.css` 删掉 47 个「选择器在 HTML/JS 里根本不存在」的规则块，3200 → 3117 行 | 全平台（像素级不变） |
| mac 图标 | 由 `icon_512.png` 生成全尺寸 `icon.icns`；`panmigrate.spec` 按平台挂 `.icns` / `.ico` | mac（onefile 产物不显示图标，图标在 `.app` 上生效） |
| 浏览器选路 | 原代码只认 Windows 的 `C:\...\msedge.exe`，mac 上必然报「没找到 Edge/Chrome」。改为按平台给候选清单，Windows 清单与顺序**原样不动** | 两端都修好，Windows 行为不变 |
| Safari 登录通道 | 新增 `panmigrate/safari.py`，用系统自带 `safaridriver` 走 W3C WebDriver 读 cookie / localStorage | 仅 macOS |

---

## 二、Safari 登录怎么用

### 1. 一次性授权（不授权会明确提示，不会静默失败）

任选一条：

- **图形界面**：Safari →「设置」→「高级」→ 勾选「显示网页开发者功能」；再到菜单栏「开发」→ 勾选「允许远程自动化」。
- **终端**：`sudo safaridriver --enable`（只需一次）。

没授权时，界面里的报错是这样的（已实测）：

> Safari 还没允许被自动化控制。请按这个顺序开一次：① Safari →「设置」→「高级」→ 勾选「显示网页开发者功能」；② 菜单栏「开发」→ 勾选「允许远程自动化」；③ 回来重试。（也可以在终端执行一次 sudo safaridriver --enable）

### 2. 选浏览器

设置 →「高级设置」→ **网页登录用哪个浏览器**：

| 选项 | 实际行为 |
|---|---|
| 自动（默认） | 先用 Chrome / Edge；macOS 上没有它们才用 Safari。**Windows 上等于原来的行为** |
| 只用 Chrome / Edge | 只用 Chromium 系，找不到就报错 |
| 只用 Safari（macOS） | 仅 macOS 且存在 `safaridriver` 时出现该选项；Windows 上这一项不显示，配置里写了也会被忽略 |

覆盖的入口：天翼「一键登录」、115 / 123「扫码登录」。

### 3. 与 Chrome/Edge 方案的差别（用之前要知道）

| | Chrome / Edge（CDP） | Safari（WebDriver） |
|---|---|---|
| 独立 profile | 有，登录态可复用，第二次登录几乎瞬间完成 | 无，每次是干净会话，需重新登录 |
| 标签页 | 多标签，能同时盯多个域 | 单窗口，123 的 token 只能等页面自己跳到 `yun.123pan.cn` 才读到 |
| 前置条件 | 装浏览器即可 | 必须先授权远程自动化 |
| 读取能力 | CDP `Storage.getCookies`（含 HttpOnly） | WebDriver cookie 接口（含 HttpOnly）+ `document.cookie` 兜底 |

---

## 三、数据目录（mac）

| 运行方式 | 位置 |
|---|---|
| 打包产物 | `~/Library/Application Support/panmigrate/`（`config.json` + `tasks.db`） |
| 源码运行 | 项目内 `data/` |
| 指定目录 | 环境变量 `PANMIGRATE_HOME=/你的路径` |
| 便携 | 程序同级放一个 `panmigrate-data/` 文件夹就优先用它 |

已实测：模拟 `sys.frozen` 后落在 `~/Library/Application Support/panmigrate`，`PANMIGRATE_HOME` 覆盖有效。

---

## 四、验证记录（都在本机跑过）

- `python3 -m unittest discover -s tests -t .`：**250 个用例全绿**（原 242 + 新增 8 个 Safari/平台闸用例）
- CSS 归并像素对比：亮色首屏差异 23 像素 / 最大通道差 9；暗色 **0 像素**。对照：同一版本连拍两次差异 919 像素 / 最大差 189 —— 归并后的差异小于渲染噪声两个数量级
- mac 上 CDP 通道冒烟：Edge 拉起成功，调试端口就绪，`https://115.com/?mode=wangpan` 页面可控制，关闭正常
- Safari 通道真机：能拉起 `safaridriver` 并建会话；未授权时按上面那段中文提示失败（说明错误翻译链路是通的）
- Windows 不受影响的三道闸（均有测试钉住）：
  1. `_chromium_candidates()` 在 `win32` 下返回的仍是原来那四条路径、同样顺序
  2. 配置里写 `safari`，在非 macOS 上一律忽略，回落到 Chromium
  3. `TyLogin` / `WebLogin` 不传 `backend` 时默认仍是 `cdp`

---

## 五、还没做的

- **mac 封包**：`dist/搬山`（onefile，arm64）尚未用 v1.26.0 重新打；`.app` 包（带图标、双击启动）也未打。需要时按 `macOS打包与迁移使用说明.md` 的命令跑，spec 已就绪。
- **首次启动的日志噪音**（既有问题，非本轮引入）：未登录时打开界面，`运行日志` 里会打一整段 Python traceback（`AliyunAuthError: 未授权`）。Windows 上同样如此。建议改成一句人话 + traceback 只落盘日志文件。

---

## 六、封包与使用（2026-10-27 补）

一条命令出两个产物：

```bash
bash build_mac.sh          # 内置跑测试 → 清缓存打包 → 组装 .app → ad-hoc 签名
```

| 产物 | 用法 |
|---|---|
| `dist/搬山.app` | **双击启动**，Dock 有图标，自动打开浏览器；界面右上角「退出」结束进程 |
| `dist/搬山` | 单文件（arm64，10MB），终端 `./dist/搬山` 启动，Ctrl+C 停止 |
| `dist/搬山-macOS-arm64-v1.26.0.zip` | `.app` 的压缩包，拷到别的 Mac 用这个（保住签名位） |

实测（2026-10-27）：

- `open dist/搬山.app` → 8899 就绪，`/`、`/style.css`、`/theme-light.css`、`/theme-dark.css`、`/app.js`、`/logo/*` 全 200；`/api/state` 六家网盘齐；`/api/qr/debug` 200
- 数据目录 `~/Library/Application Support/panmigrate`
- `codesign --verify --deep --strict` 通过（ad-hoc）
- 界面右上角「退出」→ `/api/quit` → 进程 exit 0，端口释放

注：`/tylogin.js` 返回 404 是既有现象（不在静态白名单里），源码直跑同样如此，不影响功能。

---

## 七、这轮修掉的两个登录问题

### 1. 「浏览器刚打开就自动关闭」

真因不是代码：**如果搬山进程本身跑在受限环境里**（例如被别的沙箱/终端托管），它拉起的 Edge 会继承限制，Chromium 起不来：

```
sandbox initialization failed: Operation not permitted
GPU process isn't usable. Goodbye.
```

双击 `.app` 启动（由 launchd 拉起）不受此影响，已实测能稳定停在「等待登录」。

代码侧做了两件事避免误判和难排查：

- 存活判定从 `proc.poll()` 改成**看调试端口**：macOS 上 Chromium 可能把活交给子进程、自己先退出，用进程状态会把「窗口还开着」误判成「已关闭」。连续三次端口不通才判定关闭。
- 浏览器自己的 stderr 落到数据目录（`tylogin-browser.log` / `weblogin-<站点>-browser.log`），失败时把最后几条真实报错带进提示，例如「浏览器窗口已关闭或失去响应（浏览器最后报错：GPU process isn't usable）」——不再是干巴巴一句「窗口已关闭」。

### 2. 天翼一键登录没有取消入口

只有扫码登录有取消接口，天翼那边中途放弃只能等 5 分钟超时，期间再点还会被「已有一个登录窗口在进行中」挡住。

- 新增 `POST /api/tylogin/cancel`
- 界面上「一键登录天翼云盘」按钮在等待期间变成**「取消登录」**，点一下就收摊

实测：start → waiting_login → cancel → `已取消登录`，窗口关闭。
