"""Audio intent, credential isolation, prompt routing and legacy compatibility."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_commercial_ad import sample_plan, analysis_with_source
import commercial_ad as ad
import audio_routing as audio
import advanced
import capabilities as caps
import providers as pv
import setup_keys


class AudioRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, plan=None):
        o = ad.AdOrchestrator.create('audio', self.root)
        state = o.prepare(analysis_with_source(self.root), plan or sample_plan(), target_duration=10)
        return o, state

    def test_default_without_narration_uses_video_and_no_speech_budget(self):
        p = sample_plan(); del p['narration']
        p['clips'][0]['audio'] = {'speech_text': '能量，听得见。', 'voice': '沉稳中文男声'}
        o, s = self.prepare(p)
        self.assertEqual(s['schema_version'], 10)
        self.assertEqual(s['provider_contract']['audio']['mode'], 'video')
        self.assertFalse(s['plan']['narration']['enabled'])
        self.assertEqual(s['paid_counts']['minimax'], 0)
        self.assertEqual(s['provider_contract']['narration_chunks'], [])
        self.assertEqual(s['provider_contract']['budget']['max_narration_attempts'], 0)
        prompt = s['clips'][0]['video_prompt']
        self.assertIn('能量，听得见。', prompt)
        self.assertIn('Generate synchronized audio', prompt)
        self.assertNotIn('no human voice', prompt)
        self.assertIn('视频模型', (o.project_dir / 'plan.md').read_text())

    def test_regular_narration_does_not_authorize_external_provider(self):
        p = sample_plan(narration=True); p['narration'].pop('user_requested')
        with self.assertRaisesRegex(pv.ValidationError, '用户明确要求'):
            self.prepare(p)

    def test_explicit_external_keeps_narration_and_no_native_voice(self):
        _, s = self.prepare(sample_plan(narration=True))
        self.assertEqual(s['plan']['audio']['mode'], 'external')
        self.assertEqual(s['paid_counts']['minimax'], 1)
        self.assertIn('No dialogue, no human voice', s['clips'][0]['video_prompt'])
        self.assertIn('retain music, ambience and SFX', s['clips'][0]['video_prompt'])

    def test_video_mode_rejects_external_or_disabled_native_speech(self):
        for p in (sample_plan(narration=True), sample_plan()):
            p['audio'] = {'mode': 'video', 'speech': False}
            p['clips'][0]['audio'] = {'speech_text': '测试台词'}
            with self.assertRaises(pv.ValidationError):
                audio.normalize_plan(p)

    def test_native_preflight_and_generation_never_read_minimax(self):
        o, s = self.prepare()
        s['reference_sets'] = {}
        def key(service, account):
            if service == pv.MINIMAX_KEYCHAIN_SERVICE:
                self.fail('Default route accessed MiniMax credential')
            return 'fixture-video-key'
        o.key_loader = key
        o.minimax_factory = mock.Mock(side_effect=AssertionError('Unexpected TTS call'))
        with mock.patch.object(ad, 'require_media_tools'), mock.patch.object(ad.shutil, 'which', return_value='/fixture/tool'):
            o._preflight_core(s)
        self.assertIsNone(o._generate_narration(s, {}))
        o.minimax_factory.assert_not_called()

    def test_native_routed_model_allows_speech_without_unsupported_audio_flag(self):
        _, s = self.prepare()
        clip = s['clips'][0]
        clip['audio'] = {'speech_text': '批准的台词'}
        builder = ad.VisualPromptBuilder(s['analysis'], s['plan'])
        for name in ('sd11-seedance-2.5', 'mm2-minimax-h3'):
            cap = caps.route(name); cap['execution_resolution'] = cap['resolutions'][0]
            prompt = advanced.video_prompt(builder, clip, cap)
            self.assertIn('批准的台词', prompt)
            self.assertNotIn('no speech', prompt)
            client = advanced.RoutedVideoClient('fixture', pv.TaskLedger(self.root / 'fixture-ledger.json'), contract={'capabilities': cap, 'provider_model': cap['model'], 'resolution': cap['execution_resolution']}, clip=clip, roles=['storyboard_reference'])
            payload = client.build_payload(prompt, ['https://assets.example/board.png'], aspect_ratio='9:16')
            self.assertEqual('generate_audio' in payload, name == 'sd11-seedance-2.5')

    def test_native_talent_prompt_allows_approved_speaking(self):
        _, s = self.prepare(sample_plan(human=True))
        self.assertNotIn('no speaking gestures', s['clips'][0]['video_prompt'])
        self.assertNotIn('no lip-sync', s['global_anchor'])

    def test_native_missing_or_short_source_audio_stops_before_editing(self):
        o, s = self.prepare()
        raw = self.root / 'fixture.mp4'; raw.write_bytes(b'fixture')
        o.ledger.append(provider='omni', event='submitted', task_id='fixture', clip_index=1)
        o.ledger.append(provider='omni', event='downloaded', task_id='fixture', path=str(raw), sha256=pv.sha256_file(raw))
        for info in ({'has_audio': False, 'audio_duration': 0}, {'has_audio': True, 'audio_duration': 1}):
            with mock.patch.object(ad, 'probe_media', return_value=info), mock.patch.object(ad, 'mix_narration') as mix:
                with self.assertRaisesRegex(pv.ValidationError, '不自动调用独立语音'):
                    o._assemble(s, [raw], None)
                mix.assert_not_called()

    def test_audio_contract_tampering_cannot_be_loaded(self):
        o, s = self.prepare(); s['provider_contract']['audio']['mode'] = 'external'; o.save(s)
        with self.assertRaises(pv.ValidationError):
            o.load()

    def test_legacy_schema9_is_loaded_without_changing_prompt_or_approval(self):
        o, s = self.prepare(sample_plan(narration=True))
        s['schema_version'] = 9; s['plan'].pop('audio'); s['provider_contract'].pop('audio')
        s['clips'][0]['video_prompt'] = audio.LEGACY_CONSTRAINT
        o.save(s); before = o.state_path.read_bytes()
        self.assertEqual(o.load()['clips'][0]['video_prompt'], audio.LEGACY_CONSTRAINT)
        self.assertEqual(o.state_path.read_bytes(), before)

    def test_per_shot_speech_is_not_repeated_in_every_expanded_segment(self):
        p = sample_plan(); p['clips'][0]['audio'] = {'speech_text': '整段文字'}
        audio.normalize_plan(p)
        with self.assertRaisesRegex(pv.ValidationError, '重复整段台词'):
            caps.prepare_timeline(p, 10, caps.route('omni'), 'per_shot')
        for i, shot in enumerate(p['clips'][0]['shots']):
            shot['audio'] = {'speech_text': f'第{i}句'}
        result, _ = caps.prepare_timeline(p, 10, caps.route('omni'), 'per_shot')
        self.assertEqual([c['audio']['speech_text'] for c in result['clips']], ['第0句', '第1句', '第2句'])

    def test_default_key_setup_does_not_request_or_validate_tts_key(self):
        with mock.patch.object(setup_keys.Keychain, 'bootstrap', return_value={}) as bootstrap:
            setup_keys.main([])
        self.assertNotIn(pv.MINIMAX_KEYCHAIN_SERVICE, bootstrap.call_args.kwargs['services'])
        with mock.patch.object(setup_keys.getpass, 'getpass', return_value='fixture') as getpass, mock.patch.object(setup_keys, 'store_secret') as store:
            setup_keys.main(['--interactive'])
        self.assertEqual(getpass.call_count, 1)
        self.assertEqual(store.call_args.args[0], pv.OMNI_KEYCHAIN_SERVICE)

    def test_native_money_budget_does_not_require_tts_quote(self):
        p = {'budget': {'max_cost_cny': 10, 'video_call_upper_cny': 2, 'quote_checked_at': '2026-10-05'}}
        self.assertEqual(caps.budget_contract(p, 1, 0)['narration_call_upper_cny'], 0)


if __name__ == '__main__':
    unittest.main()
