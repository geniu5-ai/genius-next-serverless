#!/usr/bin/env python3
"""Regenera o CLAUDE.md como copia completa do AGENTS.md (a fonte das regras).

O Claude Code le o CLAUDE.md; os demais agentes leem o AGENTS.md. Um ``@import`` pode nao ser
expandido em todo ambiente, entao o CLAUDE.md leva o texto inteiro. Editar SO o AGENTS.md e rodar:

    python scripts/sync_agents.py          # regrava o CLAUDE.md
    python scripts/sync_agents.py --check  # sai com 1 se estiverem diferentes (usado no teste)
"""

from __future__ import annotations

import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CABECALHO = (
    "<!-- GERADO de AGENTS.md por scripts/sync_agents.py: NAO edite este arquivo; "
    "edite AGENTS.md e rode o script. -->\n\n"
)


def gerar() -> str:
    corpo = (RAIZ / "AGENTS.md").read_text()
    return CABECALHO + corpo.replace("# AGENTS.md: Standby", "# CLAUDE.md: Standby", 1)


def main(argv: list[str]) -> int:
    esperado = gerar()
    destino = RAIZ / "CLAUDE.md"
    if argv == ["--check"]:
        atual = destino.read_text() if destino.exists() else ""
        if atual != esperado:
            print("CLAUDE.md fora de sincronia com AGENTS.md: rode scripts/sync_agents.py", file=sys.stderr)
            return 1
        return 0
    destino.write_text(esperado)
    print("CLAUDE.md regenerado a partir de AGENTS.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
