"""Compact, shot-preserving direction for newly prepared short advertisements.

No model calls, semantic rewriting, audio uploads or additional reference assets.
The version marker leaves approved historical prompts and contracts untouched.
"""
from __future__ import annotations

import json
import re

try:
    from .providers import ValidationError
except ImportError:
    from providers import ValidationError

VERSION = 1


def enabled(plan):
    return plan.get('prompt_design_version') == VERSION


def lines(*parts):
    # Only deduplicate whole generated blocks; never alter quoted copy or dialogue.
    return '\n'.join(dict.fromkeys(str(p).strip() for p in parts if str(p).strip()))


def normalize(plan, *, previous_exit=None):
    """Freeze shared sound and adjacent visual states after timeline expansion."""
    audio = plan['audio']
    shared = audio.setdefault('shared', {})
    if not isinstance(shared, dict) or set(shared) - {'voice', 'music', 'ambience'}:
        raise ValidationError('audio.shared supports voice, music and ambience text')
    if any(not isinstance(v, str) or not v.strip() for v in shared.values()):
        raise ValidationError('Shared sound directions must be nonempty text')
    speakers = audio.setdefault('speakers', {})
    if not isinstance(speakers, dict) or any(not isinstance(k, str) or not k.strip()
            or not isinstance(v, str) or not v.strip() for k, v in speakers.items()):
        raise ValidationError('audio.speakers maps stable speaker IDs to voice descriptions')
    clips = plan['clips']
    # Existing single-voice plans can seed a shared direction without a new user step.
    for key in ('voice', 'music', 'ambience'):
        if key not in shared:
            candidates = [c.get('audio', {}).get(key) for c in clips
                          if key != 'voice' or c.get('audio', {}).get('speaker_id', 'narrator') == 'narrator']
            shared[key] = next((v for v in candidates if v),
                '自然清晰的中文广告人声，中等语速、克制表达' if key == 'voice' else
                '遵循全片声音方向，保持相同音乐风格与节奏' if key == 'music' else
                '遵循当前场景，保持环境底噪与空间混响协调')
    for i, clip in enumerate(clips):
        value = clip.setdefault('audio', {})
        speaker = value.get('speaker_id', 'narrator')
        if not isinstance(speaker, str) or not speaker.strip():
            raise ValidationError('clips[].audio.speaker_id must be nonempty text')
        if speaker != 'narrator' and speaker not in speakers:
            raise ValidationError('Define each named speaker once in audio.speakers')
        reason = value.get('change_reason', '')
        if not isinstance(reason, str):
            raise ValidationError('audio.change_reason must be text')
        for key in ('voice', 'music', 'ambience'):
            expected = speakers.get(speaker, shared['voice']) if key == 'voice' else shared[key]
            actual = value.get(key) or expected
            if actual != expected and not reason.strip():
                raise ValidationError(f'第 {i+1} 段 {key} 与全片设定不同；统一描述，或用 audio.change_reason 说明有意变化')
            value[key] = actual
        value['speaker_id'] = speaker
        for key in ('entry_state', 'exit_state'):
            if key in clip and (not isinstance(clip[key], str) or not clip[key].strip()):
                raise ValidationError(f'{key} must describe a nonempty visual state')
        clip.setdefault('exit_state', clip['shots'][-1]['action'])
        mode = clip.setdefault('transition_mode', 'cut')
        if mode not in ('cut', 'match_action'):
            raise ValidationError('transition_mode must be cut or match_action')
        if mode == 'match_action':
            if i == 0 and previous_exit is None:
                raise ValidationError('The first segment has no preceding action to match')
            boundary = clips[i-1]['exit_state'] if i else previous_exit
            if clip.get('entry_state', boundary) != boundary:
                raise ValidationError('连续动作须共用同一边界状态：省略本段 entry_state 以继承前段 exit_state，或统一两者')
            clip['entry_state'] = boundary
        else:
            clip.setdefault('entry_state', clip['shots'][0]['visual'])
    for i, clip in enumerate(clips):
        clip['sequence_context'] = {
            'segment': i + 1, 'count': len(clips),
            'previous_exit': clips[i-1]['exit_state'] if i else '',
            'next_entry': clips[i+1]['entry_state'] if i+1 < len(clips) else '',
        }


