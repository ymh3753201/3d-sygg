"""Full-film scope, revised segments and narration placement; synthetic media only."""
import json
import array
import math
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import commercial_ad as ad
import media
import providers as pv
import test_commercial_ad as fixtures


class PostproductionTests(unittest.TestCase):
    _reference_rows = fixtures.StateWorkflowTests._reference_rows
    _register = fixtures.StateWorkflowTests._register

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self): self.temp.cleanup()

    def parent(self, narration=True):
        p = ad.AdOrchestrator.create('full-20s', self.root)
        plan = fixtures.sample_plan(2, narration=narration)
        if narration: plan['narration'].update(start_time=2, end_time=18)
        p.prepare(fixtures.analysis_with_source(self.root), plan, target_duration=20)
        self._register(p, 2); p.approve_references()
        return p

    def child(self, parent):
        p = ad.AdOrchestrator.create('replacement-clip2', self.root)
        plan = fixtures.sample_plan(1)
        p.prepare(parent.load()['analysis'], plan, target_duration=10,
                  parent_project=parent.project_dir, replace_clip=2)
        self._register(p, 1); p.approve_references()
        return p

    def fixture_media(self):
        paths = []
        for color, frequency in [('red', 220), ('blue', 330)]:
            p = self.root/f'{color}.mp4'
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i',f'color={color}:size=320x180:rate=12:duration=10',
                '-f','lavfi','-i',f'sine=frequency={frequency}:duration=10','-c:v','libx264','-preset','ultrafast',
                '-c:a','aac','-shortest',str(p)],check=True,capture_output=True)
            paths.append(p)
        voice = self.root/'voice.mp3'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=880:sample_rate=32000:duration=3',
                        '-c:a','libmp3lame',str(voice)],check=True,capture_output=True)
        return *paths, voice

    def test_revised_clip_returns_to_20s_parent_and_keeps_one_full_narration(self):
        red, blue, voice = self.fixture_media()
        class Http:
            def __init__(self): self.posts=[]
            def request_json(self, method, url, **kwargs):
                self.posts.append(url)
                if url.endswith('t2a_v2'):
                    return {'data':{'audio':voice.read_bytes().hex()},'base_resp':{'status_code':0}}
                return {'id':f'task-{len(self.posts)}'}
            def safe_json(self, method, url, **kwargs):
                if url.endswith('get_voice'):
                    return {'system_voice':[{'voice_id':'Chinese (Mandarin)_Reliable_Executive'}]}
                if url.endswith(('/task-3','/task-4')):
                    return {'status':'failed','error':{'code':'generation_failed','message':'generic'}}
                return {'status':'completed','video_url':url + '.mp4'}
            def safe_download(self, url, output):
                output.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(red if 'task-2' in url else blue, output)
                return output
        class Publisher:
            def __init__(self, rows, **kwargs): self.rows=rows
            def __enter__(self): return self
            def __exit__(self,*a): pass
            def health_check(self): pass
            def publish(self):
                return [pv.PublishedAsset(r['path'],f"https://fixture.invalid/{r['sha256']}.png",r['sha256']) for r in self.rows]
        http=Http()
        def configure(p):
            p.key_loader=lambda *_:'fixture'
            p.publisher_cls=Publisher
            p.omni_factory=lambda key, ledger:pv.OmniClient(key,ledger,http=http)
            p.minimax_factory=lambda key, ledger:pv.MiniMaxClient(key,ledger,http=http)
        parent=self.parent();configure(parent)
        original=parent.load()
        real_which=shutil.which
        with mock.patch.object(ad.shutil,'which',side_effect=lambda n:'/fake/cloudflared' if n=='cloudflared' else real_which(n)), mock.patch.object(ad.time,'sleep'):
            with self.assertRaises(pv.ProviderError): parent.produce()
            self.assertEqual(len(http.posts),4)  # one speech, original two videos, one failed automatic retry
            child=self.child(parent);configure(child)
            self.assertEqual(child.load()['reference_asset_plan']['generation_order'],['storyboard:1'])
            with mock.patch.object(ad, 'stitch_clips', side_effect=pv.ValidationError('local editing interrupted')):
                with self.assertRaisesRegex(pv.ValidationError, 'editing interrupted'): child.produce()
            self.assertEqual(child.load()['state'], 'segment_ready')
            self.assertEqual(parent.load()['state'], 'failed')
            result=child.resume()
        self.assertEqual(result['project_id'],parent.project_dir.name)
        self.assertEqual(result['submission_counts'], {'omni': 4, 'minimax': 1})
        self.assertEqual(child.load()['state'],'segment_ready')
        self.assertEqual(parent.load()['state'],'awaiting_manual_review', (parent.project_dir/'qa/qa-report.json').read_text() + str(media.probe_media(parent.project_dir/'final/picture-lock.mp4')))
        self.assertEqual(len(http.posts),5)
        self.assertEqual(sum(u.endswith('t2a_v2') for u in http.posts),1)
        self.assertEqual(parent.load()['approvals'],original['approvals'])
        final=Path(result['final_video']);info=media.probe_media(final)
        self.assertAlmostEqual(info['duration'],20,delta=.35)
        self.assertEqual(len(result['assembly']['clips']),2)
        self.assertEqual(result['assembly']['narration']['start_time'],2)
        self.assertIn('replacement-clip2',result['assembly']['clips'][1]['reference_approval'])
        # Verify actual shot order in the exported picture, not just metadata/duration.
        def rgb(t):
            return subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(final),'-frames:v','1',
                '-vf','scale=1:1','-pix_fmt','rgb24','-f','rawvideo','-'],check=True,capture_output=True).stdout[:3]
        self.assertGreater(rgb(5)[0],rgb(5)[2]+80)
        self.assertGreater(rgb(15)[2],rgb(15)[0]+80)
        with self.assertRaises(pv.ValidationError): child.complete_review({'reviewer':'fixture','checks':{}})
        child.resume()
        self.assertEqual(len(http.posts),5)
        # Optional retained artifact is explicitly synthetic, never an artistic acceptance.
        out=os.environ.get('SYGG_TEST_ARTIFACT_DIR')
        if out:
            target=Path(out)/'replacement-20s';target.mkdir(parents=True,exist_ok=True)
            shutil.copy2(final,target/'synthetic-20s-with-voice.mp4')
            (target/'assembly.json').write_text(json.dumps(result['assembly'],ensure_ascii=False,indent=2))
        # A changed source invalidates final review instead of silently delivering different material.
        Path(result['assembly']['clips'][1]['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(pv.ValidationError,'changed'):
            parent.complete_review({'reviewer':'fixture','checks':{k:True for k in ad.CREATIVE_REVIEW_CHECKS}})

    def test_approved_unattempted_narration_is_completed_after_existing_videos(self):
        red, blue, voice = self.fixture_media()
        parent = self.parent()
        for index, source in enumerate((red, blue), 1):
            parent.ledger.append(provider='omni', event='submitted', task_id=f'old-{index}', clip_index=index)
            parent.ledger.append(provider='omni', event='downloaded', task_id=f'old-{index}',
                                 path=str(source), sha256=pv.sha256_file(source))
        state = parent.load(); state['state']='failed'; parent.save(state)
        http = mock.Mock()
        http.request_json.return_value = {'data': {'audio': voice.read_bytes().hex()}, 'base_resp': {'status_code': 0}}
        parent.key_loader = lambda *_: 'fixture'
        parent.minimax_factory = lambda key, ledger: pv.MiniMaxClient(key, ledger, http=http)
        parent.omni_factory = mock.Mock()
        with mock.patch.object(parent, '_preflight_core', return_value={'status': 'pass',
                'system_voices': [{'voice_id': state['plan']['narration']['voice_id']}]}):
            result = parent.resume()
        self.assertEqual(result['status'], 'awaiting_manual_review')
        self.assertEqual(http.request_json.call_count, 1)
        parent.omni_factory.return_value.submit.assert_not_called()
        parent.resume()
        self.assertEqual(http.request_json.call_count, 1)

    def test_replacement_cannot_change_parent_duration_or_drop_parent_link(self):
        parent=self.parent(False);s=parent.load();s['state']='failed';parent.save(s)
        child=ad.AdOrchestrator.create('wrong',self.root)
        with self.assertRaisesRegex(pv.ValidationError,'duration'):
            child.prepare(parent.load()['analysis'],fixtures.sample_plan(1),target_duration=9,
                          parent_project=parent.project_dir,replace_clip=2)
        with self.assertRaisesRegex(pv.ValidationError,'together'):
            child.prepare(parent.load()['analysis'],fixtures.sample_plan(1),target_duration=10,parent_project=parent.project_dir)
        revision=self.child(parent);s=revision.load();s['plan']['replacement_for']['clip_index']=1;revision.save(s)
        with self.assertRaises(pv.ValidationError):revision.produce()

    def test_narration_is_explicit_and_window_fits_full_film(self):
        for narration in ({'enabled':False}, {'enabled':False,'reason':'visual only','text':'do not silently discard this'},
                          {**fixtures.sample_plan(narration=True)['narration'],'start_time':18,'end_time':12}):
            plan=fixtures.sample_plan(2);plan['narration']=narration
            with self.assertRaises(pv.ValidationError):
                ad.AdOrchestrator.create('bad-voice',self.root).prepare(fixtures.analysis_with_source(self.root),plan,target_duration=20)

    def test_partial_or_unbound_sources_cannot_be_assembled_as_full_ad(self):
        parent=self.parent(False)
        raw=self.root/'second-only.mp4';raw.write_bytes(b'not a full film')
        with self.assertRaisesRegex(pv.ValidationError,'every planned clip'):
            parent._assemble(parent.load(),[raw],None)
        with self.assertRaisesRegex(pv.ValidationError,'downloaded result'):
            parent._assemble(parent.load(),[raw,raw],None)

    def test_enabled_narration_cannot_be_skipped_during_assembly(self):
        parent=self.parent()
        with self.assertRaisesRegex(pv.ValidationError,'narration must be present'):
            parent._assemble(parent.load(),[self.root/'a',self.root/'b'],None)

    def test_short_voice_offset_fits_or_blocks_instead_of_truncating_speech(self):
        red,_,voice=self.fixture_media()
        with self.assertRaisesRegex(pv.ValidationError,'longer'):
            media.mix_narration(red,voice,self.root/'bad.mp4',start_time=8)
        media.mix_narration(red,voice,self.root/'good.mp4',start_time=2)
        info = media.probe_media(self.root/'good.mp4')
        self.assertAlmostEqual(info['duration'],10,delta=.2)
        self.assertAlmostEqual(info['audio_duration'], info['video_duration'], delta=.1)
        def energy(t, frequency):
            raw = subprocess.run(['ffmpeg','-v','error','-ss',str(t),'-i',str(self.root/'good.mp4'),
                '-t','0.5','-vn','-af',f'bandpass=f={frequency}:width_type=h:width=40',
                '-ar','8000','-ac','1','-f','f32le','-'],capture_output=True,check=True).stdout
            values = array.array('f'); values.frombytes(raw)
            return math.sqrt(sum(v*v for v in values)/len(values))
        self.assertGreater(energy(3,880), energy(.5,880)*8)
        self.assertGreater(energy(9,220), .02)  # ending ambient sound is retained, not padded silence
