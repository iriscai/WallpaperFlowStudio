const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const { saveWorks, downloadFilename } = require('../desktop/work-download.cjs');

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'work-download-'));
  t.after(() => fs.rm(root, { recursive:true, force:true }));
  return {root, destination:path.join(root, '作品.zip')};
}
const request = {ids:['work-test'], filename:'ins简约头像｜自取.zip'};

test('canceling the native save dialog never requests an archive or marks downloaded', async () => {
  const result = await saveWorks(request, {baseUrl:'http://local', choosePath:async () => '', fetchImpl:async () => { throw Error('must not request'); }});
  assert.deepEqual(result, {canceled:true});
});

test('mark downloaded only after archive stream is saved under the chosen name', async t => {
  const {destination} = await fixture(t);
  const calls = [];
  const result = await saveWorks(request, {baseUrl:'http://local', choosePath:async () => destination, fetchImpl:async (url, options) => {
    calls.push(url);
    if (url.endsWith('/download')) return new Response('zip-bytes', {headers:{'content-type':'application/zip'}});
    assert.equal(await fs.readFile(destination, 'utf8'), 'zip-bytes');
    assert.equal(JSON.parse(options.body).downloaded, true);
    return new Response('{}');
  }});
  assert.equal(result.saved, true);
  assert.equal(result.statusSaved, true);
  assert.equal(calls.length, 2);
});

test('packaging failure preserves an existing destination and leaves status unchanged', async t => {
  const {root, destination} = await fixture(t);
  await fs.writeFile(destination, 'original');
  await assert.rejects(saveWorks(request, {baseUrl:'http://local', choosePath:async () => destination, fetchImpl:async () => new Response('{"error":"文件缺失"}', {status:400})}), /文件缺失/);
  assert.equal(await fs.readFile(destination, 'utf8'), 'original');
  assert.deepEqual(await fs.readdir(root), ['作品.zip']);
});

test('interrupted stream removes partial output and never records a download', async t => {
  const {root, destination} = await fixture(t);
  let calls = 0;
  const body = new ReadableStream({start(controller) { controller.enqueue(new TextEncoder().encode('partial')); controller.error(Error('connection interrupted')); }});
  await assert.rejects(saveWorks(request, {baseUrl:'http://local', choosePath:async () => destination, fetchImpl:async () => { calls++; return new Response(body, {headers:{'content-type':'application/zip'}}); }}), /connection interrupted/);
  assert.equal(calls, 1);
  assert.deepEqual(await fs.readdir(root), []);
});

test('file write failure never changes status', async t => {
  const {root} = await fixture(t);
  let calls = 0;
  await assert.rejects(saveWorks(request, {baseUrl:'http://local', choosePath:async () => path.join(root, 'missing', '作品.zip'), fetchImpl:async () => { calls++; return new Response('zip', {headers:{'content-type':'application/zip'}}); }}));
  assert.equal(calls, 1);
});

test('status failure after successful save keeps the archive and reports the distinction', async t => {
  const {destination} = await fixture(t);
  const result = await saveWorks(request, {baseUrl:'http://local', choosePath:async () => destination, fetchImpl:async url => url.endsWith('/download') ? new Response('zip', {headers:{'content-type':'application/zip'}}) : new Response('{}', {status:500})});
  assert.equal(result.saved, true);
  assert.equal(result.statusSaved, false);
  assert.equal(await fs.readFile(destination, 'utf8'), 'zip');
});

test('Unicode work names retain safe separators and reject Windows path characters', () => {
  assert.equal(downloadFilename('ins简约头像｜自取.zip'), 'ins简约头像｜自取.zip');
  assert.equal(downloadFilename('CON.zip'), '作品-CON.zip');
  assert.doesNotMatch(downloadFilename('../../bad:name'), /[\\/:]/);
});
