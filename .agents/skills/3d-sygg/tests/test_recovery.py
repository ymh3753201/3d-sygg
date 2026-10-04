"""Exercise real orchestration/ledger with simulated HTTP; no external calls."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import commercial_ad as ad
import providers as pv
import test_commercial_ad as fixtures


class Http:
    def __init__(self, outcomes=(), post_errors=()):
        self.outcomes = list(outcomes)
        self.post_errors = list(post_errors)
        self.posts = []
        self.gets = []
        self.downloads = []
        self.lock = threading.Lock()

    def request_json(self, method, url, **kwargs):
        with self.lock:
            self.posts.append(kwargs['payload'])
            n = len(self.posts)
            error = self.post_errors[n - 1] if n <= len(self.post_errors) else None
        if error:
            raise error
        return {'id': f'task-{n}'}

    def safe_json(self, method, url, **kwargs):
        task = url.rsplit('/', 1)[-1]
        n = int(task.split('-')[-1])
        self.gets.append(task)
        status = self.outcomes[n - 1] if n <= len(self.outcomes) else 'completed'
        return {'status': status, 'video_url': f'https://fixture.invalid/{task}.mp4',
                'error': {'code': 'generation_failed', 'message': 'Generic upstream failure'} if status == 'failed' else None}

    def safe_download(self, url, output):
        self.downloads.append(url)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(url.encode())
        return output


class RecoveryTests(unittest.TestCase):
    _project = fixtures.StateWorkflowTests._project
    _reference_rows = fixtures.StateWorkflowTests._reference_rows
    _register = fixtures.StateWorkflowTests._register

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sleep = mock.patch.object(ad.time, 'sleep').start()

    def tearDown(self):
        mock.patch.stopall()
        self.temp.cleanup()

    def project(self, http, duration=15, retries=1):
        project = self._project(duration)
        state = project.load()
        state["plan"]["recovery_policy"]["max_video_retries_per_clip"] = retries
        project.save(state)
        self._register(project, len(project.load()['clips']))
        project.approve_references()
        project.key_loader = lambda *_: 'fixture'
        project.omni_factory = lambda key, ledger: pv.OmniClient(key, ledger, http=http)
        return project

    def public(self, state, host='fixture.invalid'):
        return {r['path']: f"https://{host}/{r['sha256']}.png"
                for rows in state['reference_sets'].values() for r in rows}

    def run_batches(self, project, **kwargs):
        state = project.load()
        return project._generate_video_batches(state, self.public(state), **kwargs)

    def test_second_segment_failure_retries_only_second_and_reuses_on_resume(self):
        http = Http(['completed', 'failed', 'completed'])
        project = self.project(http)
        original = project.load()
        files = self.run_batches(project)
        self.assertEqual(len(http.posts), 3)
        self.assertEqual(http.posts[1], http.posts[2])
        self.assertNotEqual(http.posts[0], http.posts[1])
        self.assertEqual(len(files), 2)
        self.assertEqual(project.load(), original)
        self.assertEqual(self.run_batches(project), files)
        self.assertEqual(len(http.posts), 3)
        self.assertEqual(len(http.downloads), 2)
        tasks = list(ad._merged_omni_tasks(project.ledger.records()).values())
        self.assertEqual(tasks[-1]['retry_of'], 'task-2')
        # Deleting a download re-fetches the SAME task; never creates a video replacement.
        files[1].unlink()
        self.run_batches(project)
        self.assertEqual(len(http.posts), 3)
        self.assertEqual(http.gets[-1], 'task-3')

    def test_failure_then_429_replacement_chain_survives_restart(self):
        http = Http(['failed', 'completed', 'completed'],
                    post_errors=[None, pv.ProviderError('busy', status_code=429)])
        project = self.project(http, 10, retries=2)
        self.run_batches(project)
        self.assertEqual(len(http.posts), 3)
        self.run_batches(project)
        self.assertEqual(len(http.posts), 3)

    def test_resume_republishes_same_failed_material_without_new_plan(self):
        http = Http(['failed', 'completed'])
        project = self.project(http, 10)
        state = project.load()
        public = self.public(state)
        calls = 0
        def interrupted_publisher():
            nonlocal calls
            calls += 1
            if calls > 1: raise pv.ProviderError('tunnel interrupted')
            return public
        with self.assertRaises(pv.ProviderError):
            self.run_batches(project, ensure_public=interrupted_publisher)
        state['state'] = 'failed'; project.save(state)
        class Publisher:
            def __init__(self, rows, **kwargs): self.rows = rows
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def health_check(self): pass
            def publish(self):
                return [pv.PublishedAsset(r['path'], f"https://new.invalid/{r['sha256']}.png", r['sha256']) for r in self.rows]
        project.publisher_cls = Publisher
        with mock.patch.object(project, '_preflight_core', return_value={'status':'pass'}), \
             mock.patch.object(project, '_assemble', return_value={'status':'assembled'}) as assemble:
            self.assertEqual(project.resume()['status'], 'assembled')
        self.assertEqual(len(http.posts), 2)
        self.assertNotEqual(http.posts[0]['images_url'], http.posts[1]['images_url'])
        self.assertEqual(http.posts[0]['prompt'], http.posts[1]['prompt'])
        self.assertEqual(project.load()['approvals'], state['approvals'])
        self.assertEqual(len(assemble.call_args.args[1]), 1)

    def test_allowance_exhaustion_is_persistent_across_repeated_runs(self):
        http = Http(['failed', 'failed'])
        project = self.project(http, 10)
        for _ in range(3):
            with self.assertRaisesRegex(pv.ProviderError, 'undetermined|exhausted'):
                self.run_batches(project)
        self.assertEqual(len(http.posts), 2)
        self.assertIsNone(project.ledger.retry_parent(1))

    def test_unknown_submission_stops_all_further_paid_calls(self):
        http = Http(post_errors=[pv.SubmissionUnknown('POST transport disconnected')])
        project = self.project(http)
        with self.assertRaises(pv.SubmissionUnknown): self.run_batches(project)
        with self.assertRaises(pv.PaidRequestBlocked): self.run_batches(project)
        self.assertEqual(len(http.posts), 1)

    def test_429_retries_but_auth_parameter_and_cancel_do_not(self):
        for code in (429, 400, 401):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temp:
                ledger = pv.TaskLedger(Path(temp) / 'ledger.json')
                http = Http(post_errors=[pv.ProviderError('rejected', status_code=code)])
                client = pv.OmniClient('fixture', ledger, http=http)
                args = self.args()
                with self.assertRaises(pv.ProviderError): client.submit(**args)
                self.assertEqual(bool(ledger.retry_parent(1)), code == 429)
        http = Http(['cancelled'])
        project = self.project(http, 10)
        with self.assertRaises(pv.ProviderError): self.run_batches(project)
        self.assertEqual(len(http.posts), 1)

    def test_429_is_retried_by_orchestrator(self):
        http = Http(post_errors=[pv.ProviderError('busy', status_code=429)])
        project = self.project(http, 10)
        self.assertEqual(len(self.run_batches(project)), 1)
        self.assertEqual(len(http.posts), 2)

    def args(self, host='fixture.invalid'):
        url = f'https://{host}/a.png'
        return dict(prompt='Approved movement', image_urls=[url], clip_index=1, aspect_ratio='9:16',
                    reference_bindings=[dict(position=1, role='storyboard_reference', sha256='a'*64, url=url)],
                    max_retries=1)

    def failed_client(self):
        ledger = pv.TaskLedger(self.root / 'ledger.json')
        http = Http(['failed', 'completed'])
        client = pv.OmniClient('fixture', ledger, http=http)
        task = client.submit(**self.args())
        with self.assertRaises(pv.ProviderError): client.poll(task)
        return ledger, http, client

    def test_new_public_url_allowed_but_modified_prompt_assets_or_budget_blocked(self):
        ledger, http, client = self.failed_client()
        for key, value in [('prompt', 'Different plan'), ('max_retries', 2),
                           ('reference_bindings', [dict(position=1, role='storyboard_reference', sha256='b'*64,
                                url='https://fixture.invalid/a.png')])]:
            args = self.args(); args[key] = value
            with self.assertRaises(pv.PaidRequestBlocked): client.submit(**args, retry_of='task-1')
        self.assertEqual(len(http.posts), 1)
        task = client.submit(**self.args('republished.invalid'), retry_of='task-1')
        self.assertEqual(task, 'task-2')
        attempts = [r for r in ledger.records() if r['event'] == 'attempted']
        self.assertEqual(attempts[0]['semantic_hash'], attempts[1]['semantic_hash'])
        self.assertNotEqual(attempts[0]['request_hash'], attempts[1]['request_hash'])

    def test_competing_retry_calls_reserve_one_replacement(self):
        ledger, http, client = self.failed_client()
        def submit():
            try: return client.submit(**self.args(), retry_of='task-1')
            except pv.PaidRequestBlocked: return 'blocked'
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sorted(result), ['blocked', 'task-2'])
        self.assertEqual(len(http.posts), 2)

    def test_publication_failure_before_retry_does_not_loop_or_spend(self):
        http = Http(['failed'])
        project = self.project(http, 10)
        state = project.load()
        calls = 0
        def publish():
            nonlocal calls
            calls += 1
            if calls > 1: raise pv.ProviderError('publication unavailable')
            return self.public(state)
        with self.assertRaisesRegex(pv.ProviderError, 'publication unavailable'):
            self.run_batches(project, ensure_public=publish)
        self.assertEqual(calls, 2)
        self.assertEqual(len(http.posts), 1)
        self.assertEqual(project.ledger.retry_parent(1), 'task-1')

    def test_pending_task_resume_queries_without_publication(self):
        http = Http()
        project = self.project(http, 10)
        state = project.load(); clip = state['clips'][0]
        rows = state['reference_sets']['1']; urls = [self.public(state)[r['path']] for r in rows]
        pv.OmniClient('fixture', project.ledger, http=http).submit(clip['omni_prompt'], urls, clip_index=1,
            aspect_ratio='9:16', max_retries=1,
            reference_bindings=[dict(position=i, role=r['role'], sha256=r['sha256'], url=urls[i-1]) for i,r in enumerate(rows,1)])
        callback = mock.Mock(side_effect=AssertionError('must not publish'))
        self.run_batches(project, ensure_public=callback)
        callback.assert_not_called()
        self.assertEqual(len(http.posts), 1)

    def test_retry_policy_is_in_both_approvals_and_cannot_be_raised(self):
        project = self.project(Http(), 10)
        self.assertIn('总上限 2 次', (project.project_dir/'reference-approval.md').read_text())
        self.assertIn('最多 2 次视频调用', (project.project_dir/'plan.md').read_text())
        state = project.load(); state['plan']['recovery_policy']['max_video_retries_per_clip'] = 2
        project.save(state)
        with self.assertRaises(pv.ValidationError): project.produce()

    def test_legacy_attempt_cannot_be_granted_more_retries_by_new_client(self):
        ledger = pv.TaskLedger(self.root/'ledger.json')
        ledger.append(provider='omni', event='attempted', clip_index=1, request_hash='legacy')
        ledger.append(provider='omni', event='submitted', clip_index=1, request_hash='legacy', task_id='old')
        ledger.append(provider='omni', event='failed', task_id='old', status='failed')
        http = Http()
        with self.assertRaises(pv.PaidRequestBlocked):
            pv.OmniClient('fixture', ledger, http=http).submit(**self.args(), retry_of='old')
        self.assertFalse(http.posts)
