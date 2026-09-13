"""Durable execution: the machine-local journal dispatch coordinates through (task-264).

``store`` is the SQLite journal and its transactional primitives, ``reducer`` the pure
versioned state machine, ``coordinator`` the replay entry point and the adapters' shape,
``factory`` the one place a machine home becomes a journal. Design section 9a of
``docs/agent-dispatch-design.md`` is the contract; ``dispatch.journal`` is where the
dispatch subsystem meets it.
"""