def boundaries(clip):
    context = clip.get('sequence_context', {})
    count = context.get('count', 1)
    parts = [f"Entry state: {clip.get('entry_state', clip['shots'][0]['visual'])}",
             f"Exit state: {clip.get('exit_state', clip['shots'][-1]['action'])}",
             f"Transitions: in={clip['transition_in']}; out={clip['transition_out']}"]
    if count > 1:
        parts.insert(0, f"Segment {context['segment']}/{count} of one ad: {clip['narrative_role']}.")
        if context.get('previous_exit'):
            parts.append('Context only: previous segment ends at ' + context['previous_exit'])
            parts.append('Continue the story; do not replay completed actions.')
        if context.get('next_entry'):
            parts.append('Context only: next segment starts at ' + context['next_entry'])
            parts.append('Leave the transition open; no extra shot or finale.')
        parts.append('Match grip, object state, position and motion direction.'
                     if clip.get('transition_mode') == 'match_action' else
                     'Use the planned editorial cut; preserve identity/style, allow framing changes.')
    return lines(*parts)


def identity(builder, clip):
    plan, analysis = builder.plan, builder.analysis
    talent = plan['talent_strategy']
    human = talent['mode'] == 'human_interaction' and bool(clip.get('talent_action'))
    cast = (f"Same clearly adult person; {talent['persona']}; wardrobe {talent['wardrobe']}; "
            f"grooming {talent['grooming']}; identity {talent['identity_anchor']}; action {clip['talent_action']}."
            if human else 'Pure product; no person, face, hands or body parts.')
    return lines(
        f"Product: {analysis['product_type']}; preserve reference geometry, material, Logo and label placement; branding: {analysis.get('logo_text') or 'do not invent marks'}.",
        f"Art direction: {plan['style']}; {plan['global_anchor']}; palette " + json.dumps(plan['palette'], ensure_ascii=False, separators=(',', ':')),
        cast)


def beats(clip, *, timed):
    result = []
    offset = clip.get('trim_start', 0) if timed else 0
    details = [s['product_detail'] for s in clip['shots']]
    shared = details[0] if len(set(details)) == 1 else None
    if shared:
        result.append('Shared product detail: ' + shared)
    for i, s in enumerate(clip['shots'], 1):
        prefix = f"[{s['start']+offset:.2f}-{s['end']+offset:.2f}s] " if timed else ''
        body = f"[Shot {s.get('shot_id', i)}] {prefix}{s['visual']}; action: {s['action']}; camera: {s['camera']}; transition: {s['transition']}"
        if not shared:
            body += '; detail: ' + s['product_detail']
        if s.get('graphics'):
            body += '; graphics: ' + json.dumps(s['graphics'], ensure_ascii=False, separators=(',', ':'))
        result.append(body)
    return lines(*result)


def storyboard(builder, clip):
    n = clip['storyboard_panel_count']
    layout = ('独立完整画面，不要分格' if n == 1 else
              f'{n} 个连续画面，单行从左到右' if n < 4 else
              '4 个连续画面，2×2，左上、右上、左下、右下')
    return lines(
        f"生成干净的商业广告分镜图：{layout}；构图适配成片 {builder.aspect_ratio}。",
        'Use the approved product master and applicable adult talent reference as identity inputs.',
        identity(builder, clip), boundaries(clip),
        f"Scene progression: {clip['visual_progression']}; camera direction: {clip['camera']}",
        beats(clip, timed=False),
        'Approved text inventory: ' + json.dumps(builder.plan.get('ad_copy', []), ensure_ascii=False) + '; render only where the shot design places it.',
        'Draw each designed composition, action phase and graphic effect visibly; varied shot sizes, not repeated poses or all actions already finished.',
        'Keep printed branding attached and unchanged. Render approved graphic text only where designed, not on every shot.',
        'No shot labels, timecodes, director notes or narration subtitles. Premium material and lighting; ' +
        ('35mm cinematic film texture.' if builder.plan['style'] == 'cinematic_neo_noir' else 'Octane Render art direction.'),
        'This storyboard is uploaded to Omni unchanged as one temporal reference.'
        if builder.plan.get('prompt_model_family') == 'omni' else
        'This storyboard is uploaded unchanged as one temporal reference.')


