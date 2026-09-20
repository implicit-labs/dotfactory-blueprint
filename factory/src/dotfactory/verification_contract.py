"""Project-owned verification methods, reviewed selection, and durable evidence gates."""
from __future__ import annotations

import base64
import copy
import fnmatch
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from . import worker

CATEGORIES = {'ios', 'frontend', 'interaction', 'backend', 'general'}


def strings(value, label):
    if not isinstance(value, list) or len(value) > 128 or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(label + ' must be a bounded string list')
    if len(set(value)) != len(value):
        raise ValueError(label + ' contains duplicates')
    return value


def validate(config):
    if not isinstance(config, dict) or set(config) != {'methods', 'defaults', 'rules'}:
        raise ValueError('verification requires methods, defaults and rules')
    methods = config['methods']
    if not isinstance(methods, dict) or not methods or len(methods) > 32:
        raise ValueError('verification requires 1 to 32 methods')
    for name, method in methods.items():
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name):
            raise ValueError('invalid verification method name')
        worker.validate_verification_method(method)
    if set(strings(config['defaults'], 'defaults')) - set(methods):
        raise ValueError('unknown default verification method')
    if not isinstance(config['rules'], list) or len(config['rules']) > 64:
        raise ValueError('verification rules must be a bounded list')
    for rule in config['rules']:
        if not isinstance(rule, dict) or set(rule) != {'paths', 'categories', 'methods'}:
            raise ValueError('verification rule requires paths, categories and methods')
        strings(rule['paths'], 'rule paths')
        if set(strings(rule['categories'], 'categories')) - CATEGORIES:
            raise ValueError('unknown change category')
        if not rule['paths'] and not rule['categories']:
            raise ValueError('verification rule requires a selector')
        if not strings(rule['methods'], 'rule methods') or set(rule['methods']) - set(methods):
            raise ValueError('verification rule requires registered methods')


def frozen(ledger, execution_id):
    return json.loads(ledger.current(execution_id)['intent_snapshot_json']).get('verification')


def categories(paths):
    result = set()
    for path in paths:
        if path.endswith(('.swift', '.storyboard', '.xib')) or '.xcodeproj/' in path:
            result.add('ios')
        elif path.endswith(('.tsx', '.jsx', '.vue', '.svelte', '.html', '.css', '.scss')):
            result.add('frontend')
        elif path.endswith(('.py', '.go', '.rs', '.java')):
            result.add('backend')
    return result or {'general'}


def resolve(config, selection):
    validate(config)
    if not isinstance(selection, dict) or set(selection) != {'paths', 'categories', 'add', 'omit'}:
        raise ValueError('verification selection requires paths, categories, add and omit')
    paths = strings(selection['paths'], 'planned paths')
    if not paths:
        raise ValueError('verification selection requires planned paths')
    for path in paths:
        if str(PurePosixPath(path)) != path or path.startswith('/') or '..' in PurePosixPath(path).parts or '\\' in path or '\x00' in path:
            raise ValueError('planned paths must be canonical repository paths')
    kinds = set(strings(selection['categories'], 'change categories')) | categories(paths)
    if kinds - CATEGORIES:
        raise ValueError('unknown change category')
    selected = set(config['defaults'])
    origins = {name: ['project default'] for name in selected}
    for index, rule in enumerate(config['rules']):
        if kinds.intersection(rule['categories']) or any(fnmatch.fnmatchcase(path, pattern) for path in paths for pattern in rule['paths']):
            for name in rule['methods']:
                selected.add(name)
                origins.setdefault(name, []).append('project rule ' + str(index + 1))
    additions = strings(selection['add'], 'additional methods')
    omitted = selection['omit']
    if not isinstance(omitted, dict) or any(not isinstance(reason, str) or not reason.strip() for reason in omitted.values()):
        raise ValueError('omitted methods require reviewable reasons')
    if (set(additions) | set(omitted)) - set(config['methods']) or set(additions) & set(omitted):
        raise ValueError('unknown or conflicting verification overrides')
    if set(omitted) - selected:
        raise ValueError('cannot omit a method that was not selected')
    for name in additions:
        origins.setdefault(name, []).append('run addition')
    selected = (selected | set(additions)) - set(omitted)
    if not selected:
        raise ValueError('verification must retain at least one method')
    required = set()
    if kinds.intersection({'ios', 'frontend'}):
        required.update({('before', 'screenshot'), ('after', 'screenshot')})
    if 'interaction' in kinds:
        required.add(('after', 'recording'))
    # Removing a selected method is a visible human-reviewed waiver, never an
    # accidental fallback to a unit-only default for a UI change.
    covered = {(a['phase'], a['kind']) for name in selected | set(omitted)
               for a in config['methods'][name]['artifacts']}
    if required - covered:
        raise ValueError('change requires registered visual evidence methods: ' +
                         ', '.join(phase + ' ' + kind for phase, kind in sorted(required - covered)))
    return {'categories': sorted(kinds), 'paths': paths,
            'methods': {name: copy.deepcopy(config['methods'][name]) for name in sorted(selected)},
            'origins': {name: origins[name] for name in sorted(selected)}, 'omitted': omitted}


