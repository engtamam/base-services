"""PrivacySweeper: the worker whose absence made every erasure endpoint decorative.

The properties under test are the ones an operator depends on at 3am: one broken target cannot
freeze erasure for the rest of the platform, a backlog degrades into several runs rather than one
that never ends, and anything jammed is NAMED rather than silently dropped.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.models import Dsar
from app.core.privacy_sweeper import PrivacySweeper


def run(c):
    return asyncio.run(c)


class _Dsars:
    """Pages through open DSARs the way the real store does."""

    def __init__(self, ids, page=2):
        self.open = [Dsar(id=i, tenant="acme", subject="u", type="erasure",
                          targets={"svc:cat": False}) for i in ids]
        self.page = page

    async def open_dsars(self, limit, after=None):
        start = int(after or 0)
        rows = self.open[start:start + min(limit, self.page)]
        nxt = start + len(rows)
        return rows, (str(nxt) if nxt < len(self.open) else None)


class _Dispatcher:
    def __init__(self, behaviour=None):
        self.driven = []
        self.behaviour = behaviour or (lambda d: {"complete": True, "stuck": []})

    async def drive(self, dsar):
        self.driven.append(dsar.id)
        out = self.behaviour(dsar)
        if isinstance(out, Exception):
            raise out
        return out


def test_sweeps_every_open_dsar_across_pages():
    disp = _Dispatcher()
    out = run(PrivacySweeper(dsars=_Dsars(["a", "b", "c", "d", "e"]), dispatcher=disp,
                             page_size=2).run())
    assert disp.driven == ["a", "b", "c", "d", "e"]
    assert out["swept"] == 5 and out["completed"] == 5 and not out["truncated"]


def test_one_exploding_target_does_not_freeze_the_rest_of_the_platform():
    # The blast radius of a single bad target has to stay at that target.
    def behave(d):
        return RuntimeError("target on fire") if d.id == "b" else {"complete": True}

    disp = _Dispatcher(behave)
    out = run(PrivacySweeper(dsars=_Dsars(["a", "b", "c"]), dispatcher=disp).run())
    assert disp.driven == ["a", "b", "c"]      # it kept going
    assert out["errored"] == 1 and out["completed"] == 2


def test_a_backlog_is_capped_per_run_and_says_it_was_truncated():
    disp = _Dispatcher()
    out = run(PrivacySweeper(dsars=_Dsars([str(i) for i in range(20)]), dispatcher=disp,
                             page_size=5, max_per_run=7).run())
    assert out["swept"] == 7 and out["truncated"] is True
    assert len(disp.driven) == 7               # a cap that silently swept everything is not a cap


def test_stuck_targets_are_named_not_summarised_away():
    stuck = {"target": "billing-service:invoices", "attempts": 8, "detail": "HTTP 500"}
    disp = _Dispatcher(lambda d: {"complete": False, "stuck": [stuck]})
    out = run(PrivacySweeper(dsars=_Dsars(["a"]), dispatcher=disp).run())
    assert out["completed"] == 0
    assert out["stuck"] == [stuck]             # the operator needs the SERVICE name, not a count


def test_nothing_open_is_a_clean_no_op():
    out = run(PrivacySweeper(dsars=_Dsars([]), dispatcher=_Dispatcher()).run())
    assert out == {"swept": 0, "completed": 0, "errored": 0, "stuck": [], "truncated": False}
