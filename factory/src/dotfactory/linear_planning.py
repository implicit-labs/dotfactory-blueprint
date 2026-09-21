"""Linear is the planning UI; authenticated reads feed the existing control boundary."""
from __future__ import annotations

import json
import re
import queue
import threading
import time
from datetime import datetime, timezone

from .control import ControlError, Principal
from .ledger import LedgerError
from .linear_api import LinearAPIError
from .observability import canonical_json
from .planning import conversation, view
from .worker import digest


def validate(policy):
    fields = {'enabled', 'organization_id', 'app_user_id', 'participants', 'reviewer_urls', 'quiet_seconds'}
    if not isinstance(policy, dict) or set(policy) - fields:
        raise ValueError('linear_planning has unknown fields')
    if type(policy.get('enabled', False)) is not bool:
        raise ValueError('linear_planning.enabled must be boolean')
    if not policy.get('enabled'):
        return
    for name in ('organization_id', 'app_user_id'):
        if not isinstance(policy.get(name), str) or not policy[name].strip():
            raise ValueError('linear_planning requires ' + name)
    roles = policy.get('participants')
    if not isinstance(roles, dict) or not roles or any(
            not isinstance(k, str) or not k or v not in ('operator', 'approver') for k, v in roles.items()):
        raise ValueError('linear_planning.participants maps Linear user IDs to operator or approver')
    if 'approver' not in roles.values():
        raise ValueError('linear_planning requires an approver')
    urls = policy.get('reviewer_urls', [])
    if not isinstance(urls, list) or any(not isinstance(u, str) or not re.fullmatch(
            r'https://linear\.app/[A-Za-z0-9_-]+/profiles/[A-Za-z0-9_-]+', u) for u in urls):
        raise ValueError('reviewer_urls must be Linear profile URLs')
    quiet = policy.get('quiet_seconds', 5)
    if type(quiet) is not int or not 0 <= quiet <= 60:
        raise ValueError('quiet_seconds must be an integer from 0 to 60')


def event(ledger, execution, kind, key, payload):
    previous = ledger.event_for_command(key)
    if previous:
        return previous['payload']
    with ledger.transaction() as db:
        ledger._event(db, execution_id=execution, state_run_id=None, attempt_id=None,
                      event_type=kind, payload=payload, idempotency_key=key)
    return payload


def events(ledger, execution, kind):
    return [json.loads(row[0]) for row in ledger.connection.execute(
        'SELECT payload_json FROM events WHERE execution_id=? AND event_type=? ORDER BY seq', (execution, kind))]


def policy_for(runtime, execution):
    current = runtime.ledger.current(execution)
    frozen = runtime.ledger.run_snapshot(execution)['intent'].get('linear_planning', {})
    if not frozen:
        return None
    configured = runtime.config.values['projects'][current['project_key']].get('linear_planning', {})
    if not configured.get('enabled') or not frozen:
        return None
    if any(configured.get(k) != frozen.get(k) for k in ('organization_id', 'app_user_id')):
        raise LedgerError('Linear planning identity changed; restore the admitted identity before continuing')
    # Current settings can revoke authority but cannot silently grant new run authority.
    result = dict(frozen)
    result['participants'] = {k: ('approver' if v == configured['participants'].get(k) == 'approver' else 'operator')
                              for k, v in frozen['participants'].items() if k in configured['participants']}
    return result


def paused(ledger, execution):
    changes = events(ledger, execution, 'linear_planning_pause')
    return bool(changes and changes[-1]['paused'])


def plan_preview(plan, *, title="Full plan and scope", include_preview=True):
    """Preserve the frozen plan; isolate ambiguous markup from the action below."""
    fence = None
    unsafe = False
    for line in plan.splitlines():
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', line)
        if marker:
            run, suffix = marker.groups()
            if fence is None:
                fence = run
            elif run[0] == fence[0] and len(run) >= len(fence) and not suffix.strip():
                fence = None
        if re.match(r'^\s*\+{3,}', line):
            unsafe = True
    if unsafe or fence:
        # Show literal Markdown rather than nesting toggles or allowing an open
        # fence to consume the approval instructions. Choose an unforgeable close.
        boundary = '`' * max(3, 1 + max((len(m.group()) for m in re.finditer(r'`+', plan)), default=0))
        return '**' + title + ' (Markdown)**\n\n' + boundary + '\n' + plan + '\n' + boundary
    preview = 'Expand the full plan to review the proposed scope and approach.'
    # Only reuse a complete short prose paragraph; never truncate Markdown.
    for block in plan.split('\n\n'):
        block = block.strip()
        if re.match(r'^(?:`{3,}|~{3,})', block):
            break
        if block and len(block) <= 400 and all(
                not re.match(r'^(?:[#>*|`~+-]|\d+[.)]\s| {4}|\t)', line)
                for line in block.splitlines()):
            preview = block
            break
    return (preview + '\n\n' if include_preview else '') + '+++ ' + title + '\n\n' + plan + '\n\n+++'


