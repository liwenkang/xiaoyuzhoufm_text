'use strict';

const $ = (id) => document.getElementById(id);
let current = null;
let records = [];
let isLoading = false;
let exportUrls = [];

function updateSubmitButton() {
  $('submit').disabled = isLoading || !$('url').value.trim();
}

function setLoading(value) {
  isLoading = value;
  $('form').setAttribute('aria-busy', String(value));
  $('submit').classList.toggle('is-loading', value);
  $('submit-label').textContent = value ? '正在读取…' : '获取节目文字';
  $('request-progress').classList.toggle('hidden', !value);
  $('url').readOnly = value;
  updateSubmitButton();
}

function icon(path) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('aria-hidden', 'true');
  const stroke = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  stroke.setAttribute('d', path);
  stroke.setAttribute('stroke', 'currentColor');
  stroke.setAttribute('stroke-width', '1.8');
  stroke.setAttribute('stroke-linecap', 'round');
  stroke.setAttribute('stroke-linejoin', 'round');
  svg.append(stroke);
  return svg;
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
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 185000);
  try {
    const response = await fetch(path, {...options, signal: controller.signal});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || '读取失败，请稍后重新获取文稿。');
    return body;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('读取时间较长，请稍后重新获取文稿。');
    if (error instanceof TypeError) throw new Error('网络连接失败，请检查网络后重试。');
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

const post = (data) => ({
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(data),
});

function file(name, content, type) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([content], {type}));
  exportUrls.push(link.href);
  link.setAttribute('download', name);
  const label = document.createElement('span');
  label.textContent = name;
  link.append(icon('M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5'), label);
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
  exportUrls.forEach((url) => URL.revokeObjectURL(url));
  exportUrls = [];
  const contents = exportContents(data);
  const name = Array.from(String(data.episode.title || data.episode.episode_id || '节目文稿')
    .replace(/[\u0000-\u001f<>:"/\\|?*]/g, '_').trim()).slice(0, 100).join('')
    .replace(/[. ]+$/g, '') || '节目文稿';
  const files = $('files');
  files.replaceChildren(
    file(`${name}.txt`, contents.txt, 'text/plain;charset=utf-8'),
    file(`${name}.md`, contents.md, 'text/markdown;charset=utf-8'),
    file(`${name}.srt`, contents.srt, 'application/x-subrip;charset=utf-8'),
    file(`${name}.vtt`, contents.vtt, 'text/vtt;charset=utf-8'),
    file(`${name}.json`, contents.json, 'application/json;charset=utf-8'),
  );
}

function renderSegments() {
  const query = $('search').value.trim().toLowerCase();
  const fragment = document.createDocumentFragment();
  const segments = current?.segments || [];
  const matches = segments.filter((segment) => !query || segment.text.toLowerCase().includes(query));
  $('segment-count').textContent = query ? `找到 ${matches.length} / ${segments.length} 段` : `共 ${segments.length} 段`;
  matches.forEach((segment) => {
      const row = document.createElement('div');
      row.className = 'segment';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'timestamp';
      button.textContent = formatTime(segment.start);
      button.title = '复制时间戳';
      button.setAttribute('aria-label', `复制时间戳 ${formatTime(segment.start)}`);
      button.addEventListener('click', async () => {
        try {
          if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
          await navigator.clipboard.writeText(formatTime(segment.start));
          button.textContent = '已复制';
          $('copy-message').className = 'copy-message';
          $('copy-message').textContent = `已复制时间戳 ${formatTime(segment.start)}`;
          setTimeout(() => { button.textContent = formatTime(segment.start); }, 1200);
        } catch (_error) {
          $('copy-message').className = 'copy-message error';
          $('copy-message').textContent = `无法自动复制，请手动复制时间戳：${formatTime(segment.start)}`;
        }
      });
      const text = document.createElement('p');
      if (!query) {
        text.textContent = segment.text;
      } else {
        let start = 0;
        let index;
        const lowerText = segment.text.toLowerCase();
        while ((index = lowerText.indexOf(query, start)) !== -1) {
          text.append(document.createTextNode(segment.text.slice(start, index)));
          const mark = document.createElement('mark');
          mark.textContent = segment.text.slice(index, index + query.length);
          text.append(mark);
          start = index + query.length;
        }
        text.append(document.createTextNode(segment.text.slice(start)));
      }
      row.append(button, text);
      fragment.append(row);
    });
  if (!matches.length) {
    const empty = document.createElement('p');
    empty.className = 'empty';
    empty.textContent = '没有找到匹配的段落，试试其他关键词或清空搜索。';
    fragment.append(empty);
  }
  $('transcript').replaceChildren(fragment);
  $('transcript').scrollTop = 0;
}

function render(data) {
  current = data;
  $('search').value = '';
  $('copy-message').textContent = '';
  const episode = data.episode;
  $('detail').classList.remove('hidden');
  $('episode-title').textContent = episode.title || episode.episode_id;
  $('podcast').textContent = episode.podcast_title || '小宇宙播客';
  $('meta').textContent = [episode.duration ? `时长 ${formatTime(episode.duration)}` : '', (episode.published_at || '').slice(0, 10)].filter(Boolean).join(' · ');
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
  renderHistory();
  $('episode-title').focus({preventScroll: true});
  $('detail').scrollIntoView({block: 'start', behavior: 'auto'});
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
    history.innerHTML = '<p class="empty">读过的节目会出现在这里，下次打开就能接着读。</p>';
    return;
  }
  history.replaceChildren(...records.map((data) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'history-item';
    button.setAttribute('aria-current', String(data.episode.episode_id === current?.episode.episode_id));
    const copy = document.createElement('span');
    copy.className = 'history-copy';
    const title = document.createElement('span');
    title.className = 'history-title';
    title.textContent = data.episode.title || data.episode.episode_id;
    const state = document.createElement('span');
    state.className = 'history-state';
    const count = document.createElement('span');
    count.textContent = `${data.segments.length} 段`;
    state.append(count, icon('m9 5 7 7-7 7'));
    const podcast = document.createElement('span');
    podcast.className = 'history-podcast';
    podcast.textContent = data.episode.podcast_title || '小宇宙播客';
    copy.append(title, podcast);
    button.append(copy, state);
    button.addEventListener('click', () => render(data));
    return button;
  }));
}

