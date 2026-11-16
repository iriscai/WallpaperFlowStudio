"""A persistent work index and bounded-memory image-package downloads."""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile

from PIL import Image, ImageOps

from app.ids import new_id
from app.paths import resolve_path
from app.store import now_iso

IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}


def sync_works(store):
    # Keep the index independent of run deletion and preserve download state on refresh.
    with store.transaction():
        rows = store.conn.execute('SELECT r.*, f.name AS flow_name FROM runs r LEFT JOIN flows f ON f.id=r.flow_id').fetchall()
        for row in rows:
            trace = json.loads(row['trace'] or '{}')
            for node_id, node in (trace.get('nodes') or {}).items():
                if node.get('type') != 'work.generate' or node.get('status') != 'succeeded':
                    continue
                output = (node.get('export') or {}).get('output') or {}
                if not (output.get('folder') or output.get('zip')):
                    continue
                stamp = row['finished_at'] or row['started_at']
                try:
                    day = datetime.fromisoformat(stamp).astimezone().date().isoformat()
                except (ValueError, TypeError):
                    continue
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(output.get('date') or '')):
                    day = output['date']
                folder = output.get('folder') or ''
                collection = output.get('collection') or ''
                category = ''
                if folder and collection:
                    try:
                        category = str(Path(folder).resolve().parent.relative_to(Path(collection).resolve()))
                        if category == '.': category = ''
                    except ValueError:
                        pass
                prior = store.conn.execute('SELECT metadata FROM works WHERE run_id=? AND node_id=?', (row['id'], node_id)).fetchone()
                old = json.loads(prior['metadata']) if prior else {}
                metadata = dict(title=output.get('title') or Path(folder or output['zip']).stem,
                                flowName=row['flow_name'] or old.get('flowName') or '已删除流程',
                                flowId=row['flow_id'], createdAt=stamp, date=day, category=category,
                                folder=folder, zip=output.get('zip') or '', collection=collection,
                                files=output.get('files') if isinstance(output.get('files'), list) else [])
                store.conn.execute('''INSERT INTO works(id,run_id,node_id,metadata) VALUES(?,?,?,?)
                    ON CONFLICT(run_id,node_id) DO UPDATE SET metadata=excluded.metadata''',
                    (new_id('work'), row['id'], node_id, json.dumps(metadata, ensure_ascii=False)))


def _record(row):
    return dict(json.loads(row['metadata']), id=row['id'], runId=row['run_id'],
                downloaded=bool(row['downloaded_at']), downloadedAt=row['downloaded_at'])


def _relative_image(name):
    if not isinstance(name, str): return None
    path = PurePosixPath(name.replace('\\', '/'))
    if path.is_absolute() or '..' in path.parts or ':' in name or path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    return path


def _folder_images(work):
    if not work['folder']: return []
    folder = resolve_path(work['folder']).resolve()
    if not folder.is_dir(): return []
    names = work['files'] or [p.name for p in sorted(folder.iterdir()) if p.is_file()]
    paths = []
    for name in names:
        relative = _relative_image(name)
        if relative is None: continue
        path = (folder / Path(*relative.parts)).resolve()
        if not path.is_relative_to(folder):
            raise ValueError('作品图片路径越出了作品文件夹')
        if not path.is_file(): return []
        paths.append((relative.as_posix(), path))
    return paths


def _zip_images(archive):
    images = []
    for info in archive.infolist():
        if info.is_dir(): continue
        relative = _relative_image(info.filename)
        if relative is None:
            raise ValueError('作品 ZIP 含有非法路径或非图片文件')
        parts = relative.parts
        # Generated work ZIPs contain one named root; normalize it on download.
        relative_name = '/'.join(parts[1:]) if len(parts) > 1 else parts[0]
        images.append((relative_name, info))
    if not images: raise ValueError('作品 ZIP 中没有图片')
    if len({name for name, _ in images}) != len(images): raise ValueError('作品 ZIP 含有重复文件名')
    return images


def _zip_path(work):
    return resolve_path(work['zip']) if work['zip'] else None