def present(ledger, execution, policy):
    """Persist the exact readable review before projecting it; never read a mutable worktree."""
    value = view(ledger, execution)
    if 'proposal' not in value or value.get('needs_revision'):
        return None
    binding = {k: value[k] for k in ('plan_sha', 'digest')}
    binding['requirements_digest'] = value['proposal']['digest']
    prior = events(ledger, execution, 'linear_planning_review')
    for item in prior:
        if item['binding'] == binding:
            return item
    number = len(prior) + 1
    proposal = value['proposal']['proposal']
    questions = proposal['questions']
    plan = value.get('plan_markdown', '')
    if not plan:
        raise LedgerError('Linear planning requires a frozen readable plan; run planning again')
    lines = [f'**Plan v{number} — ' + ('questions for you**' if questions else 'ready for your review**'), plan_preview(plan)]
    summary = re.search(r'^## Verification summary\s*\n(.*?)(?=^## |\Z)', plan, re.M | re.S)
    if summary:
        prose = summary.group(1).strip()
        if prose and len(prose) <= 400 and not re.search(r'^\s*(?:[#>`~+*|-]|\d+[.)]\s)', prose, re.M):
            lines += ['**Verification**', prose]
    if value.get('verification_summary'):
        lines += [plan_preview(value['verification_summary'], title='Verification requirements and evidence', include_preview=False)]
    changes = value['proposal']['changes']
    if changes:
        details = 'These run-specific changes replace project defaults:\n\n' + '\n'.join(
            f"- {change['stage']} / {change['field']}: {canonical_json(change['before'])} → {canonical_json(change['after'])}"
            for change in changes)
        lines += [plan_preview(details, title='Changes to project defaults', include_preview=False)]
    if questions:
        lines += ['**Decisions needed**'] + [f'{i}. {question}' for i, question in enumerate(questions, 1)]
        lines += ['Reply here in your own words. I will incorporate your answers into the next plan.']
    else:
        lines += [f'Reply **approve plan v{number}** to authorize implementation of this revision, or describe what to change.']
    lines += ['You can also reply **pause**, **resume**, or **stop**. Stop cancels this run.']
    lines += policy.get('reviewer_urls', [])
    body = '\n\n'.join(lines)
    if len(body) > 24000:
        raise LedgerError('Planning review is too long for Linear; revise into a compact plan')
    result = {'revision': number, 'binding': binding, 'body': body, 'questions': bool(questions),
              'semantic_key': f'planning-review:{number}', 'created_at': ledger.clock()}
    return event(ledger, execution, 'linear_planning_review', f'linear-review:{execution}:{number}', result)


def activities(runtime, execution, original):
    policy = policy_for(runtime, execution)
    if not policy:
        return original
    ledger = runtime.ledger
    current = ledger.current(execution)
    # Routine state/tool changes remain in the run trace, not attention messages.
    result = [a for a in original if a['semantic_key'].startswith(('incident:', 'attention:', 'terminal:', 'worker-location:'))
              or (a['semantic_key'].startswith('human-input:') and current['current_state_id'] != 'PlanReview')]
    result.insert(0, {'semantic_key': 'linear-planning-queued', 'content': {'type': 'thought', 'body':
        'Planning is queued for an eligible worker. I will gather context, batch questions here, and wait for explicit plan approval before implementation. Replies are recovered when the worker is awake.'}})
    for item in events(ledger, execution, 'linear_planning_reply'):
        result.append({'semantic_key': 'planning-reply:' + item['activity_id'], 'content': {
            'type': 'elicitation' if item['status'] == 'rejected' else 'thought', 'body': item['message']}})
    if current['current_state_id'] == 'PlanReview' and not paused(ledger, execution):
        review = present(ledger, execution, policy)
        if review:
            result.append({'semantic_key': review['semantic_key'], 'content': {'type': 'elicitation', 'body': review['body']}})
    return result


def _time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp must include timezone')
    return parsed


