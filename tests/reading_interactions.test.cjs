const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {parseHTML} = require('linkedom');

const html = fs.readFileSync('public/index.html', 'utf8');
const source = fs.readFileSync('public/static/app.js', 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));

function page({fetchTranscript, history = [], clipboard} = {}) {
  const {document, window} = parseHTML(html);
  window.HTMLElement.prototype.focus = function () {};
  window.HTMLElement.prototype.scrollIntoView = function () {};
  const storage = new Map([['platformTranscripts', JSON.stringify(history)]]);
  const requests = [];
  const revoked = [];
  let nextUrl = 0;
  const context = vm.createContext({
    document, Blob, AbortController, TypeError,
    navigator: {clipboard},
    localStorage: {getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    URL: {createObjectURL: () => `blob:test-${++nextUrl}`, revokeObjectURL: (url) => revoked.push(url)},
    setTimeout: () => 1, clearTimeout: () => {},
    fetch: async (path, options) => {
      if (path === '/api/health') return {ok: true, json: async () => ({auth_mode: 'user_supplied'})};
      requests.push({path, options});
      return fetchTranscript ? fetchTranscript(path, options) : {ok: true, json: async () => episode()};
    },
  });
  vm.runInContext(source, context);
  const element = (id) => document.getElementById(id);
  const dispatch = (id, type) => element(id).dispatchEvent(new window.Event(type, {bubbles: true, cancelable: true}));
  const fill = () => {
    element('url').value = 'a'.repeat(24);
    element('access-token').value = 'private-user-token';
    dispatch('access-token', 'input');
  };
  return {document, element, dispatch, fill, context, storage, requests, revoked};
}

function episode(id = 'a'.repeat(24)) {
  return {
    episode: {episode_id: id, title: '一起/慢慢读', podcast_title: '一档播客', duration: 60,
      source_url: `https://www.xiaoyuzhoufm.com/episode/${id}`},
    segments: [{start: 0, end: 20, text: '你好，声音。'}, {start: 20, end: 60, text: '<img src=x>阅读的时间'}],
    source_note: '文字来自平台已有文稿。', end_timestamps_inferred: false,
  };
}

test('token visibility and help are accessible without persisting credentials', () => {
  const ui = page();
  ui.fill();
  ui.dispatch('toggle-token', 'click');
  assert.equal(ui.element('access-token').type, 'text');
  assert.equal(ui.element('toggle-token').getAttribute('aria-pressed'), 'true');
  ui.dispatch('toggle-token', 'click');
  assert.equal(ui.element('access-token').type, 'password');
  ui.document.querySelector('a[href="#token-guide"]').click();
  assert.equal(ui.element('token-guide').open, true);
  assert.doesNotMatch([...ui.storage.values()].join(''), /private-user-token/);
});

test('a pending request blocks repeated submits and restores controls after success', async () => {
  let complete;
  const pending = new Promise((resolve) => { complete = resolve; });
  const ui = page({fetchTranscript: () => pending});
  ui.fill();
  ui.dispatch('form', 'submit');
  ui.dispatch('form', 'submit');
  ui.dispatch('access-token', 'input');
  assert.equal(ui.requests.length, 1);
  assert.equal(ui.element('submit').disabled, true);
  assert.equal(ui.element('form').getAttribute('aria-busy'), 'true');
  assert.equal(ui.element('request-progress').classList.contains('hidden'), false);
  complete({ok: true, json: async () => episode()});
  await flush();
  assert.equal(ui.element('submit').disabled, false);
  assert.equal(ui.element('form').getAttribute('aria-busy'), 'false');
  assert.equal(ui.element('request-progress').classList.contains('hidden'), true);
  assert.match(ui.element('form-message').textContent, /已读取 2 段/);
  assert.equal(ui.document.querySelectorAll('.segment').length, 2);
  assert.doesNotMatch(ui.storage.get('platformTranscripts'), /private-user-token/);
});

test('network failure explains recovery and allows retry', async () => {
  const ui = page({fetchTranscript: () => { throw new TypeError('Failed to fetch'); }});
  ui.fill();
  ui.dispatch('form', 'submit');
  await flush();
  assert.match(ui.element('form-message').textContent, /网络连接失败.*重试/);
  assert.equal(ui.element('submit').disabled, false);
  assert.equal(ui.element('form').getAttribute('aria-busy'), 'false');
});

test('an authentication failure keeps the entered values and displays the recovery message', async () => {
  const ui = page({fetchTranscript: () => ({ok: false, json: async () => ({detail: 'token 已失效，请重新获取。'})})});
  ui.fill();
  ui.dispatch('form', 'submit');
  await flush();
  assert.equal(ui.element('form-message').textContent, 'token 已失效，请重新获取。');
  assert.equal(ui.element('access-token').value, 'private-user-token');
  assert.equal(ui.element('submit').disabled, false);
  assert.equal(ui.element('detail').classList.contains('hidden'), true);
});

test('search highlights literal text safely, explains empty results and resets for another episode', () => {
  const ui = page();
  ui.context.render(episode());
  ui.element('search').value = '<img';
  ui.dispatch('search', 'input');
  assert.equal(ui.document.querySelectorAll('.segment').length, 1);
  assert.equal(ui.document.querySelector('mark').textContent, '<img');
  assert.equal(ui.element('transcript').querySelector('img'), null);
  assert.equal(ui.element('segment-count').textContent, '找到 1 / 2 段');
  ui.element('search').value = '没有这个词';
  ui.dispatch('search', 'input');
  assert.match(ui.element('transcript').textContent, /没有找到匹配的段落/);
  ui.context.render(episode('b'.repeat(24)));
  assert.equal(ui.element('search').value, '');
  assert.equal(ui.document.querySelectorAll('.segment').length, 2);
});

test('denied clipboard access provides the timestamp instead of claiming success', async () => {
  const ui = page({clipboard: {writeText: async () => { throw new Error('Permission denied'); }}});
  ui.context.render(episode());
  ui.document.querySelector('.timestamp').click();
  await flush();
  assert.match(ui.element('copy-message').textContent, /无法自动复制.*00:00:00/);
  assert.doesNotMatch(ui.document.querySelector('.timestamp').textContent, /已复制/);
});

test('successful copying writes only the timestamp and announces confirmation', async () => {
  const copied = [];
  const ui = page({clipboard: {writeText: async (text) => copied.push(text)}});
  ui.context.render(episode());
  ui.document.querySelector('.timestamp').click();
  await flush();
  assert.deepEqual(copied, ['00:00:00']);
  assert.equal(ui.element('copy-message').textContent, '已复制时间戳 00:00:00');
});

test('download names keep the episode title and URLs remain reusable until the next episode', () => {
  const ui = page();
  ui.context.render(episode());
  const links = ui.element('files').querySelectorAll('a');
  assert.deepEqual(Array.from(links, (link) => link.getAttribute('download')),
    ['一起_慢慢读.txt', '一起_慢慢读.md', '一起_慢慢读.srt', '一起_慢慢读.vtt', '一起_慢慢读.json']);
  links[0].click();
  links[0].click();
  assert.equal(ui.revoked.length, 0);
  ui.context.render(episode('b'.repeat(24)));
  assert.equal(ui.revoked.length, 5);
});

test('malformed browser history does not prevent the form from working', () => {
  const ui = page({history: [null, {episode: null}, {episode: {episode_id: 'a'}, segments: [null]}, episode()]});
  assert.equal(ui.document.querySelectorAll('.history-item').length, 1);
  ui.fill();
  assert.equal(ui.element('submit').disabled, false);
});
