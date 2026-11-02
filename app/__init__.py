"""Genius-Next application package.

Root module aliases keep the legacy ``app.<name>`` surface working after the
reorganization; they are the SAME module objects as the canonical paths, so
tests that patch module attributes (e.g. monkeypatch.setattr(app.outbox, ...))
keep patching the real implementation.
"""
import sys as _sys

# NAO aliasar "app.reply": isso substituia o PACOTE app/reply pelo modulo
# llm em sys.modules, e qualquer modulo novo dentro de app/reply/ virava
# inimportavel ("app.reply is not a package"). Descoberto em 25/ago/2026 ao
# criar app/reply/agent.py.

from app.durable import flow, outbox, persistence, tenant_registry
from app.ingress import evolution
from app.reply import split  # app.status.workflow ja e importado via app.durable.flow

_aliases = {
    "app.flow": flow,
    "app.outbox": outbox,
    "app.persistence": persistence,
    "app.tenant_registry": tenant_registry,
    "app.evolution": evolution,
    "app.output_splitter": split,
}
for _name, _mod in _aliases.items():
    _sys.modules[_name] = _mod
