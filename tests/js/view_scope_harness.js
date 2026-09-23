// 在桩 DOM 里真跑一遍 view.html 的内联脚本，检查**顶层作用域**是否完整。
//
// 为什么需要它：把一段 JS 插到某个函数左括号后面（例如 `async function loadTasks() {`
// 紧跟插入内容），语法依然合法 —— `node --check` 与任何 linter 都不会报错，但整段代码
// 会被关进那个函数体内，页面里调用它的顶层代码就 ReferenceError。只有「真跑」才暴露。
//
// 用法：node view_scope_harness.js <提取出的内联脚本.js>
// 退出码 0 = 顶层作用域完整且脚本可跑通；1 = 抛错或缺少必需的顶层标识符。
'use strict';

const fs = require('fs');
const vm = require('vm');

// ---- 必需出现在**顶层**的标识符：它们由脚本末尾的启动段与各处 onclick 直接调用 ----
const REQUIRED_TOP_LEVEL = [
  'loadFiles', 'loadTasks', 'loadBoard',
  'renderBoard', 'fitBoard', 'openLightbox',
];

// ---- 极简 DOM 桩：任何属性都能取、任何 DOM 方法都能调，返回自身方便链式 ----
function stubEl() {
  return new Proxy({}, {
    get(_t, key) {
      switch (key) {
        case 'classList':
          return { add() {}, remove() {}, toggle() {}, contains() { return false; } };
        case 'style': return {};
        case 'dataset': return {};
        case 'children': return [];
        case 'getBoundingClientRect':
          return () => ({ width: 800, height: 400, left: 0, top: 0, right: 800, bottom: 400 });
        case 'querySelectorAll': return () => [];
        case 'querySelector': return () => stubEl();
        case 'closest': return () => stubEl();
        case 'appendChild': case 'removeChild':
        case 'addEventListener': case 'removeEventListener':
        case 'focus': case 'blur': case 'click':
        case 'play': case 'pause': case 'remove': case 'load':
          return () => {};
        case 'innerHTML': case 'outerHTML': case 'textContent':
        case 'src': case 'href': case 'value': case 'id': case 'title':
          return '';
        case 'hidden': case 'disabled': return false;
        case 'checked': return true;
        case 'getContext': return () => null;
        default: return undefined;
      }
    },
    set() { return true; },
  });
}

const EMPTY_PAYLOAD = {
  files: [], dirs: [], crumbs: [], tasks: [], items: [], history: [],
  total: 0, count: 0, history_count: 0, page: 1,
};

const sandbox = {
  document: {
    getElementById: () => stubEl(),
    querySelector: () => stubEl(),
    querySelectorAll: () => [],
    createElement: () => stubEl(),
    addEventListener() {},
    body: stubEl(),
    documentElement: stubEl(),
  },
  location: { search: '', href: 'http://127.0.0.1:8188/roundabout/view' },
  fetch: () => Promise.resolve({ ok: true, json: async () => ({ ...EMPTY_PAYLOAD }) }),
  setInterval: () => 0,
  clearInterval() {},
  setTimeout: () => 0,
  clearTimeout() {},
  requestAnimationFrame: () => 0,
  cancelAnimationFrame() {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  getComputedStyle: () => ({ getPropertyValue: () => '', paddingTop: '0px' }),
  navigator: { userAgent: 'node' },
  console,
  URLSearchParams,
  encodeURIComponent,
  innerWidth: 1280,
  innerHeight: 800,
  addEventListener() {},
  removeEventListener() {},
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;

// 异步路径（fetch 续跑）里抛的错也要算失败，不能静默放过
let asyncError = null;
process.on('unhandledRejection', e => { asyncError = e || new Error('rejected'); });

const code = fs.readFileSync(process.argv[2], 'utf8');
try {
  vm.createContext(sandbox);
  new vm.Script(code, { filename: 'view-inline.js' }).runInContext(sandbox);
} catch (err) {
  console.log(`THROWS: ${err.constructor.name}: ${err.message}`);
  console.log('SCOPE CHECK FAILED');
  process.exit(1);
}

// 函数声明会挂到全局对象上；被关进别的函数时这里就看不到它们
const missing = REQUIRED_TOP_LEVEL.filter(name => !(name in sandbox));
if (missing.length) {
  console.log(`MISSING AT TOP LEVEL: ${missing.join(', ')}`);
  console.log('SCOPE CHECK FAILED');
  process.exit(1);
}

setTimeout(() => {
  if (asyncError) {
    console.log(`ASYNC THROWS: ${asyncError.message}`);
    console.log('SCOPE CHECK FAILED');
    process.exit(1);
  }
  console.log(`SCOPE OK (${REQUIRED_TOP_LEVEL.length} top-level entries verified)`);
  process.exit(0);
}, 60);
