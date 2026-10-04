"""Development-only regressions: preserve design, bounded recovery and low-friction editing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_commercial_ad as fixtures
import test_multimodel as mm
from test_multimodel import RelayHttp, benchmark_plan
import commercial_ad as ad
import advanced
import capabilities as caps
import media
import providers as pv


class PracticalRepairTests(unittest.TestCase):
    setUp = mm.MultiModelTests.setUp
    tearDown = mm.MultiModelTests.tearDown
    prepare = mm.MultiModelTests.prepare
    references = mm.MultiModelTests.references
    run_case = mm.RoutedRecoveryTests.run_case

    def test_per_shot_keeps_whole_scene_states_only_at_boundaries(self):
        plan = benchmark_plan()
        scene = plan['clips'][0]
        scene.update(entry_state='BOX_CLOSED', exit_state='PRODUCT_REVEALED')
        scene['shots'][1].update(entry_state='LID_OPEN', exit_state='PRODUCT_RISING')
        _, state = self.prepare(plan=plan, strategy='per_shot', t=9)
        clips = state['clips']
        self.assertEqual(clips[0]['entry_state'], 'BOX_CLOSED')
        self.assertEqual(clips[0]['exit_state'], scene['shots'][0]['action'])
        self.assertEqual((clips[1]['entry_state'], clips[1]['exit_state']), ('LID_OPEN', 'PRODUCT_RISING'))
        self.assertEqual(clips[2]['entry_state'], scene['shots'][2]['visual'])
        self.assertEqual(clips[2]['exit_state'], 'PRODUCT_REVEALED')
        self.assertNotIn('BOX_CLOSED', clips[1]['video_prompt'])

    def test_keyframes_and_actions_share_request_time_after_trim(self):
        plan = benchmark_plan(); plan['clips'][0]['trim_start'] = 2
        _, state = self.prepare(plan=plan, strategy='ordered_keyframes')
        clip = state['clips'][0]
        for i, shot in enumerate(clip['shots'], 1):
            timing = f"[{shot['start']+2:.2f}-{shot['end']+2:.2f}s]"
            self.assertIn(f"Image {i}: independent keyframe for {shot['shot_id']} at {timing}", clip['video_prompt'])
            self.assertIn(f"[Shot {shot['shot_id']}] {timing}", clip['video_prompt'])

    def test_transition_events_reach_actual_prompts_in_each_strategy(self):
        for strategy in caps.STRATEGIES:
            with self.subTest(strategy=strategy):
                plan = benchmark_plan()
                plan['clips'][0].update(transition_in='UNIQUE_ENTER_EVENT', transition_out='UNIQUE_EXIT_EVENT')
                _, state = self.prepare(plan=plan, strategy=strategy, name=strategy)
                self.assertIn('UNIQUE_ENTER_EVENT', state['clips'][0]['video_prompt'])
                self.assertIn('UNIQUE_EXIT_EVENT', state['clips'][-1]['video_prompt'])
                if strategy in ('ordered_keyframes', 'first_last'):
                    frames = state['clips'][0]['frame_prompts']
                    self.assertIn('UNIQUE_ENTER_EVENT', json.dumps(frames))
                    self.assertIn('UNIQUE_EXIT_EVENT', json.dumps(frames))

    def qualification(self, panels):
        plan = benchmark_plan(); plan['clips'][0]['shots'] = plan['clips'][0]['shots'][:panels]
        o, state = self.prepare(plan=plan, name=f'qualification-{panels}')
        self.references(o, state)
        # Synthetic delivered status isolates promotion logic, never a real sample.
        state = o.load(); state['state'] = 'delivered'
        final = o.project_dir/'final/synthetic.mp4'; final.write_bytes(b'synthetic qualification fixture')
        state['artifacts']['final_video'] = str(final); o.save(state)
        pv.atomic_write_json(o.project_dir/'qa/qa-report.json', {
            'technical_status':'pass', 'creative_review_status':'pass', 'final_sha256':pv.sha256_file(final)})
        return o

    def test_single_sample_reuses_per_shot_without_promoting_multigrid(self):
        o = self.qualification(1)
        plan = benchmark_plan(); plan['qualification_projects'] = [str(o.project_dir)]
        result, _ = caps.prepare_timeline(plan, 9, caps.route('sd11-seedance-2.0'))
        self.assertEqual([c['execution_strategy'] for c in result['clips']], ['per_shot']*3)
        self.assertNotIn('full_storyboard', result['qualification_snapshot'])
        self.assertEqual(caps.budget_contract(result, len(result['clips']), 0)['max_video_attempts'], 6)
        self.assertFalse(result['validation_run'])
        production = ad.AdOrchestrator.create('production-after-single', self.root)
        state = production.prepare(fixtures.analysis_with_source(self.root), plan,
            target_duration=9, model='sd11-seedance-2.0')
        self.assertEqual(len(state['clips']), 3)
        self.assertEqual(state['provider_contract']['budget']['max_video_attempts'], 6)
        self.assertEqual(state['reference_asset_plan']['minimum_imagegen_calls'], 4)
        with self.assertRaisesRegex(pv.ValidationError, 'Untested'):
            caps.prepare_timeline(plan, 9, caps.route('sd11-seedance-2.0'), 'full_storyboard')
        plan['clips'][0]['shots'] = plan['clips'][0]['shots'][:1]
        single, _ = caps.prepare_timeline(plan, 9, caps.route('sd11-seedance-2.0'), 'full_storyboard')
        self.assertEqual(single['clips'][0]['execution_strategy'], 'full_storyboard')

    def test_grid_evidence_reused_across_legal_duration_layout_and_output(self):
        o = self.qualification(3)
        plan = benchmark_plan(); plan['qualification_projects'] = [str(o.project_dir)]
        plan['clips'][0]['shots'].append(dict(plan['clips'][0]['shots'][-1]))
        route = caps.route('sd11-seedance-2.0')
        route.update(execution_resolution='1080p', aspect_ratio='16:9')
        result, _ = caps.prepare_timeline(plan, 15, route)
        self.assertEqual(result['clips'][0]['execution_strategy'], 'full_storyboard')
        self.assertEqual(len(result['clips'][0]['shots']), 4)
        self.assertFalse(result['validation_run'])
        # Still cannot grant a different provider/model the same competence.
        with self.assertRaises(pv.ValidationError):
            caps.prepare_timeline(plan, 15, caps.route('sd11-seedance-2.5'))

    def test_one_resume_handles_newly_confirmed_failure_and_stays_within_budget(self):
        http = RelayHttp('sd11-seedance-2.0', outcomes=['failed', 'completed'])
        o, factory = self.run_case(http, n=1)
        polls = 0
        def interrupted_factory(*args, **kwargs):
            client = factory(*args, **kwargs); real_poll = client.poll
            def poll(task):
                nonlocal polls
                polls += 1
                if polls == 1:
                    raise pv.ProviderError('temporary poll interruption')
                return real_poll(task)
            client.poll = poll
            return client
        with mock.patch.object(advanced, 'RoutedVideoClient', side_effect=interrupted_factory):
            with self.assertRaises(pv.ProviderError): o.produce()
            self.assertEqual(len(http.posts), 1)
            o.resume()
            self.assertEqual(len(http.posts), 2)
            o.resume()
            self.assertEqual(len(http.posts), 2)

    def test_recovery_no_progress_still_stops_without_extra_submit(self):
        http = RelayHttp('sd11-seedance-2.0', outcomes=['failed'])
        o, factory = self.run_case(http, n=1)
        state = o.load()
        client = factory('fake', o.ledger, contract=state['provider_contract'], clip=state['clips'][0], roles=['storyboard_reference'])
        task = client.submit('motion', ['https://assets.example/a.png'], clip_index=1, aspect_ratio='9:16', max_retries=1,
            reference_bindings=[{'position':1, 'role':'storyboard_reference', 'sha256':'fixture', 'url':'https://assets.example/a.png'}])
        with self.assertRaises(pv.ProviderError): client.poll(task)
        def unavailable_factory(*args, **kwargs):
            client = factory(*args, **kwargs)
            client.check_available = mock.Mock(side_effect=pv.ProviderError('temporarily unavailable'))
            return client
        with mock.patch.object(advanced, 'RoutedVideoClient', side_effect=unavailable_factory):
            with self.assertRaisesRegex(pv.ProviderError, 'unavailable'): o.produce()
        self.assertEqual(len(http.posts), 1)


class PracticalMediaTests(unittest.TestCase):
    def test_boundary_frame_repaired_locally_and_real_missing_frames_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); ffmpeg, _ = media.require_media_tools()
            source = root/'source.mp4'
            media._run([ffmpeg, '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=128x72:r=30',
                        '-f', 'lavfi', '-i', 'sine=frequency=300:sample_rate=48000',
                        '-vf', 'trim=end_frame=149', '-t', '5', '-c:v', 'libx264', '-c:a', 'aac', str(source)])
            # Legacy approvals retain their old editing contract and QA tolerance.
            old = media.normalize_clip(source, root/'old.mp4', 5, target_width=128, target_height=72)
            bad = media.stitch_clips([old]*6, root/'short.mp4')
            args = dict(expected_duration=30, narration_expected=False, expected_width=128, expected_height=72)
            self.assertEqual(media.write_qa_report(bad, root/'old.json', **args)['technical_status'], 'pass')
            self.assertEqual(media.write_qa_report(bad, root/'bad.json', expected_frames=900, **args)['technical_status'], 'failed')
            # Normal new production repairs this upstream in the existing encode.
            fixed = media.normalize_clip(source, root/'fixed.mp4', 5, target_width=128, target_height=72, align_frames=True)
            self.assertEqual(media.probe_media(fixed)['video_frames'], 150)
            final = media.stitch_clips([fixed]*6, root/'final.mp4')
            report = media.write_qa_report(final, root/'qa.json', expected_frames=900, **args)
            self.assertEqual(report['technical']['video_frames'], 900)
            self.assertEqual(report['technical_status'], 'pass')
            with mock.patch.object(media, '_run') as run:
                media.normalize_clip(source, fixed, 5, target_width=128, target_height=72, align_frames=True)
            run.assert_not_called()
            with self.assertRaisesRegex(pv.ValidationError, 'shorter'):
                media.normalize_clip(source, root/'missing.mp4', 5.2, target_width=128, target_height=72, align_frames=True)

    def test_final_one_frame_tolerance_and_audio_container_tail_do_not_block(self):
        info = dict(has_audio=True, width=128, height=72, duration=10.06,
                    video_duration=299/30, audio_duration=10.06, video_frames=299)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(media, 'probe_media', return_value=info), \
                mock.patch.object(media, 'full_decode_check'), mock.patch.object(media, 'sha256_file', return_value='fake'):
            report = media.write_qa_report(Path('/fake'), Path(directory)/'qa.json', expected_duration=10,
                expected_frames=300, narration_expected=False, expected_width=128, expected_height=72)
        self.assertEqual(report['technical_status'], 'pass')
