"""CLAUDE.md e uma copia gerada do AGENTS.md: as regras nunca divergem."""

from __future__ import annotations

import importlib.util
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _sync():
    spec = importlib.util.spec_from_file_location("sync_agents", RAIZ / "scripts" / "sync_agents.py")
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_claude_md_e_copia_exata_do_agents_md():
    assert (RAIZ / "CLAUDE.md").read_text() == _sync().gerar(), "rode: python scripts/sync_agents.py"
    assert _sync().main(["--check"]) == 0


def test_regras_essenciais_estao_nos_dois_arquivos():
    for nome in ("AGENTS.md", "CLAUDE.md"):
        texto = (RAIZ / nome).read_text()
        assert "Autonomia total" in texto and "Três travas" in texto
        assert "live" in texto and "backup verificado" in texto and "Dinheiro" in texto


def test_claude_md_avisa_que_e_gerado():
    assert (RAIZ / "CLAUDE.md").read_text().startswith("<!-- GERADO de AGENTS.md")
