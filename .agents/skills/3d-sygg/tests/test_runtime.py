"""Failure-oriented tests: no paid or external network calls."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import commercial_ad as ad
import providers as pv
import media
import test_commercial_ad as fixtures
from test_commercial_ad import analysis_with_source, sample_plan, png_bytes
from test_providers import FakeHttp


class RuntimeTests(unittest.TestCase):
    _project = fixtures.StateWorkflowTests._project
    _reference_rows = fixtures.StateWorkflowTests._reference_rows
    _register = fixtures.StateWorkflowTests._register

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = pv.TaskLedger(self.root / 'ledger.json')

    def tearDown(self):
        self.temp.cleanup()

    def test_malformed_plan_fails_before_copying_private_sources(self):
        for field, value in (("clips", None), ("talent_strategy", None)):
            with self.subTest(field=field):
                plan = sample_plan(); plan[field] = value
                project = ad.AdOrchestrator.create("malformed-" + field, self.root)
                with self.assertRaises(pv.ValidationError):
                    project.prepare(analysis_with_source(self.root), plan, target_duration=10)
                self.assertFalse((project.project_dir / "private").exists())

    def test_temporary_original_can_disappear_after_private_snapshot(self):
        project = self._project()
        state = project.load()
        original = Path(state["source_assets"][0]["original_path"])
        snapshot = Path(state["source_assets"][0]["path"])
        self.assertNotEqual(original, snapshot)
        original.unlink()
        self.assertTrue(snapshot.is_file())
        self._register(project, 1)
        project.approve_references()
        public = ad._unique_approved_files(project.load()["reference_sets"])
        self.assertNotIn(str(snapshot), [row["path"] for row in public])

    def test_unbound_reference_in_freeform_direction_is_rejected(self):
        plan = sample_plan(); plan["global_anchor"] = "Use Image9 for face identity"
        with self.assertRaisesRegex(pv.ValidationError, "unbound"):
            ad.AdOrchestrator.create("unbound", self.root).prepare(analysis_with_source(self.root), plan, target_duration=10)

    def test_publisher_observes_health_during_short_task_and_retains_log(self):
        publisher = pv.TemporaryPublisher([], evidence_dir=self.root / "publication")
        publisher._stop = mock.Mock()
        publisher._stop.wait.side_effect = [False, False, True]
        publisher.health_check = mock.Mock()
        publisher._observe()
        self.assertEqual(publisher.health_check.call_count, 2)
        publisher._stop = __import__("threading").Event()
        publisher.log_path = publisher.evidence_dir / "cloudflared-fixture.log"
        publisher.log_path.write_text("Registered tunnel connection")
        publisher.close()
        self.assertTrue(publisher.log_path.is_file())

    def test_upstream_generic_failure_preserves_details_without_inventing_cause(self):
        key = 'a-sensitive-fixture-credential'
        http = mock.Mock()
        http.safe_json.return_value = {'data': {'status': 'failed', 'progress': 100,
            'error': {'code': 'generation_failed', 'message': 'Check material, parameters or copyright; ' + key}}}
        with self.assertRaisesRegex(pv.ProviderError, 'cause undetermined'):
            pv.OmniClient(key, self.ledger, http=http).poll('known-task')
        record = self.ledger.records()[-1]
        self.assertEqual(record['error']['code'], 'generation_failed')
        self.assertEqual(record['cause'], 'undetermined')
        self.assertNotIn(key, self.ledger.path.read_text())
        self.assertIn('Check material', record['error']['message'])

    def test_query_disconnect_recovers_with_get_only(self):
        http = mock.Mock()
        http.safe_json.side_effect = [pv.ProviderError('disconnected'), {'status': 'processing', 'progress': 20}, {'status': 'completed'}]
        with mock.patch.object(pv.time, 'sleep'):
            data = pv.OmniClient('fixture', self.ledger, http=http).poll('known-task')
        self.assertEqual(data['status'], 'completed')
        self.assertTrue(all(call.args[0] == 'GET' for call in http.safe_json.call_args_list))
        http.request_json.assert_not_called()
        self.assertEqual([r['event'] for r in self.ledger.records()], ['query_error', 'observed', 'completed'])

    def test_query_timeout_is_resumable_and_never_posts(self):
        http = mock.Mock()
        http.safe_json.return_value = {'status': 'processing'}
        with self.assertRaisesRegex(pv.ProviderError, 'resume'):
            pv.OmniClient('fixture', self.ledger, http=http).poll('known-task', timeout_seconds=0)
        self.assertEqual(self.ledger.records()[-1]['event'], 'poll_timeout')
        http.request_json.assert_not_called()

    def test_repeated_query_transport_error_is_bounded_by_deadline(self):
        http = mock.Mock()
        http.safe_json.side_effect = pv.ProviderError('network unavailable')
        with self.assertRaisesRegex(pv.ProviderError, 'resume'):
            pv.OmniClient('fixture', self.ledger, http=http).poll('known-task', timeout_seconds=0)
        self.assertEqual(http.safe_json.call_count, 1)

    def test_mid_response_paid_disconnect_is_unknown(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.side_effect = http.client.IncompleteRead(b'partial')
        with mock.patch.object(pv.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(pv.SubmissionUnknown):
                pv.JsonHttpClient().request_json('POST', 'https://fixture.invalid/task', ambiguous_on_transport=True)

    def test_new_tunnel_url_cannot_bypass_logical_clip_once(self):
        http = FakeHttp(post_result={'id': 'known-task'})
        client = pv.OmniClient('fixture', self.ledger, http=http)
        client.submit('prompt', ['https://first.invalid/a.png'], clip_index=1, aspect_ratio='16:9')
        with self.assertRaises(pv.PaidRequestBlocked):
            client.submit('changed prompt', ['https://second.invalid/a.png'], clip_index=1, aspect_ratio='16:9')
        self.assertEqual(len(http.calls), 1)

    def test_crash_after_attempt_prevents_next_paid_operation(self):
        self.ledger.append(provider='omni', event='attempted', request_hash='first', clip_index=1)
        http = FakeHttp(post_result={'id': 'next'})
        with self.assertRaises(pv.PaidRequestBlocked):
            pv.OmniClient('fixture', self.ledger, http=http).submit('p', ['https://fixture.invalid/a'], clip_index=2, aspect_ratio='16:9')
        self.assertFalse(http.calls)

    def test_separate_processes_reserve_only_one_paid_operation(self):
        script = '''import sys
from pathlib import Path
from providers import TaskLedger, PaidRequestBlocked
try:
 TaskLedger(Path(sys.argv[1])).append(provider="omni",event="attempted",clip_index=1,request_hash=sys.argv[2])
 print("reserved")
except PaidRequestBlocked:
 print("blocked")
'''
        env = {**os.environ, 'PYTHONPATH': str(Path(pv.__file__).parent)}
        processes = [subprocess.Popen([sys.executable, '-c', script, str(self.ledger.path), str(i)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for i in range(4)]
        results = [p.communicate(timeout=10) for p in processes]
        self.assertTrue(all(p.returncode == 0 for p in processes), results)
        self.assertEqual(sum(out.strip() == 'reserved' for out, _ in results), 1)
        self.assertEqual(len(self.ledger.records()), 1)

    def test_project_lock_rejects_another_state_changing_command(self):
        project = self._project()
        with pv.file_lock(project.project_dir / '.operation.lock'):
            with self.assertRaises(pv.PaidRequestBlocked):
                project.approve_plan()
        self.assertEqual(project.load()['state'], 'awaiting_plan_approval')

    def test_reference_role_options_bind_only_real_uploaded_images(self):
        for roles in (['storyboard'], ['storyboard', 'product_master'], ['storyboard', 'talent'], ['storyboard', 'product_master', 'talent']):
            plan = sample_plan(human=True)
            clip = plan['clips'][0]
            clip['execution_reference_roles'] = roles
            clip.update(keep_duration=10, storyboard_panel_count=3)
            clip['shots'] = ad.materialize_shot_timeline(clip['shots'], 9.2)
            prompt = ad.VisualPromptBuilder(analysis_with_source(self.root), plan).omni_prompt(clip)
            self.assertNotIn(f'Image{len(roles)+1}', prompt)
            self.assertNotIn('approved talent reference', prompt)
            for position, role in enumerate(roles, 1):
                self.assertIn(f'Image{position}:', prompt)
            self.assertEqual(prompt.count(ad.SILENT_CONSTRAINT), 1)

    def test_explicit_timing_and_no_copy_survive_plan_approval(self):
        plan = sample_plan(2)
        plan['ad_copy'] = []
        plan['clips'][0].update(keep_duration=9, tail_margin=0.4, execution_reference_roles=['storyboard'])
        plan['clips'][1].update(keep_duration=6, tail_margin=0.5, execution_reference_roles=['storyboard', 'product_master'])
        project = ad.AdOrchestrator.create('timing', self.root)
        state = project.prepare(analysis_with_source(self.root), plan, target_duration=15)
        self.assertEqual([c['keep_duration'] for c in state['clips']], [9, 6])
        self.assertAlmostEqual(state['clips'][0]['shots'][-1]['end'], 8.6)
        self.assertEqual(state['paid_counts']['omni'], 2)
        project.approve_plan()
        state = project.load()
        state['clips'][0]['execution_reference_roles'].append('product_master')
        project.save(state)
        with self.assertRaisesRegex(pv.ValidationError, 'Plan changed'):
            project.register_references({'references': []})

    def test_invalid_timing_and_nonfinite_weights_rejected(self):
        for value in (float('nan'), float('inf'), 0, -1):
            plan = sample_plan()
            plan['clips'][0]['shots'][0]['duration_weight'] = value
            with self.assertRaises(pv.ValidationError):
                ad.AdOrchestrator.create('bad-time', self.root).prepare(analysis_with_source(self.root), plan, target_duration=10)

    def test_bogus_execution_file_is_rejected_before_approval(self):
        project = self._project()
        self._register(project, 1)
        state = project.load()
        state['reference_sets']['1'][0]['path'] = state['source_assets'][0]['path']
        state['reference_sets']['1'][0]['sha256'] = state['source_assets'][0]['sha256']
        project.save(state)
        with self.assertRaisesRegex(pv.ValidationError, 'not a registered'):
            project.approve_references()

    def test_missing_previously_completed_narration_cannot_be_silently_omitted_on_resume(self):
        project = self._project(narration=True)
        self._register(project, 1)
        project.approve_references()
        state = project.load(); state['state'] = 'failed'; project.save(state)
        raw = project.project_dir / 'clips/raw/clip-01.mp4'; raw.write_bytes(b'fixture')
        project.ledger.append(provider='omni', event='submitted', clip_index=1, task_id='known')
        project.ledger.append(provider='omni', event='downloaded', task_id='known', path=str(raw), sha256=pv.sha256_file(raw))
        project.key_loader = lambda *_: 'fixture'
        project.omni_factory = mock.Mock()
        project.ledger.append(provider='minimax', event='completed', request_hash='approved-but-file-lost', sha256='missing')
        with mock.patch.object(project, '_assemble') as assemble:
            with self.assertRaisesRegex(pv.ValidationError, 'narration is missing'):
                project.resume()
            assemble.assert_not_called()
        project.omni_factory.return_value.submit.assert_not_called()

    def test_resume_uses_verified_download_path_not_stale_conventional_path(self):
        project = self._project(); self._register(project, 1); project.approve_references()
        state = project.load(); state['state'] = 'failed'; project.save(state)
        verified = self.root / 'verified.mp4'; verified.write_bytes(b'correct')
        (project.project_dir / 'clips/raw/clip-01.mp4').write_bytes(b'stale')
        project.ledger.append(provider='omni', event='submitted', clip_index=1, task_id='known')
        project.ledger.append(provider='omni', event='downloaded', task_id='known', path=str(verified), sha256=pv.sha256_file(verified))
        project.key_loader = lambda *_: 'fixture'; project.omni_factory = mock.Mock()
        with mock.patch.object(project, '_assemble', return_value={}) as assemble:
            project.resume()
        self.assertEqual(assemble.call_args.args[1], [verified])
        project.omni_factory.return_value.poll.assert_not_called()

    def test_environment_credential_does_not_touch_keychain(self):
        with mock.patch.object(pv.Keychain, '_load_stored') as read, mock.patch.object(pv.Keychain, 'store') as write:
            value, origin = pv.Keychain.resolve(pv.OMNI_KEYCHAIN_SERVICE, environ={'WXART_API_KEY': 'fixture-only-key-123456789'})
        self.assertEqual(origin, 'environment:WXART_API_KEY')
        read.assert_not_called(); write.assert_not_called()

    def test_local_http_allowlist_and_retained_lifecycle_evidence(self):
        asset = self.root / 'approved.png'; asset.write_bytes(png_bytes(20))
        publisher = pv.TemporaryPublisher([{'path': str(asset), 'sha256': pv.sha256_file(asset)}], evidence_dir=self.root/'evidence')
        publisher.stage(); served = Path(publisher.temp.name); port = publisher._start_http()
        target = next(served.iterdir())
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/{target.name}', timeout=5) as response:
                self.assertEqual(response.read(), asset.read_bytes())
            for route in ('/', '/private.png', '/%2e%2e/approved.png'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(f'http://127.0.0.1:{port}{route}', timeout=5)
                self.assertEqual(error.exception.code, 404)
                error.exception.close()
        finally:
            publisher.close()
        self.assertFalse(served.exists())
        events = publisher.events.records()
        self.assertEqual(events[-1]['event'], 'closed')
        self.assertTrue(any(e.get('status') == 200 for e in events))
        self.assertTrue(all(e.get('supplier_fetch_confirmed') is False for e in events))

    def test_dead_publication_stops_before_paid_call(self):
        project = self._project(); self._register(project, 1)
        factory = mock.Mock(); project.omni_factory = factory; project.key_loader = lambda *_: 'fixture'
        with self.assertRaises(pv.ProviderError):
            project._generate_video_batches(project.load(), {}, health_check=mock.Mock(side_effect=pv.ProviderError('dead tunnel')))
        factory.return_value.submit.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
    def test_full_15_second_produce_mix_and_resume_without_paid_calls(self):
        self._full_15_second_produce_mix_and_resume()

    def test_full_15_second_second_segment_failure_recovers_and_mixes(self):
        with mock.patch.object(ad.time, 'sleep'):
            self._full_15_second_produce_mix_and_resume(fail_second=True)

    def _full_15_second_produce_mix_and_resume(self, fail_second=False):
        source = self.root / 'synthetic-10s.mp4'
        voice = self.root / 'synthetic-voice.mp3'
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=12:duration=10',
                        '-f','lavfi','-i','sine=frequency=220:sample_rate=48000:duration=10','-c:v','libx264','-preset','ultrafast',
                        '-c:a','aac','-shortest',str(source)],check=True,capture_output=True)
        subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','sine=frequency=880:sample_rate=32000:duration=2',
                        '-c:a','libmp3lame',str(voice)],check=True,capture_output=True)
        project = self._project(duration=15, narration=True); self._register(project, 2); project.approve_references()
        class Http:
            posts = 0
            def request_json(self, method, url, **kwargs):
                self.posts += 1
                if url.endswith('t2a_v2'):
                    return {'data': {'audio': voice.read_bytes().hex()}, 'base_resp': {'status_code': 0}}
                return {'id': f'task-{self.posts}', 'status': 'queued'}
            def safe_json(self, method, url, **kwargs):
                if url.endswith('get_voice'):
                    return {'system_voice':[{'voice_id':'Chinese (Mandarin)_Reliable_Executive'}], 'voice_cloning':[{'voice_id':'forbidden-clone'}]}
                if fail_second and url.endswith('/task-3'):
                    return {'status':'failed','error':{'code':'generation_failed','message':'Generic failure'}}
                return {'status':'completed','video_url':'https://fixture.invalid/result.mp4'}
            def safe_download(self, url, output):
                output.parent.mkdir(parents=True,exist_ok=True); shutil.copy2(source, output); return output
        class Publisher:
            def __init__(self, rows, **kwargs): self.rows = rows
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def health_check(self): pass
            def publish(self):
                return [pv.PublishedAsset(r['path'], f"https://fixture.invalid/{r['sha256']}.png", r['sha256']) for r in self.rows]
        http = Http()
        project.key_loader = lambda *_: 'fixture'
        project.publisher_cls = Publisher
        project.omni_factory = lambda key, ledger: pv.OmniClient(key, ledger, http=http)
        project.minimax_factory = lambda key, ledger: pv.MiniMaxClient(key, ledger, http=http)
        real_which = shutil.which
        with mock.patch.object(ad.shutil, 'which', side_effect=lambda name: '/fake/cloudflared' if name == 'cloudflared' else real_which(name)):
            result = project.produce()
        self.assertEqual(result['status'], 'awaiting_manual_review')
        self.assertEqual(http.posts, 4 if fail_second else 3)
        final = Path(result['final_video']); info = media.probe_media(final)
        self.assertAlmostEqual(info['duration'], 15, delta=.35)
        self.assertAlmostEqual(info['audio_duration'], info['video_duration'], delta=.1)
        self.assertEqual((info['width'],info['height']), (720,1280))
        # The short narration must not truncate the ambient track: decode audio at 14 seconds.
        tail = subprocess.run(['ffmpeg','-v','error','-ss','14','-i',str(final),'-t','0.5','-vn','-f','s16le','-'],capture_output=True,check=True).stdout
        self.assertGreater(len(tail), 10000)
        self.assertTrue(any(tail))
        project.resume()
        self.assertEqual(http.posts, 4 if fail_second else 3)
        with self.assertRaises(pv.ValidationError): project.produce()
        state = project.load()
        final.write_bytes(final.read_bytes()+b'changed')
        with self.assertRaisesRegex(pv.ValidationError, 'changed after technical QA'):
            project.complete_review({'reviewer':'test','checks':{k:True for k in ad.CREATIVE_REVIEW_CHECKS}})
        # Restore synthetic MP4 for an inspectable local integration artifact; never mark artistic QA passed.
        final.write_bytes(final.read_bytes()[:-7])
        artifact_dir = os.environ.get('SYGG_TEST_ARTIFACT_DIR')
        if artifact_dir:
            target = Path(artifact_dir) / ('automatic-recovery' if fail_second else 'success')
            shutil.copytree(project.project_dir, target, dirs_exist_ok=True)
            (target/'SYNTHETIC-TEST.txt').write_text('Offline engineering fixture: test pattern and tones, not a generated advertisement.\n')


if __name__ == '__main__':
    unittest.main()
