"""WAKE: pergunta ao banco, assinatura do script e o endpoint /v1/control/wake."""

from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import wake
from app.hmac_auth import verify_control_signature

SEGREDO = "segredo-de-controle-de-teste"


def _script():
    caminho = Path(__file__).resolve().parent.parent / "scripts" / "wake_check.py"
    spec = importlib.util.spec_from_file_location("wake_check", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_estado_acorda_com_qualquer_trabalho_e_dorme_sem_nada():
    assert not wake.WakeState(0, 0, 0).wake_needed
    assert wake.WakeState(1, 0, 0).wake_needed
    assert wake.WakeState(0, 1, 0).wake_needed
    assert wake.WakeState(0, 0, 3).wake_needed


def test_linha_do_banco_vira_estado_e_nulo_conta_zero():
    assert wake.state_from_row((2, 0, 1)) == wake.WakeState(2, 0, 1)
    assert wake.state_from_row({"jobs_vencidos": None, "jobs_travados": 1, "pendentes": 0}) == wake.WakeState(0, 1, 0)


def test_parametros_e_tenants():
    assert wake.parse_tenants(" a, b ,,c ") == ["a", "b", "c"]
    assert wake.parse_tenants("") == []
    params = wake.wake_params(tenants=["a", ""], lookahead_s=120)
    assert params["tenants"] == ["a"] and params["lookahead_s"] == 120
    # todos os marcadores do SQL tem parametro
    for chave in params:
        assert f"%({chave})s" in wake.WAKE_SQL


def test_assinatura_do_script_passa_na_verificacao_do_servico():
    script = _script()
    corpo = b'{"reason":{}}'
    ts = str(int(time.time()))
    assinatura = script.assinar(SEGREDO, corpo, ts)
    assert verify_control_signature(corpo, ts, assinatura, SEGREDO)
    assert not verify_control_signature(corpo, ts, assinatura, "outro")


def test_script_sem_banco_sai_com_codigo_2(monkeypatch):
    monkeypatch.delenv("WAKE_DATABASE_URL", raising=False)
    assert _script().main([]) == 2


def test_script_check_only_nao_chama_o_servico(monkeypatch):
    script = _script()
    monkeypatch.setenv("WAKE_DATABASE_URL", "postgresql://x/x")
    monkeypatch.setattr(script, "medir", lambda *a, **k: wake.WakeState(1, 0, 0))
    monkeypatch.setattr(script, "acordar", lambda *a, **k: pytest.fail("nao devia acordar"))
    assert script.main(["--check-only"]) == 0


def test_script_sem_trabalho_nao_acorda(monkeypatch):
    script = _script()
    monkeypatch.setenv("WAKE_DATABASE_URL", "postgresql://x/x")
    monkeypatch.delenv("WAKE_FORCE", raising=False)
    monkeypatch.setattr(script, "medir", lambda *a, **k: wake.WakeState(0, 0, 0))
    monkeypatch.setattr(script, "acordar", lambda *a, **k: pytest.fail("nao devia acordar"))
    assert script.main([]) == 0


def test_script_com_trabalho_acorda_e_falha_vira_codigo_1(monkeypatch):
    script = _script()
    monkeypatch.setenv("WAKE_DATABASE_URL", "postgresql://x/x")
    monkeypatch.setenv("WAKE_URL", "https://exemplo.test/")
    monkeypatch.setenv("CONTROL_HMAC_SECRET", SEGREDO)
    monkeypatch.setattr(script, "medir", lambda *a, **k: wake.WakeState(0, 2, 0))
    chamadas = []
    monkeypatch.setattr(script, "acordar", lambda url, s, m: chamadas.append(url) or {"http": 200})
    assert script.main([]) == 0 and chamadas == ["https://exemplo.test"]

    def falha(*a, **k):
        raise RuntimeError("nao acordou")

    monkeypatch.setattr(script, "acordar", falha)
    assert script.main([]) == 1


@pytest.fixture
def cliente(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.setenv("CONTROL_HMAC_SECRET", SEGREDO)
    get_settings.cache_clear()
    monkeypatch.setattr(wake, "consultar", lambda *a, **k: wake.WakeState(1, 0, 2))
    yield TestClient(main_mod.app)  # sem `with`: nao roda o lifespan (sem worker/banco)
    get_settings.cache_clear()


def _cabecalhos(corpo: bytes, segredo: str = SEGREDO, ts: str | None = None):
    ts = ts or str(int(time.time()))
    return {
        "X-Control-Timestamp": ts,
        "X-Control-Signature-256": _script().assinar(segredo, corpo, ts),
        "content-type": "application/json",
    }


def test_endpoint_wake_autorizado_devolve_o_estado(cliente):
    corpo = json.dumps({"reason": {}}).encode()
    resposta = cliente.post("/v1/control/wake", content=corpo, headers=_cabecalhos(corpo))
    assert resposta.status_code == 200
    assert resposta.json() == {
        "status": "awake", "jobs_vencidos": 1, "jobs_travados": 0, "pendentes": 2, "wake_needed": True,
    }


def test_endpoint_wake_assinatura_errada_ou_vencida_da_401(cliente):
    corpo = b"{}"
    assert cliente.post("/v1/control/wake", content=corpo, headers=_cabecalhos(corpo, "errado")).status_code == 401
    velho = str(int(time.time()) - 3600)
    assert cliente.post("/v1/control/wake", content=corpo, headers=_cabecalhos(corpo, ts=velho)).status_code == 401
    assert cliente.post("/v1/control/wake", content=corpo).status_code == 401


def test_endpoint_wake_sem_segredo_configurado_falha_fechado(monkeypatch):
    from app import main as main_mod
    from app.settings import get_settings

    monkeypatch.delenv("CONTROL_HMAC_SECRET", raising=False)
    get_settings.cache_clear()
    try:
        assert TestClient(main_mod.app).post("/v1/control/wake", content=b"{}").status_code == 503
    finally:
        get_settings.cache_clear()
