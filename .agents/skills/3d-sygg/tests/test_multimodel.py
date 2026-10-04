"""Deterministic route, storyboard, timeline, recovery and real-FFmpeg tests. No paid calls."""
import copy
import json
import math
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import commercial_ad as ad
import capabilities as cap
import advanced
import providers as pv
import media
import test_commercial_ad as f


def benchmark_plan(n=1, narration=False):
    p=f.sample_plan(n,narration)
    p['budget']={'max_cost_cny':100000,'video_call_upper_cny':30,'narration_call_upper_cny':5,'quote_checked_at':'test-fixture-not-a-live-quote'}
    return p

class MultiModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.original_read_config=cap.read_config
        self.config_patch=mock.patch.object(cap,'read_config',return_value={'model':'omni','provider':'auto'})
        self.config_patch.start()
    def tearDown(self):
        self.config_patch.stop();self.temp.cleanup()
    def prepare(self,model='sd11-seedance-2.0',n=1,t=10,strategy='full_storyboard',plan=None,name='project'):
        o=ad.AdOrchestrator.create(name,self.root)
        state=o.prepare(f.analysis_with_source(self.root),plan or benchmark_plan(n),target_duration=t,model=model,strategy=strategy,validation_run=model!='omni')
        return o,state
    def references(self,o,s):
        directory=o.project_dir/'references';rows=[]
        master=directory/'master.png';master.write_bytes(f.png_bytes(400))
        rows.append(dict(role='product_master',path=str(master),origin='codex_imagegen',identity_verified=True,
                         derived_from_source_hashes=[r['sha256'] for r in s['source_assets']]))
        for c in s['clips']:
            mode=c['execution_strategy']
            specs=([('storyboard',None)] if mode in ('full_storyboard','per_shot') else [('keyframe',sh['shot_id']) for sh in c['shots']] if mode=='ordered_keyframes' else [('end_frame',None)] if c.get('continuity_from') else [('start_frame',None),('end_frame',None)])
            for role,sid in specs:
                path=directory/f'{len(rows)}.png';path.write_bytes(f.png_bytes(401+len(rows)))
                rows.append(dict(role=role,shot_id=sid,path=str(path),origin='codex_imagegen',clip_index=c['index'],
                    identity_verified=True,directly_generated=True,clean_for_video=True,panel_order_verified=True,distinct_panels_verified=True,
                    panel_count=len(c['shots']),derived_from_product_master_sha256=pv.sha256_file(master)))
        o.approve_plan();o.register_references({'references':rows});o.approve_references()
        return rows
    def test_documented_is_not_verified(self):
        for model in list(cap.ROUTES)[1:]:
            c=cap.route(model);self.assertIn('full_storyboard',c['documented']);self.assertEqual(c['verified'],[])
            with self.assertRaisesRegex(pv.ValidationError,'qualified'):
                cap.prepare_timeline(f.sample_plan(),10,c)
    def test_omni_three_panel_prompt_and_order_remain_original(self):
        o,s=self.prepare('omni');self.references(o,s)
        c=s['clips'][0]
        self.assertEqual(c['video_prompt'],ad.VisualPromptBuilder(s['analysis'],s['plan']).omni_prompt(c))
        self.assertEqual(c['upstream_duration'],10);self.assertEqual(c['storyboard_panel_count'],3)
        self.assertEqual([r['role'] for r in o.load()['reference_sets']['1']],['storyboard_reference','product_master'])
    def test_all_duration_matrix_and_long_stress(self):
        for model in cap.ROUTES:
            c=cap.route(model)
            for total in (7.5,10,15,20,30,45,60,180,36000):
                with self.subTest(model=model,total=total):
                    count=math.ceil(total/c['maximum'])
                    p,keeps=cap.prepare_timeline(f.sample_plan(count),total,c,validation_run=True)
                    self.assertEqual(sum(x['keep_frames'] for x in p['clips']),round(total*30))
                    self.assertAlmostEqual(sum(keeps),total)
                    self.assertTrue(all(c['minimum']<=x['upstream_duration']<=c['maximum'] for x in p['clips']))
    def test_auto_duration_uses_director_reason(self):
        p=benchmark_plan();p.update(target_duration=7.5,duration_reason='一句旁白加一个动作与品牌收束')
        _,s=self.prepare(t=None,plan=p)
        self.assertEqual(s['target']['duration'],7.5)
    def test_fractional_frame_rounding_is_declared(self):
        _,s=self.prepare(t=7.51)
        self.assertEqual(s['target']['frames'],225);self.assertEqual(s['target']['requested_duration'],7.51)
    def test_request_duration_bounds_and_source_resolution(self):
        for model in list(cap.ROUTES)[1:]:
            for final_res in ('720p','1080p'):
                c=cap.route(model);contract={'capabilities':c,'provider_model':c['model'],'resolution':cap.source_resolution(c,final_res)}
                for duration in (c['minimum'],c['maximum']):
                    client=advanced.RoutedVideoClient('fake',pv.TaskLedger(self.root/'ledger'),contract=contract,clip={'upstream_duration':duration},roles=['storyboard_reference'])
                    payload=client.build_payload('approved motion',['https://assets.example/board.png'],aspect_ratio='9:16')
                    self.assertEqual(payload['duration'],duration)
                    self.assertEqual('generate_audio' in payload,c['family']!='minimax-h3')
                    client.clip['upstream_duration']=c['maximum']+1
                    with self.assertRaises(pv.ValidationError):client.build_payload('a',['https://assets.example/b.png'],aspect_ratio='9:16')
    def test_no_silent_upscale_or_family_switch(self):
        with self.assertRaises(pv.ValidationError):cap.source_resolution(cap.route('omni'),'1080p')
        with self.assertRaises(pv.ValidationError):cap.route('seedance-fast')
        with self.assertRaises(pv.ValidationError):cap.route('sd11-seedance-2.5','wxart')
    def test_ordered_keyframe_bindings_and_no_crop(self):
        o,s=self.prepare(strategy='ordered_keyframes');rows=self.references(o,s)
        saved=o.load();self.assertEqual([r['shot_id'] for r in saved['reference_sets']['1'][:3]],[x['shot_id'] for x in s['clips'][0]['shots']])
        self.assertEqual(s['reference_asset_plan']['minimum_imagegen_calls'],4)
        saved['reference_sets']['1'][0],saved['reference_sets']['1'][1]=saved['reference_sets']['1'][1],saved['reference_sets']['1'][0]
        with self.assertRaises(pv.ValidationError):o._validate_reference_files(saved)
        broken=copy.deepcopy(rows[1]);broken['directly_generated']=False
        with self.assertRaises(pv.ValidationError):o._normalize_reference_row(broken,1)
    def test_first_last_field_mapping(self):
        o,s=self.prepare(strategy='first_last');self.references(o,s)
        rows=o.load()['reference_sets']['1'];urls=[f'https://assets.example/{i}.png' for i in range(len(rows))]
        client=advanced.RoutedVideoClient('fake',o.ledger,contract=s['provider_contract'],clip=s['clips'][0],roles=[r['role'] for r in rows])
        payload=client.build_payload('motion',urls,aspect_ratio='9:16')
        self.assertEqual(payload['first_image_url'],urls[0]);self.assertEqual(payload['last_image_url'],urls[1]);self.assertEqual(payload['reference_image_urls'],urls[2:])
    def test_per_shot_preserves_every_visual_event(self):
        _,s=self.prepare(strategy='per_shot',t=7.5)
        self.assertEqual(len(s['clips']),3)
        self.assertEqual([c['shots'][0]['action'] for c in s['clips']],[x['action'] for x in f.sample_plan()['clips'][0]['shots']])
        self.assertEqual(sum(c['keep_frames'] for c in s['clips']),225)
    def test_overlap_is_subtracted_from_target(self):
        p=benchmark_plan(3)
        for i,c in enumerate(p['clips']):c.update(keep_duration=8 if i<2 else 7.5,overlap_before=.5 if i else 0)
        _,s=self.prepare(n=3,t=22.5,plan=p)
        self.assertEqual(s['clips'][-1]['global_end'],22.5)
        self.assertEqual(s['clips'][1]['global_start'],7.5)
    def test_unplanned_overlap_and_duplicate_shots_rejected(self):
        p=f.sample_plan();p['clips'][0]['overlap_before']=.5
        with self.assertRaises(pv.ValidationError):cap.prepare_timeline(p,10,cap.route())
        p=f.sample_plan();p['clips'][0]['shots'][0]['shot_id']='same';p['clips'][0]['shots'][1]['shot_id']='same'
        with self.assertRaises(pv.ValidationError):cap.prepare_timeline(p,10,cap.route())
    def test_omni_nonzero_trim_is_rejected(self):
        p=f.sample_plan();p['clips'][0]['trim_start']=.5
        with self.assertRaises(pv.ValidationError):cap.prepare_timeline(p,7,cap.route())
    def test_budget_checked_atomically_before_post(self):
        ledger=pv.TaskLedger(self.root/'ledger.json');ledger.budget={'max_video_attempts':1,'max_narration_attempts':0}
        ledger.append(provider='cangyuan',event='attempted',clip_index=1,attempt_id='a');ledger.append(provider='cangyuan',event='submitted',clip_index=1,task_id='t',attempt_id='a')
        with self.assertRaisesRegex(pv.ValidationError,'cap exhausted'):ledger.append(provider='cangyuan',event='attempted',clip_index=2,attempt_id='b')
        self.assertEqual(len(ledger.records()),2)
    def test_money_cap_accounts_for_attempts_even_rejected(self):
        b={'max_video_attempts':3,'max_narration_attempts':1,'max_cost_cny':10,'video_call_upper_cny':6,'narration_call_upper_cny':1}
        with self.assertRaisesRegex(pv.ValidationError,'money cap'):cap.check_budget([{'event':'attempted','provider':'cangyuan'}],{'provider':'cangyuan'},b)
    def test_benchmark_requires_concrete_cost_ceiling(self):
        p=f.sample_plan();p['validation_run']=True
        with self.assertRaisesRegex(pv.ValidationError,'max_cost_cny'):cap.budget_contract(p,1,0)
    def test_multiple_narration_chapters_and_single_delayed_chapter(self):
        p=benchmark_plan(narration=True);p['narration'].update(text='一。二。',start_time=1,end_time=9,chapters=[{'text':'一。','start_time':0,'max_duration':3},{'text':'二。','start_time':4,'max_duration':3}])
        self.assertEqual(len(advanced.plan_narration(p,10)),2)
        p['narration'].update(text='一。',chapters=[{'text':'一。','start_time':4,'max_duration':3}])
        o,s=self.prepare(plan=p)
        with mock.patch.object(advanced,'generate_narration',return_value=Path('/fake')) as call:
            o._generate_narration(s,{})
        call.assert_called_once()
        p['narration']['chapters'][0]['max_duration']=5
        with self.assertRaises(pv.ValidationError):advanced.plan_narration(p,10)
    def test_speech_ledger_allows_different_chapters_not_duplicates(self):
        ledger=pv.TaskLedger(self.root/'ledger.json')
        ledger.append(provider='minimax',event='attempted',operation_id='chapter1',request_hash='h1')
        ledger.append(provider='minimax',event='completed',request_hash='h1')
        ledger.append(provider='minimax',event='attempted',operation_id='chapter2',request_hash='h2')
        ledger.append(provider='minimax',event='completed',request_hash='h2')
        with self.assertRaises(pv.PaidRequestBlocked):ledger.append(provider='minimax',event='attempted',operation_id='chapter1',request_hash='h1')
    def test_unknown_speech_or_video_blocks_every_new_call(self):
        for provider in ('minimax','cangyuan'):
            ledger=pv.TaskLedger(self.root/(provider+'.json'))
            ledger.append(provider=provider,event='attempted',request_hash='h',attempt_id='x')
            ledger.append(provider=provider,event='submission_unknown',request_hash='h',attempt_id='x')
            with self.assertRaises(pv.PaidRequestBlocked):ledger.append(provider='cangyuan',event='attempted',clip_index=2)
    def test_continuity_dependency_and_invalidation_chain(self):
        p=benchmark_plan(3)
        for i,c in enumerate(p['clips'],1):
            c['execution_strategy']='first_last'
            if i>1:c['continuity_from']=i-1
        _,s=self.prepare(n=3,t=30,plan=p,strategy='first_last')
        advanced.invalidate_dependents(s,1,'changed')
        self.assertEqual(set(s['continuity_invalidated']),{'2','3'})
        advanced.invalidate_dependents(s,2,'changed2')
        self.assertEqual(set(s['continuity_invalidated']),{'3'})
    def test_config_is_secret_free_and_does_not_change_existing_approval(self):
        path=self.root/'config.json';cap.configure('sd11-seedance-2.5',path=path)
        self.assertEqual(json.loads(path.read_text())['model'],'sd11-seedance-2.5')
        path.write_text('{"api_key":"fake"}')
        # Original function, because setUp isolates machine defaults.
        with self.assertRaises(pv.ValidationError):self.original_read_config(path)
    def test_readonly_channel_empty_list_never_reports_available(self):
        with mock.patch.object(advanced.Keychain,'_load_stored',return_value='fake'),mock.patch.object(pv.JsonHttpClient,'safe_json',return_value={'data':[]}):
            with self.assertRaises(pv.ProviderError):advanced.check_channel(cap.route())
    def test_legacy_schema8_load_and_approval_hash_remain_valid(self):
        o,s=self.prepare('omni');self.references(o,s)
        s=o.load();s['schema_version']=8
        # This simulates an existing schema8 record without migration; hash does not include version.
        o.save(s);o._validate_generation_approval(o.load());self.assertEqual(o.load()['schema_version'],8)

