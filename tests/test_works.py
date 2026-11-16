"""Historical work discovery, durable download state, and safe ZIP exports."""
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from PIL import Image
from image_fixtures import png_bytes
from app.ids import new_id, is_uuid_id
from app.server import make_handler
from app.store import Store
from app.works import list_works, set_download_status, work_download, work_preview


class WorksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.environment = patch.dict(os.environ, MP_CONTENT_HOME=str(self.root))
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.store = Store(self.root / 'app.sqlite3')
        self.addCleanup(self.store.close)
        self.blob = png_bytes(40, 60)

    def create_work(self, title='ins简约头像 有自己的小情绪｜自取', day='2026-11-16', category='头像/简约', bucket='outputs'):
        collection = self.root / bucket / day
        folder = collection / category / title
        folder.mkdir(parents=True)
        (folder / '01.png').write_bytes(self.blob)
        archive_path = folder.with_name(folder.name + '.zip')
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.writestr(title + '/01.png', self.blob)
        node_id = new_id('node')
        output = dict(title=title, folder=str(folder), zip=str(archive_path), collection=str(collection), date=day, files=['01.png'])
        return self.store.add_run(dict(id=new_id('run'), flowId='', mode='once', status='succeeded',
            startedAt=day + 'T10:00:00+00:00', finishedAt=day + 'T10:01:00+00:00',
            trace={'nodes': {node_id: {'type':'work.generate', 'status':'succeeded', 'export':{'output': output}}}, 'output': output}))

    def test_history_search_sort_and_persistent_status_survive_index_refresh(self):
        old = self.create_work('ins森林壁纸 抬头看树｜可直存', '2026-11-15', '壁纸/森林')
        self.create_work()
        works = list_works(self.store)
        self.assertEqual([w['date'] for w in works], ['2026-11-16', '2026-11-15'])
        self.assertTrue(all(is_uuid_id('work', w['id']) for w in works))
        selected = list_works(self.store, query='森林')[0]
        self.assertEqual(selected['runId'], old['id'])
        self.assertEqual(list_works(self.store, sort='date-asc')[0]['id'], selected['id'])
        self.assertEqual(set_download_status(self.store, [selected['id']], True), 1)
        with self.store.lock:
            self.store.conn.execute("UPDATE works SET downloaded_at='2026-11-16T12:00:00+00:00' WHERE id=?", (selected['id'],))
            self.store.conn.commit()
        # Reopen and reindex, rather than just checking an in-memory object.
        other = Store(self.root / 'app.sqlite3')
        try:
            downloaded = list_works(other, status='downloaded')
            self.assertEqual(downloaded[0]['id'], selected['id'])
            self.assertEqual(downloaded[0]['downloadedAt'], '2026-11-16T12:00:00+00:00')
            self.assertEqual(len(list_works(other, status='pending', day='2026-11-16')), 1)
            set_download_status(other, [selected['id']], False)
            self.assertEqual(list_works(other, status='downloaded'), [])
        finally:
            other.close()

    def test_deleted_run_keeps_work_and_renamed_output_keeps_download_status(self):
        run = self.create_work()
        work = list_works(self.store)[0]
        set_download_status(self.store, [work['id']], True)
        node = next(iter(run['trace']['nodes'].values()))
        node['export']['output']['title'] = 'ins简约头像 换个小心情｜自取'
        self.store.update_run(run['id'], {'trace': run['trace']})
        renamed = list_works(self.store)[0]
        self.assertEqual(renamed['id'], work['id'])
        self.assertTrue(renamed['downloaded'])
        self.assertIn('换个小心情', renamed['title'])
        self.store.delete_runs([run['id']])
        self.assertEqual(list_works(self.store)[0]['id'], work['id'])

    def test_batch_archive_starts_at_work_folders_and_resolves_collisions_across_dates_and_categories(self):
        self.create_work(bucket='one')
        self.create_work(bucket='two')
        self.create_work(day='2026-11-15', category='壁纸/动物', bucket='three')
        works = list_works(self.store)
        with work_download(self.store, [w['id'] for w in works]) as (stream, filename):
            self.assertTrue(filename.endswith('.zip'))
            with zipfile.ZipFile(stream) as archive:
                names = archive.namelist()
                self.assertEqual(len(names), 3)
                self.assertEqual(len(set(names)), 3)
                self.assertTrue(all(name.startswith('ins') and name.count('/') == 1 for name in names))
                self.assertEqual({name.split('/')[0] for name in names},
                                 {works[0]['title'], works[0]['title'] + '-2', works[0]['title'] + '-3'})
                self.assertTrue(all(archive.read(name) == self.blob for name in names))
        self.assertFalse(any(w['downloaded'] for w in list_works(self.store)))

    def test_single_download_and_zip_fallback_when_folder_files_are_missing(self):
        self.create_work()
        work = list_works(self.store)[0]
        (Path(work['folder']) / '01.png').unlink()
        self.assertTrue(list_works(self.store)[0]['available'])
        with work_download(self.store, [work['id']]) as (stream, filename):
            self.assertEqual(filename, work['title'] + '.zip')
            with zipfile.ZipFile(stream) as archive:
                self.assertEqual(archive.namelist(), [work['title'] + '/01.png'])
                self.assertEqual(archive.read(archive.namelist()[0]), self.blob)
        with Image.open(io.BytesIO(work_preview(self.store, work['id']))) as preview:
            self.assertLessEqual(max(preview.size), 180)
        Path(work['zip']).unlink()
        self.assertFalse(list_works(self.store)[0]['available'])
        with self.assertRaises(ValueError), work_download(self.store, [work['id']]): pass
        self.assertFalse(list_works(self.store)[0]['downloaded'])

    def test_bad_selection_is_atomic_and_zip_traversal_is_rejected(self):
        self.create_work()
        work = list_works(self.store)[0]
        with self.assertRaises(ValueError): set_download_status(self.store, [work['id'], 'missing'], True)
        self.assertFalse(list_works(self.store)[0]['downloaded'])
        with self.assertRaises(ValueError): set_download_status(self.store, [work['id']], 'false')
        (Path(work['folder']) / '01.png').unlink()
        with zipfile.ZipFile(work['zip'], 'w') as archive:
            archive.writestr('../outside.png', self.blob)
        with self.assertRaises(ValueError), work_download(self.store, [work['id']]): pass
        for selection in ([], 'work', [None]):
            with self.assertRaises(ValueError), work_download(self.store, selection): pass

    def test_http_list_preview_download_and_explicit_completion_status(self):
        self.create_work()
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.store, None))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        with urlopen(base + '/api/works') as response: work = json.load(response)['works'][0]
        with urlopen(base + work['previewUrl']) as response: self.assertEqual(response.headers['Content-Type'], 'image/webp')
        def post(route, body):
            return urlopen(Request(base + route, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}))
        with post('/api/works/download', {'ids':[work['id']]}) as response:
            self.assertEqual(response.headers['Content-Type'], 'application/zip')
            with zipfile.ZipFile(io.BytesIO(response.read())) as archive: self.assertEqual(len(archive.namelist()), 1)
        self.assertFalse(list_works(self.store)[0]['downloaded'])
        with post('/api/works/status', {'ids':[work['id']], 'downloaded':True}) as response: self.assertEqual(json.load(response)['updated'], 1)
        with urlopen(base + '/api/works?status=downloaded&q=ins&sort=date-asc') as response: self.assertEqual(len(json.load(response)['works']), 1)
        with self.assertRaises(HTTPError) as failure: post('/api/works/download', {'ids':['missing']})
        self.assertEqual(failure.exception.code, 400)
        failure.exception.close()


if __name__ == '__main__': unittest.main()
