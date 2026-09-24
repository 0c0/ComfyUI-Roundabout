/**
 * view.html 前端行为冒烟：用 jsdom 真跑页面脚本，验证「产物自动同步」这条链路。
 *
 * 为什么不用真浏览器：Chromium 要另下 500MB；这里要验证的行为（探测 → 原地刷新 /
 * 左下角提示）全是 DOM 状态变化，jsdom + fetch 打桩足够覆盖，还能进 CI。
 *
 * 运行（先装 jsdom 到托管 node 工作区）：
 *   NODE_PATH=<node-workspace>/node_modules node tests/test_view_frontend.cjs
 */

const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');

// tests/ 的上一级才是节点目录
const NODE = path.join(__dirname, '..');
const HTML = fs.readFileSync(path.join(NODE, 'web', 'view.html'), 'utf8');
// 页面里的每页条数按视口高度自适应，量不到布局时退回 DEFAULT_PAGE_SIZE；
// jsdom 没有排版（所有 getBoundingClientRect 都是 0），所以整份测试走的正是这个兜底值。
const PAGE_SIZE = 120;

let failures = 0;
const check = (name, ok, detail = '') => {
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${name}${!ok && detail ? ' :: ' + detail : ''}`);
  if (!ok) failures += 1;
};

// ---------------------------------------------------------------- 假后端状态
const mkFile = (name, mtime) => ({
  name, mtime, size: 1024, kind: 'image',
  url: `/view?filename=${name}&type=output`, thumb: `/view?filename=${name}&type=output`,
});

// 造满一页多，才能测「不在第一页时不打断」
let seq = 0;
let files = [];
/** 模拟一次新生成：往 output 目录加一个最新的文件 */
function addFile() {
  seq += 1;
  const f = mkFile(`gen_${String(seq).padStart(4, '0')}.png`, 1000 + seq);
  files.push(f);
  return f;
}
files = Array.from({ length: PAGE_SIZE + 10 }, addFile);

let fullLoads = 0;   // 完整列目录（limit=PAGE_SIZE）次数 —— 判断有没有多余重载
let probes = 0;      // 轻量探测（limit=1）次数

function filesPayload(offset, limit) {
  const sorted = [...files].sort((a, b) => b.mtime - a.mtime);
  const page = sorted.slice(offset, offset + limit);
  return {
    root: 'output', root_path: 'E:\\fake\\output', path: '',
    crumbs: [{ name: 'output', path: '' }],
    dirs: [], files: page,
    total: sorted.length, dir_count: 0, offset, limit,
    has_more: offset + page.length < sorted.length, truncated: false, sort: 'mtime', order: 'desc',
  };
}

const jsonResponse = obj => Promise.resolve({ status: 200, ok: true, json: async () => obj });
const errResponse = (status, code, message) =>
  Promise.resolve({ status, ok: false, json: async () => ({ error: { code, message } }) });

// ---- 看板：一张普通图卡 + 两张外部卡（后端会把 input/output 之外的路径标成 ext）----
const boardItems = [
  { id: 'card_img_1', title: '分镜 1', kind: 'image', note: '', model: 'z-image-turbo',
    url: 'http://127.0.0.1:8188/view?filename=a.png&type=output', thumb: null,
    x: 0, y: 0, w: 200, h: 168, origin: 'url', task_id: null },
  { id: 'card_ext_dir', title: '外部产出目录', kind: 'file', note: '', model: '',
    url: null, thumb: null, x: 216, y: 0, w: 200, h: 168, origin: 'path', task_id: null,
    path: 'E:\\work\\out', ext: { path: 'E:\\work\\out', is_dir: true } },
  { id: 'card_ext_file', title: '外部视频', kind: 'video', note: '', model: '',
    url: null, thumb: null, x: 432, y: 0, w: 200, h: 168, origin: 'path', task_id: null,
    path: 'E:\\work\\out\\demo.mp4', ext: { path: 'E:\\work\\out\\demo.mp4', is_dir: false } },
];

let revealCalls = [];        // 每次 POST /reveal 的请求体
let revealStatus = 200;      // 改成 403 模拟「后端在另一台机器上」
let histDeletes = [];        // 每次 DELETE 归档的 id
let histListLoads = 0;
let boardLoads = [];         // 每次 POST /board/history/{id}/load 的归档 id
let copied = [];             // clipboard.writeText 收到的文本
let execCopies = 0;          // execCommand 回退被调用的次数（非安全上下文那条路）

function fakeFetch(url, init) {
  const u = new URL(url, 'http://127.0.0.1:8188');
  const method = (init && init.method) || 'GET';
  if (u.pathname.endsWith('/board/history') && method === 'GET') {
    histListLoads += 1;
    return jsonResponse({ history: [{ id: 'arch_1', label: '第一轮 · 草图', created: 1, count: 5 }], count: 1 });
  }
  if (u.pathname.includes('/board/history/') && method === 'DELETE') {
    histDeletes.push(u.pathname.split('/').pop());
    return jsonResponse({ ok: true, id: 'arch_1', removed: 5, count: 0 });
  }
  if (u.pathname.includes('/board/history/') && u.pathname.endsWith('/load') && method === 'POST') {
    boardLoads.push(u.pathname.split('/').slice(-2)[0]);
    return jsonResponse({ ok: true, loaded: 'arch_1', count: boardItems.length,
                          auto_archived: null, items: boardItems });
  }
  if (u.pathname.includes('/board/history/') && method === 'GET') {
    // 归档详情：只读回看拿的就是这一份快照
    return jsonResponse({ ok: true, items: boardItems,
                          archive: { id: 'arch_1', label: '第一轮 · 草图', created: 1,
                                     count: boardItems.length, items: boardItems } });
  }
  if (u.pathname.endsWith('/board')) {
    return jsonResponse({ server_time: Date.now() / 1000, items: boardItems,
                          count: boardItems.length, history_count: 1 });
  }
  if (u.pathname.endsWith('/reveal')) {
    revealCalls.push(JSON.parse((init && init.body) || '{}'));
    if (revealStatus !== 200) {
      return errResponse(revealStatus, 'reveal_not_local',
                         'This action opens a window on the machine running ComfyUI.');
    }
    return jsonResponse({ ok: true, id: 'card_ext_dir', path: 'E:\\work\\out', is_dir: true });
  }
  if (u.pathname.endsWith('/files')) {
    const offset = Number(u.searchParams.get('offset') || 0);
    const limit = Number(u.searchParams.get('limit') || PAGE_SIZE);
    if (limit === 1) probes += 1; else fullLoads += 1;
    if (process.env.DEBUG) console.log(`     [fetch] files offset=${offset} limit=${limit} total=${files.length}`);
    return jsonResponse(filesPayload(offset, limit));
  }
  if (u.pathname.endsWith('/tasks')) {
    return jsonResponse({
      server_time: Date.now() / 1000,
      tasks: [
        { id: 'bbbbbbbbbbbb', status: 'completed', model: 'z-image-turbo', elapsed: 9.7,
          url: 'http://127.0.0.1:8188/view?filename=a.png&type=output' },
        { id: 'cccccccccccc', status: 'failed', model: 'sdxl', elapsed: 3.1, error: 'boom' },
      ],
      queue: { reachable: true, running: [], pending: [] },
    });
  }
  return jsonResponse({ error: { message: 'unexpected ' + u.pathname } });
}

const names = grid => [...grid.querySelectorAll('.name')].map(n => n.textContent);

async function main() {
  const dom = new JSDOM(HTML, {
    runScripts: 'dangerously',
    url: 'http://127.0.0.1:8188/roundabout/view',
    virtualConsole: new VirtualConsole(),
    beforeParse(window) {
      window.fetch = fakeFetch;
      window.scrollTo = () => {};
      // 剪贴板：默认给一个可用的 clipboard.writeText（页面走的首选路径）。
      // 测回退时把它删掉 —— 用局域网 IP 打开页面时浏览器就是没有这个 API。
      Object.defineProperty(window.navigator, 'clipboard', {
        configurable: true,
        value: { writeText: async t => { copied.push(t); } },
      });
      Object.defineProperty(window.document, 'execCommand', {
        configurable: true,
        value: () => { execCopies += 1; return true; },
      });
      // jsdom 把页面当成后台标签（hidden=true），会让探测直接跳过；强制可见才能测
      Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => false });
      Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => 'visible' });
    },
  });
  const { window } = dom;
  const $ = id => window.document.getElementById(id);
  const settle = () => new Promise(r => setTimeout(r, 20));
  // 剪贴板桩：测「非安全上下文」时把 clipboard 摘掉、把 execCommand 换成失败版。
  // 必须用 defineProperty —— beforeParse 里注入时 value 不可写，直接赋值会静默失败。
  const stubClipboard = () => Object.defineProperty(window.navigator, 'clipboard', {
    configurable: true, value: { writeText: async t => { copied.push(t); } },
  });
  const stubExec = ok => Object.defineProperty(window.document, 'execCommand', {
    configurable: true, value: () => { execCopies += 1; return ok; },
  });

  await settle();   // 等脚本底部那一次 loadFiles / loadTasks 跑完

  console.log('== 首屏渲染 ==');
  check('资源网格渲染满一页', $('grid').children.length === PAGE_SIZE, String($('grid').children.length));
  check('文件计数已写入', $('fileCount').textContent.includes(`${PAGE_SIZE + 10} 个文件`), $('fileCount').textContent);
  check('分页条已出现', $('pager').hidden === false && $('pager').querySelectorAll('button[data-page]').length === 4);
  check('任务已渲染', $('taskList').querySelectorAll('.task').length === 2);
  check('完成态给产物链接', $('taskList').innerHTML.includes('查看产物'));
  check('失败态给错误摘要', $('taskList').innerHTML.includes('boom'));
  check('提示条初始隐藏', $('freshPill').hidden === true);

  console.log('== 探测：无变化不折腾 ==');
  const loadsBefore = fullLoads;
  await window.probeFiles();
  check('只探测未重载', fullLoads === loadsBefore && probes > 0, `full ${loadsBefore}->${fullLoads} probe=${probes}`);
  check('提示条仍隐藏', $('freshPill').hidden === true);

  console.log('== 探测：第一页有新产物 → 原地刷新 ==');
  const fresh = addFile();
  await window.probeFiles();
  await settle();
  check('新产物排在最前', names($('grid'))[0] === fresh.name, names($('grid'))[0]);
  check('未弹提示条（直接刷新了）', $('freshPill').hidden === true);
  check('计数同步更新', $('fileCount').textContent.includes(`${PAGE_SIZE + 11} 个文件`), $('fileCount').textContent);

  console.log('== 探测：不在第一页 → 只提示不打断 ==');
  const page2 = $('pager').querySelector('button[data-page="2"]');
  check('分页条有第 2 页按钮', !!page2);
  page2.click();
  await settle();
  const page2Names = names($('grid'));
  check('已切到第 2 页', page2Names.length === (PAGE_SIZE + 11) - PAGE_SIZE, String(page2Names.length));
  const loadsBefore2 = fullLoads;
  const fresh2 = addFile();
  await window.probeFiles();
  check('没有强行重载', fullLoads === loadsBefore2, `full ${loadsBefore2}->${fullLoads}`);
  check('提示条显示', $('freshPill').hidden === false);
  check('提示文案给出新增数量', $('freshText').textContent.includes('1 个新产物'), $('freshText').textContent);
  check('第 2 页内容未被冲掉', names($('grid'))[0] === page2Names[0], names($('grid'))[0]);

  console.log('== 点击提示条 → 回到首页并刷新 ==');
  $('freshPill').click();
  await settle();
  check('提示条收起', $('freshPill').hidden === true);
  check('回到第 1 页并显示最新产物', names($('grid'))[0] === fresh2.name, names($('grid'))[0]);

  console.log('== 弹层打开时不打断 ==');
  $('lightbox').hidden = false;
  const fresh3 = addFile();
  await window.probeFiles();
  check('提示条显示（未强刷）', $('freshPill').hidden === false, String($('freshPill').hidden));
  check('弹层保持打开', $('lightbox').hidden === false);
  check('网格未被冲掉', !names($('grid')).includes(fresh3.name));

  // ---- 看板：铺满视口 + 外部卡（点它先确认，再交给后端去拉系统文件管理器）----
  console.log('== 看板：铺满视口 + 外部卡标记 ==');
  $('tabBoard').click();
  await settle();
  const wrapH = parseInt($('canvasWrap').style.height, 10);
  check('画布铺满视口（不再是固定 420px）', wrapH > 420 && wrapH <= window.innerHeight,
        `${$('canvasWrap').style.height} / innerHeight=${window.innerHeight}`);
  check('看板显示、资源面板收起', $('boardPanel').hidden === false && $('filesPanel').hidden === true);
  const cards = [...window.document.querySelectorAll('.board-card')];
  const extCards = [...window.document.querySelectorAll('.board-card.is-ext')];
  check('三张卡都渲染出来', cards.length === 3, String(cards.length));
  check('两张外部卡带「外部」徽标',
        extCards.length === 2 && extCards.every(c => c.querySelector('.bc-ext').textContent === '外部'),
        String(extCards.length));
  check('外部卡副标题说清「外部目录 / 外部文件」',
        extCards[0].querySelector('.bc-sub').textContent.includes('外部目录') &&
        extCards[1].querySelector('.bc-sub').textContent.includes('外部文件'),
        extCards.map(c => c.querySelector('.bc-sub').textContent).join(' | '));
  check('外部卡写明「在资源管理器中打开」',
        extCards[0].querySelector('.bc-dir-go').textContent.includes('资源管理器'));
  check('普通图卡没有外部徽标', !cards[0].querySelector('.bc-ext'));

  console.log('== 外部卡：先确认，再交给后端打开 ==');
  extCards[0].click();
  await settle();
  check('点外部卡弹出确认层', $('askLayer').hidden === false);
  check('标题说清要开的类型', $('askTitle').textContent.includes('打开这个目录'), $('askTitle').textContent);
  check('确认层把路径摊开', $('askText').textContent === 'E:\\work\\out', $('askText').textContent);
  check('说明窗口开在哪台机器', $('askNote').textContent.includes('运行 ComfyUI'), $('askNote').textContent);
  check('还没点确定 → 一个请求都没发', revealCalls.length === 0, JSON.stringify(revealCalls));

  $('askCancel').click();
  await settle();
  check('取消收起弹层', $('askLayer').hidden === true);
  check('取消不触发打开', revealCalls.length === 0);

  extCards[0].click();
  await settle();
  $('askOk').click();
  await settle();
  check('确定后发出打开请求', revealCalls.length === 1, JSON.stringify(revealCalls));
  check('请求体只有卡片 id（前端不拼路径）',
        JSON.stringify(revealCalls[0]) === '{"id":"card_ext_dir"}', JSON.stringify(revealCalls[0]));
  check('成功后弹层已收起', $('askLayer').hidden === true);
  check('成功给一句反馈', $('toast').textContent.includes('资源管理器'), $('toast').textContent);

  // 远程访问时后端会拒（窗口只会开在服务器那台）—— 页面要把原因说清楚，而不是默默失败
  revealStatus = 403;
  extCards[1].click();
  await settle();
  $('askOk').click();
  await settle();
  check('被拒时点明「另一台机器」', $('toast').textContent.includes('另一台机器'), $('toast').textContent);
  revealStatus = 200;

  // ---- 复制归档 ID：认哪一轮交给用户，agent 照着 id 载入就行，不必先扫一遍历史列表 ----
  console.log('== 复制归档 ID ==');
  $('boardHistoryBtn').click();
  await settle();
  const copyBtn = $('boardHistoryList').querySelector('[data-copy]');
  check('历史行有「复制 ID」按钮', !!copyBtn);
  check('按钮 title 里带出 ID 本身',
        !!copyBtn && copyBtn.getAttribute('title').includes('arch_1'),
        copyBtn && copyBtn.getAttribute('title'));
  copyBtn.click();
  await settle();
  check('复制的内容就是归档 id', copied.length === 1 && copied[0] === 'arch_1', JSON.stringify(copied));
  check('按钮给出「已复制」反馈', copyBtn.textContent.includes('已复制'), copyBtn.textContent);
  check('提示说清是交给 agent 接着做', $('toast').textContent.includes('agent'), $('toast').textContent);

  // 非安全上下文（用局域网 IP 打开页面）没有 navigator.clipboard ⇒ 必须退到 execCommand，
  // 否则这批用户点了永远是失败 —— 而这功能恰恰是他们要用的
  delete window.navigator.clipboard;
  copyBtn.click();
  await settle();
  check('没有 clipboard API 时走 execCommand 回退', execCopies === 1, String(execCopies));

  // 两条路都不通：明说失败并把 ID 摊出来，而不是「点了没反应」
  stubExec(false);
  copyBtn.click();
  await settle();
  check('复制失败时把 ID 摊在提示里',
        $('toast').textContent.includes('复制失败') && $('toast').textContent.includes('arch_1'),
        $('toast').textContent);

  stubClipboard();          // 恢复可用剪贴板，后面「回看横幅也能复制」还要用
  stubExec(true);
  $('boardHistoryClose').click();

  console.log('== 删一份归档：也要先确认 ==');
  $('boardHistoryBtn').click();
  await settle();
  const dropBtn = $('boardHistoryList').querySelector('[data-drop]');
  check('历史行有删除按钮', !!dropBtn);
  dropBtn.click();
  await settle();
  check('弹确认层', $('askLayer').hidden === false && $('askTitle').textContent.includes('删除'),
        $('askTitle').textContent);
  check('被删的归档名摊在确认层里', $('askText').textContent.includes('第一轮'), $('askText').textContent);
  check('确认按钮是危险色', $('askOk').classList.contains('danger'));
  check('还没点确定 → 没发 DELETE', histDeletes.length === 0);
  const listLoadsBefore = histListLoads;
  $('askOk').click();
  await settle();
  check('确定后发出 DELETE', histDeletes.length === 1 && histDeletes[0] === 'arch_1', JSON.stringify(histDeletes));
  check('删完刷新了历史列表', histListLoads > listLoadsBefore, `${listLoadsBefore} -> ${histListLoads}`);
  $('boardHistoryClose').click();

  // ---- 回看归档：只读（写入口整条撤掉）----
  console.log('== 回看归档：只读，写入口消失 ==');
  $('boardHistoryBtn').click();
  await settle();
  check('历史行已无「载入」按钮', !$('boardHistoryList').querySelector('[data-load]'));
  const viewBtn = $('boardHistoryList').querySelector('[data-view]');
  check('历史行有「查看」按钮', !!viewBtn);
  const deletesBeforeView = histDeletes.length;
  viewBtn.click();
  await settle();
  check('回看横幅出现且写明只读',
        $('boardViewing').hidden === false && $('boardViewingText').textContent.includes('只读'),
        $('boardViewingText').textContent);
  check('看板被打上只读标记', $('boardPanel').classList.contains('reading'));
  check('提示行换成只读文案', $('canvasHint').textContent.includes('只读'), $('canvasHint').textContent);
  check('回看只发读请求（没有写）', histDeletes.length === deletesBeforeView && boardLoads.length === 0,
        `deletes=${histDeletes.length} loads=${boardLoads.length}`);
  check('清空按钮在只读下被撤掉',
        window.getComputedStyle($('boardClearBtn')).display === 'none',
        window.getComputedStyle($('boardClearBtn')).display);
  const delBtns = [...window.document.querySelectorAll('.board-card .bc-del')];
  check('卡片 × 在只读下被撤掉',
        delBtns.length > 0 && delBtns.every(b => window.getComputedStyle(b).display === 'none'),
        `${delBtns.length} 个 ×`);

  console.log('== 回看横幅也能复制 ID（只读态保留）==');
  check('只读下复制入口仍在',
        window.getComputedStyle($('boardCopyIdBtn')).display !== 'none',
        window.getComputedStyle($('boardCopyIdBtn')).display);
  const copiedBefore = copied.length;
  $('boardCopyIdBtn').click();
  await settle();
  check('横幅复制的是正在回看的归档 id',
        copied.length === copiedBefore + 1 && copied[copied.length - 1] === 'arch_1',
        JSON.stringify(copied));

  console.log('== 回看里替换当前看板：先确认，再 POST ==');
  $('boardReplaceBtn').click();
  await settle();
  check('弹出确认层', $('askLayer').hidden === false && $('askTitle').textContent.includes('替换'),
        $('askTitle').textContent);
  check('确认层摊开是哪份归档', $('askText').textContent.includes('第一轮'), $('askText').textContent);
  check('确认层说明当前内容会先自动归档', $('askNote').textContent.includes('归档'), $('askNote').textContent);
  check('确认按钮是危险色', $('askOk').classList.contains('danger'));
  check('还没点确定 → 没有 POST', boardLoads.length === 0, JSON.stringify(boardLoads));

  $('askOk').click();
  await settle();
  check('确定后发出载回请求', boardLoads.length === 1 && boardLoads[0] === 'arch_1', JSON.stringify(boardLoads));
  check('替换后退出只读',
        !$('boardPanel').classList.contains('reading') && $('boardViewing').hidden === true,
        `reading=${$('boardPanel').classList.contains('reading')} bar=${$('boardViewing').hidden}`);
  check('退出后写入口回来（清空按钮可见）',
        window.getComputedStyle($('boardClearBtn')).display !== 'none',
        window.getComputedStyle($('boardClearBtn')).display);
  check('提示行恢复常规文案', !$('canvasHint').textContent.includes('只读'), $('canvasHint').textContent);

  // jsdom 没有排版，前面所有用例走的都是兜底值；这里手动喂一套几何，
  // 验证「每页条数 = 一屏能放下的行数 × 列数」这条公式本身。
  // 例：内容区 1558 宽 → 9 列、列宽 162.4、行高 162.4+47+12=221.4；
  //     1080 高的屏去掉 header 53 + 标题栏 42 + 分页预留 72 = 913 → 5 行 → 45 条。
  console.log('== 每页条数按视口高度自适应 ==');
  const grid = $('grid');
  const header = window.document.querySelector('header');
  const title = window.document.querySelector('main .panel > h2');
  const box = w => ({ width: w, height: 0, top: 0, left: 0, right: w, bottom: 0, x: 0, y: 0 });
  grid.getBoundingClientRect = () => box(1558);
  header.getBoundingClientRect = () => Object.assign(box(1920), { height: 53 });
  title.getBoundingClientRect = () => Object.assign(box(1558), { height: 42 });
  const setHeight = h => Object.defineProperty(window, 'innerHeight', { configurable: true, value: h });

  setHeight(1080);
  check('列数按网格宽度算', window.gridCols(1558) === 9, String(window.gridCols(1558)));
  check('1080 高的屏一页 45 条（5 行 × 9 列）', window.computePageSize() === 45, String(window.computePageSize()));

  setHeight(600);
  const short = window.computePageSize();
  check('矮屏一页更少', short < 45 && short > 0, String(short));

  setHeight(1080);
  grid.getBoundingClientRect = () => box(400);
  check('窄屏列数跟着变少', window.gridCols(400) === 2, String(window.gridCols(400)));
  check('窄屏一页更少', window.computePageSize() < 45, String(window.computePageSize()));

  window.close();
  console.log(failures === 0 ? '\nALL PASS' : `\n${failures} FAILED`);
  return failures === 0 ? 0 : 1;
}

main().then(code => process.exit(code), err => {
  console.error(err);
  process.exit(1);
});
