"""Real localhost transport and project provenance checks; no paid/external calls."""
import base64
import copy
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import image_api as images
from commercial_ad import AdOrchestrator
from providers import ValidationError, SubmissionUnknown, PaidRequestBlocked, ProviderError, sha256_file
from test_commercial_ad import analysis_with_source, sample_plan, png_bytes


class ImageAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.posts = []; self.gets = []; self.mode = 'sync'; self.status = 'running'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.posts.append((self.path, dict(self.headers), body))
                if owner.mode == 'disconnect':
                    self.connection.close(); return
                if owner.mode == 'reject':
                    self.send_response(401); self.end_headers(); self.wfile.write(b'private-echo-key'); return
                data = {'id': 'job/1'} if owner.mode == 'async' else owner.result()
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(data).encode())
            def do_GET(self):
                owner.gets.append((self.path, dict(self.headers)))
                if self.path == '/image.png':
                    self.send_response(200); self.end_headers(); self.wfile.write(png_bytes(33)); return
                if self.path == '/redirect':
                    self.send_response(302); self.send_header('Location', '/image.png'); self.end_headers(); return
                data = {'status': owner.status, **(owner.result() if owner.status == 'done' else {})}
                self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(data).encode())
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.config = images.validate_config({**copy.deepcopy(images.PRESETS['seedream']),
            'provider': 'test-images', 'model': 'documented-model-id', 'base_url': self.url + '/v1',
            'documentation': 'https://example.com/image-api', 'size': '2048x2048'})
        self.patch = mock.patch.object(images, 'read_config', return_value=self.config); self.patch.start()
        self.o = AdOrchestrator.create('images-test', self.root)
        self.analysis = analysis_with_source(self.root)
        self.o.prepare(self.analysis, sample_plan(), target_duration=10)
        self.o.approve_plan()
        self.inputs = [r['path'] for r in self.o.load()['source_assets']]

    def tearDown(self):
        self.patch.stop(); self.server.shutdown(); self.server.server_close(); self.thread.join(); self.temp.cleanup()

    def result(self):
        return {'data': [{'url': self.url + '/image.png'}]} if self.mode == 'url' else {
            'data': [{'b64_json': base64.b64encode(png_bytes(33)).decode()}]}

    def generate(self, asset='product_master', inputs=None, **kw):
        return images.generate(self.o, asset, self.inputs if inputs is None else inputs,
            key_loader=lambda c: 'test-secret-value', **kw)

    def master_row(self, entry):
        return {'role': 'product_master', 'path': entry['path'], 'origin': 'external_image_api',
            'identity_verified': True, 'generation_receipt': entry['receipt'],
            'derived_from_source_hashes': [sha256_file(Path(p)) for p in self.inputs]}

    def test_json_input_and_b64_output_are_real_files(self):
        entry = self.generate()
        body = json.loads(self.posts[0][2])
        self.assertEqual(body['model'], self.config['model'])
        self.assertEqual(base64.b64decode(body['image'][0].split(',')[1]), Path(self.inputs[0]).read_bytes())
        self.assertEqual(body['size'], '2048x2048')
        self.assertEqual(Path(entry['path']).read_bytes(), png_bytes(33))
        self.assertEqual(images.verify_receipt(self.o.project_dir, self.master_row(entry)), entry['receipt'])
        self.assertNotIn('test-secret-value', (self.o.project_dir / 'requests/image-tasks.json').read_text())
        self.assertNotIn('response', images.public_result(entry))

    def test_completed_request_is_reused_without_another_post(self):
        first = self.generate(); second = self.generate()
        self.assertEqual(first['sha256'], second['sha256']); self.assertEqual(len(self.posts), 1)

    def test_url_download_never_receives_the_api_key(self):
        self.mode = 'url'; self.generate()
        self.assertNotIn('Authorization', self.gets[0][1])
        self.assertEqual(self.posts[0][1]['Authorization'], 'Bearer test-secret-value')

    def test_multipart_sends_each_reference_and_uses_edits(self):
        config = {**self.config, **images.PRESETS['openai']}
        client = images.ImageClient(config, 'test-secret-value')
        source2 = self.root / 'second.png'; source2.write_bytes(png_bytes(41))
        url, body, kind = client.build_request('identity', [Path(self.inputs[0]), source2])
        self.assertTrue(url.endswith('/v1/images/edits'))
        self.assertTrue(kind.startswith('multipart/form-data; boundary='))
        self.assertEqual(body.count(b'name="image[]"'), 2)
        self.assertIn(source2.read_bytes(), body)
        response = json.loads(client.request(url, data=body, content_type=kind))
        self.assertEqual(client.image_bytes(response), png_bytes(33))

    def test_custom_nested_json_fields_and_response(self):
        config = {**self.config, 'model_field': 'request.model', 'prompt_field': 'request.prompt',
            'image_field': 'request.images', 'response': {'b64_path': 'output.0.image'}}
        client = images.ImageClient(config, 'key')
        _, body, _ = client.build_request('new prompt', [Path(self.inputs[0])])
        self.assertEqual(json.loads(body)['request']['model'], self.config['model'])
        data = {'output': [{'image': base64.b64encode(png_bytes(4)).decode()}]}
        self.assertEqual(client.image_bytes(data), png_bytes(4))

    def test_async_resume_queries_same_task_only(self):
        self.mode = 'async'
        self.config['async'] = {'id_path': 'id', 'poll_path': '/tasks/{task_id}',
            'status_path': 'status', 'success': ['done'], 'failure': ['failed']}
        self.o = AdOrchestrator.create('async-images', self.root)
        self.o.prepare(self.analysis, sample_plan(), target_duration=10); self.o.approve_plan()
        self.inputs = [r['path'] for r in self.o.load()['source_assets']]
        self.assertEqual(self.generate()['status'], 'pending')
        self.status = 'done'
        self.assertEqual(self.generate(resume=True)['status'], 'completed')
        self.assertEqual(len(self.posts), 1)
        self.assertIn('job%2F1', self.gets[-1][0])

    def test_uncertain_submission_blocks_all_retry_posts(self):
        self.mode = 'disconnect'
        with self.assertRaises(SubmissionUnknown): self.generate()
        with self.assertRaises(SubmissionUnknown): self.generate()
        with self.assertRaises(SubmissionUnknown): self.generate(retry_reason='try again')
        self.assertEqual(len(self.posts), 1)

    def test_rejected_key_is_not_echoed_or_automatically_retried(self):
        self.mode = 'reject'
        with self.assertRaises(ProviderError) as e: self.generate()
        self.assertNotIn('private-echo-key', str(e.exception))
        with self.assertRaises(PaidRequestBlocked): self.generate()
        self.assertEqual(len(self.posts), 1)

    def test_one_visual_correction_then_stop(self):
        self.generate(); second = self.generate(retry_reason='visible wrong label')
        self.assertEqual(second['attempt'], 2)
        with self.assertRaises(PaidRequestBlocked): self.generate(retry_reason='third candidate')
        self.assertEqual(len(self.posts), 2)

    def test_resume_never_creates_a_request(self):
        with self.assertRaises(PaidRequestBlocked): self.generate(resume=True)
        self.assertEqual(self.posts, [])

    def test_unapproved_plan_and_wrong_input_block_before_post(self):
        other = AdOrchestrator.create('not-approved', self.root)
        other.prepare(self.analysis, sample_plan(), target_duration=10)
        with self.assertRaises(ValidationError): images.generate(other, 'product_master', self.inputs, key_loader=lambda c: 'key')
        with self.assertRaises(ValidationError): self.generate(inputs=[])
        self.assertEqual(self.posts, [])

    def test_configuration_is_frozen_in_the_plan(self):
        frozen = self.o.load()['reference_asset_plan']['image_config']
        self.config['model'] = 'different-model'
        self.generate()
        self.assertEqual(json.loads(self.posts[0][2])['model'], frozen['model'])
        state = self.o.load(); state['reference_asset_plan']['image_config']['model'] = 'tampered'
        self.o.save(state)
        with self.assertRaises(ValidationError): self.generate()

    def test_receipt_rejects_changed_file_and_false_input_relationship(self):
        entry = self.generate(); row = self.master_row(entry)
        row['derived_from_source_hashes'] = ['fake-input']
        with self.assertRaises(ValidationError): images.verify_receipt(self.o.project_dir, row)
        row = self.master_row(entry); Path(row['path']).write_bytes(png_bytes(45))
        with self.assertRaises(ValidationError): images.verify_receipt(self.o.project_dir, row)

    def test_full_external_reference_pack_passes_existing_workflow(self):
        master = self.generate()
        with mock.patch.object(images.ImageClient, 'image_bytes', return_value=png_bytes(39)):
            board = self.generate('storyboard:1', [master['path']])
        row = {'role': 'storyboard', 'clip_index': 1, 'path': board['path'],
            'origin': 'external_image_api', 'generation_receipt': board['receipt'],
            'derived_from_product_master_sha256': master['sha256'], 'panel_count': 3,
            'clean_for_video': True, 'panel_order_verified': True, 'distinct_panels_verified': True}
        result = self.o.register_references({'references': [self.master_row(master), row]})
        self.assertEqual(len(result['references']), 2)
        self.o.approve_references()

    def test_reference_limits_and_secret_configs_are_rejected(self):
        client = images.ImageClient({**self.config, 'max_reference_images': 1}, 'key')
        with self.assertRaises(ValidationError): client.build_request('x', [Path(self.inputs[0])] * 2)
        for bad in ({'api_key': 'secret'}, {'extra_body': {'token': 'secret'}},
                    {'generate_path': '//other-host/api'}, {'base_url': 'https://user:secret@example.com'},
                    {'extra_body': {'n': 3}}, {'extra_body': {'stream': True}}):
            with self.assertRaises(ValidationError): images.validate_config({**self.config, **bad})

    def test_redirect_does_not_forward_credentials(self):
        client = images.ImageClient(self.config, 'key')
        with self.assertRaises(ProviderError): client.request(self.url + '/redirect')
        self.assertEqual(len(self.gets), 1)

    def test_download_failure_resumes_without_repeating_post(self):
        with mock.patch.object(images.ImageClient, 'image_bytes', side_effect=ProviderError('download interrupted')):
            with self.assertRaises(ProviderError): self.generate()
        self.assertEqual(self.generate(resume=True)['status'], 'completed')
        self.assertEqual(len(self.posts), 1)

    def test_human_scene_uses_both_real_global_images(self):
        self.o = AdOrchestrator.create('human-images', self.root)
        self.o.prepare(self.analysis, sample_plan(human=True), target_duration=10); self.o.approve_plan()
        self.inputs = [r['path'] for r in self.o.load()['source_assets']]
        master = self.generate()
        with mock.patch.object(images.ImageClient, 'image_bytes', return_value=png_bytes(52)):
            talent = self.generate('talent', [])
        with self.assertRaises(ValidationError): self.generate('storyboard:1', [master['path']])
        with mock.patch.object(images.ImageClient, 'image_bytes', return_value=png_bytes(53)):
            board = self.generate('storyboard:1', [master['path'], talent['path']])
        self.assertEqual(len(json.loads(self.posts[-1][2])['image']), 2)
        self.assertEqual(board['status'], 'completed')

    def test_independent_keyframes_register_with_their_own_receipts(self):
        self.o = AdOrchestrator.create('keyframe-images', self.root)
        plan = sample_plan()
        plan['budget'] = {'max_cost_cny': 1000, 'video_call_upper_cny': 30,
                          'narration_call_upper_cny': 5, 'quote_checked_at': 'offline-test-only'}
        self.o.prepare(self.analysis, plan, target_duration=10,
            model='sd11-seedance-2.0', strategy='ordered_keyframes', validation_run=True)
        self.o.approve_plan(); self.inputs = [r['path'] for r in self.o.load()['source_assets']]
        master = self.generate(); rows = [self.master_row(master)]
        for i, shot in enumerate(self.o.load()['clips'][0]['shots']):
            with mock.patch.object(images.ImageClient, 'image_bytes', return_value=png_bytes(60+i)):
                frame = self.generate('keyframe:1:' + shot['shot_id'], [master['path']])
            rows.append({'role': 'keyframe', 'shot_id': shot['shot_id'], 'clip_index': 1,
                'path': frame['path'], 'origin': 'external_image_api', 'generation_receipt': frame['receipt'],
                'identity_verified': True, 'clean_for_video': True, 'directly_generated': True,
                'derived_from_product_master_sha256': master['sha256']})
        self.o.register_references({'references': rows}); self.o.approve_references()

    def test_failure_query_never_silently_resubmits(self):
        self.mode = 'async'; self.status = 'failed'
        self.config['async'] = {'id_path': 'id', 'poll_path': '/tasks/{task_id}',
            'status_path': 'status', 'success': ['done'], 'failure': ['failed']}
        self.o = AdOrchestrator.create('failed-images', self.root)
        self.o.prepare(self.analysis, sample_plan(), target_duration=10); self.o.approve_plan()
        self.inputs = [r['path'] for r in self.o.load()['source_assets']]
        with self.assertRaises(ProviderError): self.generate()
        with self.assertRaises(PaidRequestBlocked): self.generate(resume=True)
        self.assertEqual(len(self.posts), 1)

    def test_completed_file_reuse_does_not_require_a_key(self):
        self.generate()
        with mock.patch.object(images, 'load_key', side_effect=AssertionError('No Key should be loaded')):
            result = images.generate(self.o, 'product_master', self.inputs, key_loader=images.load_key)
        self.assertEqual(result['status'], 'completed')

    def test_external_plan_rejects_forged_builtin_origin(self):
        master = self.generate(); row = self.master_row(master); row['origin'] = 'codex_imagegen'
        with self.assertRaises(ValidationError): self.o.register_references({'references': [row]})


if __name__ == '__main__': unittest.main()
