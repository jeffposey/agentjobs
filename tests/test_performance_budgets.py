"""Budgets that stop the task-130 performance work from being given back quietly.

**These assert on work done, not on elapsed time.** A wall-clock threshold means
something different on every machine, drifts with hardware, fails on a loaded laptop,
and eventually gets loosened until it catches nothing -- or deleted, which is worse
than never having had it. "This request read no task files" means the same thing on
every machine forever, and it is precisely the property the original defect violated:
a single `GET /tasks` used to walk a 119-file corpus 476 times.

Measured on the real corpus before the task-131 fixes and after:

    GET /dashboard          7344ms, 952 parses  ->  174ms, 119 parses
    GET /tasks              3659ms, 476 parses  ->  181ms, 119 parses
    GET /tasks/{id}/detail  3954ms, 478 parses  ->  162ms, 119 parses

**The budget is now zero, and that is a stronger assertion than the one it replaces.**
Records are rows (task-402), so a request that parses a task file is a request that has
started reading a directory again -- the exact coupling the migration removed, and the
kind of regression that would otherwise be invisible because it would still return the
right answer. The counter is kept for this and for nothing else.

**And it had nothing left to catch on its own** (task-486). Between task-402 and
2026-09-19 the listing endpoint grew to 10.4 MB and 2.3 seconds on the real backlog
while every assertion in this file stayed green, because an endpoint that runs one
query and serialises ten megabytes parses exactly zero files. The parse counter is a
tripwire on one specific regression, not a measure of how much work a request does.
So two counters were added beside it, chosen for the same durability:

*   **Bytes on the wire, per record.** Exact, identical on every machine and under any
    load, and the units the defect was actually in. Budgeted per record rather than in
    total so the number does not move when the corpus does.
*   **SQL statements per request.** The post-rows equivalent of "files parsed per
    request": the N+1 that would replace the four-walk defect is a query in a loop, and
    nothing else here can see one. Budgeted as a small constant **and** asserted to be
    the same number at two corpus sizes, which is the assertion that fails on the shape
    of the code rather than on how big the backlog got.

**The corpus is generated, at a fixed size.** Running against the repository's own
backlog would tie the thresholds to something that grows, so the suite would start
failing because the project succeeded. The generator is the one `scripts/bench.py` uses,
so a budget and a benchmark run cannot drift apart.
"""

from __future__ import annotations

import atexit
import importlib.util
import shutil
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Iterator, List, Tuple

import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_script(name: str) -> ModuleType:
    """Load a repository script by path, the way the other script-reading tests do.

    ``from scripts.bench import ...`` looks equivalent and is not, because it depends on
    nothing else having claimed the name ``scripts`` first. Something does:
    ``scripts/build_release.py`` opens with ``from scripts.build_frontend import ...``
    under a ``try``, and ``test_frontend_packaging`` execs that file with the repository
    root off ``sys.path``. On Windows the import then succeeds against **pywin32's**
    ``site-packages/win32/scripts`` namespace package, the fallback branch quietly
    works, and ``sys.modules['scripts']`` is left pointing at pywin32 for the rest of
    the session. This module is collected after that one, so the package-style import
    resolved to a directory with no ``bench`` in it and the whole suite stopped at
    collection with ``No module named 'scripts.bench'``.

    Loading by file location cannot be poisoned that way, and it is what
    ``test_frontend_packaging`` and ``test_worktree_bootstrap`` already do.
    """
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = load_script("bench")  # the shared synthetic-corpus generator

from agentjobs.api.dependencies import reset_dependency_cache  # noqa: E402
from agentjobs.api.main import PARSE_COUNT_HEADER, app  # noqa: E402
from agentjobs.execution.factory import execution_store_for  # noqa: E402
from agentjobs.models_v2 import Task  # noqa: E402
from agentjobs.project_setup import build_project_config  # noqa: E402
from agentjobs.sqlstore import SqlTaskStore  # noqa: E402
from agentjobs.store_factory import (  # noqa: E402
    LOCAL_PROJECT_ID,
    default_home,
    local_database,
    open_database,
)
from support import task_store  # noqa: E402