class SyntheticTimelineTests(unittest.TestCase):
    def test_three_segment_45_second_dissolve_and_exact_frames(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);ffmpeg,_=media.require_media_tools();paths=[]
            clips=[{'keep_frames':480,'overlap_frames':0},{'keep_frames':480,'overlap_frames':30},{'keep_frames':450,'overlap_frames':30}]
            for i,c in enumerate(clips):
                source=root/f'{i}.mp4'
                media._run([ffmpeg,'-y','-f','lavfi','-i',f'color=c={"red" if i==0 else "blue" if i==1 else "green"}:s=128x72:r=30',
                    '-f','lavfi','-i',f'sine=frequency={200+i*100}:sample_rate=48000','-t',str(c['keep_frames']/30),'-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-ac','2',str(source)])
                paths.append(source)
            final=root/'film.mp4';advanced.stitch_timeline(paths,clips,final)
            info=media.probe_media(final);self.assertAlmostEqual(info['video_duration'],45,places=3);self.assertTrue(info['has_audio'])
            digest=pv.sha256_file(final)
            with mock.patch.object(media,'_run',wraps=media._run) as runner:advanced.stitch_timeline(paths,clips,final)
            self.assertEqual(pv.sha256_file(final),digest)
            self.assertTrue(all(Path(call.args[0][0]).name=='ffprobe' for call in runner.call_args_list))
    def test_normalize_30_seconds_trim_exact_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);source=root/'30.mp4';target=root/'keep.mp4';ffmpeg,_=media.require_media_tools()
            media._run([ffmpeg,'-y','-f','lavfi','-i','testsrc2=s=128x72:r=30','-f','lavfi','-i','sine=frequency=400:sample_rate=48000','-t','30','-c:v','libx264','-c:a','aac',str(source)])
            media.normalize_clip(source,target,15.5,target_width=128,target_height=72,trim_start=2)
            self.assertAlmostEqual(media.probe_media(target)['video_duration'],15.5,places=3)
            with mock.patch.object(media,'_run') as run:media.normalize_clip(source,target,15.5,target_width=128,target_height=72,trim_start=2)
            run.assert_not_called()

