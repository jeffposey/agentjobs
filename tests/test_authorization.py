"""The route table: every mutating endpoint is named, and the gate actually runs.

The first class here is the one that stops this task rotting. The capability table is
only as good as its coverage, and coverage that depends on somebody remembering to add a
line is coverage that will be wrong within a month -- so the enumeration is checked
against the application's own router rather than against a list in a docstring.
"""

from __future__ import annotations

from typing import Set

from fastapi.routing import APIRoute

from agentjobs.api.authorization import (
    ROUTE_CAPABILITIES,
    RouteRule,
    denial_status,
    rule_for,
)
from agentjobs.api.main import app
from agentjobs.capabilities import (
    ACTOR_MISMATCH,
    CAPABILITY_DENIED,
    Capability,
    IDENTITY_UNRESOLVED,
    OWN_TASK_ONLY,
    WRONG_RUN,
    WRONG_TASK,
)

READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def mutating_endpoints() -> Set[str]:
    """Every endpoint behind a method that writes, taken from the live application.

    Read off ``app.routes`` rather than off ``openapi.json``, because the question is
    what this process will actually serve. Deduplicated by function name for the reason
    the table is keyed that way: each router is mounted twice and there is one function
    behind both mounts.
    """
    names: Set[str] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if set(route.methods or set()) <= READ_METHODS:
            continue
        names.add(route.endpoint.__name__)
    return names


class TestEveryMutatingRouteIsAccountedFor:
    """ac-1: the enumeration is written down, and checked against reality."""

    def test_no_mutating_route_is_missing_from_the_table(self) -> None:
        """A new POST with no decision about who may call it turns this red.

        The failure message names the endpoints rather than only the count, because the
        fix is to add a line per endpoint and a count does not say which.
        """
        missing = sorted(mutating_endpoints() - set(ROUTE_CAPABILITIES))
        assert not missing, (
            "These mutating endpoints have no entry in ROUTE_CAPABILITIES, so nothing "
            "checks who may call them: " + ", ".join(missing)
        )

    def test_the_table_names_no_route_that_does_not_exist(self) -> None:
        """The other direction: a renamed endpoint silently unchecks its route.

        Keyed by function name, so a rename is invisible at import and would leave the
        route unguarded with the old entry still sitting in the table looking correct.
        The two output routes are reads and so are excluded from the mutating set.
        """
        reads = {"read_dispatch_run_output", "read_task_finish_output"}
        stale = sorted(set(ROUTE_CAPABILITIES) - mutating_endpoints() - reads)
        assert not stale, (
            "ROUTE_CAPABILITIES names endpoints the application does not serve, so "
            "their routes may have been renamed out from under the table: " + ", ".join(stale)
        )

    def test_the_two_output_reads_are_served(self) -> None:
        """ac-5's routes are in the table on purpose; assert they are real routes."""
        served = {route.endpoint.__name__ for route in app.routes if isinstance(route, APIRoute)}
        assert {"read_dispatch_run_output", "read_task_finish_output"} <= served

    def test_a_scoped_capability_is_never_declared_without_something_to_scope_it_by(
        self,
    ) -> None:
        """A rule granting a scoped capability with no path parameter scopes nothing.

        Review is exempt and stays exempt: no run holds TASK_REVIEW at all, so the scope
        check is unreachable there and declaring a parameter would imply a scoped grant
        that does not exist.
        """
        for name, rule in sorted(ROUTE_CAPABILITIES.items()):
            if rule.capability in OWN_TASK_ONLY:
                assert rule.task_param, f"{name} needs the task parameter it is scoped by"
            if rule.capability is Capability.RUN_OUTPUT:
                assert rule.task_param or rule.run_param, f"{name} scopes nothing"


class TestTheGateIsReachable:
    """The wiring, as opposed to the rule."""

    def test_the_capability_dependency_runs_for_every_api_route(self) -> None:
        """Installed application-wide, so a route added later is covered by default.

        Asserted on the solved dependant of every route rather than on the app's
        declaration, because the declaration is the intent and this is the effect --
        FastAPI copies application dependencies into each route as it is registered, and
        a route registered by a path this application does not take would not get one.
        """
        from agentjobs.api.authorization import enforce_capability

        without = [
            route.path
            for route in app.routes
            if isinstance(route, APIRoute)
            and enforce_capability
            not in {dependency.call for dependency in route.dependant.dependencies}
        ]
        assert not without, "routes the capability gate does not run for: " + ", ".join(without)

    def test_rule_for_ignores_an_endpoint_the_table_does_not_name(self) -> None:
        """Reads fall through untouched -- what the API exposes is task-333's question."""

        def some_read() -> None:  # pragma: no cover - never called
            return None

        assert rule_for(some_read) is None
        assert rule_for(None) is None

    def test_a_known_endpoint_resolves_to_its_rule(self) -> None:
        from agentjobs.api.routes.tasks import approve_task

        rule = rule_for(approve_task)
        assert rule == RouteRule(Capability.TASK_REVIEW)


class TestDenialStatus:
    """Which refusal is a 403 and which is a 400, and why that is not arbitrary."""

    def test_every_who_are_you_refusal_is_forbidden(self) -> None:
        for code in (CAPABILITY_DENIED, WRONG_TASK, WRONG_RUN, ACTOR_MISMATCH):
            assert denial_status(code) == 403

    def test_an_unresolvable_identity_is_a_bad_request(self) -> None:
        """Not a refusal of the caller: the machine cannot tell who a legitimate caller
        is, and the fix is a config file rather than a different request."""
        assert denial_status(IDENTITY_UNRESOLVED) == 400

    def test_an_unlisted_code_is_forbidden_rather_than_allowed(self) -> None:
        """Including every resolution problem out of `principals`: refused, not
        diagnosed."""
        assert denial_status("something_nobody_has_written_yet") == 403
