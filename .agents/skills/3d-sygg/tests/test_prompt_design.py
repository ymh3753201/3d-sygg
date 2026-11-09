"""No paid calls: boundary information, audio inheritance and real request limits."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_commercial_ad import sample_plan, analysis_with_source, VisualPromptBuilder
import commercial_ad as ad
import capabilities as caps
import prompt_limits
import providers as pv
import image_api


def limit(**bounds):
    return {**bounds, 'source': 'test-fixture-only-not-a-provider-quote', 'checked_at': '2026-10-17'}


class PromptDesignTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = mock.patch.object(caps, 'read_config', return_value={'model': 'omni', 'provider': 'auto'})
        self.config.start()
        self.images = mock.patch.object(image_api, 'read_config', return_value={'provider': 'builtin'})
        self.images.start()

    def tearDown(self):
        self.images.stop()
        self.config.stop()
        self.temp.cleanup()

    def prepare(self, plan=None, *, model='omni', strategy='auto', name='project'):
        plan = plan or sample_plan()
        if model != 'omni':
            plan['budget'] = {'max_cost_cny': 1000, 'video_call_upper_cny': 10,
                              'quote_checked_at': 'test-fixture-only'}
        o = ad.AdOrchestrator.create(name, self.root)
        s = o.prepare(analysis_with_source(self.root), plan, target_duration=10*len(plan['clips']),
                      model=model, strategy=strategy, validation_run=model != 'omni')
        return o, s

    def test_single_or_three_segments_follow_script_without_extra_assets(self):
        for n in (1, 3):
            _, s = self.prepare(sample_plan(n), name=f'p{n}')
            self.assertEqual(len(s['clips']), n)
            self.assertEqual(s['paid_counts'], {'omni': n, 'minimax': 0})
            self.assertEqual(s['reference_asset_plan']['generation_order'], ['product_master'] + [f'storyboard:{i}' for i in range(1,n+1)])
            self.assertTrue(all(c['execution_strategy'] == 'full_storyboard' for c in s['clips']))
            if n == 1:
                self.assertNotIn('previous segment', s['clips'][0]['video_prompt'])

    def test_explicit_states_reach_both_prompts_all_models(self):
        for model in caps.ROUTES:
            p = sample_plan(); p['clips'][0].update(entry_state='ENTRY_UNIQUE', exit_state='EXIT_UNIQUE')
            _, s = self.prepare(p, model=model, name=model)
            for key in ('storyboard_prompt', 'video_prompt'):
                self.assertIn('ENTRY_UNIQUE', s['clips'][0][key])
                self.assertIn('EXIT_UNIQUE', s['clips'][0][key])

    def test_shared_audio_inherited_exactly_but_each_segment_has_own_words(self):
        p = sample_plan(3)
        p['audio'] = {'shared': {'voice': '低沉温和男声，语速适中', 'music': '克制的电子节奏，96 BPM', 'ambience': '室内轻微空间混响'}}
        for i, c in enumerate(p['clips']):
            c['audio'] = {'speech_text': f'第{i+1}段专属台词'}
        _, s = self.prepare(p)
        for i, c in enumerate(s['clips']):
            for value in p['audio']['shared'].values():
                self.assertIn(value, c['video_prompt'])
            for j in range(3):
                self.assertEqual(f'第{j+1}段专属台词' in c['video_prompt'], i == j)
        self.assertIn('fresh intro', s['clips'][1]['video_prompt'])
        self.assertIn('No musical finale', s['clips'][1]['video_prompt'])
        self.assertNotIn('No musical finale', s['clips'][-1]['video_prompt'])

    def test_conflicting_sound_needs_intent_not_silent_replacement(self):
        for key in ('voice', 'music', 'ambience'):
            p = sample_plan(2)
            p['clips'][0]['audio'] = {key: '方向一'}
            p['clips'][1]['audio'] = {key: '方向二'}
            with self.assertRaisesRegex(pv.ValidationError, 'change_reason'):
                self.prepare(p, name=key)
            p['clips'][1]['audio']['change_reason'] = '从室内切到户外，按脚本有意变化'
            _, s = self.prepare(p, name=key+'-intentional')
            self.assertIn('方向二', s['clips'][1]['video_prompt'] if key != 'voice' else s['clips'][1]['audio']['voice'])
            self.assertIn('Approved sound change', s['clips'][1]['video_prompt'])

    def test_named_speakers_keep_their_own_voice(self):
        p = sample_plan(2)
        p['audio'] = {'speakers': {'customer': '轻快女声', 'host': '温和男声'}}
        for c, who in zip(p['clips'], ('customer', 'host')):
            c['audio'] = {'speaker_id': who, 'speech_text': '批准台词'}
        _, s = self.prepare(p)
        self.assertIn('轻快女声', s['clips'][0]['video_prompt'])
        self.assertIn('温和男声', s['clips'][1]['video_prompt'])

    def test_match_action_inherits_one_boundary_without_new_reference_upload(self):
        p = sample_plan(2)
        p['clips'][0]['exit_state'] = '右手握瓶停在画面左边'
        p['clips'][1]['transition_mode'] = 'match_action'
        _, s = self.prepare(p)
        self.assertEqual(s['clips'][1]['entry_state'], p['clips'][0]['exit_state'])
        self.assertIsNone(s['clips'][1]['continuity_from'])
        self.assertIn('previous segment ends at 右手握瓶', s['clips'][1]['storyboard_prompt'])
        p['clips'][1]['entry_state'] = '左手握瓶'
        with self.assertRaisesRegex(pv.ValidationError, '边界状态'):
            self.prepare(p, name='conflict')

    def test_editorial_cut_can_change_framing_and_only_neighbors_are_context(self):
        p = sample_plan(3)
        for i, c in enumerate(p['clips']):
            c['entry_state'] = f'ENTRY{i}'; c['exit_state'] = f'EXIT{i}'
        _, s = self.prepare(p)
        self.assertIn('ENTRY1', s['clips'][0]['storyboard_prompt'])
        self.assertNotIn('ENTRY2', s['clips'][0]['storyboard_prompt'])
        self.assertIn('planned editorial cut', s['clips'][1]['video_prompt'])

    def test_compaction_preserves_actions_graphics_copy_and_exact_dialogue(self):
        p = sample_plan()
        text = '品牌：“能量，听得见。”'
        p['ad_copy'] = [text]
        p['clips'][0]['audio'] = {'speech_text': 'Hello  world! “你好”。'}
        p['clips'][0]['shots'][1]['graphics'] = [{'kind':'text', 'content':text, 'direction':'字沿弧线绕行并停稳'}]
        _, s = self.prepare(p)
        c = s['clips'][0]
        for key in ('video_prompt', 'storyboard_prompt'):
            for shot in p['clips'][0]['shots']:
                for field in ('visual', 'action', 'product_detail', 'camera', 'transition'):
                    self.assertIn(shot[field], c[key])
            self.assertIn('字沿弧线绕行并停稳', c[key])
        self.assertIn(json.dumps(p['clips'][0]['audio']['speech_text'], ensure_ascii=False), c['video_prompt'])

    def test_length_is_reported_as_unknown_without_fabricated_provider_limit(self):
        o, s = self.prepare()
        report = json.loads((o.project_dir/'prompt-check.json').read_text())
        self.assertTrue(all(x['status'] == 'limit_unknown' for x in report.values()))
        self.assertIn('上限未确认', (o.project_dir/'plan.md').read_text())

    def test_limit_failure_happens_during_prepare_before_approval(self):
        p = sample_plan(); p['prompt_limits'] = {'wxart:omni-flash': limit(max_characters=50)}
        with self.assertRaisesRegex(pv.ValidationError, 'exceeds documented'):
            self.prepare(p)
        self.assertFalse((self.root/'project/project.json').exists())
        self.assertFalse((self.root/'project/requests/task-ledger.json').exists())

    def test_fallback_has_its_own_limit(self):
        p = sample_plan(); p['prompt_limits'] = {'wxart:omni-flash': limit(max_characters=100000),
            'cangyuan:omni-fast-no-water': limit(max_utf8_bytes=30)}
        with self.assertRaisesRegex(pv.ValidationError, 'cangyuan'):
            self.prepare(p)

    def test_utf8_bytes_not_confused_with_characters_and_exact_boundary_allowed(self):
        self.assertEqual(prompt_limits.check('你好', limit(max_characters=2, max_utf8_bytes=6))['status'], 'within_documented_limit')
        with self.assertRaises(pv.ValidationError):
            prompt_limits.check('你好', limit(max_utf8_bytes=5))
        for rule in ({'max_characters': 4}, limit(max_characters=True), limit(max_tokens=100)):
            with self.assertRaises(pv.ValidationError): prompt_limits.validate_rule(rule)

    def test_actual_video_post_blocked_without_ledger_attempt(self):
        ledger = pv.TaskLedger(self.root/'ledger.json')
        ledger.prompt_limits = {'wxart:omni-flash': limit(max_characters=2)}
        http = mock.Mock()
        client = pv.OmniClient('test-only', ledger, http=http)
        with self.assertRaisesRegex(pv.ValidationError, 'exceeds documented'):
            client.submit('long prompt', ['https://images.example/a.png'], clip_index=1, aspect_ratio='9:16')
        self.assertEqual(ledger.records(), [])
        http.request_json.assert_not_called()

    def test_actual_image_limit_precedes_credentials_and_ledger(self):
        o, state = self.prepare()
        config = {'prompt_limit': limit(max_characters=2)}
        loader = mock.Mock()
        with mock.patch.object(image_api, 'project_image_state', return_value=(state, config)), \
             mock.patch.object(image_api, 'asset_spec', return_value=('long prompt', 'product_master')):
            with self.assertRaisesRegex(pv.ValidationError, 'exceeds documented'):
                image_api.generate(o, 'product_master', [], key_loader=loader)
        loader.assert_not_called()
        self.assertFalse((o.project_dir/'requests/image-tasks.json').exists())

    def test_middle_replacement_keeps_parent_neighbors_and_sound(self):
        p = sample_plan(3)
        p['audio'] = {'shared': {'voice': '温和男声', 'music': '96 BPM 木吉他', 'ambience': '安静室内'}}
        p['clips'][0]['exit_state'] = '右手握瓶'
        p['clips'][1].update(transition_mode='match_action', exit_state='瓶盖已打开')
        p['clips'][2]['transition_mode'] = 'match_action'
        parent, original = self.prepare(p)
        original['approvals']['references'] = 'test-only-parent-approval'
        parent.save(original)
        child = ad.AdOrchestrator.create('replacement', self.root)
        revised = sample_plan()
        revised['clips'][0]['exit_state'] = '瓶盖已打开'
        with mock.patch.object(ad.AdOrchestrator, '_validate_generation_approval'):
            state = child.prepare(original['analysis'], revised, target_duration=10,
                parent_project=parent.project_dir, replace_clip=2)
            c = state['clips'][0]
            self.assertEqual(c['entry_state'], '右手握瓶')
            self.assertEqual(c['sequence_context']['segment'], 2)
            self.assertEqual(c['sequence_context']['count'], 3)
            self.assertIn('96 BPM 木吉他', c['video_prompt'])
            self.assertIn('No musical finale', c['video_prompt'])
            revised['clips'][0]['exit_state'] = '瓶盖仍关闭'
            with self.assertRaisesRegex(pv.ValidationError, '下一段动作接续'):
                child.prepare(original['analysis'], revised, target_duration=10,
                    parent_project=parent.project_dir, replace_clip=2)

    def test_per_shot_keeps_own_words_and_inherits_matching_boundary(self):
        p = sample_plan()
        p['audio'] = {'shared': {'voice': '温和男声', 'music': '96 BPM 木吉他', 'ambience': '安静室内'}}
        for i, shot in enumerate(p['clips'][0]['shots']):
            shot['audio'] = {'speech_text': f'词{i}'}
        p['clips'][0]['shots'][1]['transition_mode'] = 'match_action'
        _, s = self.prepare(p, model='sd11-seedance-2.0', strategy='per_shot')
        self.assertEqual(s['clips'][1]['entry_state'], s['clips'][0]['exit_state'])
        for i, c in enumerate(s['clips']):
            self.assertIn('96 BPM 木吉他', c['video_prompt'])
            for j in range(3):
                self.assertEqual(f'词{j}' in c['video_prompt'], i == j)

    def test_frozen_limit_does_not_follow_later_local_configuration(self):
        p = sample_plan()
        p['prompt_limits'] = {'wxart:omni-flash': limit(max_characters=100000)}
        o, s = self.prepare(p)
        with mock.patch.object(caps, 'read_config', return_value={'prompt_limits': {'wxart:omni-flash': limit(max_characters=2)}}):
            report = prompt_limits.inspect_project(o.load())
        self.assertEqual(report['video:1:wxart:omni-flash']['limit']['max_characters'], 100000)

    def test_approved_policy_and_sound_changes_invalidate_approval(self):
        o, s = self.prepare(); o.approve_plan(); s = o.load()
        for key in ('audio', 'prompt_limits'):
            changed = copy.deepcopy(s)
            changed['provider_contract'][key] = {'changed': True}
            with self.assertRaisesRegex(pv.ValidationError, 'Plan changed'):
                o._validate_plan_approval(changed)

    def test_legacy_load_does_not_rebuild_or_normalize_existing_prompts(self):
        o, s = self.prepare()
        s['plan'].pop('prompt_design_version')
        s['plan']['audio'].pop('shared'); s['plan']['audio'].pop('speakers')
        s['provider_contract']['audio'] = copy.deepcopy(s['plan']['audio'])
        s['clips'][0]['video_prompt'] = 'FROZEN historical prompt'
        o.save(s); before = o.state_path.read_bytes()
        self.assertEqual(o.load()['clips'][0]['video_prompt'], 'FROZEN historical prompt')
        self.assertEqual(o.state_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