def summary(contract):
    lines = ['Verification: ' + ', '.join(contract['categories'])]
    for name, method in contract['methods'].items():
        artifacts = ', '.join(a['phase'] + ' ' + a['kind'] + ' (' + a['scenario'] + ')' for a in method['artifacts']) or 'command result'
        lines.append(name + ': ' + method['description'] + '; hosts: ' + ', '.join(method['hosts']) + '; requires: ' + ', '.join(method['requires']) + '; evidence: ' + artifacts)
    lines.extend('Omit ' + name + ': ' + reason for name, reason in contract['omitted'].items())
    return '\n'.join(lines)


def approved(ledger, execution_id):
    from .delivery import planned_receipt
    receipt = planned_receipt(ledger, execution_id)['receipt']
    return (receipt.get('planning_requirements') or {}).get('verification')


def assert_coverage(contract, config, paths):
    # Check the actual diff too: planner classification is not authoritative.
    paths = [p for p in paths if p and not p.startswith('.factory/')]
    if not paths:
        return
    actual = resolve(config, {'paths': paths, 'categories': contract['categories'], 'add': [], 'omit': {}})
    missing = set(actual['methods']) - set(contract['methods']) - set(contract['omitted'])
    if missing:
        raise ValueError('changed source requires a revised verification plan: ' + ', '.join(sorted(missing)))


def evidence_root(ledger):
    return ledger.path.resolve().parent / 'verification-artifacts'


def preserve(ledger, method, phase, result):
    expected = [a for a in method['artifacts'] if a['phase'] == phase]
    actual = result.get('artifacts')
    if not isinstance(actual, list) or len(actual) != len(expected):
        raise ValueError('verification worker omitted required artifacts')
    if result.get('exit_code') != 0:
        raise ValueError('verification command did not pass')
    directory = evidence_root(ledger)
    directory.mkdir(exist_ok=True)
    total = 0
    for declared, item in zip(expected, actual):
        if any(item.get(key) != value for key, value in declared.items()):
            raise ValueError('verification artifact declaration mismatch')
        data = base64.b64decode(item.pop('data_base64'), validate=True)
        total += len(data)
        sha = hashlib.sha256(data).hexdigest()
        if not data or total > 8 * 1024 * 1024 or item.get('sha256') != sha or item.get('bytes') != len(data):
            raise ValueError('verification artifact bytes do not match receipt')
        path = directory / sha
        if path.is_symlink():
            raise ValueError('artifact store contains a symlink')
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise ValueError('stored verification artifact was modified')
        else:
            with path.open('xb') as output:
                output.write(data)
        item['uri'] = str(path)


def check_evidence(ledger, receipt):
    for method in receipt['methods'].values():
        for artifact in method.get('result', {}).get('artifacts', []):
            path = evidence_root(ledger) / artifact['sha256']
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']:
                raise ValueError('verification artifact missing or changed')


def baseline(ledger, execution_id, contract, plan_sha):
    rows = ledger.connection.execute(
        "SELECT payload_json FROM events WHERE execution_id=? AND event_type='verification_methods_completed' ORDER BY seq DESC",
        (execution_id,))
    for row in rows:
        receipt = json.loads(row[0])
        if receipt['phase'] == 'before' and receipt['source_sha'] == plan_sha and receipt['contract_digest'] == worker.digest(contract) and receipt['passed']:
            check_evidence(ledger, receipt)
            return receipt
    raise ValueError('required pre-implementation verification receipt is missing')


