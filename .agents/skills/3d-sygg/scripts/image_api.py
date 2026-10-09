#!/usr/bin/env python3
"""Document-driven image APIs, project-bound provenance and paid-call recovery.

Uses only the Python standard library. Does not replace Codex's built-in tool.
"""
from __future__ import annotations

import argparse
import base64
import copy
import getpass
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

try:
    from .providers import (ValidationError, ProviderError, SubmissionUnknown, PaidRequestBlocked,
        Keychain, CREDENTIAL_ENV_NAMES, CREDENTIAL_SOURCE_PATH_ENV, CREDENTIAL_SOURCE_FILES,
        atomic_write_json, canonical_hash, sha256_file, validate_reference_image, file_lock)
    from .media import probe_media
except ImportError:
    from providers import (ValidationError, ProviderError, SubmissionUnknown, PaidRequestBlocked,
        Keychain, CREDENTIAL_ENV_NAMES, CREDENTIAL_SOURCE_PATH_ENV, CREDENTIAL_SOURCE_FILES,
        atomic_write_json, canonical_hash, sha256_file, validate_reference_image, file_lock)
    from media import probe_media

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config/image.local.json'
MAX_BYTES = 50 * 1024 * 1024
PRESETS = {
    'openai': {'protocol': 'multipart', 'generate_path': '/images/generations',
        'edit_path': '/images/edits', 'image_field': 'image[]', 'extra_body': {'n': 1}},
    'seedream': {'protocol': 'json', 'generate_path': '/images/generations',
        'edit_path': '/images/generations', 'image_field': 'image',
        'extra_body': {'response_format': 'b64_json', 'stream': False,
                       'sequential_image_generation': 'disabled', 'watermark': False}},
}


def get_path(value, path):
    """Dot paths support both objects and array indices, e.g. data.0.url."""
    try:
        for part in path.split('.'):
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def set_path(value, path, item):
    parts = path.split('.')
    for part in parts[:-1]:
        value = value.setdefault(part, {})
    value[parts[-1]] = item


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    local = parsed.hostname in {'localhost', '127.0.0.1', '::1'}
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or (parsed.scheme != 'https' and not (local and parsed.scheme == 'http'))):
        raise ValidationError('Image API requires HTTPS without embedded credentials (localhost HTTP for tests)')
    return url


def endpoint(config, path):
    # Paths stay on the configured service; never send its Key to another host.
    if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '?' in path or '#' in path:
        raise ValidationError('API paths must start with / and contain no host, query credentials or fragment')
    return config['base_url'].rstrip('/') + path


