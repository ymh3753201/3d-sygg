"""Validate creative intent transport; never claim these tests judge artistic quality."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import commercial_ad as ad
import providers as pv
import test_commercial_ad as fixtures


class VisualDirectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def plan(self):
        plan = fixtures.sample_plan()
        plan['ad_copy'] = ['一眼入场']
        plan['clips'][0]['shots'][1]['graphics'] = [
            {'kind':'text', 'content':'一眼入场', 'direction':'镀铬立体字从后景沿弧线跃入前景，碰撞回弹后让出商品；停留时清楚可读'},
            {'kind':'icon', 'content':'抽象圆环', 'direction':'细线图标在背景跟随已设计运动，不附着商品'},
            {'kind':'effect', 'content':'液态光带', 'direction':'liquid mercury chrome 沿外部布景聚拢，保持商品本体不变'}]
        return plan

    def project(self, plan=None):
        project = ad.AdOrchestrator.create('visual', self.root)
        project.prepare(fixtures.analysis_with_source(self.root), plan or self.plan(), target_duration=10)
        return project

    def test_graphics_survive_timeline_and_both_prompts_and_both_approvals(self):
        project = self.project(); state = project.load()
        graphic = state['plan']['clips'][0]['shots'][1]['graphics']
        self.assertEqual(state['clips'][0]['shots'][1]['graphics'], graphic)
        for field in ('storyboard_prompt','omni_prompt'):
            prompt = state['clips'][0][field]
            for row in graphic:
                self.assertIn(row['content'], prompt); self.assertIn(row['direction'], prompt)
            self.assertEqual(prompt.count(graphic[0]['direction']), 1)
        _, _, rows = fixtures.StateWorkflowTests._reference_rows(self, project, 1)
        project.approve_plan(); project.register_references({'references':rows}); project.approve_references()
        for filename in ('plan.md','reference-approval.md'):
            self.assertIn(graphic[0]['direction'], (project.project_dir/filename).read_text())

    def test_approved_graphic_motion_cannot_change_without_confirmation(self):
        project = self.project(); project.approve_plan()
        state = project.load(); state['clips'][0]['shots'][1]['graphics'][0]['direction'] = '另一种未批准动作'
        with self.assertRaisesRegex(pv.ValidationError, 'Plan changed'):
            project._validate_plan_approval(state)

    def test_text_must_be_in_copy_inventory(self):
        plan = self.plan(); plan['clips'][0]['shots'][1]['graphics'][0]['content'] = '未确认的广告承诺'
        with self.assertRaisesRegex(pv.ValidationError, 'approved ad_copy'):
            self.project(plan)

    def test_invalid_graphics_rejected_before_image_generation(self):
        for graphic in (None, {}, [{'kind':'certificate','content':'认证','direction':'弹出'}],
                        [{'kind':'text','content':'一眼入场','direction':''}]):
            with self.subTest(graphic=graphic):
                plan = self.plan(); plan['clips'][0]['shots'][1]['graphics'] = graphic
                with self.assertRaises(pv.ValidationError): self.project(plan)

    def test_no_fixed_copy_count_and_no_automatic_graphics_added(self):
        plan = fixtures.sample_plan(); plan['ad_copy'] = ['标题一','标题二','标题三','标题四']
        state = self.project(plan).load()
        self.assertEqual(len(state['plan']['ad_copy']), 4)
        self.assertTrue(all('graphics' not in s for s in state['clips'][0]['shots']))
        self.assertIn('not on every shot', state['clips'][0]['omni_prompt'])

    def test_no_text_concept_can_use_icons_and_effects(self):
        plan = self.plan(); plan['ad_copy'] = []
        plan['clips'][0]['shots'][1]['graphics'] = plan['clips'][0]['shots'][1]['graphics'][1:]
        state = self.project(plan).load()
        self.assertIn('抽象圆环', state['clips'][0]['omni_prompt'])
        self.assertNotIn('一眼入场', state['clips'][0]['omni_prompt'])

    def test_graphics_are_copied_not_mutably_shared(self):
        shots = self.plan()['clips'][0]['shots']; frozen = ad.materialize_shot_timeline(shots, 9.2)
        before = copy.deepcopy(frozen[1]['graphics'])
        shots[1]['graphics'][0]['direction'] = 'later mutation'
        self.assertEqual(frozen[1]['graphics'], before)

    def test_unbound_reference_inside_graphics_is_not_smuggled_into_prompt(self):
        plan = self.plan(); plan['clips'][0]['shots'][1]['graphics'][0]['direction'] = 'Use Image9 for lettering'
        with self.assertRaisesRegex(pv.ValidationError, 'unbound'):
            self.project(plan)

    def test_graphic_progression_counts_as_a_distinct_segment_design(self):
        plan = fixtures.sample_plan(2)
        plan['ad_copy'] = ['轮廓', '纹理']
        plan['clips'][1]['shots'] = copy.deepcopy(plan['clips'][0]['shots'])
        for index, clip in enumerate(plan['clips']):
            clip['shots'][1]['graphics'] = [{'kind':'text', 'content':plan['ad_copy'][index],
                'direction':'立体字从产品后方进入并短暂停稳；两段表达不同的视觉信息'}]
        project = ad.AdOrchestrator.create('graphic-progression', self.root)
        state = project.prepare(fixtures.analysis_with_source(self.root), plan, target_duration=20)
        self.assertEqual(state['paid_counts']['omni'], 2)
        self.assertNotEqual(state['clips'][0]['shots'][1]['graphics'], state['clips'][1]['shots'][1]['graphics'])
