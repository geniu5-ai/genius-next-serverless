"""Durable auto-release da marcacao de dono humano (automation_state='crew').

Ordem do chairman (08/out/2026): a marcacao de dono humano com prazo --
clique Assumir em qualquer tenant, handoff automatico (modelo, promocao, guarda
de saida) nos tenants de ``HANDOFF_AUTO_RELEASE_TENANTS`` -- volta sozinha para
o bot ``CREW_IN_AUTO_RELEASE_MINUTES`` (4,5 min) depois do ultimo ato da equipe
naquele contato, desde que o paciente nao esteja esperando resposta.  A rota
fixa de seguranca e as marcacoes anteriores a esta lei nao tem prazo
(crew_in_at NULL).  A regra mora inteira no SQL (``_CREW_RELEASE_ELIGIBLE`` em
app/durable/persistence.py); aqui so o relogio.

Relogio (20/out/2026): quem chama ``release_stale_crew_in`` e o tick unico do
Manager (app/durable/manager_tick.py), a cada 60 s -- antes era um
agendamento proprio de 20 s.  A devolucao cai entre 4:30 e ~5:30 depois
do ultimo ato da equipe.  O follow-up nao depende deste relogio: na hora do
disparo ele mesmo devolve a marcacao vencida (mesma regra), o que cobre restart
e atraso de fila.
"""

from __future__ import annotations

import logging

from app.durable import persistence
from app.durable.persistence import auto_release_stale_crew_in_txn
from app.settings import get_settings

logger = logging.getLogger(__name__)

# 20/out/2026: o relogio deixou de ser um agendamento proprio de 20 s (4.315
# execucoes/dia, 27 devolucoes em 7 dias) e passou a ser o tick unico do Manager
# (app/durable/manager_tick.py, 60 s). A regra e o SQL nao mudaram; a devolucao
# cai entre 4:30 e ~5:30 depois do ultimo ato da equipe.
CREW_RELEASE_SWEEP_INTERVAL_SECONDS = 60


def crew_release_window_seconds() -> float:
    """Janela de inatividade da equipe, em segundos (4,5 min = 270 s)."""
    return persistence.crew_release_window_seconds(get_settings())


def log_crew_release_config() -> None:
    """Startup: registra a configuracao e acusa a janela que mata o follow-up.

    Nunca levanta.  A 1a tentativa do follow-up sai 5 min depois da resposta
    de origem; com a janela em 5 min ou mais a marcacao ainda vale nessa hora
    (ou vence no mesmo segundo) e o follow-up depois de um handoff automatico
    para (stopped) em silencio.  Por isso o ERRO no journal.

    Com ``HANDOFF_AUTO_RELEASE_TENANTS`` vazio a lei de 08/out/2026 nao vale
    para nenhum handoff automatico: a marcacao fica sem prazo e sem follow-up,
    exatamente como antes do branch.  E o unico ajuste que o deploy tem de
    fazer fora do codigo, e falha em silencio -- por isso o AVISO.
    """
    try:
        from app.durable.followup import FOLLOWUP_V2_WAITS_SECONDS

        settings = get_settings()
        window = persistence.crew_release_window_seconds(settings)
        first_attempt = float(FOLLOWUP_V2_WAITS_SECONDS[0])
        tenants = sorted(
            tenant.strip()
            for tenant in (getattr(settings, "HANDOFF_AUTO_RELEASE_TENANTS", "") or "").split(",")
            if tenant.strip()
        )
        logger.info(
            "crew release: janela %.0f s, sweep a cada %d s, handoff automatico com prazo em %s",
            window,
            CREW_RELEASE_SWEEP_INTERVAL_SECONDS,
            ", ".join(tenants) or "nenhum tenant",
        )
        if window >= first_attempt:
            logger.error(
                "crew release: CREW_IN_AUTO_RELEASE_MINUTES=%s (%.0f s) nao fica abaixo da 1a "
                "tentativa do follow-up (%.0f s): o follow-up depois de um handoff automatico "
                "vai parar. Use 4.5 (com ponto) ou remova a linha do env.",
                settings.CREW_IN_AUTO_RELEASE_MINUTES,
                window,
                first_attempt,
            )
        if not tenants:
            logger.warning(
                "crew release: HANDOFF_AUTO_RELEASE_TENANTS vazio - nenhum tenant cria "
                "marcacao de equipe com prazo nem agenda follow-up no handoff automatico "
                "(lei de 08/out/2026 desligada; so o clique Assumir expira). Defina a "
                "lista no env antes de considerar a lei em vigor."
            )
    except Exception:  # pragma: no cover - log de startup nunca derruba o boot
        logger.warning("crew release: configuracao nao verificada", exc_info=True)


def release_stale_crew_in() -> list[dict]:
    """Devolve ao bot as marcacoes de equipe vencidas. Chamada pelo tick do Manager.

    Mesma regra de antes (``auto_release_stale_crew_in_txn``); so mudou quem
    chama o relogio.  Devolve as linhas liberadas (para teste e log).
    """
    released = auto_release_stale_crew_in_txn(crew_release_window_seconds())
    if released:
        # Telefone so com os 4 ultimos digitos: o log vai para o journal.
        # .get: uma linha gravada pelo codigo anterior (sem automation_state no
        # output) e recuperada pelo codigo novo nao pode quebrar no log.
        logger.info(
            "crew_in_auto_release_scheduled: released %d stale handoff(s): %s",
            len(released),
            ", ".join(
                f"{row.get('tenant_id')}/...{str(row.get('phone'))[-4:]}"
                f"->{row.get('automation_state', '?')}"
                for row in released
            ),
        )
    return list(released or [])
