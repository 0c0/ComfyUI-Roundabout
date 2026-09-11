/**
 * view.html 前端行为冒烟：用 jsdom 真跑页面脚本，验证「产物自动同步」这条链路。
 *
 * 为什么不用真浏览器：Chromium 要另下 500MB；这里要验证的行为（探测 → 原地刷新 /
 * 左下角提示）全是 DOM 状态变化，jsdom + fetch 打桩足够覆盖，还能进 CI。
 *
 * 运行（先装 jsdom 到托管 node 工作区）：
 *   NODE_PATH=<node-workspace>/node_modules node test_view_frontend.cjs
 */

const fs = require('node:fs');
const path = require('node:path');
const { JSDOM, VirtualConsole } = require('jsdom');

const HTML = fs.readFileSync(path.join(__dirname, 'web', 'view.html'), 'utf8');
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

function fakeFetch(url) {
  const u = new URL(url, 'http://127.0.0.1:8188');
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
      // jsdom 把页面当成后台标签（hidden=true），会让探测直接跳过；强制可见才能测
      Object.defineProperty(window.document, 'hidden', { configurable: true, get: () => false });
      Object.defineProperty(window.document, 'visibilityState', { configurable: true, get: () => 'visible' });
    },
  });
  const { window } = dom;
  const $ = id => window.document.getElementById(id);
  const settle = () => new Promise(r => setTimeout(r, 20));

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