def validate_config(value):
    if not isinstance(value, dict):
        raise ValidationError('Image configuration must be an object')
    allowed = {'provider', 'base_url', 'model', 'protocol', 'generate_path', 'edit_path',
        'image_field', 'model_field', 'prompt_field', 'size', 'size_field', 'extra_body',
        'response', 'async', 'key_env', 'auth_header', 'auth_prefix', 'documentation',
        'max_reference_images', 'timeout', 'max_attempts_per_asset', 'unit_cost_upper_bound', 'price_checked_at'}
    if set(value) - allowed:
        raise ValidationError('Unknown image configuration fields; never store a Key in configuration')
    config = copy.deepcopy(value)
    if config.get('provider') == 'builtin':
        if set(config) != {'provider'}:
            raise ValidationError('Built-in image configuration needs only provider=builtin')
        return config
    for key in ('provider', 'base_url', 'model', 'documentation', 'size'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValidationError(f'Image configuration requires {key} from the service documentation')
        if 'YOUR-' in config[key] or 'ACTUAL-' in config[key]:
            raise ValidationError(f'Replace the {key} placeholder with the actual documented value')
    validate_url(config['base_url'])
    if urllib.parse.urlsplit(config['base_url']).query:
        raise ValidationError('base_url must not contain query parameters')
    if config.get('protocol') not in {'json', 'multipart'}:
        raise ValidationError('Supported protocols: json / multipart; other protocols need a documented adapter')
    config.setdefault('edit_path', config.get('generate_path'))
    for key in ('generate_path', 'edit_path'):
        endpoint(config, config.get(key))
    config.setdefault('key_env', 'IMAGE_API_KEY')
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', config['key_env']):
        raise ValidationError('key_env must name an environment variable, never contain the actual Key')
    config.setdefault('auth_header', 'Authorization')
    config.setdefault('auth_prefix', 'Bearer ')
    if not re.fullmatch(r'[A-Za-z0-9-]+', config['auth_header']) or config['auth_prefix'] not in {'Bearer ', ''}:
        raise ValidationError('Use Authorization: Bearer or a documented API-key header')
    for key, default in (('image_field', 'image'), ('model_field', 'model'),
                         ('prompt_field', 'prompt'), ('size_field', 'size')):
        config.setdefault(key, default)
        if not re.fullmatch(r'[A-Za-z0-9_.\[\]-]+', config[key]):
            raise ValidationError(f'Invalid request field: {key}')
    config.setdefault('extra_body', {})
    if not isinstance(config['extra_body'], dict):
        raise ValidationError('extra_body must be an object')
    def check_extra(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if re.search(r'key|token|secret|authorization|password', k, re.I):
                    raise ValidationError('Do not store credentials in extra_body')
                check_extra(v)
        elif isinstance(node, list):
            for v in node: check_extra(v)
    check_extra(config['extra_body'])
    if config['extra_body'].get('stream') is True or config['extra_body'].get('n', 1) != 1:
        raise ValidationError('One non-streaming candidate per request is required')
    if config['extra_body'].get('sequential_image_generation', 'disabled') != 'disabled':
        raise ValidationError('Automatic image sets exceed the one-candidate budget')
    config.setdefault('response', {'b64_path': 'data.0.b64_json', 'url_path': 'data.0.url'})
    if not isinstance(config['response'], dict) or set(config['response']) - {'b64_path', 'url_path'}:
        raise ValidationError('response supports b64_path / url_path')
    if not any(config['response'].values()) or any(not isinstance(v, str) for v in config['response'].values()):
        raise ValidationError('Specify documented image response paths')
    for key, default, maximum in (('max_reference_images', 14, 100), ('timeout', 180, 600),
                                 ('max_attempts_per_asset', 2, 2)):
        config.setdefault(key, default)
        if type(config[key]) is not int or not 1 <= config[key] <= maximum:
            raise ValidationError(f'{key} must be an integer between 1 and {maximum}')
    if 'async' in config:
        a = config['async']
        if not isinstance(a, dict) or set(a) != {'id_path', 'poll_path', 'status_path', 'success', 'failure'}:
            raise ValidationError('async needs id_path, poll_path, status_path, success and failure')
        if not all(isinstance(a[k], str) and a[k] for k in ('id_path', 'poll_path', 'status_path')):
            raise ValidationError('Invalid async paths')
        if '{task_id}' not in a['poll_path']:
            raise ValidationError('poll_path needs {task_id}')
        endpoint(config, a['poll_path'])
        if any(not isinstance(a[k], list) or not a[k] or any(not isinstance(v, str) for v in a[k])
               for k in ('success', 'failure')) or set(a['success']) & set(a['failure']):
            raise ValidationError('Async success/failure must be disjoint lists of documented states')
    if 'unit_cost_upper_bound' in config:
        cost = config['unit_cost_upper_bound']
        if type(cost) not in (int, float) or cost <= 0 or not config.get('price_checked_at'):
            raise ValidationError('Price upper bound must be positive with a checked-at date')
    return config


def read_config(path=None):
    path = CONFIG if path is None else Path(path)
    return validate_config(json.loads(path.read_text())) if path.exists() else {'provider': 'builtin'}


def render_contract(state):
    plan = state['reference_asset_plan']
    config = plan.get('image_config')
    if config is None: return ''  # Preserve old approved-project reports.
    count = plan['minimum_imagegen_calls']
    maximum = plan['maximum_image_calls']
    name = 'Codex 内置生图' if config['provider'] == 'builtin' else f"{config['provider']} / {config['model']} / {config['size']}"
    cost = (f"单次收费上界 {config['unit_cost_upper_bound']}；核对日期 {config['price_checked_at']}；最多 {maximum * config['unit_cost_upper_bound']:.2f}（同报价币种）"
            if 'unit_cost_upper_bound' in config else '金额未核对，正式生图前按账号报价说明；调用次数受控')
    upload = ('原商品照片通过该图片 API 的参考图输入直接传给所选图片服务，后续图使用生成母版与适用人物图；不公开原图 URL。'
              if config['provider'] != 'builtin' else '原商品照片只作为内置生图输入。')
    return f'图片服务：{name}\n\n生图基础 {count} 次；含每资产最多一次针对性修正，上限 {maximum} 次。{cost}。\n\n{upload}\n\n'


def key_service(config):
    # Separate credentials per configured host+provider, so switching services cannot reuse a stored Key.
    return '3d-sygg-image-' + canonical_hash([config['base_url'], config['provider']])[:16]


def load_key(config):
    service = key_service(config)
    CREDENTIAL_ENV_NAMES[service] = (config['key_env'],)
    CREDENTIAL_SOURCE_PATH_ENV[service] = 'SYGG_IMAGE_KEY_SOURCE'
    CREDENTIAL_SOURCE_FILES[service] = ()
    return Keychain.load(service)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ImageClient:
    def __init__(self, config, key):
        self.config = validate_config(config)
        self.key = key

    def request(self, url, *, data=None, content_type=None, authenticated=True):
        validate_url(url)
        headers = {'User-Agent': '3d-sygg/2.3.0'}
        if authenticated:
            headers[self.config['auth_header']] = self.config['auth_prefix'] + self.key
        if content_type: headers['Content-Type'] = content_type
        request = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.config['timeout']) as response:
                body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ProviderError('Image response exceeds the size limit')
            return body
        except urllib.error.HTTPError as exc:
            # Do not print service response bodies: they may echo secrets or image data.
            code = exc.code
            exc.close()
            if data is not None and (code >= 500 or 300 <= code < 400 or code == 408):
                raise SubmissionUnknown(f'Image submission HTTP {code}; retain the original request') from None
            raise ProviderError(f'Image API HTTP {code}', status_code=code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if data is not None:
                raise SubmissionUnknown('Image submission interrupted; do not submit again') from None
            raise ProviderError('Image query/download interrupted; resume the existing request') from None

    def build_request(self, prompt, inputs):
        c = self.config
        if len(inputs) > c['max_reference_images']:
            raise ValidationError('Image API reference limit exceeded; never silently discard product photos')
        fields = copy.deepcopy(c['extra_body'])
        for key, value in ((c['model_field'], c['model']), (c['prompt_field'], prompt), (c['size_field'], c['size'])):
            set_path(fields, key, value)
        path = c['edit_path'] if inputs else c['generate_path']
        if not inputs or c['protocol'] == 'json':
            if inputs:
                images = []
                for p in inputs:
                    suffix = validate_reference_image(p)
                    mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[suffix]
                    images.append(f'data:{mime};base64,' + base64.b64encode(p.read_bytes()).decode('ascii'))
                set_path(fields, c['image_field'], images)
            body = json.dumps(fields, ensure_ascii=False).encode()
            content_type = 'application/json'
        else:
            boundary = 'sygg-' + uuid.uuid4().hex
            parts = []
            for name, value in fields.items():
                if not re.fullmatch(r'[A-Za-z0-9_.\[\]-]+', name):
                    raise ValidationError('Invalid multipart field name')
                text = value if isinstance(value, str) else json.dumps(value)
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{text}\r\n'.encode())
            for i, p in enumerate(inputs):
                suffix = validate_reference_image(p)
                mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.webp': 'image/webp'}[suffix]
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{c["image_field"]}"; filename="reference-{i}{suffix}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + p.read_bytes() + b'\r\n')
            body = b''.join(parts) + f'--{boundary}--\r\n'.encode()
            content_type = f'multipart/form-data; boundary={boundary}'
        if len(body) > MAX_BYTES:
            raise ValidationError('Image request exceeds the size limit')
        return endpoint(c, path), body, content_type

    def image_bytes(self, response):
        for kind, path in self.config['response'].items():
            value = get_path(response, path)
            if not value: continue
            if not isinstance(value, str): raise ProviderError('Image result field is not a string')
            if kind == 'url_path':
                # Returned CDN URLs receive no API credentials. Redirects stay disabled.
                return self.request(value, authenticated=False)
            if value.startswith('data:'): value = value.split(',', 1)[-1]
            try: return base64.b64decode(value, validate=True)
            except ValueError: raise ProviderError('Invalid image base64 result') from None
        raise ProviderError('No image at the configured response paths; retain response and adjust only its mapping')


def asset_spec(state, asset, inputs):
    if asset not in state['reference_asset_plan']['generation_order']:
        raise ValidationError('Asset is not in the approved generation plan')
    parts = asset.split(':'); role = parts[0]
    if role == 'product_master':
        prompt = state['product_master_prompt']
        expected = [s['sha256'] for s in state['source_assets']]
    elif role == 'talent':
        prompt = state['talent_reference_prompt']
        expected = []
    else:
        clip = next((c for c in state['clips'] if c['index'] == int(parts[1])), None)
        if not clip: raise ValidationError('Unknown clip')
        prompt = clip['storyboard_prompt'] if role == 'storyboard' else clip['frame_prompts'][parts[2] if role == 'keyframe' else role]
        expected = []
        for required in (['product_master', 'talent'] if clip.get('talent_action') else ['product_master']):
            rows = [r for r in state['references'] + state['reference_asset_plan'].get('reusable_assets', []) if r['role'] == required]
            # During generation the full reference manifest has not yet been registered.
            if not rows:
                raise ValidationError(f'Provide an inspected {required} via --input; its generation receipt is required')
            expected.append(rows[0]['sha256'])
    hashes = [sha256_file(p) for p in inputs]
    if sorted(hashes) != sorted(expected):
        raise ValidationError('Actual input images must match all approved product/talent hashes')
    return prompt, role


def project_image_state(orchestrator):
    state = orchestrator.load()
    orchestrator._validate_plan_approval(state)
    orchestrator._validate_source_files(state)
    if state['state'] != 'awaiting_reference_approval':
        raise ValidationError('Generate image assets only after plan approval and before video approval')
    config = state['reference_asset_plan'].get('image_config', {'provider': 'builtin'})
    if config['provider'] == 'builtin':
        raise ValidationError('This approved plan uses Codex imagegen; use its built-in tool')
    return state, validate_config(config)


def generated_globals(project, state):
    """Use receipts during incremental generation, retaining explicit human inspection outside the script."""
    tasks = project / 'requests/image-tasks.json'
    rows = json.loads(tasks.read_text()) if tasks.exists() else []
    result = copy.deepcopy(state)
    for asset in ('product_master', 'talent'):
        candidates = [r for r in rows if r['asset'] == asset and r['status'] == 'completed']
        if candidates and not any(r['role'] == asset for r in result['references']):
            r = candidates[-1]
            if not Path(r['path']).is_file() or sha256_file(Path(r['path'])) != r['sha256']:
                raise ValidationError('Generated global asset changed; retain its original file')
            result['references'].append({'role': asset, 'sha256': r['sha256'], 'path': r['path']})
    return result


def generate(orchestrator, asset, inputs, *, retry_reason=None, resume=False, key_loader=load_key):
    project = orchestrator.project_dir
    with file_lock(project / '.operation.lock', blocking=False):
        state, config = project_image_state(orchestrator)
        inputs = [Path(p).expanduser().resolve() for p in inputs]
        for p in inputs: validate_reference_image(p)
        prompt, role = asset_spec(generated_globals(project, state), asset, inputs)
        ledger_path = project / 'requests/image-tasks.json'
        rows = json.loads(ledger_path.read_text()) if ledger_path.exists() else []
        history = [r for r in rows if r['asset'] == asset]
        previous = history[-1] if history else None
        contract = {'asset': asset, 'prompt_sha256': canonical_hash(prompt),
            'input_hashes': [sha256_file(p) for p in inputs], 'config_sha256': canonical_hash(config),
            'plan_hash': state['approvals']['plan']}
        client = None
        if previous and not retry_reason:
            if previous['contract'] != contract: raise ValidationError('Image request changed after approval')
            if previous['status'] == 'completed':
                if sha256_file(Path(previous['path'])) != previous['sha256']:
                    raise ValidationError('Completed image changed; do not re-submit')
                return previous
            if previous['status'] in {'attempted', 'submission_unknown'}:
                raise SubmissionUnknown('Original image submission is uncertain; do not create another paid request')
            if previous['status'] == 'rejected': raise PaidRequestBlocked('Correct configuration in a new approved plan')
            if previous['status'] == 'failed': raise PaidRequestBlocked('Explicit retry reason required for the failed asset')
            entry = previous
        else:
            if resume: raise PaidRequestBlocked('Resume never creates a new image request')
            if any(r['status'] in {'attempted', 'submission_unknown'} for r in rows):
                raise SubmissionUnknown('An image submission is uncertain; stop all new paid image calls')
            if retry_reason and (not previous or previous['status'] not in {'completed', 'failed'}):
                raise PaidRequestBlocked('Only a known failed or visually incorrect candidate can be corrected')
            if len(history) >= config['max_attempts_per_asset']:
                raise PaidRequestBlocked('Approved image attempt limit reached')
            client = ImageClient(config, key_loader(config))
            url, body, content_type = client.build_request(prompt, inputs)
            entry = {'asset': asset, 'attempt': len(history) + 1, 'contract': contract,
                     'status': 'attempted', 'retry_reason': retry_reason, 'request_sha256': canonical_hash(base64.b64encode(body).decode())}
            rows.append(entry)
            atomic_write_json(ledger_path, rows)  # Durable BEFORE the paid POST.
            try:
                raw = client.request(url, data=body, content_type=content_type)
                response = json.loads(raw)
                if not isinstance(response, dict): raise ValueError('response is not an object')
            except (SubmissionUnknown, ProviderError, ValueError) as exc:
                entry['status'] = 'rejected' if isinstance(exc, ProviderError) and exc.status_code else 'submission_unknown'
                atomic_write_json(ledger_path, rows)
                if isinstance(exc, ValueError): raise SubmissionUnknown('Image response unreadable; do not repeat the POST') from None
                raise
            # Some services echo inputs. Remove an echoed API credential even from private caches.
            response = json.loads(json.dumps(response).replace(client.key, '[REDACTED]'))
            entry['response'] = response  # Private cache; never print raw service response.
            entry['status'] = 'received'
            a = config.get('async')
            if a:
                task_id = get_path(response, a['id_path'])
                if task_id is not None and str(task_id):
                    entry['task_id'] = str(task_id)
                    entry['status'] = 'pending'
            atomic_write_json(ledger_path, rows)
        if client is None:
            client = ImageClient(config, key_loader(config))
        if entry['status'] == 'pending':
            a = config['async']
            url = endpoint(config, a['poll_path'].replace('{task_id}', urllib.parse.quote(entry['task_id'], safe='')))
            try: response = json.loads(client.request(url))
            except ValueError: raise ProviderError('Image task query returned invalid JSON; resume later') from None
            status = get_path(response, a['status_path'])
            if status in a['failure']:
                entry['status'] = 'failed'
                atomic_write_json(ledger_path, rows)
                raise ProviderError('Original image task failed; no automatic paid retry')
            if status not in a['success']:
                return entry
            response = json.loads(json.dumps(response).replace(client.key, '[REDACTED]'))
            entry.update(status='received', response=response)
            atomic_write_json(ledger_path, rows)
        data = client.image_bytes(entry['response'])
        if not data or len(data) > MAX_BYTES: raise ProviderError('Invalid image result size')
        name = asset.replace(':', '-') + f'-candidate-{entry["attempt"]}'
        folder = project / 'references'
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / (name + '.download')
        temp.write_bytes(data)
        try:
            suffix = validate_reference_image(temp)
            info = probe_media(temp)
            if not info.get('width') or not info.get('height'): raise ProviderError('Image result cannot be decoded')
            path = temp.with_suffix(suffix)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        entry.update(status='completed', path=str(path.resolve()), sha256=sha256_file(path),
                     width=info['width'], height=info['height'])
        receipt = project / 'requests/images' / (name + '.json')
        entry['receipt'] = str(receipt.resolve())
        atomic_write_json(ledger_path, rows)
        atomic_write_json(receipt, {k: v for k, v in entry.items() if k != 'response'})
        return entry


def verify_receipt(project, row, *, expected_role=None):
    receipt = Path(str(row.get('generation_receipt', ''))).resolve()
    try: receipt.relative_to((project / 'requests/images').resolve())
    except ValueError: raise ValidationError('External image needs a receipt inside this project') from None
    if not receipt.is_file(): raise ValidationError('External image generation receipt is missing')
    data = json.loads(receipt.read_text())
    state = json.loads((project / 'project.json').read_text())
    config = state['reference_asset_plan'].get('image_config', {'provider': 'builtin'})
    if (config['provider'] == 'builtin' or data['contract']['plan_hash'] != state['approvals']['plan']
            or data['contract']['config_sha256'] != canonical_hash(config)):
        raise ValidationError('Image receipt does not belong to this approved image service/plan')
    ledger = json.loads((project / 'requests/image-tasks.json').read_text())
    if not any({k: v for k, v in r.items() if k != 'response'} == data for r in ledger):
        raise ValidationError('Image receipt does not match the original paid-call ledger')
    if data['status'] != 'completed' or data['sha256'] != sha256_file(Path(row['path'])):
        raise ValidationError('External image does not match the generated file')
    parts = data['asset'].split(':')
    if parts[0] != (expected_role or row['role']): raise ValidationError('Image receipt role mismatch')
    if len(parts) > 1 and int(parts[1]) != int(row.get('clip_index') or 0):
        raise ValidationError('Image receipt clip mismatch')
    if parts[0] == 'keyframe' and parts[2] != row.get('shot_id'):
        raise ValidationError('Image receipt shot mismatch')
    declared = row.get('derived_from_source_hashes') if parts[0] == 'product_master' else (
        [row.get('derived_from_product_master_sha256')] + ([row['derived_from_talent_sha256']] if row.get('derived_from_talent_sha256') else [])
        if parts[0] not in {'talent', 'style'} else [])
    if sorted(declared or []) != sorted(data['contract']['input_hashes']):
        raise ValidationError('Image receipt input hashes do not match the declared generation relationship')
    return str(receipt)


def public_result(entry):
    return {k: entry[k] for k in ('asset', 'attempt', 'status', 'path', 'sha256', 'receipt', 'width', 'height') if k in entry}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    configure = sub.add_parser('configure')
    configure.add_argument('--preset', choices=['builtin', *PRESETS])
    configure.add_argument('--profile', type=Path, help='Documented JSON profile; never include credentials')
    for name in ('base-url', 'model', 'size', 'documentation', 'key-env'):
        configure.add_argument('--' + name)
    sub.add_parser('check', help='Local configuration/key check only; no network or charge')
    sub.add_parser('set-key', help='Explicit hidden input to macOS Keychain')
    for name in ('generate', 'resume'):
        command = sub.add_parser(name)
        command.add_argument('--project', type=Path, required=True)
        command.add_argument('--asset', required=True)
        command.add_argument('--input', action='append', default=[], help='Actual reference path; repeat for every image')
        if name == 'generate': command.add_argument('--retry-reason')
    args = parser.parse_args()
    try:
        if args.command == 'configure':
            if args.profile and args.preset: raise ValidationError('Use either --profile or --preset')
            if args.profile: config = json.loads(args.profile.read_text())
            elif args.preset == 'builtin': config = {'provider': 'builtin'}
            elif args.preset:
                config = {**copy.deepcopy(PRESETS[args.preset]), 'provider': args.preset}
                for key in ('base_url', 'model', 'size', 'documentation', 'key_env'):
                    if getattr(args, key): config[key] = getattr(args, key)
            else: raise ValidationError('Choose a documented preset or --profile')
            config = validate_config(config)
            atomic_write_json(CONFIG, config)
            result = {'configured': True, 'provider': config['provider']}
        elif args.command in {'check', 'set-key'}:
            config = read_config()
            if config['provider'] == 'builtin': result = {'provider': 'builtin', 'requires_api_key': False, 'generation_tested': False}
            else:
                if args.command == 'set-key': Keychain.store(key_service(config), getpass.getpass('Image API Key (hidden): '))
                result = {'provider': config['provider'], 'key_available': bool(load_key(config)),
                          'configuration_valid': True, 'generation_tested': False}
        else:
            try: from .commercial_ad import AdOrchestrator
            except ImportError: from commercial_ad import AdOrchestrator
            result = public_result(generate(AdOrchestrator(args.project), args.asset, args.input,
                retry_reason=getattr(args, 'retry_reason', None), resume=args.command == 'resume'))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValidationError, ProviderError, SubmissionUnknown, PaidRequestBlocked) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
