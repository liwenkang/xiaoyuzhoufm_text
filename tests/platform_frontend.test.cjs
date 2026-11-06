const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('frontend sends only the episode URL and keeps exports and history', () => {
  const source = fs.readFileSync('public/static/app.js', 'utf8');
  assert.match(source, /\/api\/transcript/);
  assert.match(source, /post\(\{url\}\)/);
  assert.doesNotMatch(source, /access-token|refresh.token|\/api\/jobs/);
  assert.match(source, /\$\{name\}\.srt/);
  assert.match(source, /localStorage/);
});

test('public help explains URL-only use and maintenance recovery', () => {
  const html = fs.readFileSync('public/index.html', 'utf8');
  assert.match(html, /无需登录或填写账号凭据/);
  assert.match(html, /联系维护者/);
  assert.doesNotMatch(html, /login-with-sms|x-jike|access.token|refresh.token/);
});