def receive(runtime, execution, session):
    """Consume one complete authenticated API snapshot, oldest first, on the ledger writer."""
    ledger = runtime.ledger
    policy = policy_for(runtime, execution)
    if not policy:
        return
    snapshot = ledger.run_snapshot(execution)
    bound = ledger.linear_agent_session(execution)
    issue = session.get('issue') or {}
    project = runtime.config.resolve_project(snapshot['project_key'], environment=runtime.environment)
    if (session.get('id') != bound['session_id'] or issue.get('id') != snapshot['intent'].get('linear_issue_id')
            or (session.get('appUser') or {}).get('id') != policy['app_user_id']
            or (session.get('organization') or {}).get('id') != policy['organization_id']
            or (issue.get('project') or {}).get('id') != project['tracker_project_id']
            or (project.get('tracker_team_id') and (issue.get('team') or {}).get('id') != project['tracker_team_id'])):
        raise LedgerError('Linear planning session identity does not match this run')
    service = runtime.control_service(snapshot['project_key'])
    pending = []
    for item in session['activities']:
        if (item.get('content') or {}).get('type') != 'prompt' or item.get('queued') is True:
            continue
        identity = str(item.get('id') or '')
        if not identity:
            raise LedgerError('Linear prompt requires an activity ID')
        key = 'linear-reply:' + execution + ':' + identity
        if ledger.event_for_command(key):
            continue
        pending.append(item)
    pending.sort(key=lambda a: (_time(a['createdAt']), a['id']))
    for item in pending:
        identity = item['id']
        key = 'linear-reply:' + execution + ':' + identity
        if ledger.event_for_command(key):
            continue
        author = (item.get('user') or {}).get('id')
        role = policy['participants'].get(author)
        body = (item.get('content') or {}).get('body', '').strip()
        if item.get('signal') == 'stop':
            body = 'stop'
        current = ledger.current(execution)
        state = current['current_state_id']
        status, message = 'accepted', 'Answer saved. I will use it in the next planning revision.'
        command = 'linear:' + digest([execution, identity])
        try:
            if not role:
                raise ControlError('unauthorized', 'This user is not an authorized participant for this run.')
            if not body or len(body) > 8000:
                raise ControlError('invalid_reply', 'Please keep the reply between 1 and 8,000 characters.')
            principal = Principal('linear:' + author, role, 'linear-agent-session')
            def execute(action, parameters=None, suffix=''):
                # Freeze requests so replay after a transition uses the original expected state.
                request = event(ledger, execution, 'linear_planning_command', command + suffix + ':request', {
                    'action': action, 'expected_state': state, 'confirmed': True, 'parameters': parameters or {}})
                receipt = service.execute(execution, command_id=command + suffix, principal=principal, request=request)
                if receipt['status'] != 'completed':
                    raise ControlError('command_denied', 'This action was not authorized by the workflow.')
                return receipt
            approval = re.fullmatch(r'approve plan v([1-9][0-9]*)', body, re.IGNORECASE)
            if approval:
                # Recover an approval already committed before a crash, without interpreting
                # the same words as permission for a later checkpoint.
                try:
                    previous = ledger.control_command(command)
                except LedgerError:
                    previous = None
                if previous and previous['status'] == 'completed':
                    event(ledger, execution, 'linear_planning_reply', key, {'activity_id': identity,
                        'status': 'accepted', 'message': 'Your plan approval was already recorded.',
                        'received_at': ledger.clock(), 'author': author})
                    continue
                if state != 'PlanReview':
                    raise ControlError('not_plan_review', 'Plan approval is available only at the planning review checkpoint.')
                # A complete snapshot may contain a newer answer after this approval.
                # Never start implementation while known scope changes remain unconsumed.
                for other in pending:
                    other_body = (other.get('content') or {}).get('body', '').strip()
                    if (other['id'] != identity and (other.get('user') or {}).get('id') in policy['participants']
                            and other.get('signal') != 'stop' and other_body
                            and other_body.lower() not in ('pause', 'resume', 'stop')
                            and not re.match(r'^approve\b', other_body, re.IGNORECASE)):
                        raise ControlError('pending_answers', 'There are new answers in this conversation. Wait for a revised plan before approving.')
                if role != 'approver':
                    raise ControlError('approval_denied', 'Only a configured approver may approve this plan.')
                reviews = events(ledger, execution, 'linear_planning_review')
                review = reviews[-1] if reviews else None
                if not review or review['revision'] != int(approval[1]) or review['questions'] or paused(ledger, execution):
                    raise ControlError('stale_revision', 'That revision is not available for approval. Review the latest plan and resolve its questions first.')
                sent = ledger.connection.execute('SELECT confirmed_at FROM linear_agent_activities WHERE execution_id=? AND semantic_key=? AND status=\'confirmed\'',
                                                 (execution, review['semantic_key'])).fetchone()
                if not sent or _time(item['createdAt']) <= _time(sent[0]):
                    raise ControlError('unseen_revision', 'Approval must be sent after the plan is displayed in this session.')
                latest = view(ledger, execution)
                binding = review['binding']
                if latest.get('needs_revision') or latest.get('plan_sha') != binding['plan_sha'] or latest.get('digest') != binding['digest']:
                    raise ControlError('stale_revision', 'New answers changed the plan. Wait for the revised plan before approving.')
                execute('approve', {'plan_sha': binding['plan_sha'], 'requirements_digest': binding['requirements_digest'], 'note': body})
                message = f"Plan v{review['revision']} approved. Implementation is queued; merge and deployment remain separate."
            elif item.get('signal') == 'stop' or body.lower() == 'stop':
                execute('cancel', {'reason': 'Stopped from Linear'})
                message = 'Run canceled. No further stages will start.'
            elif body.lower() in ('pause', 'resume'):
                if current['status'] != 'running':
                    raise ControlError('terminal_run', 'This run has ended.')
                is_paused = body.lower() == 'pause'
                event(ledger, execution, 'linear_planning_pause', command, {'paused': is_paused, 'author': author})
                message = ('Paused before the next stage. Any in-flight stage may finish; use stop to cancel.' if is_paused else 'Resumed. Saved answers will be incorporated before approval.')
            elif re.match(r'^approve\b', body, re.IGNORECASE):
                raise ControlError('ambiguous_approval', 'To approve, reply exactly “approve plan vN” using the latest displayed revision number.')
            else:
                if state not in ('Todo', 'PlanQueued', 'Planning', 'Autoplanning', 'PlanReview'):
                    raise ControlError('planning_closed', 'Planning is closed for this run. Start a new planning run for scope changes.')
                # A running child retains its frozen input. Keep the prompt durable in Linear
                # and retry after it exits instead of weakening the dispatch fence.
                if current.get('attempt') or ledger.connection.execute(
                        "SELECT 1 FROM scheduler_dispatches WHERE execution_id=? AND status='dispatching'", (execution,)).fetchone():
                    continue
                execute('planning_message', {'body': body})
        except (ControlError, LedgerError) as error:
            status, message = 'rejected', str(error)
        event(ledger, execution, 'linear_planning_reply', key, {'activity_id': identity, 'status': status,
              'message': message, 'received_at': ledger.clock(), 'author': author})
    resume_planning(runtime, execution, policy)