#: Fixed, and stated. Raised from 60 to 480 by task-486: this repository's own backlog
#: was 479 records on 2026-09-20 and grows by roughly a hundred a month, and at 60 a
#: per-record cost is a rounding error inside every threshold here. Fixed rather than
#: "the real backlog" for the reason the module docstring gives -- a threshold tuned to
#: something that grows starts failing because the project succeeded.
CORPUS_SIZE = 480

#: The second size the query budgets are proved against. A count that is the same at 60
#: records and at 480 is a count that does not depend on the corpus, which is the whole
#: claim; a count that merely sits under a ceiling at one size is not.
COMPARISON_CORPUS_SIZE = 60

#: Wall-clock budgets exist only to catch an order-of-magnitude collapse -- a
#: per-record query in a loop, or a lost index. They are deliberately far looser than
#: the measured numbers (a whole-corpus read measures well under 60ms for this size;
#: the budget is 5 seconds).
#:
#: **Do not tighten these.** A performance test that fails on a busy laptop gets
#: disabled, and a disabled test catches nothing at all. The parse-count assertions
#: below are the real gate; these only catch catastrophe.
CATASTROPHE_SECONDS = 5.0

LOCAL = "/api/projects/_local"
SAMPLE_TASK = "task-001-generated-benchmark-task"


# ---------------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------------

#: One built database per size, per process, kept to be copied rather than rebuilt.
#:
#: The fixture below is per test, and has to be -- the suite re-points ``AGENTJOBS_HOME``
#: for every test, so a project built once for the module would be sitting in a home the
#: next test no longer looks in. What that made expensive was building the rows: 1.3
#: seconds per test at 480 records, times every test in this module. Copying a file is
#: about 30ms, and the copy is the same rows.
_DATABASE_TEMPLATES: Dict[int, Path] = {}

#: The YAML text of each generated record, per size, per process. Dumping it is 6ms a
#: record -- the whole cost of the corpus generator -- and the files are identical for
#: every test, so it is paid once and written many times.
_CORPUS_TEXT: Dict[int, List[Tuple[str, str]]] = {}


def _documents(count: int) -> List[Dict[str, Any]]:
    # `bench` is loaded by file location rather than imported, so it is untyped here.
    documents: List[Dict[str, Any]] = bench.synthetic_documents(count)
    return documents


def _corpus_text(count: int) -> List[Tuple[str, str]]:
    cached = _CORPUS_TEXT.get(count)
    if cached is None:
        cached = [
            (
                f"{document['id']}.yaml",
                yaml.safe_dump(document, sort_keys=False, allow_unicode=False),
            )
            for document in _documents(count)
        ]
        _CORPUS_TEXT[count] = cached
    return cached


def _template_database(count: int) -> Path:
    """A store holding the generated corpus, built once per process for copying."""
    cached = _DATABASE_TEMPLATES.get(count)
    if cached is not None and cached.exists():
        return cached
    directory = Path(tempfile.mkdtemp(prefix=f"agentjobs-budget-{count}-"))
    path = directory / "template.db"
    store = SqlTaskStore(open_database(path), LOCAL_PROJECT_ID)
    store.ensure_project(root=str(directory))
    for document in _documents(count):
        store.save_task(Task.model_validate(document))
    # Fold the write-ahead log into the file before anything copies it: the copy is one
    # file, and rows still sitting in the WAL would not be in it.
    store.database.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _DATABASE_TEMPLATES[count] = path
    # It is outside pytest's temporary root, because it outlives the test that built it.
    # Nothing else would remove it, and a full suite builds one per size per xdist
    # worker -- five megabytes each, on a machine that runs three gates at once.
    atexit.register(shutil.rmtree, directory, True)
    return path


