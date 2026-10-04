"""Multi-model execution extensions. Legacy Omni payload and approvals remain separate."""
from __future__ import annotations
import copy
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
try:
    from . import capabilities as caps
    from . import media
    from .providers import (CangyuanClient, OmniClient, MiniMaxClient, TaskLedger, Keychain,
        KEYCHAIN_ACCOUNT, CANGYUAN_KEYCHAIN_SERVICE, OMNI_KEYCHAIN_SERVICE, MINIMAX_KEYCHAIN_SERVICE,
        CREDENTIAL_ENV_NAMES, ValidationError, ProviderError, PaidRequestBlocked, SubmissionUnknown,
        atomic_write_json, canonical_hash, sha256_file, validate_public_https_url, file_lock)
except ImportError:
    import capabilities as caps
    import media
    from providers import (CangyuanClient, OmniClient, MiniMaxClient, TaskLedger, Keychain,
        KEYCHAIN_ACCOUNT, CANGYUAN_KEYCHAIN_SERVICE, OMNI_KEYCHAIN_SERVICE, MINIMAX_KEYCHAIN_SERVICE,
        CREDENTIAL_ENV_NAMES, ValidationError, ProviderError, PaidRequestBlocked, SubmissionUnknown,
        atomic_write_json, canonical_hash, sha256_file, validate_public_https_url, file_lock)