def video(builder, clip, cap, audio_prompt):
    mode = clip['execution_strategy']
    refs = []
    n = len(clip['shots'])
    if mode in ('full_storyboard', 'per_shot'):
        meanings = {'storyboard': f"story order and composition, {n} beats read {clip['storyboard_reading_order']}",
                    'product_master': 'product identity only; not scene or camera',
                    'talent': 'adult identity and wardrobe only; not pose or scene'}
        refs = [f'Image{i}: {meanings[role]}.' for i, role in enumerate(clip['execution_reference_roles'], 1)]
        refs.append('References are not literal initial frames. Image1 controls compositions, action phases and effects; identity refs must not override it.')
    elif mode == 'ordered_keyframes':
        offset = clip.get('trim_start', 0)
        refs = [f"Image {i}: independent keyframe for {s['shot_id']} at [{s['start']+offset:.2f}-{s['end']+offset:.2f}s]." for i, s in enumerate(clip['shots'], 1)]
        refs.append(f'Image{n+1}: product identity only.')
        if clip.get('talent_action'):
            refs.append(f'Image{n+2}: adult identity only.')
    else:
        refs.append('first_image_url: opening state; last_image_url: final state; other images: product/adult identity only.')
    finish = clip['keep_duration'] + clip.get('trim_start', 0)
    prompt = lines(
        f'Create a premium {builder.aspect_ratio} commercial. ' + ' '.join(refs),
        identity(builder, clip), boundaries(clip),
        f"Scene progression: {clip['visual_progression']}; camera direction: {clip['camera']}",
        'In a single unbroken scene; no scene cuts.' if n == 1 else f'Follow exactly {n} sequential shots in timecode order.',
        beats(clip, timed=True),
        'Approved text inventory: ' + json.dumps(builder.plan.get('ad_copy', []), ensure_ascii=False) + '; render only where the shot design places it.',
        f"Resolve action by {clip['shots'][-1]['end']+clip.get('trim_start',0):.2f}s; continue exit state through the cut at {finish:.2f}s and generated tail.",
        'Animate shots/graphics, not still panels. Preserve packaging text. Graphics belong to assigned shots, not on every shot.',
        'No invented parts/claims, narration subtitles, unrelated text, watermark, static slideshow, split screen, storyboard grid, borders, shot labels or timecodes.',
        audio_prompt)
    count = len(clip['execution_reference_roles']) if mode in ('full_storyboard', 'per_shot') else (
        n + 1 + bool(clip.get('talent_action')) if mode == 'ordered_keyframes' else 3 + bool(clip.get('talent_action')))
    if any(not 1 <= int(i) <= count for i in re.findall(r'\bImage\s*(\d+)\b', prompt)) or '<IMAGE_REF_' in prompt:
        raise ValidationError('Video prompt contains an unbound or unsupported image reference')
    return prompt


def summary(state):
    if not enabled(state['plan']):
        return ''
    audio = state['plan']['audio']
    shared = {k: v for k, v in audio['shared'].items() if k != 'voice' or (audio['mode'] == 'video' and audio['speech'])}
    parts = ['\n## 全片一致性安排\n',
             f"- 按脚本生成 {len(state['clips'])} 段；每段使用对应分镜，不额外生成声音参考文件。",
             f"- 统一声音：{json.dumps(shared, ensure_ascii=False)}",
             '- 提示词约束用于减少差异，不等于实际声画完全一致；最后整片观看试听。']
    if audio.get('speakers') and audio['mode'] == 'video' and audio['speech']:
        parts.append('- 角色音色：' + json.dumps(audio['speakers'], ensure_ascii=False))
    for c in state['clips']:
        parts.append(f"- 第 {c['index']} 段：入场 {c['entry_state']}；出场 {c['exit_state']}；衔接 {c.get('transition_mode', 'cut')}。")
        if c.get('audio', {}).get('change_reason'):
            parts.append(f"  声音有意变化：{c['audio']['change_reason']}")
    return '\n'.join(parts) + '\n\n'
