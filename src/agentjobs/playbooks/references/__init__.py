"""The reference playbooks AgentJobs ships.

A package rather than a bare directory so ``importlib.resources`` can address it the
same way on every install layout. ``agentjobs playbook init`` copies these into a
project, which is the only way one is ever read at run time: the project's copy is
authoritative, so a brief behind a name never depends on the installed version.
"""