def list_works(store, query='', status='', day='', sort='date-desc'):
    if status not in ('', 'downloaded', 'pending'): raise ValueError('下载状态无效')
    if sort not in ('date-desc', 'date-asc'): raise ValueError('日期排序方式无效')
    sync_works(store)
    with store.lock:
        rows = store.conn.execute('SELECT * FROM works').fetchall()
    works = []
    query = query.strip().casefold()
    for row in rows:
        work = _record(row)
        if status and work['downloaded'] != (status == 'downloaded'): continue
        if day and work['date'] != day: continue
        if query and query not in ' '.join(str(work.get(k) or '') for k in ('title', 'flowName', 'category', 'date')).casefold(): continue
        try:
            images = _folder_images(work)
            source_zip = _zip_path(work)
            zipped = source_zip is not None and source_zip.is_file()
            work['available'] = bool(images or zipped)
            work['imageCount'] = len(images) or len(work['files'])
            work['bytes'] = sum(p.stat().st_size for _, p in images) if images else source_zip.stat().st_size if zipped else 0
        except (OSError, ValueError):
            work.update(available=False, imageCount=len(work['files']), bytes=0)
        work['previewUrl'] = '/api/works/' + work['id'] + '/preview' if work['available'] else ''
        works.append(work)
    return sorted(works, key=lambda w: (datetime.fromisoformat(w['createdAt']).timestamp(), w['id']), reverse=sort == 'date-desc')


def selected_works(store, ids):
    if not isinstance(ids, list) or not ids or len(ids) > 500 or any(not isinstance(i, str) for i in ids):
        raise ValueError('请选择 1 到 500 件作品')
    ids = list(dict.fromkeys(ids))
    with store.lock:
        rows = store.conn.execute('SELECT * FROM works WHERE id IN (' + ','.join('?' for _ in ids) + ')', ids).fetchall()
    by_id = {row['id']: _record(row) for row in rows}
    if any(i not in by_id for i in ids): raise ValueError('所选作品不存在，请刷新列表')
    return [by_id[i] for i in ids]


def set_download_status(store, ids, downloaded):
    if not isinstance(downloaded, bool): raise ValueError('下载状态必须是布尔值')
    with store.transaction():
        works = selected_works(store, ids)
        for work in works:
            store.conn.execute('UPDATE works SET downloaded_at=? WHERE id=?', (now_iso() if downloaded else None, work['id']))
    return len(works)


def work_preview(store, work_id):
    work = selected_works(store, [work_id])[0]
    images = _folder_images(work)
    if images:
        source = images[0][1]
        with Image.open(source) as image:
            thumbnail = ImageOps.exif_transpose(image).convert('RGB')
    else:
        source_zip = _zip_path(work)
        if source_zip is None or not source_zip.is_file(): raise FileNotFoundError('作品文件不存在')
        with zipfile.ZipFile(source_zip) as archive:
            _, info = _zip_images(archive)[0]
            if info.file_size > 30_000_000: raise ValueError('预览图片过大')
            with Image.open(io.BytesIO(archive.read(info))) as image:
                thumbnail = ImageOps.exif_transpose(image).convert('RGB')
    thumbnail.thumbnail((180, 180))
    result = io.BytesIO()
    thumbnail.save(result, format='WEBP', quality=80)
    return result.getvalue()


def _safe_component(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' ', str(value)).strip(' .')[:100].rstrip(' .') or '作品'
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value): value = '作品-' + value
    return value


@contextmanager
def work_download(store, ids):
    works = selected_works(store, ids)
    single = len(works) == 1
    filename = _safe_component(works[0]['title']) + '.zip' if single else '壁纸作品-' + datetime.now().strftime('%Y-%m-%d') + '.zip'
    with SpooledTemporaryFile(max_size=8_000_000) as result:
        used = set()
        with zipfile.ZipFile(result, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as target:
            for work in works:
                title = _safe_component(work['title'])
                prefix = title
                base, index = prefix, 2
                while prefix.casefold() in used:
                    prefix = base + '-' + str(index)
                    index += 1
                used.add(prefix.casefold())
                images = _folder_images(work)
                if images:
                    for relative, source in images:
                        target.write(source, prefix + '/' + relative)
                else:
                    source_zip = _zip_path(work)
                    if source_zip is None or not source_zip.is_file(): raise ValueError('作品文件已缺失：' + work['title'])
                    with zipfile.ZipFile(source_zip) as original:
                        for relative, info in _zip_images(original):
                            with original.open(info) as source, target.open(prefix + '/' + relative, 'w', force_zip64=True) as destination:
                                shutil.copyfileobj(source, destination, length=1024 * 1024)
        result.seek(0)
        yield result, filename