class RelayHttp:
    def __init__(self,model,outcomes=(),unknown=False,download_failure=False):
        self.model=model;self.outcomes=list(outcomes);self.unknown=unknown;self.download_failure=download_failure
        self.posts=[];self.gets=[];self.downloads=[]
    def request_json(self,method,url,**kw):
        self.posts.append(kw['payload'])
        if self.unknown:raise pv.SubmissionUnknown('test lost reply')
        return {'id':f'job-{len(self.posts)}'}
    def safe_json(self,method,url,**kw):
        self.gets.append(url)
        if url.endswith('/models'):return {'data':[{'id':self.model}]}
        number=int(url.rsplit('-',1)[-1]);status=self.outcomes[number-1] if number<=len(self.outcomes) else 'completed'
        return {'status':status,'video_url':f'https://media.example/job-{number}.mp4'}
    def safe_download(self,url,output,**kw):
        self.downloads.append(url)
        if self.download_failure:raise pv.ProviderError('temporary failed download')
        output.parent.mkdir(parents=True,exist_ok=True);output.write_bytes(url.encode());return output

class Publisher:
    def __init__(self,assets,**kw):self.assets=assets
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def health_check(self):pass
    def publish(self):
        from types import SimpleNamespace
        return [SimpleNamespace(source=r['path'],sha256=r['sha256'],url=f"https://images.example/{r['sha256']}.png") for r in self.assets]

