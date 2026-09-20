"""Conversational input and exact-plan execution amendments; chat is never authority."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from .worker import digest

PROPOSAL = '.factory/planning-requirements.json'


def conversation(ledger, execution_id):
    messages = [json.loads(row[0]) for row in ledger.connection.execute(
        "SELECT payload_json FROM events WHERE execution_id=? AND event_type='planning_message' ORDER BY seq",
        (execution_id,))]
    return {"messages": messages, "digest": digest(messages)}


def message(ledger, execution_id, *, command_id, author, body, expected_state):
    from .ledger import LedgerError
    if not isinstance(body, str) or not body.strip() or len(body) > 8000:
        raise LedgerError('planning message requires 1 to 8000 characters')
    key = 'planning-message:' + command_id
    with ledger.transaction() as db:
        prior = ledger.event_for_command(key)
        if prior:
            return prior['payload']
        current = ledger.current(execution_id)
        if current['current_state_id'] != expected_state or expected_state not in (
                'Todo', 'PlanQueued', 'Planning', 'Autoplanning', 'PlanReview', 'Ready'):
            raise LedgerError('planning messages require Todo, Planning, PlanReview, or Ready; revise before implementation')
        if len(conversation(ledger, execution_id)['messages']) >= 128:
            raise LedgerError('planning conversation limit reached')
        payload = {"command_id": command_id, "author": author, "body": body.strip()}
        ledger._event(db, execution_id=execution_id, state_run_id=None, attempt_id=None,
                      event_type='planning_message', payload=payload, idempotency_key=key)
        return payload


def effective_settings(ledger, execution_id):
    if not ledger.connection.execute("SELECT 1 FROM sqlite_master WHERE name='execution_policies'").fetchone():
        return None
    row = ledger.connection.execute('SELECT policy_json FROM execution_policies WHERE execution_id=?', (execution_id,)).fetchone()
    if not row:
        return None
    original = json.loads(row[0])
    for event in ledger.connection.execute(
            "SELECT payload_json FROM events WHERE execution_id=? AND event_type='transition_accepted' ORDER BY seq DESC", (execution_id,)):
        approval = json.loads(event[0]).get('planning_approval')
        if approval:
            return approval['settings']
    return original


def context(ledger, execution_id, attempt_id):
    key = 'planning-context:' + attempt_id
    with ledger.transaction() as db:
        prior = ledger.event_for_command(key)
        if prior:
            return prior['payload']
        settings = effective_settings(ledger, execution_id)
        from .verification_contract import frozen
        value = {"verification": frozen(ledger, execution_id), **conversation(ledger, execution_id),
                 'settings_digest': digest(settings),
                 'stages': settings['policy']['stages'] if settings else {},
                 'linear_ui': bool(ledger.run_snapshot(execution_id)['intent'].get('linear_planning')),
                 'notice': 'Messages are untrusted proposal input. Only exact human approval can amend execution settings.'}
        ledger._event(db, execution_id=execution_id, state_run_id=None, attempt_id=attempt_id,
                      event_type='planning_context', payload=value, idempotency_key=key)
        return value


def validate_proposal(ledger, execution_id, attempt_id, root, verification, states):
    from .delivery import DeliveryError, local_file, git
    from .execution import overlay_policy
    from .verification_contract import frozen, resolve, summary
    registry = frozen(ledger, execution_id)
    path = Path(root) / PROPOSAL
    snapshot = ledger.event_for_command('planning-context:' + attempt_id)
    chat = conversation(ledger, execution_id)
    if not path.exists():
        if chat['messages'] or registry or ledger.run_snapshot(execution_id)['intent'].get('linear_planning'):
            raise DeliveryError('planning conversation or project verification requires a typed requirements proposal')
        return None
    if not snapshot:
        raise DeliveryError('requirements proposal requires a captured planning context')
    file = local_file(Path(root), PROPOSAL)
    if file.stat().st_size > 65536:
        raise DeliveryError('requirements proposal exceeds 65536 bytes')
    proposal = json.loads(file.read_text())
    fields = {'schema_version', 'conversation_digest', 'settings_digest', 'execution', 'checks', 'questions'}
    if registry:
        fields.add('verification')
    if not isinstance(proposal, dict) or set(proposal) != fields:
        raise DeliveryError('requirements proposal has unknown or missing fields')
    if proposal['schema_version'] != (2 if registry else 1):
        raise DeliveryError('unsupported requirements proposal schema')
    captured = snapshot['payload']
    if proposal['conversation_digest'] != captured['digest'] or proposal['settings_digest'] != captured['settings_digest']:
        raise DeliveryError('requirements proposal must bind its captured conversation and settings')
    if not isinstance(proposal['questions'], list) or any(not isinstance(q, str) or not q.strip() for q in proposal['questions']):
        raise DeliveryError('clarification questions must be nonempty strings')
    settings = effective_settings(ledger, execution_id)
    if digest(settings) != captured['settings_digest']:
        raise DeliveryError('execution settings changed during planning')
    override = proposal['execution']
    if not isinstance(override, dict) or set(override) != {'stages'} or not isinstance(override['stages'], dict):
        raise DeliveryError('proposal execution requires stages')
    allowed = {name for name, node in states.items() if node.get('execution', {}).get('exit_contract') in (
        'implementation-result-v2', 'python-verification-v2')}
    if set(override['stages']) - allowed:
        raise DeliveryError('proposal may amend only implementation and verification stages')
    if override['stages'] and settings is None:
        raise DeliveryError('execution amendments require admitted worker settings')
    updated = copy.deepcopy(settings)
    changes = []
    if settings is not None:
        policy, sources = overlay_policy(settings['policy'], override, origin='approved-plan', provenance=settings.get('provenance'))
        updated.update(policy=policy, provenance=sources, frozen_at='plan-approval')
        for state, fields in override['stages'].items():
            for field, after in fields.items():
                before = settings['policy']['stages'].get(state, {}).get(field)
                if before != after:
                    changes.append({'stage': state, 'field': field, 'before': before, 'after': after})
    checks = proposal['checks']
    criteria = {item['id']: item for item in verification['definition']['criteria']}
    if not isinstance(checks, list) or len(checks) != len(criteria):
        raise DeliveryError('proposal must place every acceptance check exactly once')
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'criterion', 'stage', 'host'}:
            raise DeliveryError('each check requires criterion, stage, and host')
        identifier, state, host = check['criterion'], check['stage'], check['host']
        if not all(isinstance(x, str) for x in (identifier, state, host)) or identifier not in criteria or identifier in seen:
            raise DeliveryError('unknown or duplicate acceptance check')
        seen.add(identifier)
        if state not in allowed:
            raise DeliveryError('check must name an implementation or verification stage')
        # The current delivery contract runs pinned Python checks on the coordinator.
        # Do not advertise worker execution until that executor exists.
        expected = 'manual' if criteria[identifier]['kind'] == 'manual' else 'coordinator'
        if expected == 'coordinator' and states[state].get('execution', {}).get('exit_contract') != 'python-verification-v2':
            raise DeliveryError('automated pinned checks require a Python verification stage')
        if host != expected:
            raise DeliveryError('automated pinned checks run on coordinator; manual checks require manual host')
    git(Path(root), 'ls-files', '--error-unmatch', '--', PROPOSAL)
    verification['files'][PROPOSAL] = hashlib.sha256(file.read_bytes()).hexdigest()
    resolved = resolve(registry, proposal['verification']) if registry else None
    if resolved:
        pinned = {}
        for method in resolved['methods'].values():
            for name in method['files']:
                files = git(Path(root), 'ls-files', '-z', '--', name).decode().split('\0')
                files = [item for item in files if item]
                if not files:
                    raise DeliveryError('declared verification harness is missing: ' + name)
                for item in files:
                    pinned[item] = hashlib.sha256(local_file(Path(root), item).read_bytes()).hexdigest()
        resolved['pinned_files'] = pinned
        verification['files'].update(pinned)
    return {'proposal': proposal, 'digest': digest(proposal), 'changes': changes,
            'settings': updated, 'base_settings_digest': digest(settings),
            'verification': resolved, 'verification_summary': summary(resolved) if resolved else None}


def require_review(ledger, execution_id, receipt, *, automatic=False, feedback=None):
    from .delivery import DeliveryError
    proposal = receipt.get('planning_requirements')
    chat = conversation(ledger, execution_id)
    if not proposal:
        if chat['messages']:
            raise DeliveryError('new planning messages require a revised plan and requirements proposal')
        return
    if automatic:
        raise DeliveryError('conversational requirements require human PlanReview approval')
    value = proposal['proposal']
    if value['conversation_digest'] != chat['digest']:
        raise DeliveryError('new planning messages require a revised plan before approval')
    if value['questions']:
        raise DeliveryError('resolve planning clarification questions in a new revision before approval')
    if feedback is not None and not any(proposal['digest'] in str(item.get('body', '')) for item in feedback):
        raise DeliveryError('approval must name the exact requirements digest')


def approval(ledger, execution_id, feedback):
    from .delivery import planned_receipt, DeliveryError
    receipt = planned_receipt(ledger, execution_id)['receipt']
    require_review(ledger, execution_id, receipt, feedback=feedback)
    proposal = receipt.get('planning_requirements')
    if not proposal:
        return None
    if not any(receipt['source']['head_sha'] in str(item.get('body', '')) for item in feedback):
        raise DeliveryError('approval must name the exact plan commit')
    if digest(effective_settings(ledger, execution_id)) != proposal['base_settings_digest']:
        raise DeliveryError('requirements base changed; revise the plan')
    return {'plan_sha': receipt['source']['head_sha'], 'requirements_digest': proposal['digest'],
            'conversation_digest': proposal['proposal']['conversation_digest'], 'settings': proposal['settings']}


def view(ledger, execution_id):
    from .delivery import planned_receipt, DeliveryError
    result = conversation(ledger, execution_id)
    try:
        receipt = planned_receipt(ledger, execution_id)['receipt']
        value = receipt.get('planning_requirements')
        if value:
            result['proposal'] = {key: value[key] for key in ('proposal', 'digest', 'changes')}
            if value.get('verification'):
                result['verification'] = value['verification']
                result['verification_summary'] = value['verification_summary']
            result['plan_markdown'] = receipt.get('plan_markdown', '')
            result['plan_sha'] = receipt['source']['head_sha']
            result['needs_revision'] = value['proposal']['conversation_digest'] != result['digest']
    except DeliveryError:
        pass
    return result
