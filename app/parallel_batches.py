"""Persistent bounded flow batches; each round completes before the next starts."""
import json
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

from app.engine import Busy
from app.execution_config import get_execution_config
from app.schedule import mark_after_run, normalize_schedule

class BatchRunner:
    def __init__(self, store, engine):
        self.store, self.engine = store, engine
        self.stop, self.wake = threading.Event(), threading.Event()
        self.thread = None
        with store.lock:
            store.conn.execute('''CREATE TABLE IF NOT EXISTS batch_runs (
                id TEXT PRIMARY KEY, status TEXT NOT NULL, next_run_at TEXT NOT NULL,
                created_at TEXT NOT NULL, payload TEXT NOT NULL)''')
            for row in store.conn.execute("SELECT payload FROM batch_runs WHERE status='running'").fetchall():
                job=json.loads(row[0])
                job.update(status='interrupted', currentFlow='', interruptedTasks=job.get('activeTasks',[]), activeTasks=[], lastError='程序关闭时仍有流程在执行，请核对运行记录后重新创建批次。')
                self._write(job)
            store.conn.commit()

    def _read(self, batch_id):
        row=self.store.conn.execute('SELECT payload FROM batch_runs WHERE id=?',(batch_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, job):
        self.store.conn.execute('INSERT OR REPLACE INTO batch_runs VALUES(?,?,?,?,?)',
            (job['id'],job['status'],job.get('nextRunAt') or '',job['createdAt'],json.dumps(job,ensure_ascii=False)))

    def create(self, body, now=None):
        if not isinstance(body,dict):raise ValueError('批量执行配置格式不正确。')
        ids=body.get('ids');repeat=body.get('repeat',1);mode=body.get('mode','now')
        if not isinstance(ids,list) or not ids or len(ids)>1000 or any(not isinstance(i,str) or not i.strip() for i in ids):
            raise ValueError('请勾选要执行的流程，每批最多1000条。')
        if type(repeat) is not int or not 1<=repeat<=50:raise ValueError('循环次数必须为1到50的整数。')
        if mode not in ('now','schedule'):raise ValueError('请选择立即执行或定时执行。')
        if not isinstance(body.get('continueOnError',False),bool):raise ValueError('失败后继续需要勾选设置。')
        now=now or datetime.now().astimezone();schedule=None
        if mode=='schedule':
            if not isinstance(body.get('schedule'),dict):raise ValueError('请设置定时日期和时间。')
            schedule=normalize_schedule({**body['schedule'],'enabled':True},now)
        with self.store.lock:
            ids=list(dict.fromkeys(self.store.resolve_id('flow',i) for i in ids));names=[]
            for fid in ids:
                flow=self.store.get_flow(fid)
                if flow is None:raise ValueError('勾选的流程已不存在，请刷新列表。')
                if flow.get('status')=='停用':raise ValueError('请先启用流程「'+flow['name']+'」。')
                names.append(flow['name'])
            stamp=now.astimezone(timezone.utc).isoformat(timespec='microseconds')
            job=dict(id='batch_'+str(uuid.uuid4()),flowIds=ids,flowNames=names,repeat=repeat,total=len(ids)*repeat,
                mode=mode,schedule=schedule,status='queued' if mode=='now' else 'scheduled',nextRunAt=stamp if mode=='now' else schedule['nextRunAt'],createdAt=stamp,
                completed=0,succeeded=0,failed=0,nextIndex=0,activeTasks=[],retryIndices=[],doneIndices=[],currentFlow='',currentRound=0,
                continueOnError=body.get('continueOnError',False),stopRequested=False,recentRuns=[],lastError='',cyclesCompleted=0)
            self._write(job);self.store.conn.commit()
        self.wake.set();return job

    def list(self):
        with self.store.lock:
            rows=self.store.conn.execute('''SELECT payload FROM batch_runs ORDER BY
                CASE WHEN status IN ('running','queued','scheduled') THEN 0 ELSE 1 END, created_at DESC LIMIT 30''').fetchall()
            jobs=[json.loads(r[0]) for r in rows]
        config=get_execution_config(self.store)
        for job in jobs:job['flowConcurrency']=config['flowConcurrency']
        return jobs

    def cancel(self,batch_id):
        with self.store.lock:
            job=self._read(batch_id)
            if job is None:raise ValueError('批次不存在。')
            if job['status'] in ('queued','running','scheduled'):
                job['stopRequested']=True
                if not job.get('activeTasks'):job.update(status='cancelled',nextRunAt=None)
                self._write(job);self.store.conn.commit()
        self.wake.set();return job

    def _claim(self,now):
        stamp=now.astimezone(timezone.utc).isoformat(timespec='microseconds')
        limit=get_execution_config(self.store)['flowConcurrency']
        with self.store.lock:
            rows=self.store.conn.execute('''SELECT payload FROM batch_runs WHERE
                status IN ('queued','running','scheduled') AND next_run_at<=?
                ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END,next_run_at,created_at''',(stamp,)).fetchall()
            jobs=[json.loads(row[0]) for row in rows]
            # Include tasks from any batch, not only this batch or this timer tick.
            active=sum(len(json.loads(row[0]).get('activeTasks',[])) for row in self.store.conn.execute("SELECT payload FROM batch_runs WHERE status='running'").fetchall())
            if active>=limit:return None
            for job in jobs:
                if job['stopRequested'] or job.get('haltOnFailure'):continue
                job.setdefault('activeTasks',[]);job.setdefault('nextIndex',job['completed']);job.setdefault('retryIndices',[]);job.setdefault('doneIndices',list(range(job['completed'])))
                if job['status']=='scheduled' and job['completed']>=job['total']:
                    job.update(completed=0,succeeded=0,failed=0,nextIndex=0,retryIndices=[],doneIndices=[],recentRuns=[],lastError='')
                if job['completed']>=job['total']:continue
                # Never allow round n+1 to start while round n still has work in flight.
                round_end=min(job['total'],(job['completed']//len(job['flowIds'])+1)*len(job['flowIds']))
                if job['retryIndices']:
                    index=job['retryIndices'].pop(0)
                elif job['nextIndex']<round_end:
                    index=job['nextIndex'];job['nextIndex']+=1
                else:continue
                pos=index%len(job['flowIds'])
                task={'index':index,'flowId':job['flowIds'][pos],'name':job['flowNames'][pos],'round':index//len(job['flowIds'])+1}
                job['activeTasks'].append(task)
                job.update(status='running',currentFlow='、'.join(t['name'] for t in job['activeTasks']),currentRound=task['round'])
                self._write(job);self.store.conn.commit()
                return job,task
        return None

    def tick(self,now=None):
        now=now or datetime.now().astimezone()
        claimed=self._claim(now)
        if not claimed:return False
        job,task=claimed
        busy=False
        try:
            result=self.engine.run(task['flowId'],mode='once');ok=result['status']=='succeeded';error=result.get('error') or ''
        except Busy:
            result,ok,error,busy={},False,'',True
        except Exception as exc:
            result,ok,error={},False,str(exc) or type(exc).__name__
        with self.store.lock:
            current=self._read(job['id'])
            current['activeTasks']=[t for t in current['activeTasks'] if t['index']!=task['index']]
            if busy:
                current['retryIndices'].append(task['index'])
            else:
                current['completed']+=1;current['succeeded' if ok else 'failed']+=1;current['doneIndices'].append(task['index'])
                current['recentRuns']=(current['recentRuns']+[{'index':task['index'],'flowId':task['flowId'],'runId':result.get('id'),'status':'succeeded' if ok else 'failed'}])[-50:]
                if error:current['lastError']=error
                if not ok and not current['continueOnError']:current['haltOnFailure']=True
            current['currentFlow']='、'.join(t['name'] for t in current['activeTasks'])
            if current['activeTasks']:
                current['status']='running'
            elif current['stopRequested']:
                current.update(status='cancelled',nextRunAt=None)
            elif current.get('haltOnFailure'):
                current.update(status='failed',nextRunAt=None)
            elif current['completed']>=current['total']:
                current['cyclesCompleted']+=1
                if current['mode']=='schedule':current['schedule']=mark_after_run(current['schedule'],datetime.now(now.tzinfo) if self.thread else now)
                nxt=(current.get('schedule') or {}).get('nextRunAt');current.update(status='scheduled' if nxt else 'completed',nextRunAt=nxt)
            else:current['status']='queued'
            self._write(current);self.store.conn.commit()
        self.wake.set();return not busy

    def start(self):
        self.thread=threading.Thread(target=self._loop,name='mp-parallel-batches',daemon=True);self.thread.start()

    def _loop(self):
        with ThreadPoolExecutor(max_workers=12,thread_name_prefix='batch-flow') as pool:
            pending=set()
            while not self.stop.is_set():
                limit=get_execution_config(self.store)['flowConcurrency']
                # Running futures are retained when the configured limit is lowered.
                while len(pending)<limit and not self.stop.is_set():
                    pending.add(pool.submit(self.tick))
                done,pending=wait(pending,timeout=.2,return_when=FIRST_COMPLETED)
                worked=False
                for future in done:
                    try:worked=bool(future.result()) or worked
                    except Exception as exc:print('批量执行检查失败：'+str(exc))
                if not worked:
                    self.wake.wait(.5);self.wake.clear()
