const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('serverless frontend uses the stateless transcript endpoint', () => {
  const source = fs.readFileSync('public/app.js', 'utf8');
  assert.match(source, /\/api\/transcript/);
  assert.match(source, /access-token/);
  assert.match(source, /post\(\{url, token\}\)/);
  assert.doesNotMatch(source, /\/api\/jobs/);
  assert.match(source, /\$\{name\}\.srt/);
  assert.match(source, /localStorage/);
});

test('page explains how users obtain the token', () => {
  const html = fs.readFileSync('public/index.html', 'utf8');
  assert.match(html, /login-with-sms/);
  assert.match(html, /x-jike-access-token/);
  assert.match(html, /不会保存到服务器或浏览器存储/);
});
