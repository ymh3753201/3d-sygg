"""Freeze audio intent for new projects without migrating approved legacy projects."""
from __future__ import annotations

import json

try:
    from .providers import ValidationError
except ImportError:
    from providers import ValidationError

LEGACY_CONSTRAINT = (
    "silent, no dialogue, no human voice, no speech, "
    "ambient SFX and mechanical sound effects only"
)


def normalize_plan(plan):
    audio = plan.setdefault('audio', {})
    if not isinstance(audio, dict):
        raise ValidationError('plan.audio must be an object')
    narration = plan.setdefault('narration', {'enabled': False, 'text': '',
        'reason': '声音由视频模型生成，未请求独立配音'})
    if not isinstance(narration, dict):
        raise ValidationError('plan.narration must be an object')
    requested = audio.get('user_requested_external') is True or narration.get('user_requested') is True
    audio.setdefault('mode', 'external' if requested else 'video')
    if audio['mode'] not in ('video', 'external'):
        raise ValidationError('plan.audio.mode must be video or external')
    if audio['mode'] == 'external':
        if not requested:
            raise ValidationError('独立语音模型仅在用户明确要求时使用；请记录 user_requested_external=true')
        if not narration.get('enabled') and not plan.get('replacement_for'):
            raise ValidationError('External audio requires enabled narration')
    elif narration.get('enabled'):
        raise ValidationError('默认由视频模型生成人声；只有用户明确要求独立配音才能启用 narration')
    audio.setdefault('speech', True)
    if not isinstance(audio['speech'], bool):
        raise ValidationError('plan.audio.speech must be a boolean')
    clips = plan.get('clips')
    if not isinstance(clips, list) or any(not isinstance(c, dict) for c in clips):
        raise ValidationError('plan.clips must be a list of objects')
    for clip in clips:
        value = clip.get('audio', {})
        if not isinstance(value, dict):
            raise ValidationError('clips[].audio must be an object')
        for key in ('speech_text', 'voice', 'music', 'ambience'):
            if key in value and not isinstance(value[key], str):
                raise ValidationError(f'clips[].audio.{key} must be text')
        if value.get('speech_text') and (audio['mode'] != 'video' or not audio['speech']):
            raise ValidationError('Native speech text conflicts with the approved audio route')


def native(plan):
    return plan.get('audio', {}).get('mode') == 'video'


def prompt(plan, clip):
    if plan.get('prompt_design_version') == 1:
        return compact_prompt(plan, clip)
    audio = plan.get('audio')
    if not audio:
        sound = str(clip.get('sfx', '')).replace(LEGACY_CONSTRAINT, '').strip(' .;。；')
        return f'SFX: {sound}. {LEGACY_CONSTRAINT}.'
    value = clip.get('audio', {})
    sound = str(clip.get('sfx', '')).replace(LEGACY_CONSTRAINT, '').strip(' .;。；')
    lines = ['Generate synchronized audio with the video: sound effects, background music and environmental ambience.',
        'Sound direction: ' + str(plan.get('audiovisual_tone', '')), 'SFX: ' + sound]
    for key, title in [('music', 'Music'), ('ambience', 'Ambience')]:
        if value.get(key):
            lines.append(title + ': ' + value[key])
    if audio['mode'] == 'external' or not audio['speech']:
        lines.append('No dialogue, no human voice, no speech; retain music, ambience and SFX.')
    elif value.get('speech_text'):
        trim = float(clip.get('trim_start', 0))
        start = trim + float(clip.get('overlap_frames', 0)) / 30
        end = trim + float(clip['keep_duration']) - max(float(clip.get('tail_margin', 0)), float(clip.get('next_overlap_frames', 0)) / 30)
        if end <= start:
            raise ValidationError('转场后没有足够的原生台词窗口；请调整台词或分段')
        lines += ['Generate the following approved spoken words exactly once in this segment; do not invent extra dialogue: '
            + json.dumps(value['speech_text'], ensure_ascii=False),
            'Voice and delivery: ' + value.get('voice', '自然清晰的中文广告人声，保持整片音色一致'),
            f'Complete all speech within [{start:.2f}-{end:.2f}s] so the retained edit does not cut words.',
            'Use off-screen narration unless the approved shot explicitly asks the visible adult to speak; '
            'in that case synchronize their lips. Keep speech intelligible above music and effects.']
    else:
        lines.append('This segment has no approved spoken words: do not invent dialogue. Retain the designed music, ambience and SFX.')
    return ' '.join(lines)


def compact_prompt(plan, clip):
    """Text-only audio direction; no reference files or extra service calls."""
    audio = plan['audio']
    value = {**audio.get('shared', {}), **clip.get('audio', {})}
    parts = ['Generate synchronized audio with the video.',
             'Overall sound: ' + plan['audiovisual_tone']]
    for key, title in (('music', 'Music'), ('ambience', 'Ambience')):
        if value.get(key):
            parts.append(title + ': ' + value[key])
    parts.append('Action SFX: ' + str(clip.get('sfx', '')).replace(LEGACY_CONSTRAINT, '').strip(' .;。；'))
    if audio['mode'] == 'external' or not audio['speech']:
        parts.append('No dialogue, no human voice, no speech; retain music, ambience and SFX.')
    elif value.get('speech_text'):
        start = float(clip.get('trim_start', 0)) + float(clip.get('overlap_frames', 0)) / 30
        end = float(clip.get('trim_start', 0)) + float(clip['keep_duration']) - max(
            float(clip.get('tail_margin', 0)), float(clip.get('next_overlap_frames', 0)) / 30)
        if end <= start:
            raise ValidationError('转场后没有足够的原生台词窗口；请调整台词或分段')
        parts.extend([
            f"Speaker {value.get('speaker_id', 'narrator')}: {value.get('voice', '')}",
            'Speak exactly once: ' + json.dumps(value['speech_text'], ensure_ascii=False),
            f'Complete all speech within [{start:.2f}-{end:.2f}s]; no extra words or cut-off syllables.',
            'Off-screen narration unless the approved shot explicitly requires visible speech; then synchronize lips. Keep speech clear above music and effects.'])
    else:
        parts.append('No approved spoken words in this segment; do not invent dialogue.')
    context = clip.get('sequence_context', {})
    if context.get('count', 1) > 1:
        parts.append('Across segments: same musical palette/tempo and acoustic space, except approved changes below.' +
                     (' Keep each speaker timbre/delivery consistent.' if audio['mode'] == 'video' and audio['speech'] else ''))
        if value.get('change_reason'):
            parts.append('Approved sound change: ' + value['change_reason'] + '; use the segment directions above for this change.')
        if context.get('previous_exit'):
            parts.append('Continue sound; no fresh intro or opening sting.')
        if context.get('next_entry'):
            parts.append('No musical finale or fade-out here unless designed.')
    if value.get('change_reason'):
        parts.append('Intentional local sound change: ' + value['change_reason'])
    return '\n'.join(parts)


def summary(plan):
    if native(plan):
        texts = [c.get('audio', {}).get('speech_text', '') for c in plan.get('clips', [])]
        return '视频模型生成人声、音效、背景音乐与环境音；独立语音调用 0 次。' + (
            '批准台词：' + ' / '.join(t for t in texts if t) if any(texts) else '本方案未安排台词。')
    n = plan.get('replacement_for', {}).get('narration', plan['narration'])
    return ('用户明确要求独立配音：' + n['text']) if n['enabled'] else '未启用独立配音：' + n.get('reason', '')
