import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]

def png(path,color=(240,200,200)):
    path.parent.mkdir(parents=True,exist_ok=True);Image.new('RGB',(64,64),color).save(path);return path

class ArkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec=importlib.util.spec_from_file_location('ark',ROOT/'scripts/ark.py')
        cls.a=importlib.util.module_from_spec(spec);spec.loader.exec_module(cls.a)
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=Path(self.tmp.name)/'order';self.w=self.a.w;self.w.init(self.p)
        self.env=patch.dict('os.environ',{'ARK_API_KEY':'synthetic-not-a-real-key'});self.env.start();self.addCleanup(self.env.stop)
    def file(self,name,text='合成测试，不是真实新人故事。'):
        f=self.p/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(text,encoding='utf-8');return f
    def advance_to(self,step,artifacts=None):
        artifacts=artifacts or {}
        for n in range(1,step):
            files=artifacts.get(n) or [self.file(f'step{n}.txt')]
            self.w.prepare(self.p,n,files);self.w.approve(self.p,n,'couple' if n==3 else 'producer','合成测试确认')
            if n==14:self.w.approve(self.p,n,'couple','合成测试：新人验收')
    def fake_download(self,color=(10,120,200)):
        def _dl(url,dest):png(Path(dest),color);return dest
        return _dl

    def test_character_needs_step_seven_and_authorization(self):
        photo=png(self.p/'photos/groom.jpg');style=self.file('STYLE.md','Q版二头身，柔和水彩上色')
        with patch.object(self.a,'call',side_effect=AssertionError('不应付费')):
            with self.assertRaises(ValueError):self.a.character(self.p,'新郎',[photo],style,'characters/groom-v1.png',True)
        self.advance_to(7)
        with patch.object(self.a,'call',side_effect=AssertionError('不应付费')):
            with self.assertRaises(ValueError):self.a.character(self.p,'新郎',[photo],style,'characters/groom-v1.png',False)

    def test_character_sheet_payload_and_sidecar(self):
        photo=png(self.p/'photos/groom.jpg');style=self.file('STYLE.md','Q版二头身，柔和水彩上色')
        self.advance_to(7);sent={}
        def fake(method,url,key,payload=None,timeout=180):
            sent.update(payload);return {'data':[{'url':'https://example.invalid/x.png'}]}
        with patch.object(self.a,'call',side_effect=fake),patch.object(self.a,'download',side_effect=self.fake_download()):
            out=self.a.character(self.p,'新郎',[photo],style,'characters/groom-v1.png',True)
        self.assertTrue(sent['image'].startswith('data:image/jpeg;base64,'))
        self.assertIn('Q版二头身',sent['prompt']);self.assertFalse(sent['watermark'])
        meta=json.loads(out.with_suffix('.gen.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['sources'][0]['sha256'],self.w.sha(photo))

    def test_step_eight_keyframe_requires_approved_characters(self):
        style=self.file('STYLE.md','Q版二头身');sheet=png(self.p/'characters/groom-v1.png')
        chars=self.file('CHARACTERS.json',json.dumps({'style':'STYLE.md','characters':[{'id':'groom','name':'新郎','sheet':'characters/groom-v1.png'}]}))
        prompt=self.file('prompts/S02-image.txt','咖啡馆窗边，他低头看书')
        self.advance_to(8,{7:[chars,sheet]})
        sent={}
        def fake(method,url,key,payload=None,timeout=180):
            sent.update(payload);return {'data':[{'url':'https://example.invalid/x.png'}]}
        with patch.object(self.a,'call',side_effect=fake),patch.object(self.a,'download',side_effect=self.fake_download()):
            self.a.keyframe(self.p,'S02',prompt,chars,['groom'],'images/S02-v1.png','16:9',True)
        self.assertIn('第1张参考图是「新郎」',sent['prompt']);self.assertEqual(sent['size'],'2560x1440')
        png(sheet,(1,2,3))  # 改动已确认设定图
        with patch.object(self.a,'call',side_effect=AssertionError('不应付费')):
            with self.assertRaises(ValueError):self.a.keyframe(self.p,'S03',prompt,chars,['groom'],'images/S03-v1.png','16:9',True)

    def video_project(self):
        plan=self.file('SHOT_PLAN.json',json.dumps({'shots':[{'id':'S01'},{'id':'S02'}]}))
        i1=png(self.p/'images/S01-v1.png');i2=png(self.p/'images/S02-v1.png',(0,0,0))
        p1=self.file('prompts/S01-video.txt','她抬头微笑');p2=self.file('prompts/S02-video.txt','他递过咖啡')
        pack=self.file('VIDEO_PACK.json',json.dumps({'plan':'SHOT_PLAN.json','shots':[
            {'id':'S01','image':'images/S01-v1.png','prompt':'prompts/S01-video.txt'},
            {'id':'S02','image':'images/S02-v1.png','prompt':'prompts/S02-video.txt'}]}))
        self.advance_to(9,{6:[plan],7:[i1],8:[i2]});return pack

    def test_batch_stops_on_first_failure_and_respects_limit(self):
        pack=self.video_project();calls=[]
        def fake(method,url,key,payload=None,timeout=180):
            calls.append(method)
            if method=='POST':
                self.assertEqual(payload['content'][1]['role'],'first_frame');return {'id':'cgt-test1'}
            return {'status':'failed','error':{'code':'InputImageSensitive'}}
        with patch.object(self.a,'call',side_effect=fake):
            with self.assertRaises(ValueError):self.a.i2v_batch(self.p,pack,'videos','v1',5,5,'16:9','720p',True,interval=0)
        self.assertEqual(calls.count('POST'),1)
        def ok(method,url,key,payload=None,timeout=180):
            return {'id':'cgt-ok'} if method=='POST' else {'status':'succeeded','content':{'video_url':'https://example.invalid/v.mp4'}}
        def dl(url,dest):Path(dest).write_bytes(b'not really a video');return dest
        with patch.object(self.a,'call',side_effect=ok),patch.object(self.a,'download',side_effect=dl):
            r=self.a.i2v_batch(self.p,pack,'videos','v2',1,5,'16:9','720p',True,interval=0)
        self.assertEqual(r['generated'],['S01']);self.assertEqual(r['remaining'],['S02'])

    def test_timeout_leaves_pending_task_instead_of_resubmitting(self):
        pack=self.video_project()
        def slow(method,url,key,payload=None,timeout=180):
            return {'id':'cgt-slow'} if method=='POST' else {'status':'running'}
        with patch.object(self.a,'call',side_effect=slow):
            with self.assertRaises(ValueError):self.a.i2v(self.p,pack,'S01','videos/S01-v1.mp4',5,'16:9','720p',True,interval=0,timeout=0)
        pending=json.loads((self.p/'videos/S01-v1.pending.json').read_text(encoding='utf-8'))
        self.assertEqual(pending['task_id'],'cgt-slow')

    def test_purge_only_after_acceptance_and_keeps_state_valid(self):
        photo=png(self.p/'photos/bride.jpg')
        with self.assertRaises(ValueError):self.a.purge(self.p,'photos',True)
        self.advance_to(15,{1:[photo,self.file('FACTS.md')]})
        with self.assertRaises(ValueError):self.a.purge(self.p,'photos',False)
        log=self.a.purge(self.p,'photos',True)
        self.assertFalse(photo.exists());self.assertEqual(log[0]['path'],'photos/bride.jpg')
        self.w.validate(self.p)

if __name__=='__main__':unittest.main()
