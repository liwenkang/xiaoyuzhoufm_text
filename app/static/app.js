'use strict';

const $ = (id) => document.getElementById(id);
let current = null;
let records = [];

function updateSubmitButton() {
  $('submit').disabled = !$('url').value.trim() || !$('access-token').value.trim();
}

function formatTime(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return [Math.floor(value / 3600), Math.floor(value % 3600 / 60), value % 60]
    .map((item) => String(item).padStart(2, '0')).join(':');
}

function timestamp(seconds, separator = ',') {
  const value = Math.max(0, Math.round((Number(seconds) || 0) * 1000));
  const hours = Math.floor(value / 3600000);
  const minutes = Math.floor(value % 3600000 / 60000);
  const secondsPart = Math.floor(value % 60000 / 1000);
  const milliseconds = value % 1000;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(secondsPart).padStart(2, '0')}${separator}${String(milliseconds).padStart(3, '0')}`;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || '请求失败，请稍后重试。');
  return body;
}

const post = (data) => ({
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(data),
});

function file(name, content, type) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([content], {type}));
  link.download = name;
  link.textContent = `↓ ${name}`;
  link.addEventListener('click', () => setTimeout(() => URL.revokeObjectURL(link.href), 1000), {once: true});
  return link;
}

function exportContents(data) {
  const {episode, segments} = data;
  const title = (episode.title || episode.episode_id).replace(/\n/g, ' ');
  const text = `${segments.map((segment) => segment.text).join('\n')}\n`;
  const note = data.end_timestamps_inferred ? '\n> 平台未提供的结束时间由下一段开始时间或节目时长推算。\n' : '';
  const markdown = `# ${title}\n\n节目：${episode.podcast_title || '未命名播客'}\n\n来源：${episode.source_url}\n\n> 文稿来源：小宇宙平台已有文稿；未在本机重新识别。请结合原音频核对。\n${note}\n${segments.map((segment) => `**[${timestamp(segment.start, '.').slice(0, -4)}]** ${segment.text}`).join('\n\n')}\n`;
  const srt = `${segments.map((segment, index) => `${index + 1}\n${timestamp(segment.start)} --> ${timestamp(segment.end)}\n${segment.text}`).join('\n\n')}\n`;
  const vtt = `WEBVTT\n\n${segments.map((segment) => `${timestamp(segment.start, '.')} --> ${timestamp(segment.end, '.')}\n${segment.text}`).join('\n\n')}\n`;
  const json = JSON.stringify({
    episode_id: episode.episode_id,
    title: episode.title,
    podcast_title: episode.podcast_title,
    source_url: episode.source_url,
    duration: episode.duration,
    language: 'und',
    model: null,
    transcript_source: 'xiaoyuzhou',
    end_timestamps_inferred: Boolean(data.end_timestamps_inferred),
    segments,
  }, null, 2);
  return {txt: text, md: markdown, srt, vtt, json};
}

function renderExports(data) {
  const contents = exportContents(data);
  const files = $('files');
  files.replaceChildren(
    file('transcript.txt', contents.txt, 'text/plain;charset=utf-8'),
    file('transcript.md', contents.md, 'text/markdown;charset=utf-8'),
    file('transcript.srt', contents.srt, 'application/x-subrip;charset=utf-8'),
    file('transcript.vtt', contents.vtt, 'text/vtt;charset=utf-8'),
    file('transcript.json', contents.json, 'application/json;charset=utf-8'),
  );
}

function renderSegments() {
  const query = $('search').value.trim().toLowerCase();
  const fragment = document.createDocumentFragment();
  (current?.segments || []).filter((segment) => !query || segment.text.toLowerCase().includes(query))
    .forEach((segment) => {
      const row = document.createElement('div');
      row.className = 'segment';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'timestamp';
      button.textContent = formatTime(segment.start);
      button.title = '复制时间戳';
      button.addEventListener('click', async () => {
        await navigator.clipboard?.writeText(formatTime(segment.start));
        button.textContent = '已复制';
        setTimeout(() => { button.textContent = formatTime(segment.start); }, 900);
      });
      const text = document.createElement('p');
      text.textContent = segment.text;
      row.append(button, text);
      fragment.append(row);
    });
  $('transcript').replaceChildren(fragment);
}

function render(data) {
  current = data;
  const episode = data.episode;
  $('detail').classList.remove('hidden');
  $('episode-title').textContent = episode.title || episode.episode_id;
  $('podcast').textContent = episode.podcast_title || '小宇宙播客';
  $('meta').textContent = `时长 ${formatTime(episode.duration)} · ${(episode.published_at || '').slice(0, 10)}`;
  $('source-note').textContent = data.source_note + (data.end_timestamps_inferred ? ' 部分结束时间由相邻段落推算。' : '');
  const cover = episode.cover_image_url;
  if (cover && cover.startsWith('https://')) {
    $('cover').src = cover;
    $('cover').classList.remove('hidden');
  } else {
    $('cover').classList.add('hidden');
  }
  $('transcript-panel').classList.remove('hidden');
  renderExports(data);
  renderSegments();
}

function saveHistory(data) {
  try {
    records = [data, ...records.filter((item) => item.episode.episode_id !== data.episode.episode_id)].slice(0, 8);
    localStorage.setItem('platformTranscripts', JSON.stringify(records));
  } catch (_error) {
    // Long transcripts can exceed localStorage. The current result remains usable.
  }
  renderHistory();
}

function renderHistory() {
  const history = $('history');
  if (!records.length) {
    history.innerHTML = '<p class="empty">还没有节目。粘贴一个链接，开始读取。</p>';
    return;
  }
  history.replaceChildren(...records.map((data) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'history-item';
    const title = document.createElement('span');
    title.className = 'history-title';
    title.textContent = data.episode.title || data.episode.episode_id;
    const state = document.createElement('span');
    state.className = 'history-state';
    state.textContent = `${data.segments.length} 段`;
    button.append(title, state);
    button.addEventListener('click', () => render(data));
    return button;
  }));
}

function loadHistory() {
  try {
    records = JSON.parse(localStorage.getItem('platformTranscripts') || '[]');
    if (!Array.isArray(records)) records = [];
  } catch (_error) {
    records = [];
  }
  renderHistory();
}

$('form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const url = $('url').value.trim();
  const token = $('access-token').value.trim();
  if (!url || !token) {
    $('form-message').className = 'message error';
    $('form-message').textContent = '请填写节目链接和 access token。';
    updateSubmitButton();
    return;
  }
  $('submit').disabled = true;
  $('form-message').className = 'message';
  $('form-message').textContent = '正在读取平台文稿…';
  $('job-error').textContent = '';
  try {
    const data = await api('/api/transcript', post({url, token}));
    render(data);
    saveHistory(data);
    $('form-message').textContent = `已读取 ${data.segments.length} 段平台文稿。`;
  } catch (error) {
    $('form-message').className = 'message error';
    $('form-message').textContent = error.message;
  } finally {
    updateSubmitButton();
  }
});

$('url').addEventListener('input', updateSubmitButton);
$('access-token').addEventListener('input', updateSubmitButton);
$('search').addEventListener('input', renderSegments);
loadHistory();
api('/api/health').then((data) => {
  $('platform-config').textContent = data.auth_mode === 'user_supplied'
    ? '请在上方填写自己的 access token；它只用于当前请求。'
    : '请在上方填写 access token。';
}).catch(() => {
  $('platform-config').textContent = '无法读取部署状态，请检查 Vercel Function 配置。';
});
updateSubmitButton();

