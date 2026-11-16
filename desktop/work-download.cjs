const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const { Readable } = require('node:stream');
const { pipeline } = require('node:stream/promises');

function downloadFilename(value) {
  let name = String(value || '壁纸作品').replace(/\.zip$/i, '').replace(/[<>:"/\\|?*\x00-\x1f]/g, ' ').trim().replace(/[. ]+$/, '').slice(0, 100) || '壁纸作品';
  if (/^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)/i.test(name)) name = '作品-' + name;
  return name + '.zip';
}

async function saveWorks(request, { choosePath, baseUrl, fetchImpl = fetch }) {
  if (!Array.isArray(request?.ids) || !request.ids.length || request.ids.length > 500 || request.ids.some(id => typeof id !== 'string')) throw Error('请选择 1 到 500 件作品');
  const chosen = await choosePath(downloadFilename(request.filename));
  if (!chosen) return { canceled: true };
  const temporary = path.join(path.dirname(chosen), '.' + path.basename(chosen) + '.' + randomUUID() + '.part');
  const post = (route, body) => fetchImpl(baseUrl + route, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    signal: AbortSignal.timeout(600000),
  });
  try {
    const response = await post('/api/works/download', { ids: request.ids });
    if (!response.ok) throw Error((await response.json().catch(() => ({}))).error || '作品打包失败');
    if (!response.body || !response.headers.get('content-type')?.includes('application/zip')) throw Error('没有收到作品 ZIP');
    await pipeline(Readable.fromWeb(response.body), fs.createWriteStream(temporary, { flags: 'wx' }));
    await fs.promises.rename(temporary, chosen);
  } catch (error) {
    await fs.promises.unlink(temporary).catch(() => {});
    throw error;
  }
  try {
    const marked = await post('/api/works/status', { ids: request.ids, downloaded: true });
    if (!marked.ok) throw Error('下载状态未保存');
    return { saved: true, statusSaved: true, path: chosen };
  } catch {
    return { saved: true, statusSaved: false, path: chosen };
  }
}

module.exports = { saveWorks, downloadFilename };