def validate_target(state):
    target=state['target']; contract=state['provider_contract']; cap=contract['capabilities']
    native=caps.source_resolution(cap,target['resolution'])
    wh=(720,1280) if target['aspect_ratio']=='9:16' else (1280,720)
    if target['aspect_ratio'] not in ('9:16','16:9'):
        raise ValidationError('Unsupported output aspect ratio')
    if target['resolution']=='1080p': wh=tuple(v*3//2 for v in wh)
    if (target['width'],target['height'])!=wh or contract['aspect_ratio']!=target['aspect_ratio'] or contract['resolution']!=native:
        raise ValidationError('Frozen output and provider specifications disagree')
    if caps.frames(target['duration']) != target['frames']:
        raise ValidationError('Frozen frame count does not match duration')

def video_prompt(builder, clip, cap):
    if cap['family']=='omni':
        return builder.omni_prompt(clip)
    strategy=clip['execution_strategy']
    shots=clip['shots']
    offset=clip.get('trim_start',0)
    binding=[]
    if strategy in ('full_storyboard','per_shot'):
        binding.append(f"Image 1 is the complete {len(shots)}-panel temporal storyboard. Read {clip['storyboard_reading_order']}. Each panel maps to these stable shot IDs: " + ', '.join(s['shot_id'] for s in shots))
        roles=clip['execution_reference_roles'][1:]
        binding.extend(f'Image {i+2}: {role}, identity reference only.' for i,role in enumerate(roles))
    elif strategy=='ordered_keyframes':
        binding.extend(f"Image {i+1}: independent keyframe for {s['shot_id']} at [{s['start']+offset:.2f}-{s['end']+offset:.2f}s]." for i,s in enumerate(shots))
        binding.append(f"Image {len(shots)+1}: product master; identity only.")
        if clip.get('talent_action'): binding.append(f"Image {len(shots)+2}: same approved adult talent identity.")
    else:
        binding.append('first_image_url is the approved opening state; last_image_url is the approved final state. Other reference images preserve product/talent identity.')
    beats=[]
    for s in shots:
        beats.append(f"[Shot {s['shot_id']}] [{s['start']+offset:.2f}-{s['end']+offset:.2f}s] Purpose: {s['purpose']}; framing: {s['visual']}; action: {s['action']}; product fidelity: {s['product_detail']}; camera: {s['camera']}; transition: {s['transition']}; animated graphics: {json.dumps(s.get('graphics',[]),ensure_ascii=False)}")
    return '\n'.join([builder.global_anchor(), 'Execute the SAME approved commercial design, retaining every listed visual event.',
        *binding, f"Entry state: {clip['entry_state']}; exit state: {clip['exit_state']}",
        f"Opening transition: {clip['transition_in']}; closing transition: {clip['transition_out']}", *beats,
        f"After {clip['keep_duration']+offset:.2f}s maintain the resolved exit state through {clip['upstream_duration']}s. No new opening or ending between segments.",
        'Output normal full-screen continuous motion. Never show a contact sheet, collage, panel border, static slideshow, unapproved captions or skipped/reordered shots.',
        'Preserve approved product brand marks and exact approved text; do not remove real logos. Animate typography as designed.',
        'silent, no dialogue, no human voice, no speech, ambient SFX and mechanical sound effects only',
        'Soundscape: '+clip['sfx']])

def configure_asset_plan(order, clips):
    order[:]=[v for v in order if not v.startswith('storyboard:')]
    for c in clips:
        if c['execution_strategy'] in ('full_storyboard','per_shot'):
            order.append(f"storyboard:{c['index']}")
        elif c['execution_strategy']=='ordered_keyframes':
            order.extend(f"keyframe:{c['index']}:{s['shot_id']}" for s in c['shots'])
        else:
            if not c.get('continuity_from'): order.append(f"start_frame:{c['index']}")
            order.append(f"end_frame:{c['index']}")

def normalize_reference(o,row,order):
    if row.get('identity_verified') is not True or row.get('clean_for_video') is not True or row.get('directly_generated') is not True:
        raise ValidationError('Independent frames require identity_verified, clean_for_video and directly_generated=true; do not crop approved boards')
    if row['role']=='keyframe' and not row.get('shot_id'):
        raise ValidationError('Keyframe requires the stable shot_id')
    proxy={**row,'role':'storyboard','panel_count':1,'panel_order_verified':True,'distinct_panels_verified':True}
    normalized=o._normalize_reference_row(proxy,order)
    normalized.update(role=row['role'],shot_id=row.get('shot_id'),identity_verified=True,directly_generated=True)
    return normalized

def validate_references(o,state,registered):
    # Preserve the legacy globals and full-board validation, including all product evidence.
    globals_=[r for r in registered if r['role'] in ('product_master','talent','style')]
    masters=[r for r in globals_ if r['role']=='product_master']
    talents=[r for r in globals_ if r['role']=='talent']
    if len(masters)!=1 or sorted(masters[0].get('derived_from_source_hashes') or [])!=sorted(r['sha256'] for r in state['source_assets']):
        raise ValidationError('One generated master derived from every frozen product photo is required')
    human=state['plan']['talent_strategy']['mode']=='human_interaction'
    if len(talents)!=(1 if human else 0) or (human and any(r['role']=='style' for r in globals_)):
        raise ValidationError('Talent references must match the approved persona policy')
    if sum(r['role']=='style' for r in globals_)>1:
        raise ValidationError('At most one style reference is allowed')
    source_hashes={r['sha256'] for r in state['source_assets']}; source_pixels={r['pixel_sha256'] for r in state['source_assets']}
    if len({r['sha256'] for r in registered})!=len(registered): raise ValidationError('Duplicate reference files')
    for r in registered:
        if r['sha256'] in source_hashes or r['pixel_sha256'] in source_pixels:
            raise ValidationError('Original product photos must never be uploaded to the video model')
    sets={}; consumed={r['sha256'] for r in globals_}
    for c in state['clips']:
        mode=c['execution_strategy']; rows=[r for r in registered if r.get('clip_index')==c['index']]
        if mode in ('full_storyboard','per_shot'):
            if len(rows)!=1 or rows[0]['role']!='storyboard' or rows[0]['panel_count']!=len(c['shots']):
                raise ValidationError('Full storyboard must match its approved panel count')
            selected=[rows[0]]+[next(r for r in globals_ if r['role']==role) for role in c['execution_reference_roles'][1:]]
        elif mode=='ordered_keyframes':
            if len(rows)!=len(c['shots']) or {r.get('shot_id') for r in rows}!={s['shot_id'] for s in c['shots']} or any(r['role']!='keyframe' for r in rows):
                raise ValidationError('Every approved shot needs exactly one ordered independently generated keyframe')
            selected=[next(r for r in rows if r['shot_id']==s['shot_id']) for s in c['shots']]+masters
            if c.get('talent_action'): selected+=talents
        else:
            expected=['end_frame'] if c.get('continuity_from') else ['start_frame','end_frame']
            if sorted(r['role'] for r in rows)!=sorted(expected):
                raise ValidationError('First-last strategy needs exactly the planned start/end frames')
            selected=[next(r for r in rows if r['role']==role) for role in expected]+masters
            if c.get('talent_action'): selected+=talents
        for r in rows:
            if r.get('derived_from_product_master_sha256')!=masters[0]['sha256'] or r['pixel_sha256']==masters[0]['pixel_sha256']:
                raise ValidationError('Every scene frame must be generated from the approved product master')
            if c.get('talent_action') and (not talents or r.get('derived_from_talent_sha256')!=talents[0]['sha256']):
                raise ValidationError('Every talent scene must use the same approved talent identity')
        consumed.update(r['sha256'] for r in rows)
        # Count first/last inputs conservatively as images too.
        if len(selected)+(1 if c.get('continuity_from') else 0)>state['provider_contract']['capabilities']['images']:
            raise ValidationError('Approved binding exceeds route image limit')
        sets[str(c['index'])]=[{**r,'order':i,'role':'storyboard_reference' if r['role']=='storyboard' else r['role']} for i,r in enumerate(selected,1)]
    if consumed!={r['sha256'] for r in registered}: raise ValidationError('Manifest contains an unplanned frame')
    return sets

def verify_bindings(o,state):
    expected=validate_references(o,state,state['references'])
    if expected!=state['reference_sets']:
        raise ValidationError('Execution bindings differ from the approved ordered assets')

def plan_narration(plan,total):
    n=plan['narration']
    if not n['enabled']: return []
    window=n.get('end_time',total)-n.get('start_time',0)
    chapters=n.get('chapters')
    if not chapters:
        if len(n['text'])>2500:
            raise ValidationError('Long narration must have natural chapters of at most 2500 characters, with start_time and max_duration')
        return [{'operation_id':'narration','text':n['text'],'start_time':0,'max_duration':window}]
    if not isinstance(chapters,list) or not chapters:
        raise ValidationError('Narration chapters must be a nonempty list')
    if ''.join(c['text'] for c in chapters).replace('\n','').replace(' ','')!=n['text'].replace('\n','').replace(' ',''):
        raise ValidationError('Narration chapters must preserve the complete approved text')
    last=0; result=[]
    for i,c in enumerate(chapters,1):
        start=c.get('start_time'); duration=c.get('max_duration')
        if not c['text'].strip() or len(c['text'])>2500 or not isinstance(start,(int,float)) or not isinstance(duration,(int,float)) or not math.isfinite(start+duration) or start<last or duration<=0 or start+duration>window:
            raise ValidationError('Narration chapter timing, text length, or overlap is invalid')
        result.append(dict(operation_id=f'narration-{i:04d}',text=c['text'],start_time=start,max_duration=duration))
        last=start+duration
    return result

def chunk_hash(n,c):
    payload=MiniMaxClient.build_payload(c['text'],n['voice_id'],speed=float(n.get('speed',1)))
    return canonical_hash({'operation_id':c['operation_id'],'payload':payload})

def cached_narration(o,state):
    path=o.project_dir/'audio/narration.mp3'; receipt=path.with_suffix('.assembly.json')
    if not path.exists() or not receipt.exists(): return None
    data=json.loads(receipt.read_text())
    contract=state['provider_contract']['narration_chunks']
    if data.get('contract')!=canonical_hash([contract,state['plan']['narration']]) or data.get('sha256')!=sha256_file(path): return None
    if any(not Path(r['path']).is_file() or sha256_file(Path(r['path']))!=r['sha256'] for r in data['chunks']): return None
    return path

def generate_narration(o,state,preflight):
    old=cached_narration(o,state)
    if old:return old
    n=state['plan']['narration']; chunks=state['provider_contract']['narration_chunks']; inputs=[]
    client=o.minimax_factory(o.key_loader(MINIMAX_KEYCHAIN_SERVICE,KEYCHAIN_ACCOUNT),o.ledger)
    for c in chunks:
        path=o.project_dir/'audio'/f"{c['operation_id']}.mp3"; digest=chunk_hash(n,c)
        records=[r for r in o.ledger.records() if r.get('provider')=='minimax' and r.get('request_hash')==digest]
        completed=next((r for r in reversed(records) if r['event']=='completed'),None)
        if not (completed and path.is_file() and sha256_file(path)==completed.get('sha256')):
            if records: raise PaidRequestBlocked('Narration chunk was attempted; recover its existing bytes, never synthesize it again')
            client.synthesize(c['text'],n['voice_id'],path,verified_system_voices=preflight['system_voices'],speed=float(n.get('speed',1)),operation_id=c['operation_id'])
        info=media.probe_media(path)
        if not info['has_audio'] or info['duration']>c['max_duration']+.05:
            raise ValidationError('Narration chapter exceeds its approved window; no video call was made')
        inputs.append(path)
    ffmpeg,_=media.require_media_tools(); command=[ffmpeg,'-y']; filters=[]
    for i,(c,p) in enumerate(zip(chunks,inputs)):
        command+=['-i',str(p)]
        filters.append(f"[{i}:a]adelay={round(c['start_time']*1000)}:all=1[a{i}]")
    filters.append(''.join(f'[a{i}]' for i in range(len(inputs)))+f'amix=inputs={len(inputs)}:normalize=0:duration=longest[out]')
    out=o.project_dir/'audio/narration.mp3'
    media._run(command+['-filter_complex',';'.join(filters),'-map','[out]','-c:a','libmp3lame','-b:a','192k',str(out)])
    atomic_write_json(out.with_suffix('.assembly.json'),{'contract':canonical_hash([chunks,n]),'sha256':sha256_file(out),'chunks':[{'path':str(p),'sha256':sha256_file(p)} for p in inputs]})
    return out

class RoutedVideoClient(CangyuanClient):
    def __init__(self,key,ledger,*,contract,clip,roles,http=None):
        super().__init__(key,ledger,http=http)
        self.contract=contract; self.clip=clip; self.roles=roles
        self.model=contract['provider_model']
    def build_payload(self,prompt,image_urls,*,aspect_ratio):
        cap=self.contract['capabilities']; duration=self.clip['upstream_duration']; resolution=self.contract['resolution']
        if type(duration) is not int or not cap['minimum']<=duration<=cap['maximum'] or resolution not in cap['resolutions']:
            raise ValidationError('Video request exceeds the frozen route duration/resolution contract')
        if not prompt.strip() or aspect_ratio not in ('9:16','16:9') or len(image_urls)!=len(self.roles) or not 1<=len(image_urls)<=cap['images']:
            raise ValidationError('Video prompt or ordered image binding is invalid')
        for url in image_urls: validate_public_https_url(url)
        value=dict(model=self.model,prompt=prompt,duration=duration,aspect_ratio=aspect_ratio,resolution=resolution)
        references=[]
        for role,url in zip(self.roles,image_urls):
            if role=='start_frame':value['first_image_url']=url
            elif role=='end_frame':value['last_image_url']=url
            else:references.append(url)
        if value.get('last_image_url') and not value.get('first_image_url'):
            raise ValidationError('End frame requires a start frame')
        if references:value['reference_image_urls']=references
        if cap['generate_audio']:value['generate_audio']=True
        # H3 has no generate_audio field in this relay's published contract.
        return value
    def check_available(self):
        data=self.http.safe_json('GET',f'{self.base_url}/v1/models',headers={'Authorization':'Bearer '+self.api_key},attempts=2,timeout=20)
        if self.model not in {r.get('id') for r in data.get('data',[]) if isinstance(r,dict)}:
            raise ProviderError('Configured model not visible on this channel; no paid request sent')

def check_channel(cap):
    service=CANGYUAN_KEYCHAIN_SERVICE if cap['provider']=='cangyuan' else OMNI_KEYCHAIN_SERVICE
    key=next((os.environ[n] for n in CREDENTIAL_ENV_NAMES[service] if os.environ.get(n)),None) or Keychain._load_stored(service,KEYCHAIN_ACCOUNT)
    if not key: raise ValidationError('No environment/Keychain credential; read-only check does not migrate credentials')
    client=(CangyuanClient if cap['provider']=='cangyuan' else OmniClient)(key,TaskLedger(Path(os.devnull)))
    client.model=cap['model']
    data=client.http.safe_json('GET',f'{client.base_url}/v1/models',headers={'Authorization':'Bearer '+key},attempts=2,timeout=20)
    if client.model not in {r.get('id') for r in data.get('data',[]) if isinstance(r,dict)}:
        raise ProviderError('Configured model not visible in the returned model list')
    return {'route':cap['route_id'],'model_list_visible':True,'generation_tested':False,'visual_quality_qualified':False,'requests':'GET /v1/models only'}

def continuity_asset(o,state,clip,source,successor=None):
    previous=state['clips'][clip['continuity_from']-1]
    digest=sha256_file(source)
    frame=o.project_dir/'references'/f"continuity-{clip['index']:04d}-{digest[:12]}.png"
    t=previous.get('trim_start',0)+previous['keep_duration']-1/30
    if not frame.exists():
        ffmpeg,_=media.require_media_tools()
        media._run([ffmpeg,'-y','-ss',f'{t:.9f}','-i',str(source),'-frames:v','1',str(frame)])
    descriptor={'clip_index':clip['index'],'source_sha256':digest,'sha256':sha256_file(frame),'path':str(frame),'time':t,
                'reference_approval':state['approvals']['references'],'role':'start_frame'}
    if successor is not None:
        successor_hash=sha256_file(successor)
        opening=o.project_dir/'qa'/f"junction-{clip['index']:04d}-{successor_hash[:12]}.png"
        if not opening.exists():
            ffmpeg,_=media.require_media_tools()
            media._run([ffmpeg,'-y','-ss',str(clip.get('trim_start',0)),'-i',str(successor),'-frames:v','1',str(opening)])
        descriptor.update(successor_sha256=successor_hash,successor_frame=str(opening),successor_frame_sha256=sha256_file(opening),
                          review_purpose='Compare both frames and watch the existing successor motion before accepting the changed junction')
    pending=o.project_dir/'qa'/f"continuity-{clip['index']:04d}.json"
    receipt=pending.with_suffix('.review.json')
    atomic_write_json(pending,descriptor)
    accepted=json.loads(receipt.read_text()) if receipt.exists() else {}
    if accepted.get('binding')!=canonical_hash(descriptor) or not all(accepted.get(k) is True for k in ('identity_preserved','action_state_correct','no_grid_or_text_artifacts')):
        raise ValidationError(f'Inspect continuity frame and record review-continuity before resume: {pending}')
    return descriptor

def review_continuity(o,review):
    with file_lock(o.project_dir/'.orchestrator.lock'):
        state=o.load(); o._validate_generation_approval(state,allowed_states={'failed','generating'})
        i=review.get('clip_index')
        if type(i) is not int or not 1<=i<=len(state['clips']) or not state['clips'][i-1].get('continuity_from'):
            raise ValidationError('No planned continuity frame for this clip')
        path=o.project_dir/'qa'/f'continuity-{i:04d}.json'; data=json.loads(path.read_text())
        if review.get('sha256')!=data['sha256'] or sha256_file(Path(data['path']))!=data['sha256'] or not review.get('reviewer'):
            raise ValidationError('Continuity review must identify the inspected frame hash and reviewer')
        if data.get('successor_sha256') and review.get('successor_sha256')!=data['successor_sha256']:
            raise ValidationError('A changed junction review must also identify and inspect the existing successor video')
        for key in ('identity_preserved','action_state_correct','no_grid_or_text_artifacts'):
            if review.get(key) is not True: raise ValidationError('Continuity inspection failed; do not propagate this frame')
        receipt={**review,'binding':canonical_hash(data)}
        atomic_write_json(path.with_suffix('.review.json'),receipt)
        return receipt

def generate_videos(o,state,ensure_public,allow_paid):
    # Imports are delayed to avoid the orchestrator/extension import cycle.
    try: from .commercial_ad import _merged_omni_tasks
    except ImportError: from commercial_ad import _merged_omni_tasks
    replacements=o._replacement_sources(state)
    state=copy.deepcopy(state)
    for i,r in replacements.items():
        state['clips'][i-1]['trim_start']=r['clip'].get('trim_start',0)
    outputs={}
    key=o.key_loader(CANGYUAN_KEYCHAIN_SERVICE,KEYCHAIN_ACCOUNT)
    clips=state['clips']; retry=state['plan']['recovery_policy']['max_video_retries_per_clip']
    checked=False
    with ExitStack() as stack:
        def run_poll(client,task,index):
            data=client.poll(task)
            return client.download_result(task,data,o.project_dir/'clips/raw'/f'clip-{index:02d}.mp4')
        while len(outputs)<len(clips):
            ready=[c for c in clips if c['index'] not in outputs and (not c.get('continuity_from') or c['continuity_from'] in outputs)][:2]
            if not ready:raise ValidationError('Unresolved continuity dependency')
            failures=[]; futures=[]
            attempts_before=sum(r.get('event')=='attempted' for r in o.ledger.records())
            retryable_before={c['index']:o.ledger.retry_parent(c['index']) for c in ready}
            with ThreadPoolExecutor(max_workers=2) as pool:
                for c in ready:
                    i=c['index']; rows=copy.deepcopy(state['reference_sets'][str(i)])
                    try:
                        if i in replacements:
                            replacement_path=Path(replacements[i]['path'])
                            if str(i) in state.get('continuity_invalidated',{}):
                                continuity_asset(o,state,c,outputs[c['continuity_from']],successor=replacement_path)
                            outputs[i]=replacement_path
                            continue
                        tasks=[t for t in _merged_omni_tasks(o.ledger.records()).values() if t['clip_index']==i]
                        task=tasks[-1] if tasks else None
                        if task and task.get('downloaded'):
                            path=Path(task['path'])
                            if path.exists() and sha256_file(path)==task.get('sha256'):
                                if str(i) in state.get('continuity_invalidated',{}):
                                    continuity_asset(o,state,c,outputs[c['continuity_from']],successor=path)
                                outputs[i]=path; continue
                        client=RoutedVideoClient(key,o.ledger,contract=state['provider_contract'],clip=c,roles=[r['role'] for r in rows])
                        retry_of=o.ledger.retry_parent(i)
                        if task and task.get('status') not in ('failed','error','cancelled','canceled'):
                            futures.append((i,pool.submit(run_poll,client,task['task_id'],i))); continue
                        if not allow_paid:raise PaidRequestBlocked('This approval permits existing-task recovery only')
                        if task and not retry_of:raise PaidRequestBlocked('No remaining approved retry for this failed task')
                        extra=None
                        if c.get('continuity_from'):
                            extra=continuity_asset(o,state,c,outputs[c['continuity_from']])
                            rows.insert(0,extra)
                        if not checked:client.check_available();checked=True
                        public=ensure_public()
                        if extra:
                            publisher=stack.enter_context(o.publisher_cls([extra],evidence_dir=o.project_dir/'requests/publication'))
                            public={**public,**{str(Path(a.source).resolve()):a.url for a in publisher.publish()}}
                            publisher.health_check()
                        urls=[public[str(Path(r['path']).resolve())] for r in rows]
                        bindings=[{'position':p,'role':r['role'],'clip_index':i,'shot_id':r.get('shot_id'),'sha256':r['sha256'],'url':url} for p,(r,url) in enumerate(zip(rows,urls),1)]
                        client.roles=[r['role'] for r in rows]
                        task_id=client.submit(c['video_prompt'],urls,clip_index=i,aspect_ratio=state['target']['aspect_ratio'],reference_bindings=bindings,max_retries=retry,retry_of=retry_of)
                        futures.append((i,pool.submit(run_poll,client,task_id,i)))
                    except (ProviderError,ValidationError,PaidRequestBlocked,SubmissionUnknown) as exc:
                        failures.append((i,exc));break
                for i,future in futures:
                    try:
                        downloaded=future.result()
                        if str(i) in state.get('continuity_invalidated',{}):
                            continuity_asset(o,state,clips[i-1],outputs[clips[i-1]['continuity_from']],successor=downloaded)
                        outputs[i]=downloaded
                    except (ProviderError,ValidationError,PaidRequestBlocked,SubmissionUnknown) as exc:failures.append((i,exc))
            if failures:
                fatal=next((exc for i,exc in failures if isinstance(exc,(SubmissionUnknown,ValidationError,PaidRequestBlocked)) or not o.ledger.retry_parent(i)),None)
                if fatal:raise fatal
                # A resumed poll confirming failure is progress even without a POST.
                # Availability/publication failures with no progress must still stop.
                if (sum(r.get('event')=='attempted' for r in o.ledger.records())==attempts_before
                        and all(o.ledger.retry_parent(i)==retryable_before.get(i) for i,_ in failures)):
                    raise failures[0][1]
    return [outputs[c['index']] for c in clips]

def stitch_timeline(paths,clips,output):
    """Pairwise cached render bounds decoder memory even for long films."""
    ffmpeg,_=media.require_media_tools(); current=paths[0]; accumulated=clips[0]['keep_frames']
    cache=output.parent/'transitions';cache.mkdir(parents=True,exist_ok=True)
    for i,(path,c) in enumerate(zip(paths[1:],clips[1:]),1):
        overlap=c.get('overlap_frames',0); total=accumulated+c['keep_frames']-overlap
        digest=canonical_hash([sha256_file(current),sha256_file(path),accumulated,c['keep_frames'],overlap])
        target=cache/f'{i:04d}-{digest[:16]}.mp4';receipt=target.with_suffix('.json')
        valid=target.exists() and receipt.exists() and json.loads(receipt.read_text()).get('sha256')==sha256_file(target)
        if not valid:
            if overlap:
                filters=(f'[0:v]settb=AVTB,setpts=PTS-STARTPTS[v0];[1:v]settb=AVTB,setpts=PTS-STARTPTS[v1];'
                    f'[v0][v1]xfade=transition=fade:duration={overlap/30:.9f}:offset={(accumulated-overlap)/30:.9f}[v];'
                    f'[0:a][1:a]acrossfade=d={overlap/30:.9f}:c1=tri:c2=tri[a]')
                media._run([ffmpeg,'-y','-i',str(current),'-i',str(path),'-filter_complex_threads','1','-filter_complex',filters,
                    '-map','[v]','-map','[a]','-t',f'{total/30:.9f}','-r','30','-c:v','libx264','-preset','medium','-crf','18',
                    '-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2',str(target)],timeout=max(600,math.ceil(total/30)*10))
            else:media.stitch_clips([current,path],target)
            atomic_write_json(receipt,{'sha256':sha256_file(target),'frames':total})
        current=target;accumulated=total
    import shutil
    shutil.copy2(current,output)
    info=media.probe_media(output)
    if abs(info['video_duration']-accumulated/30)>1/30+.002:
        raise ValidationError('Assembled transition timeline differs from the approved frame count')

def render_plan(s):
    p=s['plan'];c=s['provider_contract'];t=s['target'];n=p['narration']
    lines=[f"# 3D sygg 完整广告方案：{s['project_id']}",'',f"核心创意：{p['big_idea']}",f"故事：{p['story_arc']}",
        f"风格：{p['style']}；{p['style_rationale']}",f"视听：{p['audiovisual_tone']}",
        f"全片身份/光线基准：{s['global_anchor']}",f"成片：{t['duration']} 秒 / {t['frames']} 帧 / {t['width']}×{t['height']}；{t['aspect_ratio_reason']}",
        f"时长依据：{p.get('duration_reason','用户明确指定')}；请求时长与成片时长分别管理。",
        f"通道：{c['route_id']}；源分辨率：{c['resolution']}；配置已冻结。",
        f"策略实片验证记录：{p['qualification_snapshot']}；本项目验证任务：{p['validation_run']}",
        f"商品母版 → 人物身份图（如需）→ 分镜素材：{s['reference_asset_plan']['generation_order']}",
        f"最少生图次数：{s['reference_asset_plan']['minimum_imagegen_calls']}",
        f"视频基础调用：{len(s['clips'])}；中文旁白调用：{s['paid_counts']['minimax']}；最多两个独立视频并行。",
        '付费上限（包含失败尝试，非服务商账单）：'+json.dumps(c['budget'],ensure_ascii=False),
        '若未配置金额上限，仅调用次数受控；首次实测必须提供金额上限。',
        '不上传原商品照片；不擅自裁切批准分镜；不自动换模型、降清晰度或简化创意。','']
    for clip in s['clips']:
        lines += [f"## 第 {clip['index']} 段：{clip['narrative_role']}",
            f"{clip['global_start']:.3f}–{clip['global_end']:.3f}s；请求 {clip['upstream_duration']}s；保留 {clip['keep_duration']}s；转场重叠 {clip['overlap_frames']/30}s",
            f"执行：{clip['execution_strategy']}；{clip['storyboard_panel_count']} 格；阅读顺序：{clip['storyboard_reading_order']}",
            f"入场：{clip['entry_state']}；出场：{clip['exit_state']}；前段末帧依赖：{clip.get('continuity_from')}"]
        for shot in clip['shots']:
            lines += [f"- {shot['shot_id']} [{shot['start']:.2f}–{shot['end']:.2f}s] {shot['purpose']}；画面 {shot['visual']}；动作 {shot['action']}；细节 {shot['product_detail']}；运镜 {shot['camera']}；转场 {shot['transition']}；动态图文 {json.dumps(shot.get('graphics',[]),ensure_ascii=False)}"]
        lines += ['','分镜生图设计：',clip['storyboard_prompt'] if clip['execution_strategy'] in ('full_storyboard','per_shot') else json.dumps(clip['frame_prompts'],ensure_ascii=False,indent=2),'','视频执行提示词：',clip['video_prompt'],'']
    lines += ['## 中文旁白',n['text'] if n['enabled'] else '无旁白：'+n['reason'],json.dumps(c['narration_chunks'],ensure_ascii=False,indent=2),
        '', '商品母版提示词：',s['product_master_prompt'],'','人物参考提示词：',str(s['talent_reference_prompt']),
        '', '等待确认：**确认方案并生成参考图**。参考图确认后才进行付费视频与旁白生成。']
    return '\n'.join(lines)

def render_references(s):
    lines=[f"# 实际参考图确认：{s['project_id']}",'',f"通道 {s['provider_contract']['route_id']}；成片 {s['target']['duration']} 秒。",
        '所有上传顺序由同一素材绑定记录生成；原商品照片只供生图取证。','']
    for c in s['clips']:
        lines += [f"## 第 {c['index']} 段：{c['execution_strategy']}"]
        for r in s['reference_sets'][str(c['index'])]:
            lines += [f"{r['order']}. {r['role']} {r.get('shot_id') or ''} / SHA256 {r['sha256']}",f"![{r['role']}]({r['path']})",'']
        if c.get('continuity_from'):lines.append('已计划使用前段保留区间末帧；生成后必须通过身份/动作检查再接续。')
    lines += [render_plan(s).split('等待确认：')[0],'','等待确认：**确认参考图并生成视频**。']
    return '\n'.join(lines)

def needs_extended_report(state):
    return (state['provider_contract']['capabilities']['family']!='omni'
        or any(c['execution_strategy']!='full_storyboard' or c.get('overlap_frames') for c in state['clips'])
        or bool(state['plan']['narration'].get('chapters')))

def frozen_summary(state):
    c=state['provider_contract']
    return f"冻结通道：{c['route_id']}；源规格：{c['resolution']}；次数及金额上限：{json.dumps(c['budget'],ensure_ascii=False)}；旁白 {len(c['narration_chunks'])} 次；最少生图 {state['reference_asset_plan']['minimum_imagegen_calls']} 次。金额上限未列出时仅控制调用次数。"

def invalidate_dependents(state,index,new_hash):
    """Invalidate acceptance, not paid history. A new junction review may reuse good footage."""
    invalid=state.setdefault('continuity_invalidated',{})
    invalid.pop(str(index),None)
    affected={index}
    for c in state['clips']:
        if c.get('continuity_from') in affected:
            affected.add(c['index'])
            invalid[str(c['index'])]={'changed_ancestor':index,'replacement_sha256':new_hash}


def frame_prompts(builder,clip):
    base = builder.global_anchor() + "\nGenerate ONE independent full-screen keyframe from the approved product master and applicable talent reference. Preserve exact product and branding, adult identity, wardrobe, set, light and palette. No collage, border, shot labels, timecodes or narration subtitles. No cropping an existing approved storyboard."
    result = {}
    for shot in clip['shots']:
        result[shot['shot_id']] = base + f"\nShot {shot['shot_id']}: {shot['visual']}. Show a representative action state making this motion legible: {shot['action']}. Product details: {shot['product_detail']}. Camera composition: {shot['camera']}. Approved spatial graphics: {json.dumps(shot.get('graphics',[]),ensure_ascii=False)}."
    first = clip['shots'][0]['shot_id']
    last = clip['shots'][-1]['shot_id']
    result[first] += '\nOpening transition visual state: ' + clip['transition_in']
    result[last] += '\nClosing transition visual state: ' + clip['transition_out']
    if clip['execution_strategy']=='first_last':
        result['start_frame'] = result[clip['shots'][0]['shot_id']] + '\nPrecisely show the approved opening state: ' + clip['entry_state']
        result['end_frame'] = result[clip['shots'][-1]['shot_id']] + '\nPrecisely show the approved resolved state: ' + clip['exit_state']
    return result


def validate_source_video(path,state):
    info=media.probe_media(path)
    minimum=720 if state['target']['resolution']=='720p' else 1080
    if not info['has_video'] or min(info['width'],info['height'])<minimum:
        raise ValidationError('Provider returned a lower resolution than approved; do not upscale and claim matching quality')
    ratio=state['target']['width']/state['target']['height']
    if abs(info['width']/info['height']/ratio-1)>.02:
        raise ValidationError('Provider source aspect ratio differs from the approved composition')
