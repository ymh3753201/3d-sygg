"""Business contracts only; synthetic fixtures do not verify AI image understanding."""
import contextlib
import io
import math
import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import commercial_ad as ad
import providers as pv
import media
import test_commercial_ad as fixtures


class BusinessFlexibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_duration_has_no_presets_and_keeps_minimum_paid_count(self):
        for duration in [*range(1, 122), 7.5, 10.1, 23.5, 37.25, 65.5]:
            with self.subTest(duration=duration):
                keeps = ad.balanced_keep_durations(duration)
                self.assertEqual(len(keeps), math.ceil(duration / 10))
                self.assertAlmostEqual(sum(keeps), duration, places=6)
                self.assertTrue(all(0 < d <= 10 for d in keeps))

    def test_missing_or_invalid_duration_never_silently_becomes_ten_seconds(self):
        for value in (None, True, 0, -1, float('nan'), float('inf'), '20'):
            with self.subTest(value=value), self.assertRaises(pv.ValidationError):
                ad.balanced_keep_durations(value)
        with self.assertRaisesRegex(pv.ValidationError, 'explicitly planned'):
            ad.AdOrchestrator.create('missing', self.root).prepare(fixtures.analysis_with_source(self.root), fixtures.sample_plan())
        parsed = ad._parser().parse_args(['prepare', '--project-id', 'missing', '--analysis', 'a.json', '--plan', 'p.json'])
        self.assertIsNone(parsed.target_duration)  # director plan must supply a reasoned duration

    def test_arbitrary_requested_seconds_reach_approved_plan_and_request_count(self):
        for duration in (7.5, 23.5, 37, 65):
            count = math.ceil(duration / 10)
            project = ad.AdOrchestrator.create(f'duration-{duration}', self.root)
            state = project.prepare(fixtures.analysis_with_source(self.root), fixtures.sample_plan(count), target_duration=duration)
            self.assertEqual(state['target']['duration'], duration)
            self.assertEqual(state['paid_counts']['omni'], count)
            self.assertAlmostEqual(state['clips'][-1]['global_end'], duration)
            self.assertTrue(all(c['upstream_duration'] == 10 for c in state['clips']))
            project.approve_plan()
            self.assertEqual(project.load()['target']['duration'], duration)

    def test_mixed_product_and_talent_segments_bind_only_actual_cast(self):
        plan = fixtures.sample_plan(2, human=True)
        plan['clips'][0]['talent_action'] = ''
        plan['talent_strategy']['wardrobe'] = '自然米色衬衫'
        project = ad.AdOrchestrator.create('mixed', self.root)
        state = project.prepare(fixtures.analysis_with_source(self.root), plan, target_duration=17.5)
        first, second = state['clips']
        self.assertNotIn('talent', first['execution_reference_roles'])
        self.assertIn('talent', second['execution_reference_roles'])
        self.assertIn('no person', first['omni_prompt'])
        self.assertNotIn('自然米色衬衫', first['storyboard_prompt'])
        self.assertIn('自然米色衬衫', second['storyboard_prompt'])
        self.assertNotIn('in every panel', second['storyboard_prompt'])
        _, _, rows = fixtures.StateWorkflowTests._reference_rows(self, project, 2)
        talent = project.project_dir / 'references' / 'talent.png'
        talent.write_bytes(fixtures.png_bytes(765))
        rows.append({'role': 'talent', 'path': str(talent), 'origin': 'codex_imagegen'})
        rows[2]['derived_from_talent_sha256'] = pv.sha256_file(talent)
        project.approve_plan()
        project.register_references({'references': rows})
        project.approve_references()
        bindings = project.load()['reference_sets']
        self.assertEqual([r['role'] for r in bindings['1']], ['storyboard_reference', 'product_master'])
        self.assertEqual([r['role'] for r in bindings['2']], ['storyboard_reference', 'product_master', 'talent'])
        first['execution_reference_roles'].append('talent')
        with self.assertRaisesRegex(pv.ValidationError, 'product-only'):
            ad.execution_roles(plan, first)

    def test_global_human_strategy_without_any_appearance_is_rejected(self):
        plan = fixtures.sample_plan(human=True)
        plan['clips'][0]['talent_action'] = ''
        with self.assertRaisesRegex(pv.ValidationError, 'actual planned appearance'):
            ad.AdOrchestrator.create('unused-talent', self.root).prepare(fixtures.analysis_with_source(self.root), plan, target_duration=8)

    def test_product_categories_are_freeform_not_a_historical_template(self):
        for product in ('工业密封圈', '针织围巾', '护肤瓶', '果汁纸盒', '手工陶瓷器皿'):
            analysis = fixtures.analysis_with_source(self.root)
            analysis.update(product_type=product, visible_features=['图中可见的完整轮廓与表面纹理'], materials=[], uncertainties=['未展示的结构未知'])
            plan = fixtures.sample_plan()
            prompt = ad.VisualPromptBuilder(analysis, plan).product_master_prompt()
            self.assertIn(product, prompt)
            self.assertNotIn('音箱', prompt)
            self.assertNotIn('three-quarter hero', prompt)
            self.assertNotIn('full-frame 9:16', prompt)
            self.assertIsNone(ad.VisualPromptBuilder(analysis, plan).talent_reference_prompt())

    def test_fractional_duration_survives_real_ffmpeg_trimming_and_stitching(self):
        ffmpeg, _ = media.require_media_tools()
        source = self.root / 'source.mp4'
        media._run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '10',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source)])
        clips = [media.normalize_clip(source, self.root / f'clip-{i}.mp4', duration,
                                      target_width=160, target_height=90)
                 for i, duration in enumerate(ad.balanced_keep_durations(15.5))]
        final = media.stitch_clips(clips, self.root / 'final.mp4')
        info = media.probe_media(final)
        self.assertAlmostEqual(info['video_duration'], 15.5, delta=0.08)
        self.assertGreaterEqual(info['audio_duration'], 15.4)
        media.full_decode_check(final)
