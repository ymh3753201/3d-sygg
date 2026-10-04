"""Route contracts, shot-preserving planning and local qualification evidence.

Documentation support is deliberately separate from successful visual qualification.
No network requests or credential reads occur while planning.
"""
from __future__ import annotations
import copy
import json
import math
from pathlib import Path
try:
    from .providers import ValidationError, canonical_hash, atomic_write_json, sha256_file
except ImportError:
    from providers import ValidationError, canonical_hash, atomic_write_json, sha256_file

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config' / 'config.local.json'
STRATEGIES = ('full_storyboard', 'ordered_keyframes', 'first_last', 'per_shot')
SOURCES = {
    'omni': 'https://deepmind.google/models/gemini-omni/prompt-guide/',
    'seedance-2.0': 'https://seed.bytedance.com/en/blog/seedance-2-0-official-launch',
    'seedance-2.5': 'https://docs.volcengine.com/docs/ark/seedance-2-5-prompt-guide?lang=zh',
    'minimax-h3': 'https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md',
}
ROUTES = {
    'omni': dict(family='omni', provider='wxart', model='omni-flash', minimum=10, maximum=10,
                 resolutions=['720p'], images=3, videos=0, audios=0, generate_audio=False,
                 documented=['full_storyboard', 'per_shot'], verified=['full_storyboard', 'per_shot']),
    'sd11-seedance-2.0': dict(family='seedance-2.0', provider='cangyuan', model='sd11-seedance-2.0',
                 minimum=4, maximum=15, resolutions=['480p','720p','1080p'], images=9, videos=3, audios=3,
                 generate_audio=True, documented=list(STRATEGIES), verified=[]),
    'sd11-seedance-2.5': dict(family='seedance-2.5', provider='cangyuan', model='sd11-seedance-2.5',
                 minimum=4, maximum=30, resolutions=['480p','720p','1080p'], images=30, videos=10, audios=10,
                 generate_audio=True, documented=list(STRATEGIES), verified=[]),
    'mm2-minimax-h3': dict(family='minimax-h3', provider='cangyuan', model='mm2-minimax-h3',
                 minimum=4, maximum=15, resolutions=['768P','2K'], images=9, videos=3, audios=3,
                 generate_audio=False, documented=list(STRATEGIES), verified=[]),
}
ALIASES = {'omni-flash':'omni', 'omni-fast-no-water':'omni', 'Omini':'omni',
           'seedance-2.0':'sd11-seedance-2.0','seedance-2.5':'sd11-seedance-2.5','minimax-h3':'mm2-minimax-h3'}

def route(model='omni', provider='auto'):
    name = ALIASES.get(model, model)
    if name not in ROUTES:
        raise ValidationError('Unknown model route; inspect capabilities. No automatic model substitution.')
    value = copy.deepcopy(ROUTES[name])
    if provider not in ('auto', value['provider'], 'cangyuan' if name == 'omni' else value['provider']):
        raise ValidationError('This model is not configured on the selected provider')
    if name == 'omni' and provider == 'cangyuan':
        value.update(provider='cangyuan', model='omni-fast-no-water')
    value.update(route_id=f"{value['provider']}:{value['model']}", revision='2026-09-29.1',
                 source=SOURCES[value['family']], native_extend=False,
                 reference_video_verified=False, continuity_verified=False,
                 channel_source=('https://ai.cangyuansuanli.cn/docs/models/' + value['model'])
                    if value['provider']=='cangyuan' else None)
    return value

def read_config(path=CONFIG):
    if not path.exists():
        return {'model':'omni', 'provider':'auto', 'output_resolution':'720p', 'strategy':'auto'}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or any(k not in {'model','provider','output_resolution','strategy'} for k in value):
        raise ValidationError('Configuration supports model, provider, output_resolution and strategy only; never store keys')
    route(value.get('model','omni'), value.get('provider','auto'))
    return value

