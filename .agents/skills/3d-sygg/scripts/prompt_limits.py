"""Evidence-backed prompt limits. Unknown is not passed; never truncate prompts."""
from __future__ import annotations

import copy
from datetime import date

try:
    from .providers import ValidationError
except ImportError:
    from providers import ValidationError


def validate_rule(rule):
    if rule is None:
        return None
    if not isinstance(rule, dict) or set(rule) - {'max_characters', 'max_utf8_bytes', 'source', 'checked_at'}:
        raise ValidationError('Prompt limit supports max_characters/max_utf8_bytes plus source and checked_at')
    bounds = [k for k in ('max_characters', 'max_utf8_bytes') if k in rule]
    if not bounds or any(type(rule[k]) is not int or rule[k] <= 0 for k in bounds):
        raise ValidationError('A documented prompt limit must be a positive integer with an explicit unit')
    if not isinstance(rule.get('source'), str) or not rule['source'].strip():
        raise ValidationError('Prompt limit needs its actual provider documentation source')
    try:
        date.fromisoformat(rule['checked_at'])
    except (KeyError, TypeError, ValueError):
        raise ValidationError('Prompt limit checked_at must be YYYY-MM-DD') from None
    return copy.deepcopy(rule)


def validate_routes(rules):
    if not isinstance(rules, dict):
        raise ValidationError('prompt_limits must map provider:model route IDs to documented limits')
    result = {}
    for route, rule in rules.items():
        if not isinstance(route, str) or ':' not in route or not all(route.split(':', 1)) or rule is None:
            raise ValidationError('Each prompt limit requires a concrete provider:model route and rule')
        result[route] = validate_rule(rule)
    return result


def check(prompt, rule=None, *, label='prompt'):
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError(f'{label} must be nonempty text')
    metrics = {'characters': len(prompt), 'utf8_bytes': len(prompt.encode('utf-8'))}
    rule = validate_rule(rule)
    if rule:
        for key, metric in (('max_characters', 'characters'), ('max_utf8_bytes', 'utf8_bytes')):
            if key in rule and metrics[metric] > rule[key]:
                raise ValidationError(f'{label} exceeds documented {key}: {metrics[metric]} > {rule[key]}. '
                    '提交前精简重复描述并保留镜头、动作、图文、台词；不得截断或自动增加生成段数。')
    return {**metrics, 'status': 'within_documented_limit' if rule else 'limit_unknown', 'limit': rule}


def inspect_project(state):
    contract = state['provider_contract']
    rules = validate_routes(contract.get('prompt_limits', {}))
    routes = [contract['route_id']]
    fallback = contract.get('fallback', {})
    if fallback.get('enabled'):
        routes.append(f"{fallback['provider']}:{fallback['model']}")
    report = {}
    for c in state['clips']:
        for route in routes:
            label = f"video:{c['index']}:{route}"
            actual = c['omni_prompt'] if contract['capabilities']['family'] == 'omni' else c['video_prompt']
            report[label] = check(actual, rules.get(route), label=label)
    image_config = state['reference_asset_plan'].get('image_config', {})
    rule = image_config.get('prompt_limit')
    prompts = {'product_master': state['product_master_prompt']}
    if state.get('talent_reference_prompt'):
        prompts['talent'] = state['talent_reference_prompt']
    for c in state['clips']:
        prompts[f"storyboard:{c['index']}"] = c['storyboard_prompt']
        for key, value in c.get('frame_prompts', {}).items():
            asset = f"keyframe:{c['index']}:{key}" if key not in ('start_frame', 'end_frame') else f"{key}:{c['index']}"
            prompts[asset] = value
    for asset in state['reference_asset_plan']['generation_order']:
        report['image:' + asset] = check(prompts[asset], rule, label='image:' + asset)
    return report


def render(state):
    if not state['plan'].get('prompt_design_version'):
        return ''
    report = inspect_project(state)
    unknown = sum(r['status'] == 'limit_unknown' for r in report.values())
    maximum = max((r['characters'] for r in report.values()), default=0)
    return (f'\n提示词检查：最长 {maximum} 字符；{unknown} 项通道上限未确认，'
            '其余已按已登记的文档限制检查。未确认不代表长度已获服务商保证；详见 prompt-check.json。\n\n')