def resume_planning(runtime, execution, policy):
    ledger = runtime.ledger
    current = ledger.current(execution)
    if current['current_state_id'] != 'PlanReview' or paused(ledger, execution):
        return
    value = view(ledger, execution)
    if not value.get('needs_revision'):
        return
    replies = events(ledger, execution, 'linear_planning_reply')
    if replies and (datetime.now(timezone.utc) - _time(replies[-1]['received_at'])).total_seconds() < policy.get('quiet_seconds', 5):
        return
    messages = conversation(ledger, execution)['messages']
    author = messages[-1]['author']
    user = author.removeprefix('linear:')
    if user not in policy['participants']:
        return
    runtime.control_service(current['project_key']).execute(execution,
        command_id='linear-revise:' + digest([execution, value['digest']]),
        principal=Principal(author, policy['participants'][user], 'linear-agent-session'),
        request={'action': 'transition', 'expected_state': 'PlanReview', 'parameters': {
            'to_state': 'Planning', 'owner': runtime.owner + ':linear-planning', 'feedback': [{
                'source': 'linear', 'kind': 'changes_requested', 'author': author,
                'body': 'Incorporate the saved planning conversation and produce the next review.',
                'url': 'control://linear-planning/' + execution}]}})


def _async_read(runtime, key, read, consume, *, launch=True):
    """At most one background planning read; all ledger/control work stays on its owner."""
    if not hasattr(runtime, '_planning_reads'):
        runtime._planning_reads = {}
    entry = runtime._planning_reads.setdefault(key, {'inflight': False, 'last': None, 'results': queue.Queue()})
    try:
        value, error, completed_at = entry['results'].get_nowait()
    except queue.Empty:
        pass
    else:
        entry['inflight'] = False
        entry['last'] = time.monotonic()
        if time.monotonic() - completed_at > 10:
            entry['last'] = None
            return
        if error is not None:
            raise error
        consume(value)
        return
    if not launch or any(item['inflight'] and item['results'].empty() for item in runtime._planning_reads.values()):
        return
    now = time.monotonic()
    if entry['last'] is not None and now - entry['last'] < 5:
        return
    entry['last'], entry['inflight'] = now, True
    results = entry['results']
    def run():
        try:
            results.put((read(), None, time.monotonic()))
        except Exception as error:
            results.put((None, error, time.monotonic()))
    threading.Thread(target=run, name='dotfactory-planning-read', daemon=True).start()


