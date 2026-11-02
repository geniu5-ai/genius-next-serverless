"""Módulo de status: regra determinística + classificação consultiva + workflow durável.

``rules`` — regex autoritativo (win/loss/None).
``audit`` — contrato Pydantic e a cascata rule→embedding→LLM residual.
``workflow`` — o job durável que roda pós-rascunho (``STATUS_AUDIT_ENABLED``).
"""
