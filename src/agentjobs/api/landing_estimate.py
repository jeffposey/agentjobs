"""The landing estimate as every surface serves it (task-586).

One function, :func:`landing_estimate`, called by the three places a live finish is
drawn -- the task list's ``live_finish`` chip, the task page's finish panel and the slot
board -- with the :class:`~agentjobs.dispatch.finish_status.FinishStatus` each of them
already holds. The model lives in :mod:`agentjobs.finish_estimate`; this module only
reads it from the right project's store and renders it as
:class:`~agentjobs.models_v2.LandingEstimate`.

**The model is cached per project until history moves.** A task page polls every two
seconds and a list refreshes on its own cadence, and each would otherwise re-read forty
finishes and their steps for an answer that changes only when a finish ends or the
correction is reset. The cache key is exactly those two facts, read with one indexed
query, so a stale model is impossible rather than merely unlikely.

**Nothing here raises.** A store that cannot be read gives no estimate, which a surface
renders as elapsed time only: a label may not cost the read it rides on.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from agentjobs.dispatch.finish_status import FinishStatus
from agentjobs.finish_estimate import Estimate, EstimateModel, estimate_status
from agentjobs.models_v2 import LandingEstimate

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CACHE: Dict[Tuple[str, str], Tuple[Tuple[Any, ...], EstimateModel]] = {}


def _history_key(storage: Any) -> Tuple[Any, ...]:
    connection = storage.read_connection()
    row = connection.execute(
        "SELECT MAX(finished_at) AS last, COUNT(*) AS n FROM finish "
        "WHERE project_id = ? AND outcome = 'finished'",
        (storage.project_id,),
    ).fetchone()
    reset = connection.execute(
        "SELECT reset_at FROM finish_estimator WHERE project_id = ?", (storage.project_id,)
    ).fetchone()
    return (
        row["last"] if row else None,
        row["n"] if row else 0,
        reset["reset_at"] if reset else None,
    )


def model_for(storage: Any) -> Optional[EstimateModel]:
    """The project's estimate model, from cache while its history has not moved."""
    try:
        key = _history_key(storage)
        slot = (str(storage.project_id), str(getattr(storage.database, "path", id(storage))))
        with _LOCK:
            cached = _CACHE.get(slot)
            if cached is not None and cached[0] == key:
                return cached[1]
        model: EstimateModel = storage.finish_estimate_model()
        with _LOCK:
            _CACHE[slot] = (key, model)
        return model
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.warning("could not read the landing estimate model", exc_info=True)
        return None


def view(answer: Optional[Estimate]) -> Optional[LandingEstimate]:
    if answer is None:
        return None
    return LandingEstimate(
        kind=answer.kind,
        progress=answer.progress,
        eta_seconds=answer.eta_seconds,
        overrun=answer.overrun,
        basis=answer.basis,
        typical_seconds=answer.typical_seconds,
        elapsed_seconds=answer.elapsed_seconds,
    )


def landing_estimate(
    status: Optional[FinishStatus], storage: Any, now: Optional[datetime] = None
) -> Optional[LandingEstimate]:
    """The estimate for ``status`` if it is live, from ``storage``'s history."""
    if status is None or not status.live:
        return None
    try:
        model = model_for(storage) if storage is not None else None
        return view(estimate_status(status, model, now))
    except Exception:  # noqa: BLE001 - see the module docstring
        logger.warning("could not estimate a landing", exc_info=True)
        return None


def forget() -> None:
    """Drop every cached model. For tests that rebuild a store under the same path."""
    with _LOCK:
        _CACHE.clear()