class RoutedRecoveryTests(MultiModelTests):
    # Reuse fixture helpers without repeating inherited test cases in discovery.
    def run_case(self,http,n=3,strategy='full_storyboard'):
        o,s=self.prepare(n=n,t=n*5,strategy=strategy)
        self.references(o,s);o.key_loader=lambda *a:'fake';o.publisher_cls=Publisher
        o._preflight_core=lambda state:{'status':'pass'}
        o._assemble=lambda state,paths,narration:{'paths':paths}
        real=advanced.RoutedVideoClient
        def factory(*a,**kw):return real(*a,**kw,http=http)
        return o,factory
    def test_three_segment_produce_resume_does_not_repay(self):
        http=RelayHttp('sd11-seedance-2.0');o,factory=self.run_case(http)
        with mock.patch.object(advanced,'RoutedVideoClient',side_effect=factory):
            result=o.produce();self.assertEqual(len(result['paths']),3)
            o.resume()
        self.assertEqual(len(http.posts),3)
        self.assertEqual([p['duration'] for p in http.posts],[5,5,5])
        self.assertTrue(all('reference_image_urls' in p for p in http.posts))
    def test_unknown_first_submit_stops_other_segments_and_resume(self):
        http=RelayHttp('sd11-seedance-2.0',unknown=True);o,factory=self.run_case(http)
        with mock.patch.object(advanced,'RoutedVideoClient',side_effect=factory):
            with self.assertRaises(pv.SubmissionUnknown):o.produce()
            with self.assertRaises(pv.PaidRequestBlocked):o.resume()
        self.assertEqual(len(http.posts),1)
    def test_failed_segment_retries_once_without_repeating_success(self):
        http=RelayHttp('sd11-seedance-2.0',outcomes=['failed','completed','completed']);o,factory=self.run_case(http,n=2)
        with mock.patch.object(advanced,'RoutedVideoClient',side_effect=factory):o.produce();o.resume()
        self.assertEqual(len(http.posts),3)
        attempted=[r for r in o.ledger.records() if r['event']=='attempted']
        self.assertEqual([r['clip_index'] for r in attempted],[1,2,1])
    def test_download_failure_recovers_existing_id(self):
        http=RelayHttp('sd11-seedance-2.0',download_failure=True);o,factory=self.run_case(http,n=1)
        with mock.patch.object(advanced,'RoutedVideoClient',side_effect=factory):
            with self.assertRaises(pv.ProviderError):o.produce()
            http.download_failure=False;o.resume()
        self.assertEqual(len(http.posts),1)
    def test_new_semantic_hash_ignores_changed_tunnel_url(self):
        http=RelayHttp('sd11-seedance-2.0',outcomes=['failed','completed']);o,factory=self.run_case(http,n=1)
        state=o.load();o.ledger.schema_version=9
        client=factory('fake',o.ledger,contract=state['provider_contract'],clip=state['clips'][0],roles=['storyboard_reference'])
        binding=lambda url:[{'position':1,'role':'storyboard_reference','sha256':'fixed','url':url}]
        first='https://first.example/a.png';second='https://second.example/a.png'
        task=client.submit('motion',[first],aspect_ratio='9:16',clip_index=1,reference_bindings=binding(first),max_retries=1)
        with self.assertRaises(pv.ProviderError):client.poll(task)
        client.submit('motion',[second],aspect_ratio='9:16',clip_index=1,reference_bindings=binding(second),max_retries=1,retry_of=task)
        self.assertEqual(len(http.posts),2)

