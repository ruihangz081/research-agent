const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/research_agent/web_static/router.js'), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

function setup() {
  const requests = [];
  const mount = { dataset: {}, classList: { add() {}, remove() {} }, innerHTML: '', offsetHeight: 1 };
  const location = { origin: 'http://localhost', pathname: '/app/research', search: '' };
  const views = new Map(['research', 'results', 'settings'].map(name => [name, { init() {}, destroy() {} }]));
  const Lumitrace = { mountShell() {}, escapeHtml: String, views: { get: name => views.get(name) }, api(url, { signal }) {
    return new Promise((resolve, reject) => {
      requests.push({ url, resolve });
      signal.addEventListener('abort', () => reject(new Error('请求已取消')));
    });
  } };
  const window = { location, Lumitrace, scrollTo() {}, addEventListener() {}, setTimeout(fn) { fn(); } };
  const document = { readyState: 'complete', getElementById: () => mount, querySelectorAll: () => [], addEventListener() {} };
  const history = { pushState(_, __, value) { Object.assign(location, { pathname: new URL(value, location.origin).pathname, search: new URL(value, location.origin).search }); } };
  vm.runInNewContext(source, { window, document, history, Lumitrace, AbortController, URL, console });
  return { requests, mount, location, navigate: window.Lumitrace.navigate };
}

for (const destination of ['results', 'research']) {
  test(`rapid navigation ends at ${destination}, including returning to the mounted view`, async () => {
    const app = setup();
    app.requests[0].resolve({ title: 'research', html: 'research' });
    await tick();
    app.navigate('/app/settings');
    app.navigate(`/app/${destination}?project=latest`);
    await tick();
    if (destination === 'results') {
      app.requests.at(-1).resolve({ title: destination, html: destination });
      await tick();
    }
    assert.equal(app.location.pathname, `/app/${destination}`);
    assert.equal(app.location.search, '?project=latest');
    assert.equal(app.mount.innerHTML, destination);
  });
}