function loadHistory() {
  try {
    records = JSON.parse(localStorage.getItem('platformTranscripts') || '[]');
    if (!Array.isArray(records)) records = [];
    records = records.filter((item) => item?.episode && typeof item.episode.episode_id === 'string'
      && Array.isArray(item.segments) && item.segments.length
      && item.segments.every((segment) => segment && typeof segment.text === 'string'))
      .slice(0, 8);
  } catch (_error) {
    records = [];
  }
  renderHistory();
}

$('form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (isLoading) return;
  const url = $('url').value.trim();
  if (!url) {
    $('form-message').className = 'message error';
    $('form-message').textContent = '请填写节目链接。';
    updateSubmitButton();
    return;
  }
  setLoading(true);
  $('form-message').className = 'message';
  $('form-message').textContent = '正在读取平台文稿…';
  try {
    const data = await api('/api/transcript', post({url}));
    render(data);
    saveHistory(data);
    $('form-message').textContent = `已读取 ${data.segments.length} 段平台文稿。`;
  } catch (error) {
    $('form-message').className = 'message error';
    $('form-message').textContent = error.message;
  } finally {
    setLoading(false);
  }
});

$('url').addEventListener('input', updateSubmitButton);
$('search').addEventListener('input', renderSegments);
document.querySelectorAll('a[href="#usage-guide"]').forEach((link) => {
  link.addEventListener('click', () => {
    $('usage-guide').open = true;
    $('usage-guide').querySelector('summary').focus({preventScroll: true});
  });
});
$('cover').addEventListener('error', () => $('cover').classList.add('hidden'));
loadHistory();
api('/api/health').then((data) => {
  $('platform-config').textContent = data.auth_mode === 'server_shared'
    ? '服务已连接 · 粘贴节目链接即可读取'
    : '服务配置已变更，请刷新页面后重试。';
}).catch(() => {
  $('platform-config').classList.add('error');
  $('platform-config').textContent = '暂时无法连接服务，请刷新页面后重试。';
});
updateSubmitButton();

