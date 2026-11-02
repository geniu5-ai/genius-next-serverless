"""Ferramentas que o agente de resposta pode chamar durante um turno."""
from . import procuracao_tool as _procuracao_tool  # noqa: F401 — registra @reply_agent.tool
from . import web_search as _web_search  # noqa: F401 — registra pesquisar_web (opt-in por tenant)
from . import reasoning as _reasoning  # noqa: F401 — registra raciocinar (flag MANAGER_AGENT_TENANTS)
from . import agenda as _agenda  # noqa: F401 — registra listar_horarios/reservar_horario (flag AGENDA_TOOL_TENANTS)
