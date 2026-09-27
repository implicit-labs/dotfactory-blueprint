"""Read-only effective execution configuration; no runtime or credential resolution."""
from __future__ import annotations

import json
from pathlib import Path

from .execution import resolve_settings
from .worker import digest
from .selection import resolve as resolve_selection


def load_override(path):
    if not path:
        return None
    data = Path(path).read_bytes()
    if len(data) > 65536:
        raise ValueError("execution config exceeds 65536 bytes")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("execution config must be an object")
    return value


def resolve(config, project, override=None):
    graph, selection, placement = resolve_selection(config, project, override)
    if not config.values.get('execution'):
        if placement:
            raise ValueError('run execution overrides require worker execution configuration')
        return {"stages": {}}, {}, graph, selection
    policy, provenance = resolve_settings(config.values['execution'],
        config.values['projects'][project].get('execution', {}), placement,
        work_states=[state['id'] for state in graph.states if state['kind'] == 'work'])
    return policy, provenance, graph, selection


def preview(config, project, override=None, *, environment=None):
    policy, provenance, graph, selection = resolve(config, project, override)
    from .evidence_policy import resolve as resolve_evidence
    evidence = resolve_evidence(config.values, project, (override or {}).get("evidence_policy"))
    from .projection_policy import resolve as resolve_projection
    projection = resolve_projection(config.values, project, (override or {}).get("projection_policy"), environment=environment)
    # Connection metadata and credential references are intentionally not exposed.
    result = {'schema_version': 1, 'project': project, 'workflow_digest': graph.digest,
              'stages': policy['stages'], 'provenance': provenance,
              'selection': selection,
              'evidence_policy': evidence,
              'projection_policy': projection,
              'lanes': {'worker': 'stage checks and readiness on the selected worker',
                        'coordinator': 'pinned delivery verifier with isolated HOME/PATH and no credentials'},
              'probes_executed': False}
    result['digest'] = digest({'stages': policy['stages'], 'provenance': provenance,
                               'workflow_digest': graph.digest, 'selection': selection,
                               'evidence_policy': evidence, 'projection_policy': projection})
    return result