def configure(model, provider='auto', output_resolution='720p', strategy='auto', path=CONFIG):
    cap = route(model, provider)
    source_resolution(cap, output_resolution)
    if strategy not in ('auto', *STRATEGIES):
        raise ValidationError('Unknown storyboard strategy')
    value = dict(model=model, provider=provider, output_resolution=output_resolution, strategy=strategy)
    atomic_write_json(path, value)
    return value

def source_resolution(cap, output):
    if output not in ('720p','1080p'):
        raise ValidationError('Output resolution must be 720p or 1080p')
    selected = ('768P' if output == '720p' else '2K') if cap['family']=='minimax-h3' else output
    if selected not in cap['resolutions']:
        raise ValidationError('This route cannot supply the requested quality; choose an explicit capable model')
    return selected

def frames(seconds):
    if isinstance(seconds, bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or seconds<=0:
        raise ValidationError('Duration must be explicitly planned as a positive finite number')
    value = round(seconds*30)
    if value < 1:
        raise ValidationError('Duration must be at least one frame')
    return value

def verified_strategies(cap, evidence_paths=()):
    verified = set(cap['verified'])
    for p in evidence_paths:
        try:
            project = json.loads((Path(p)/'project.json').read_text())
            qa = json.loads((Path(p)/'qa/qa-report.json').read_text())
            contract = project['provider_contract']
            if (project['state'] != 'delivered' or contract.get('route_id') != cap['route_id']
                    or contract.get('revision') != cap['revision'] or qa.get('creative_review_status') != 'pass'
                    or project.get('clip_replacements')
                    or qa.get('technical_status') != 'pass'
                    or sha256_file(Path(project['artifacts']['final_video'])) != qa['final_sha256']):
                raise ValidationError('Qualification needs an unchanged, delivered, visually reviewed film on this exact route')
            try:
                from .commercial_ad import _plan_contract, _reference_contract
            except ImportError:
                from commercial_ad import _plan_contract, _reference_contract
            if (project['approvals']['plan']!=canonical_hash(_plan_contract(project))
                    or project['approvals']['references']!=canonical_hash(_reference_contract(project))):
                raise ValidationError('Qualification project approval changed')
            for clip in project['clips']:
                strategy = clip['execution_strategy']
                if strategy == 'full_storyboard':
                    # One image proves single-shot execution, not grid reading order.
                    # Reuse this evidence for per-shot editing without another paid test.
                    verified.add('per_shot')
                    if len(clip['shots']) > 1:
                        verified.add('full_storyboard')
                else:
                    verified.add(strategy)
            if any(c.get('continuity_from') for c in project['clips']):
                cap['continuity_verified']=True
        except (OSError, KeyError, ValueError) as exc:
            raise ValidationError('Cannot read qualification project evidence') from exc
    return sorted(verified)

def prepare_timeline(plan, target_duration, cap, strategy='auto', validation_run=False):
    """Use director clip durations when provided, otherwise distribute across planned clips.
    Never fabricate extra shots to fill the maximum duration of a model.
    """
    plan = copy.deepcopy(plan)
    requested = target_duration if target_duration is not None else plan.get('target_duration')
    if requested is None:
        if not plan.get('duration_reason') or not all('keep_duration' in c for c in plan.get('clips',[])):
            raise ValidationError('Target duration must be explicitly planned: director must propose target_duration and duration_reason based on story/voice')
        requested = sum(c['keep_duration'] for c in plan['clips']) - sum(c.get('overlap_before',0) for c in plan['clips'])
    if target_duration is None and not plan.get('duration_reason'):
        raise ValidationError('An automatically planned duration requires duration_reason')
    total_frames = frames(requested)
    raw_clips=plan.get('clips')
    if not isinstance(raw_clips,list) or not raw_clips or any(not isinstance(c,dict) or type(c.get('index')) is not int for c in raw_clips):
        raise ValidationError('Plan clips must be indexed objects')
    if sorted(c['index'] for c in raw_clips)!=list(range(1,len(raw_clips)+1)):
        raise ValidationError('Plan clip indexes must be contiguous from one')
    clips = sorted(raw_clips, key=lambda c:c['index'])
    if not clips:
        raise ValidationError('Plan the complete story before dividing requests')
    verified = verified_strategies(cap, plan.get('qualification_projects', []))
    plan['qualification_snapshot'] = verified
    seen = set()
    for c in clips:
        selected = c.get('execution_strategy', strategy)
        if selected == 'auto':
            selected = next((s for s in ('full_storyboard','ordered_keyframes','per_shot','first_last') if s in verified), None)
            if selected is None:
                if validation_run:
                    selected = 'full_storyboard'
                else:
                    raise ValidationError('Route is documented but not visually qualified. Prepare a bounded validation_run first.')
        if selected not in cap['documented']:
            raise ValidationError('Strategy has no documented support on this route')
        single_board = selected == 'full_storyboard' and len(c.get('shots', [])) == 1 and 'per_shot' in verified
        if selected not in verified and not single_board and not validation_run:
            raise ValidationError('Untested strategy requires a separately approved validation_run')
        c['execution_strategy'] = selected
        for j,s in enumerate(c.get('shots',[]),1):
            sid = s.setdefault('shot_id',f"C{c['index']:04d}-S{j:02d}")
            if not isinstance(sid,str) or not sid.strip() or sid in seen:
                raise ValidationError('Shot IDs must be nonempty and unique throughout the advertisement')
            seen.add(sid)
    overlaps = [0 if i==0 else round(c.get('overlap_before',0)*30) for i,c in enumerate(clips)]
    if clips[0].get('overlap_before',0) or any(o<0 for o in overlaps):
        raise ValidationError('First clip has no overlap; overlaps cannot be negative')
    available = total_frames + sum(overlaps)
    if any('keep_duration' in c for c in clips):
        if not all('keep_duration' in c for c in clips):
            raise ValidationError('Provide keep_duration for every clip or none')
        durations = [frames(c['keep_duration']) for c in clips]
        if sum(durations) != available:
            raise ValidationError('Clip frames minus transition overlap must equal the final duration')
    else:
        base=math.floor(available/30/len(clips))*30
        if base>0 and cap['family']=='omni' and not any(overlaps):
            durations=[base+min(30,max(0,available-base*len(clips)-i*30)) for i in range(len(clips))]
        else:
            durations = [available//len(clips) + (i<available%len(clips)) for i in range(len(clips))]
    expanded=[]
    for c,n,o in zip(clips,durations,overlaps):
        if c['execution_strategy']=='per_shot' and len(c['shots'])>1:
            weights=[float(s.get('duration_weight',1)) for s in c['shots']]
            if any(not math.isfinite(w) or w<=0 for w in weights):
                raise ValidationError('Shot weights must be positive')
            prior=0
            for j,s in enumerate(c['shots']):
                end=round(n*sum(weights[:j+1])/sum(weights))
                item=copy.deepcopy(c)
                item.update(shots=[s],narrative_role=f"{c['narrative_role']} / {s['shot_id']}",
                            director_clip_index=c['index'], overlap_frames=o if j==0 else 0,
                            keep_frames=end-prior, camera=s['camera'], visual_progression=s['visual'],
                            entry_state=c.get('entry_state', s['visual']) if j==0 else s.get('entry_state', s['visual']),
                            exit_state=c.get('exit_state', s['action']) if j==len(c['shots'])-1 else s.get('exit_state', s['action']),
                            transition_in=c['transition_in'] if j==0 else c['shots'][j-1]['transition'],
                            transition_out=c['transition_out'] if j==len(c['shots'])-1 else s['transition'])
                expanded.append(item); prior=end
        else:
            c.update(keep_frames=n,overlap_frames=o)
            expanded.append(c)
    cursor=0
    for i,c in enumerate(expanded,1):
        n,o=c['keep_frames'],c['overlap_frames']
        if n<=o or n<1 or (i>1 and o>=expanded[i-2]['keep_frames']):
            raise ValidationError('Overlap must be shorter than both adjacent clips')
        trim=round(c.get('trim_start',0)*30)
        if cap['family']=='omni' and trim:
            raise ValidationError('Omni preserves its original action timing; plan trim_start=0')
        if trim<0:
            raise ValidationError('trim_start cannot be negative')
        upstream=max(cap['minimum'],math.ceil((n+trim)/30))
        if upstream>cap['maximum']:
            raise ValidationError(f"Director segment exceeds {cap['maximum']}s route limit. Split its shot sequence into approved segments.")
        c.update(index=i,keep_duration=n/30,trim_start=trim/30,overlap_before=o/30,
                 upstream_duration=upstream, global_start=(cursor-o)/30,global_end=(cursor-o+n)/30)
        dependency=c.get('continuity_from')
        if dependency is not None:
            if type(dependency) is not int or dependency!=i-1 or i==1 or trim:
                raise ValidationError('Continuity must reference the preceding clip, with trim_start=0')
            if c['execution_strategy']!='first_last':
                raise ValidationError('Tail-frame continuity requires first_last strategy')
            if cap['family']=='omni' or (not cap['continuity_verified'] and not validation_run):
                raise ValidationError('Tail-frame continuity is unverified; only a separately approved validation run may use it')
        cursor=cursor-o+n
    plan['director_clips']=copy.deepcopy(clips)
    plan['clips']=expanded
    plan['requested_duration']=requested
    plan['target_duration']=total_frames/30
    plan['validation_run']=bool(validation_run)
    return plan, [c['keep_duration'] for c in expanded]

def budget_contract(plan, count, narration_calls):
    retry=plan.get('recovery_policy',{}).get('max_video_retries_per_clip',1)
    limit=count*(retry+1)
    budget=copy.deepcopy(plan.get('budget',{}))
    budget.setdefault('max_video_attempts',limit)
    budget.setdefault('max_narration_attempts',narration_calls)
    for key, minimum in [('max_video_attempts',count),('max_narration_attempts',narration_calls)]:
        if type(budget[key]) is not int or budget[key]<minimum:
            raise ValidationError('Budget must cover the base approved calls: '+key)
    if budget['max_video_attempts']>limit:
        raise ValidationError('Video budget exceeds approved per-clip recovery limit')
    if 'max_cost_cny' in budget:
        for key in ('max_cost_cny','video_call_upper_cny','narration_call_upper_cny'):
            if isinstance(budget.get(key),bool) or not isinstance(budget.get(key),(int,float)) or not math.isfinite(budget[key]) or budget[key]<0:
                raise ValidationError('Money caps require nonnegative cost upper bounds for both video and narration')
        if not budget.get('quote_checked_at'):
            raise ValidationError('Record when provider price upper bounds were checked')
        if count*budget['video_call_upper_cny']+narration_calls*budget['narration_call_upper_cny']>budget['max_cost_cny']:
            raise ValidationError('Money cap does not cover base calls')
    if plan.get('validation_run') and 'max_cost_cny' not in budget:
        raise ValidationError('A paid validation plan requires explicit max_cost_cny and per-call upper quotes')
    return budget

def check_budget(records, record, budget):
    """Called under the ledger lock, immediately before recording any paid POST."""
    if not budget: return
    attempts=[r for r in records if r.get('event')=='attempted']
    video=sum(r.get('provider') in ('omni','cangyuan') for r in attempts)
    speech=sum(r.get('provider')=='minimax' for r in attempts)
    if record.get('provider') in ('omni','cangyuan'): video+=1
    elif record.get('provider')=='minimax': speech+=1
    if video>budget['max_video_attempts'] or speech>budget['max_narration_attempts']:
        raise ValidationError('Approved paid call cap exhausted; completed assets are retained')
    if 'max_cost_cny' in budget and video*budget['video_call_upper_cny']+speech*budget['narration_call_upper_cny']>budget['max_cost_cny']+1e-9:
        raise ValidationError('Approved money cap would be exceeded; no paid request sent')