# Do not duplicate the base tests through unittest inheritance.
for _name in list(MultiModelTests.__dict__):
    if _name.startswith('test_'):
        setattr(RoutedRecoveryTests,_name,None)

class FinalGuardTests(unittest.TestCase):
    def test_replaced_successor_also_requires_new_junction_review(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);first=root/'a.mp4';second=root/'b.mp4';first.write_bytes(b'a');second.write_bytes(b'b')
            replacements={1:{'path':str(first),'clip':{}},2:{'path':str(second),'clip':{}}}
            o=SimpleNamespace(_replacement_sources=lambda state:replacements,key_loader=lambda *a:'fake',ledger=pv.TaskLedger(root/'ledger'),project_dir=root)
            state={'clips':[{'index':1},{'index':2,'continuity_from':1}],
                   'plan':{'recovery_policy':{'max_video_retries_per_clip':1}},
                   'reference_sets':{'1':[],'2':[]},'continuity_invalidated':{'2':{'changed_ancestor':1}}}
            with mock.patch.object(advanced,'continuity_asset',side_effect=pv.ValidationError('inspect new junction')) as inspect:
                with self.assertRaisesRegex(pv.ValidationError,'new junction'):
                    advanced.generate_videos(o,state,lambda: self.fail('must not publish'),True)
            self.assertEqual(inspect.call_args.kwargs['successor'],second)
            self.assertEqual(inspect.call_args.args[3],first)
    def test_low_resolution_or_wrong_composition_not_silently_upscaled(self):
        state={'target':{'resolution':'720p','width':720,'height':1280}}
        for w,h in ((480,854),(1280,720)):
            with mock.patch.object(media,'probe_media',return_value={'has_video':True,'width':w,'height':h}):
                with self.assertRaises(pv.ValidationError):advanced.validate_source_video(Path('/fake'),state)
    def test_schema8_cangyuan_semantic_hash_is_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            ledger=pv.TaskLedger(Path(d)/'ledger');ledger.schema_version=8
            http=RelayHttp('omni-fast-no-water');client=pv.CangyuanClient('fake',ledger,http=http)
            url='https://assets.example/a.png';bindings=[{'position':1,'role':'storyboard_reference','sha256':'fixed','url':url}]
            client.submit('approved motion',[url],aspect_ratio='9:16',clip_index=1,reference_bindings=bindings,max_retries=1)
            payload=client.build_payload('approved motion',[url],aspect_ratio='9:16')
            expected=pv.canonical_hash({'clip_index':1,'payload':{k:v for k,v in payload.items() if k!='images_url'},'references':[{k:v for k,v in r.items() if k!='url'} for r in bindings]})
            self.assertEqual(ledger.records()[0]['semantic_hash'],expected)