def _diagnostic(runtime, project, execution, error):
    runtime.ledger.record_operating_receipt('linear-planning', project, execution,
        {'status': 'needs_attention', 'message': str(error)})


def poll(runtime):
    for run in runtime._owned_runs('running'):
        execution = str(run['id'])
        try:
            policy = policy_for(runtime, execution)
            if not policy:
                continue
            bound = runtime.ledger.linear_agent_session(execution)
            if not bound.get('session_id'):
                continue
            worker = runtime.linear_agent_workers.get(run['project_key'])
            if not worker:
                raise LedgerError('Linear Agent Session projection is unavailable')
            _async_read(runtime, 'session:' + execution,
                lambda client=worker.client, identity=bound['session_id']: client.planning_session(identity),
                lambda value, execution=execution: receive(runtime, execution, value),
                launch=not runtime.ledger.current(execution).get('attempt'))
        except (LinearAPIError, LedgerError, ControlError, ValueError) as error:
            _diagnostic(runtime, run['project_key'], execution, error)
    # Drain abandoned reads too: cancellation/config changes must not leave the
    # global read slot permanently occupied by a result that no longer has a consumer.
    active = {'session:' + str(run['id']) for run in runtime._owned_runs('running')
              if runtime.config.values['projects'][run['project_key']].get('linear_planning', {}).get('enabled')}
    for key, entry in getattr(runtime, '_planning_reads', {}).items():
        if key.startswith('session:') and key not in active:
            try:
                entry['results'].get_nowait()
                entry['inflight'] = False
            except queue.Empty:
                pass


def discovery_read(client, config, existing):
    from .work_queue import rejection
    for issue in client.queue_issues(project_id=config['tracker_project_id'], status_names=['Planning']):
        if issue.get('identifier') in existing:
            continue
        fresh = client.queue_issue(issue['identifier'])
        # Status is the opt-in here; normal label admission is unchanged elsewhere.
        if rejection(fresh, {'admission_label': None}, config['tracker_project_id'], ['Planning'], config.get('tracker_team_id')) is None:
            return fresh
    return None


def discover(runtime):
    """Opt-in Planning pickup; network reads cannot block the local owner loop."""
    from .budgets import evaluate
    from .lifecycle import LifecycleError
    for key in runtime.project_keys:
        policy = runtime.config.values['projects'][key].get('linear_planning', {})
        if not policy.get('enabled'):
            continue
        try:
            config = runtime.config.resolve_project(key, environment=runtime.environment)
            if key not in runtime.linear_workers or key not in runtime.linear_agent_workers:
                raise LedgerError('Enable Linear and Agent Session projection before planning.')
            client = runtime.linear_workers[key].client
            existing = {r['work_item_identifier'] for r in runtime._owned_runs() if r['project_key'] == key}
            def admit(issue, project_key=key):
                if issue is not None and not runtime._has_execution(project_key, issue['identifier']):
                    if evaluate(runtime.ledger, runtime.config.values.get('budgets', {}), project_key)['status'] != 'blocked':
                        runtime.start_issue(project_key, issue['identifier'], admission_snapshot=issue)
            _async_read(runtime, 'discovery:' + key,
                lambda client=client, config=config, existing=existing: discovery_read(client, config, existing), admit)
        except (LinearAPIError, LedgerError, LifecycleError, ValueError) as error:
            _diagnostic(runtime, key, None, error)
    enabled = {'discovery:' + key for key in runtime.project_keys
               if runtime.config.values['projects'][key].get('linear_planning', {}).get('enabled')}
    for key, entry in getattr(runtime, '_planning_reads', {}).items():
        if key.startswith('discovery:') and key not in enabled:
            try:
                entry['results'].get_nowait()
                entry['inflight'] = False
            except queue.Empty:
                pass
