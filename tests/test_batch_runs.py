"""Batch ordering, timers, cancellation and persisted recovery; no model requests."""
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.batch_runs import BatchRunner
from app.carry import FlowError
from app.engine import Busy
from app.server import make_handler
from app.store import Store

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'app.sqlite3');self.addCleanup(self.store.close)
        self.a=self.store.upsert_flow({'name':'甲','graph':{'nodes':[],'edges':[]}})
        self.b=self.store.upsert_flow({'name':'乙','graph':{'nodes':[],'edges':[]}})
        self.engine=Mock();self.engine.run.side_effect=lambda fid,**kw: {'id':'run-'+fid,'status':'succeeded','error':None}
        self.runner=BatchRunner(self.store,self.engine)
        self.now=datetime(2026,11,8,9,0,tzinfo=timezone(timedelta(hours=8)))
    def create(self,**kwargs):return self.runner.create({'ids':[self.a['id'],self.b['id']],'repeat':2,**kwargs},self.now)
    def test_rounds_keep_selection_order_and_refresh_keeps_progress(self):
        job=self.create(ids=[self.a['id'],self.b['id'],self.a['id']])
        for i in range(4):self.assertTrue(self.runner.tick(self.now));self.assertEqual(self.runner.list()[0]['completed'],i+1)
        self.assertEqual([c.args[0] for c in self.engine.run.call_args_list],[self.a['id'],self.b['id']]*2)
        self.assertEqual(self.runner.list()[0]['status'],'completed');self.assertFalse(self.runner.tick(self.now))
        self.assertEqual(self.store.get_flow(self.a['id'])['schedule'],self.a['schedule'])
    def test_busy_retries_same_item_without_consuming_a_round(self):
        self.create();self.engine.run.side_effect=Busy('已有流程正在执行')
        self.assertFalse(self.runner.tick(self.now));self.assertEqual(self.runner.list()[0]['completed'],0)
        self.engine.run.side_effect=lambda *a,**k:{'id':'r','status':'succeeded'}
        self.runner.tick(self.now);self.assertEqual(self.runner.list()[0]['completed'],1)
        self.assertEqual(self.engine.run.call_args.args[0],self.a['id'])
    def test_failure_stops_unless_continue_was_requested(self):
        job=self.create();self.engine.run.return_value={'status':'failed','error':'模拟失败'};self.engine.run.side_effect=None
        self.runner.tick(self.now);self.assertEqual(self.runner.list()[0]['status'],'failed');self.assertFalse(self.runner.tick(self.now))
        next_job=self.create(continueOnError=True)
        for _ in range(4):self.runner.tick(self.now)
        result=next(x for x in self.runner.list() if x['id']==next_job['id'])
        self.assertEqual(result['status'],'completed');self.assertEqual(result['failed'],4)
    def test_cancel_during_current_flow_allows_only_that_flow_to_finish(self):
        job=self.create()
        def run(fid,**kwargs):
            self.runner.cancel(job['id']);return {'status':'succeeded','id':'r'}
        self.engine.run.side_effect=run
        self.runner.tick(self.now);result=self.runner.list()[0]
        self.assertEqual(result['status'],'cancelled');self.assertEqual(result['completed'],1);self.assertFalse(self.runner.tick(self.now))
    def test_cancel_queued_and_scheduled_batches_makes_no_calls(self):
        job=self.create();self.runner.cancel(job['id'])
        timed=self.create(mode='schedule',schedule={'frequency':'once','date':'2026-11-08','time':'10:00'})
        self.runner.cancel(timed['id']);self.assertFalse(self.runner.tick(self.now+timedelta(days=1)));self.engine.run.assert_not_called()
    def test_once_timer_fires_when_due_and_survives_reopen(self):
        job=self.create(mode='schedule',schedule={'frequency':'once','date':'2026-11-08','time':'10:00'})
        self.assertFalse(self.runner.tick(self.now));self.runner=BatchRunner(self.store,self.engine)
        due=self.now+timedelta(hours=2)
        for _ in range(4):self.runner.tick(due)
        result=self.runner.list()[0];self.assertEqual(result['status'],'completed');self.assertIsNone(result['nextRunAt'])
    def test_recurring_timer_starts_a_new_round_set_next_day(self):
        self.create(mode='schedule',schedule={'frequency':'daily','time':'10:00'})
        due=self.now+timedelta(hours=1)
        for _ in range(4):self.runner.tick(due)
        result=self.runner.list()[0];self.assertEqual(result['status'],'scheduled');self.assertEqual(result['completed'],4)
        self.assertFalse(self.runner.tick(due));self.assertGreater(datetime.fromisoformat(result['nextRunAt']),due)
        self.runner.tick(due+timedelta(days=1));self.assertEqual(self.runner.list()[0]['completed'],1)
    def test_restart_does_not_replay_uncertain_inflight_work(self):
        job=self.create()
        with self.store.lock:
            job['status']='running';self.runner._write(job);self.store.conn.commit()
        reopened=BatchRunner(self.store,self.engine)
        self.assertEqual(reopened.list()[0]['status'],'interrupted');self.assertFalse(reopened.tick(self.now));self.engine.run.assert_not_called()
    def test_validation_is_atomic_for_missing_disabled_and_invalid_counts(self):
        for body in [None,[],{'ids':[]},{'ids':[self.a['id']],'repeat':True},{'ids':[self.a['id']],'repeat':1.5},{'ids':[self.a['id']],'repeat':51},{'ids':[self.a['id'],'missing']}]:
            with self.subTest(body=body),self.assertRaises(ValueError):self.runner.create(body,self.now)
        disabled=self.store.upsert_flow({'name':'停用','status':'停用'})
        with self.assertRaises(ValueError):self.create(ids=[self.a['id'],disabled['id']])
        with self.assertRaises((ValueError,FlowError)):self.create(mode='schedule',schedule={'frequency':'once','date':'2026-11-07','time':'08:00'})
        with self.assertRaises((ValueError,FlowError)):self.create(mode='schedule',schedule={'frequency':'weekly','weekdays':[],'time':'10:00'})
        self.assertEqual(self.runner.list(),[])
    def test_concurrent_ticks_do_not_duplicate_a_flow(self):
        self.create();entered=threading.Barrier(3);finish=threading.Event()
        def run(*a,**k):entered.wait(3);finish.wait(3);return {'status':'succeeded'}
        self.engine.run.side_effect=run
        threads=[threading.Thread(target=self.runner.tick,args=(self.now,)) for _ in range(2)]
        for thread in threads:thread.start()
        entered.wait(3);self.assertFalse(self.runner.tick(self.now));finish.set()
        for thread in threads:thread.join()
        self.assertEqual(self.engine.run.call_count,2)
        self.assertEqual({c.args[0] for c in self.engine.run.call_args_list},{self.a['id'],self.b['id']})
    def test_http_create_list_cancel_and_invalid_payload(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store,self.engine,self.runner))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(method,path,body=None):
            req=Request('http://127.0.0.1:'+str(server.server_port)+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'},method=method)
            with urlopen(req) as response:return json.load(response)
        try:
            job=request('POST','/api/batch-runs',{'ids':[self.a['id']],'repeat':2})['batch']
            self.assertEqual(request('GET','/api/batch-runs')['batches'][0]['total'],2)
            self.assertEqual(request('POST','/api/batch-runs/'+job['id']+'/cancel',{})['batch']['status'],'cancelled')
            with self.assertRaises(HTTPError) as failure:request('POST','/api/batch-runs',{'ids':['missing']})
            self.assertEqual(failure.exception.code,400)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
