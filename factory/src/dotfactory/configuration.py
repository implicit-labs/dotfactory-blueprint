"""Read-only effective execution configuration; no runtime or credential resolution."""
from __future__ import annotations

import json
from pathlib import Path

from .execution import resolve_settings
from .worker import digest
from .workflow import load_workflow


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
    selected = config.resolve_workflow(project)
    graph = load_workflow(selected['path'], profile_paths=selected['profile_paths'],
                          factory_defaults=selected['defaults'])
    if not config.values.get('execution'):
        raise ValueError('effective execution configuration requires worker execution settings')
    policy, provenance = resolve_settings(config.values['execution'],
        config.values['projects'][project].get('execution', {}), override,
        work_states=[state['id'] for state in graph.states if state['kind'] == 'work'])
    return policy, provenance, graph


def preview(config, project, override=None):
    policy, provenance, graph = resolve(config, project, override)
    # Connection metadata and credential references are intentionally not exposed.
    result = {'schema_version': 1, 'project': project, 'workflow_digest': graph.digest,
              'stages': policy['stages'], 'provenance': provenance,
              'lanes': {'worker': 'stage checks and readiness on the selected worker',
                        'coordinator': 'pinned delivery verifier with isolated HOME/PATH and no credentials'},
              'probes_executed': False}
    result['digest'] = digest({'stages': policy['stages'], 'provenance': provenance,
                               'workflow_digest': graph.digest})
    return result
