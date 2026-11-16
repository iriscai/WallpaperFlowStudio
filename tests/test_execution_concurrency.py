import contextlib, io, json, os, sys, tempfile, threading, time, unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from app.batch_runs import BatchRunner
from app.engine import Engine, Busy
from app.nodes import NodeResult, _worker_count
from app.store import Store
from app.execution_config import CONFIG_KEY, get_execution_config, save_execution_config

class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.environment=patch.dict(os.environ,MP_CONTENT_HOME=str(self.root));self.environment.start();self.addCleanup(self.environment.stop)
        self.store=Store(self.root/'app.sqlite3');self.addCleanup(self.store.close)
        self.flows=[self.store.upsert_flow({'name':'流程'+str(i),'graph':{'nodes':[{'id':'node-'+str(i),'type':'start.theme','params':{'themeText':str(i)},'imports':[]}],'edges':[]}}) for i in range(6)]
    def config(self,f=3,p=6,i=6):return save_execution_config(self.store,{'flowConcurrency':f,'promptConcurrency':p,'imageConcurrency':i})
    def test_persistence_validation_and_worker_override(self):
        self.assertEqual(get_execution_config(self.store)['flowConcurrency'],3);self.config(2,3,4)
        with contextlib.closing(__import__('sqlite3').connect(self.root/'app.sqlite3')) as c:self.assertEqual(json.loads(c.execute('select value from settings where key=?',(CONFIG_KEY,)).fetchone()[0])['imageConcurrency'],4)
        for value in [None,{}, {'flowConcurrency':True,'promptConcurrency':3,'imageConcurrency':4},{'flowConcurrency':2,'promptConcurrency':0,'imageConcurrency':4},{'flowConcurrency':2,'promptConcurrency':3,'imageConcurrency':13}]:
            with self.assertRaises(ValueError):save_execution_config(self.store,value)
        self.assertEqual(get_execution_config(self.store)['flowConcurrency'],2)
        self.assertEqual(_worker_count({'payload':{'concurrency':1}},8,['fake-key'],override=4),4)
        self.assertEqual(_worker_count({'payload':{}},2,[],override=8),2)
    def test_engine_global_limit_duplicate_flow_and_dynamic_lowering(self):
        self.config(2);engine=Engine(self.store);entered=threading.Barrier(3);release=threading.Event()
        def node(ctx):entered.wait(3);release.wait(3);return NodeResult({'value':ctx.flow['id']},'local','done')
        with patch('app.engine.run_node',side_effect=node),ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(engine.run,f['id']) for f in self.flows[:2]];entered.wait(3)
            with self.assertRaises(Busy):engine.run(self.flows[2]['id'])
            with self.assertRaises(Busy):engine.run(self.flows[0]['id'])
            self.config(1);self.assertEqual(engine.capacity(),0);release.set();runs=[f.result() for f in futures]
        self.assertEqual({r['flowId'] for r in runs},{f['id'] for f in self.flows[:2]});self.assertEqual(len({r['id'] for r in runs}),2);self.assertEqual(engine.capacity(),1)
        for run in runs:self.assertEqual(next(iter(run['trace']['nodes'].values()))['export']['value'],run['flowId'])
    def test_batch_limit_round_barrier_and_unique_claims(self):
        self.config(3);lock=threading.Lock();active=0;peak=0;events=[];counts={}
        def run(fid,**kwargs):
            nonlocal active,peak
            with lock:counts[fid]=counts.get(fid,0)+1;round_no=counts[fid];active+=1;peak=max(peak,active);events.append(('start',round_no,time.monotonic()))
            time.sleep(.08 if fid==self.flows[0]['id'] else .02)
            with lock:active-=1;events.append(('end',round_no,time.monotonic()))
            return {'status':'succeeded','id':fid+str(round_no)}
        engine=Mock();engine.run.side_effect=run;runner=BatchRunner(self.store,engine);runner.create({'ids':[f['id'] for f in self.flows],'repeat':2})
        with ThreadPoolExecutor(max_workers=8) as pool:
            while runner.list()[0]['status'] not in ('completed','failed'):
                list(pool.map(lambda _:runner.tick(),range(8)))
        job=runner.list()[0];self.assertEqual(peak,3);self.assertEqual(job['completed'],12);self.assertEqual(len(set(job['doneIndices'])),12);self.assertEqual(set(counts.values()),{2})
        self.assertGreaterEqual(min(t for k,r,t in events if k=='start' and r==2),max(t for k,r,t in events if k=='end' and r==1))
    def test_failure_drains_active_tasks_without_launching_more(self):
        self.config(2);a_started=threading.Event();b_started=threading.Event();fail=threading.Event();finish=threading.Event()
        def run(fid,**kwargs):
            if fid==self.flows[0]['id']:a_started.set();fail.wait(3);return {'status':'failed','error':'test failure'}
            b_started.set();finish.wait(3);return {'status':'succeeded'}
        engine=Mock();engine.run.side_effect=run;runner=BatchRunner(self.store,engine);runner.create({'ids':[f['id'] for f in self.flows],'repeat':1})
        with ThreadPoolExecutor(max_workers=2) as pool:
            first=pool.submit(runner.tick);a_started.wait(2);second=pool.submit(runner.tick);b_started.wait(2);fail.set();first.result()
            self.assertFalse(runner.tick());self.assertEqual(len(runner.list()[0]['activeTasks']),1);finish.set();second.result()
        self.assertEqual(runner.list()[0]['status'],'failed');self.assertEqual(runner.list()[0]['completed'],2);self.assertEqual(engine.run.call_count,2)
    def test_cancel_drains_all_active_tasks(self):
        self.config(2);started=threading.Barrier(3);finish=threading.Event();engine=Mock()
        def run(*a,**kw):started.wait(3);finish.wait(3);return {'status':'succeeded'}
        engine.run.side_effect=run;runner=BatchRunner(self.store,engine);job=runner.create({'ids':[f['id'] for f in self.flows],'repeat':2})
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(runner.tick) for _ in range(2)];started.wait(3);runner.cancel(job['id']);self.assertFalse(runner.tick());finish.set()
            for future in futures:future.result()
        self.assertEqual(runner.list()[0]['status'],'cancelled');self.assertEqual(runner.list()[0]['completed'],2)
    def test_legacy_serial_batch_continues_after_completed_prefix(self):
        self.config(3);engine=Mock();engine.run.return_value={'status':'succeeded'};runner=BatchRunner(self.store,engine)
        job=runner.create({'ids':[f['id'] for f in self.flows],'repeat':1})
        job.update(completed=2,succeeded=2)
        for field in ['activeTasks','nextIndex','doneIndices','retryIndices']:job.pop(field)
        with self.store.lock:runner._write(job);self.store.conn.commit()
        while runner.list()[0]['status']!='completed':runner.tick()
        self.assertEqual([c.args[0] for c in engine.run.call_args_list],[f['id'] for f in self.flows[2:]])
    def test_prompt_and_image_limits_control_real_node_worker_pools(self):
        self.config(2,2,3);engine=Engine(self.store)
        text=self.store.upsert_element({'type':'model_profile','name':'text','payload':{'kind':'text','model':'fake','concurrency':1}})
        image=self.store.upsert_element({'type':'model_profile','name':'image','payload':{'kind':'image','model':'fake','concurrency':1}})
        for m in [text,image]:self.store.set_secret(m['id'],'local-test-key')
        ref_ids=[]
        for i in range(4):
            p=self.root/f'ref{i}.png';Image.new('RGB',(8,8)).save(p)
            ref=self.store.upsert_element({'type':'reference_image','name':str(i),'payload':{'path':str(p),'note':'说明'+str(i)}});ref_ids.append(ref['id'])
        nodes=[{'id':'multi','type':'prompt.from_multi_refs','params':{'referenceCount':4,'count':4},'imports':[]}]
        nodes[0]['imports']=[{'elementId':text['id']}]+[{'elementId':r} for r in ref_ids]
        nodes.append({'id':'gen','type':'image.generate','params':{'sizePreset':'square-512'},'imports':[{'elementId':image['id']}]})
        flow=self.store.upsert_flow({'name':'worker test','graph':{'nodes':nodes,'edges':[{'id':'edge','from':'multi','to':'gen','carry':[{'source':'prompts','target':'prompts'}]}]}})
        lock=threading.Lock();active={'text':0,'image':0};peaks={'text':0,'image':0};buf=io.BytesIO();Image.new('RGB',(512,512)).save(buf,'PNG');blob=buf.getvalue()
        def work(kind):
            with lock:active[kind]+=1;peaks[kind]=max(peaks[kind],active[kind])
            time.sleep(.04)
            with lock:active[kind]-=1
        def chat(*a,**k):work('text');return '一张独立图片的完整提示词'
        def gen(*a,**k):work('image');return blob,'512x512'
        with patch('app.nodes.chat',side_effect=chat),patch('app.nodes.generate_image',side_effect=gen):result=engine.run(flow['id'])
        self.assertEqual(result['status'],'succeeded',result['error']);self.assertEqual(peaks,{'text':2,'image':3})

if __name__=='__main__':unittest.main()