def run_phase(ledger, launch, phase, source_sha):
    """Execute once per attempt; uncertain starts require a new reviewed attempt."""
    from .execution import call, ExecutionError
    from .planning import effective_settings
    from .delivery import DeliveryError
    request = launch.request
    contract = approved(ledger, request.execution_id)
    if not contract:
        if frozen(ledger, request.execution_id):
            raise DeliveryError('approved verification contract is missing')
        return None
    key = 'verification-methods:' + request.attempt_id + ':' + phase
    saved = ledger.event_for_command(key)
    if saved:
        value = saved['payload']
        if value['source_sha'] != source_sha or value['contract_digest'] != worker.digest(contract):
            raise DeliveryError('verification evidence belongs to different source or contract')
        if not value['passed']:
            raise DeliveryError('verification methods failed; start a new attempt')
        check_evidence(ledger, value)
        return value
    started = ledger.event_for_command(key + ':started')
    if started:
        raise DeliveryError('verification execution is uncertain; investigate before a new attempt')
    settings = effective_settings(ledger, request.execution_id)
    workers = settings['policy']['workers'] if settings else {}
    value = {'source_sha': source_sha, 'contract_digest': worker.digest(contract), 'phase': phase, 'passed': False, 'methods': {}}
    with ledger.transaction() as db:
        ledger.assert_attempt_active(request.attempt_id, request.fence_token)
        ledger._event(db, execution_id=request.execution_id, state_run_id=None, attempt_id=request.attempt_id,
                      event_type='verification_methods_started', payload=value, idempotency_key=key + ':started')
    try:
        for name, method in contract['methods'].items():
            # Check every method's host before implementation, even without before artifacts.
            selected = None
            for host in method['hosts']:
                config = workers.get(host)
                if host != 'coordinator' and config is None:
                    continue
                probe = {'op': 'verification-probe', 'method': method}
                try:
                    report = worker.dispatch(probe) if host == 'coordinator' else call(config, probe)
                except (ExecutionError, worker.WorkerError, OSError):
                    continue
                if report.get('available'):
                    selected = (host, config, report)
                    break
            if selected is None:
                raise ValueError('no eligible verification host for ' + name)
            host, config, report = selected
            ledger.assert_attempt_active(request.attempt_id, request.fence_token)
            if phase not in method['commands']:
                value['methods'][name] = {'host': host, 'readiness': report, 'status': 'ready'}
                continue
            spec = {'op': 'verify-method', 'method': method, 'phase': phase,
                    'source_sha': source_sha, 'pinned_files': contract['pinned_files'], 'bundle': worker.bundle(launch.workspace_path, source_sha)}
            result = worker.dispatch(spec) if host == 'coordinator' else call(config, spec, timeout=method['timeout_seconds'] + 60)
            if result.get('source_sha') != source_sha or result.get('phase') != phase or result.get('method_digest') != worker.digest(method):
                raise ValueError('verification worker returned mismatched provenance')
            if result.get('passed'):
                preserve(ledger, method, phase, result)
            value['methods'][name] = {'host': host, 'location': 'unknown' if host == 'coordinator' else config.get('location', 'unknown'), 'result': result}
            if not result.get('passed'):
                raise ValueError('verification method failed: ' + name)
        value['passed'] = True
    except (ValueError, OSError, ExecutionError, worker.WorkerError) as error:
        value['error'] = str(error)
    ledger.assert_attempt_active(request.attempt_id, request.fence_token)
    with ledger.transaction() as db:
        ledger._event(db, execution_id=request.execution_id, state_run_id=None, attempt_id=request.attempt_id,
                      event_type='verification_methods_completed', payload=value, idempotency_key=key)
    if not value['passed']:
        raise DeliveryError(value['error'])
    return value


def before(ledger, launch):
    if launch.request.config.get('exit_contract') != 'implementation-result-v2' or not frozen(ledger, launch.request.execution_id):
        return
    from .delivery import approved_plan
    plan = approved_plan(ledger, launch.request.execution_id)
    run_phase(ledger, launch, 'before', plan['head_sha'])