def build_budget_project(root: Path, count: int) -> Path:
    """A generated project of stated size, as both rows and files.

    **Both, and the files are not decoration.** The zero-parse tripwire asserts that no
    request reads the task directory; against an empty directory it would assert nothing,
    because a regression that walked it would find nothing to parse and report zero.
    """
    config = root / ".agentjobs" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        yaml.safe_dump(
            build_project_config(project_name="Budget project", user="Budget Human"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    tasks_dir = root / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    for name, text in _corpus_text(count):
        (tasks_dir / name).write_text(text, encoding="utf-8")

    # The rows arrive as a copy of the template rather than by importing the files. The
    # directory is left in place: it is what the implicit project's database is named
    # from, and it is what the parse tripwire needs to exist.
    database = local_database(tasks_dir)
    database.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_template_database(count), database)
    # Opening it through the product's own resolver, which stamps this root onto the
    # project row the template carried the template's own directory in.
    task_store(tasks_dir, root=root)

    # And the machine's execution journal, which is a second database and is created on
    # the first request that builds a row (task-476's queued-dispatch binding). Creating
    # it is twenty-odd `CREATE TABLE` statements; they are once-per-process work, they
    # would land on whichever endpoint a test happened to measure first, and a per-request
    # budget is not about them. Opened here so that cost is outside every measurement
    # rather than inside an arbitrary one.
    execution_store_for(default_home())
    return root


@pytest.fixture()
def budget_project(tmp_path_factory, count_sql) -> Iterator[Path]:
    """A generated project of stated size.

    Per test rather than per module, and the reason is the machine home: the suite
    re-points ``AGENTJOBS_HOME`` for every test, and a project's database is resolved
    against it. A corpus imported once for the module would be sitting in a home the
    next test no longer looks in.

    It takes ``count_sql`` so the statement counter is installed *before* any connection
    to this project is opened. A connection opened first is a connection with no trace
    callback on it, and a query budget that silently counts nothing would pass forever.
    """
    yield build_budget_project(tmp_path_factory.mktemp("budget-project"), CORPUS_SIZE)


@contextmanager
def client_for(root: Path, monkeypatch) -> Iterator[TestClient]:
    """A client served from one project root."""
    monkeypatch.setenv("AGENTJOBS_PROJECT_ROOT", str(root))
    reset_dependency_cache()
    try:
        yield TestClient(app)
    finally:
        reset_dependency_cache()


@pytest.fixture
def budget_client(budget_project: Path, monkeypatch) -> Iterator[TestClient]:
    with client_for(budget_project, monkeypatch) as client:
        yield client


# ---------------------------------------------------------------------------------
# The counters
# ---------------------------------------------------------------------------------


def _parses(response) -> int:
    assert response.status_code == 200, response.text
    return int(response.headers[PARSE_COUNT_HEADER])


#: Statements that are not queries against the data: connection setup and transaction
#: control. Excluding them is what makes a count comparable between the first request a
#: process serves -- which pays for opening and configuring a connection -- and every
#: request after it.
NOT_A_QUERY = frozenset({"PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE"})

#: SQLite's marker for a statement it ran *for* another statement, rather than one the
#: application asked for.
#:
#: **This one is the difference between a budget and a false alarm.** An FTS5 ``MATCH``
#: makes the virtual table read its own index, and every one of those internal reads
#: arrives at the trace callback as its own statement, comment-prefixed. A counter that
#: takes them at face value reports ``GET /search`` running 993 statements over 480
#: records and 171 over 60 -- a textbook query-per-record fan-out that does not exist:
#: ``store.search_tasks`` batches, and the count follows the *index*, not the code.
#: Filed here because the first run of these budgets reported exactly that, and the
#: shape of the number was convincing.
NESTED_STATEMENT_PREFIX = "--"


class StatementLog:
    """Every SQL statement sqlite3 was asked to prepare, in this process."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def record(self, statement: str) -> None:
        self.statements.append(statement)

    def reset(self) -> None:
        self.statements.clear()

    @property
    def queries(self) -> List[str]:
        """The statements the application asked for, of the ones sqlite3 reported."""
        kept = []
        for statement in self.statements:
            text = statement.strip()
            if text.startswith(NESTED_STATEMENT_PREFIX):
                continue
            if text.split(None, 1)[0].upper() in NOT_A_QUERY:
                continue
            kept.append(text)
        return kept


@pytest.fixture
def count_sql(monkeypatch) -> Iterator[StatementLog]:
    """Trace every connection this test opens.

    ``sqlite3.Connection`` is a C type whose methods cannot be patched, so the hook goes
    on ``sqlite3.connect`` instead -- which means it only sees connections opened after
    it is installed. Every fixture here that opens one depends on this one for that
    reason, and :func:`measure` asserts it saw something, so a counter installed too
    late fails loudly instead of reporting a very good zero.
    """
    log = StatementLog()
    real_connect = sqlite3.connect

    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection: sqlite3.Connection = real_connect(*args, **kwargs)
        connection.set_trace_callback(log.record)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield log


class Measurement:
    """What one request cost, in the two units that mean the same thing everywhere."""

    def __init__(self, path: str, response: Any, queries: List[str]) -> None:
        self.path = path
        self.bytes = len(response.content)
        self.queries = queries
        self.response = response

    def per_record(self, records: int) -> float:
        return self.bytes / records

    def report(self) -> str:
        return "\n".join(f"    {' '.join(query.split())[:120]}" for query in self.queries)


def measure(client: TestClient, log: StatementLog, path: str) -> Measurement:
    """Issue ``path`` and count the bytes back and the statements run to produce them.

    **The warm-up is a different endpoint, on purpose.** The first request a process
    serves opens and migrates the database, which is once-per-process work and not what
    a per-request budget is about; warming with the endpoint under test would instead
    hand it a second run of itself, which is exactly the "do not set up the state you
    are checking" trap. ``/revision`` is one statement and touches nothing else, and
    every endpoint here measures the same after it as it does on a second identical
    request -- verified at three corpus sizes on 2026-09-20, which is what says the
    warm-up removes setup rather than removing work.
    """
    client.get(f"{LOCAL}/revision")
    log.reset()
    response = client.get(path)
    assert response.status_code == 200, response.text
    queries = log.queries
    assert queries, (
        f"{path} ran no SQL at all, which cannot be true. The statement counter is "
        "installed on `sqlite3.connect`, so it only sees connections opened after it: "
        "something opened this project's database before the `count_sql` fixture ran."
    )
    return Measurement(path, response, queries)


# ---------------------------------------------------------------------------------
# What a request reads
# ---------------------------------------------------------------------------------


class TestARequestParsesNothing:
    """The durable form of task-132, tightened to zero by task-402.

    Each of these endpoints computes dependency facts, which used to mean four
    independent passes over a directory. None of them may touch a file now, on any
    machine, at any speed.
    """

    @pytest.mark.parametrize(
        "path",
        [
            "/api/projects/_local/tasks",
            "/api/projects/_local/dashboard",
            "/api/projects/_local/tasks/task-001-generated-benchmark-task/detail",
            "/api/projects/_local/search?q=generated",
            "/api/projects/_local/tasks/next",
            "/api/projects/_local/tasks/broken",
        ],
    )
    def test_a_request_reads_no_task_files_at_all(self, budget_client, path: str) -> None:
        parses = _parses(budget_client.get(path))
        assert parses == 0, (
            f"{path} parsed {parses} task files. A project's records are rows, so a "
            "request that parses YAML has started reading a directory again -- which "
            "would still return the right answer today and be wrong the moment the "
            "directory is retired. See task-132 for the 4x walk this replaces."
        )


class TestTheRevisionPollStaysCheap:
    """The 15-second poll runs forever, per connected client, on every device.

    It answers "has anything changed", and it must do that without parsing anything.
    Parsing in the poll would put the whole corpus through pydantic every 15 seconds
    for a question that never needed the answer.
    """

    def test_the_poll_parses_nothing(self, budget_client) -> None:
        parses = _parses(budget_client.get("/api/projects/_local/revision"))
        assert parses == 0, (
            f"GET /revision parsed {parses} task files. The revision signal exists to "
            "answer without loading or validating anything; if it is parsing, something "
            "has started loading tasks to compute it."
        )


# ---------------------------------------------------------------------------------
# What a request sends
# ---------------------------------------------------------------------------------


#: Bytes per record in the response, measured 2026-09-20 against this corpus, with the
#: ceiling each is held to. Per record rather than in total because that is the number
#: that does not move when the backlog does -- which is what makes it a budget on the
#: shape of the payload rather than on the size of the project.
#:
#: The listing's ceiling is the one the front-end budget already holds the same endpoint
#: to (``frontend/e2e/perf-budget.spec.ts``, task-487), deliberately: two budgets on one
#: payload that disagree about what is acceptable is one budget and one argument.
#:
#: **Two of these three numbers are the defect, not the target.** ``/dashboard`` and
#: ``/search`` still answer with whole ``TaskRead`` records -- every task's spec prose,
#: acceptance criteria and complete log -- which is 5.2 MB each at this corpus and the
#: same shape task-484 took out of ``/tasks``. They are recorded and held where they
#: stand rather than fixed, because fixing an endpoint was out of scope for the task
#: that wrote this file (task-486); the finding is on that task's record. A ceiling
#: close above a bad number still catches it getting worse, which is more than the file
#: did before.
PAYLOAD_BUDGETS: Dict[str, Tuple[int, int]] = {
    # path                        measured  ceiling
    f"{LOCAL}/tasks": (785, 1_500),
    f"{LOCAL}/dashboard": (10_888, 15_000),
    f"{LOCAL}/search?q=generated": (10_781, 15_000),
}

#: Responses whose size is set by one record rather than by the backlog. Held to an
#: absolute ceiling here and to not growing with the corpus in
#: :class:`TestAPayloadDoesNotGrowWithTheBacklog`.
FIXED_PAYLOAD_BUDGETS: Dict[str, Tuple[int, int]] = {
    # path                                  measured  ceiling
    f"{LOCAL}/tasks/{SAMPLE_TASK}/detail": (10_822, 32_000),
    f"{LOCAL}/tasks/next": (10_633, 32_000),
}


class TestAResponseIsSizedByWhatItDraws:
    """The defect task-483 exists for, in the units it happened in.

    ``GET /tasks`` was 10.4 MB of whole records -- every task's spec prose, acceptance
    criteria and complete log -- to draw a column of titles (task-484). Nothing in this
    file noticed, because serialising ten megabytes out of one query parses no files and
    runs no extra queries. Bytes are what noticed.
    """

    @pytest.mark.parametrize("path", sorted(PAYLOAD_BUDGETS))
    def test_a_listing_sends_a_row_not_a_record(
        self, budget_client, count_sql: StatementLog, path: str
    ) -> None:
        measured, ceiling = PAYLOAD_BUDGETS[path]
        result = measure(budget_client, count_sql, path)
        # The denominator is the corpus, so say out loud that the response really is
        # answering about all of it. ``?q=generated`` matches every generated record,
        # and a generator that stopped saying "generated" would otherwise quietly turn
        # this budget into a much weaker one.
        body = result.response.json()
        if isinstance(body, list):
            assert len(body) == CORPUS_SIZE, (
                f"{path} answered with {len(body)} of {CORPUS_SIZE} records, so bytes "
                "per record here is not measured over the corpus this budget is stated "
                "against."
            )
        per_record = result.per_record(CORPUS_SIZE)
        assert per_record < ceiling, (
            f"{path} sent {result.bytes:,} bytes for {CORPUS_SIZE} records -- "
            f"{per_record:,.0f} bytes each, against a ceiling of {ceiling:,} and "
            f"{measured:,} when this budget was written. A payload that grew per record "
            "is the task-484 defect: whole records, log entries and all, serialised to "
            "draw a list. Send the fields the surface reads and fetch the rest on open."
        )

    @pytest.mark.parametrize("path", sorted(FIXED_PAYLOAD_BUDGETS))
    def test_a_single_record_response_stays_one_record(
        self, budget_client, count_sql: StatementLog, path: str
    ) -> None:
        measured, ceiling = FIXED_PAYLOAD_BUDGETS[path]
        result = measure(budget_client, count_sql, path)
        assert result.bytes < ceiling, (
            f"{path} sent {result.bytes:,} bytes against a ceiling of {ceiling:,} and "
            f"{measured:,} when this budget was written. This response is about one "
            "task, so its size is set by that task and not by how big the backlog is."
        )


class TestAPayloadDoesNotGrowWithTheBacklog:
    """The assertion the ceilings above cannot make.

    A ceiling says a number is small today. This says a number is not a function of the
    corpus -- which is the property that fails on the *shape* of the code, and the one
    that would have caught task-484 on the day it was written rather than at 479
    records.
    """

    def test_a_single_record_response_is_the_same_size_at_any_corpus_size(
        self, tmp_path_factory, monkeypatch, count_sql: StatementLog
    ) -> None:
        sizes: Dict[int, int] = {}
        for count in (COMPARISON_CORPUS_SIZE, CORPUS_SIZE):
            root = build_budget_project(tmp_path_factory.mktemp(f"grow-{count}"), count)
            with client_for(root, monkeypatch) as client:
                sizes[count] = measure(
                    client, count_sql, f"{LOCAL}/tasks/{SAMPLE_TASK}/detail"
                ).bytes
        small, large = sizes[COMPARISON_CORPUS_SIZE], sizes[CORPUS_SIZE]
        # Not equality: the generated prose says "of 60" in one corpus and "of 480" in
        # the other, which is a handful of bytes. Anything that scales with the corpus
        # is eight times larger here, not a handful.
        assert large < small * 1.05, (
            f"GET /tasks/{{id}}/detail sent {small:,} bytes over {COMPARISON_CORPUS_SIZE} "
            f"records and {large:,} over {CORPUS_SIZE}. One task's detail page is about "
            "one task: a response that grows with the backlog is carrying the backlog."
        )


# ---------------------------------------------------------------------------------
# What a request asks the database
# ---------------------------------------------------------------------------------


#: Statements per request, measured 2026-09-20, with the ceiling each is held to.
#:
#: **Every one of these is a constant**, and :class:`TestTheQueryCountIsAConstant` is
#: what says so. The number that would break them is a query per record: at this corpus
#: that is 480 rather than 32, so the ceilings are set for headroom on a route that
#: grows an honest query or two, not for a fan-out.
#:
#: Two of the measured numbers are worse than they need to be and are recorded rather
#: than fixed -- fixing an endpoint is out of scope for the task that wrote this file
#: (task-486). ``/detail`` assembles the whole corpus twice; ``/dashboard`` asks
#: ``import_quarantine`` four separate times.
QUERY_BUDGETS: Dict[str, Tuple[int, int]] = {
    # path                                  measured  ceiling
    f"{LOCAL}/tasks": (5, 12),
    f"{LOCAL}/dashboard": (14, 24),
    f"{LOCAL}/tasks/{SAMPLE_TASK}/detail": (32, 48),
    f"{LOCAL}/search?q=generated": (17, 28),
    f"{LOCAL}/tasks/next": (11, 20),
    f"{LOCAL}/tasks/broken": (1, 8),
    f"{LOCAL}/revision": (1, 4),
}


class TestARequestRunsAConstantNumberOfQueries:
    """The post-rows form of "this request read no task files".

    The defect the parse counter was built for was a request walking a directory once
    per record. Rows did not make that class of defect impossible, they made it
    invisible: the same fan-out is now a ``SELECT`` per record, it parses nothing, it
    returns the right answer, and it is only slow. This counts statements instead.
    """

    @pytest.mark.parametrize("path", sorted(QUERY_BUDGETS))
    def test_a_request_runs_a_small_constant_number_of_queries(
        self, budget_client, count_sql: StatementLog, path: str
    ) -> None:
        measured, ceiling = QUERY_BUDGETS[path]
        result = measure(budget_client, count_sql, path)
        assert len(result.queries) <= ceiling, (
            f"{path} ran {len(result.queries)} SQL statements over {CORPUS_SIZE} "
            f"records, against a ceiling of {ceiling} and {measured} when this budget "
            "was written. A count near the corpus size is a query per record -- the "
            "shape task-131 removed from the file backend, arriving back as SQL. The "
            "statements were:\n" + result.report()
        )


class TestTheQueryCountIsAConstant:
    """Same request, eight times the corpus, same number of statements.

    This is the assertion worth having. A ceiling can be met by a route that runs one
    query per record on a corpus small enough to fit under it; a count that is identical
    at 60 records and at 480 cannot be, whatever the ceiling says.
    """

    def test_the_count_does_not_move_with_the_corpus(
        self, tmp_path_factory, monkeypatch, count_sql: StatementLog
    ) -> None:
        counts: Dict[int, Dict[str, int]] = {}
        for count in (COMPARISON_CORPUS_SIZE, CORPUS_SIZE):
            root = build_budget_project(tmp_path_factory.mktemp(f"scale-{count}"), count)
            with client_for(root, monkeypatch) as client:
                counts[count] = {
                    path: len(measure(client, count_sql, path).queries)
                    for path in sorted(QUERY_BUDGETS)
                }
        moved = {
            path: (counts[COMPARISON_CORPUS_SIZE][path], counts[CORPUS_SIZE][path])
            for path in sorted(QUERY_BUDGETS)
            if counts[COMPARISON_CORPUS_SIZE][path] != counts[CORPUS_SIZE][path]
        }
        assert not moved, (
            "These endpoints ran a different number of SQL statements at "
            f"{COMPARISON_CORPUS_SIZE} records than at {CORPUS_SIZE}:\n"
            + "\n".join(f"    {path}: {small} -> {large}" for path, (small, large) in moved.items())
            + "\nA statement count that follows the corpus is a query in a loop. The "
            "ceilings in QUERY_BUDGETS would not have caught it: they are sized for a "
            "constant, and a fan-out passes them at a small corpus."
        )


# ---------------------------------------------------------------------------------
# Below the API
# ---------------------------------------------------------------------------------


class TestTheStorageLayerItself:
    """Below the API, so a regression is attributed to storage rather than to a route.

    The two tests that used to live here pinned the per-request parse snapshot -- that
    each file was parsed once inside a scope, and that nothing was cached across scopes.
    Both went with the parser they memoised (task-402); the assertion that survives them
    is the zero above, which is stronger than either.
    """

    def test_a_whole_corpus_load_has_not_collapsed(self, budget_project: Path) -> None:
        """The catastrophe check: order of magnitude only, never a percentage."""
        import time

        storage = task_store(budget_project / "tasks")
        started = time.perf_counter()
        assert len(storage.list_tasks()) == CORPUS_SIZE
        elapsed = time.perf_counter() - started
        assert elapsed < CATASTROPHE_SECONDS, (
            f"Reading {CORPUS_SIZE} records took {elapsed:.2f}s against a "
            f"{CATASTROPHE_SECONDS}s catastrophe budget. This budget is loose on "
            "purpose, so failing it means something structural: a per-record query in "
            "a loop, or an index gone. Do not fix this by raising the number."
        )
