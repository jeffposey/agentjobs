# agentjobs-v1 

The task schema as currently implemented in src/agentjobs/models.py, transcribed into LinkML so it can be rendered, browsed, and diffed against v2 by real schema tooling rather than hand-drawn diagrams.
This is a DESCRIPTIVE schema: it records v1 as it actually is, warts included, so the v1-to-v2 comparison is honest. Where v1 documents a vocabulary in prose but does not enforce it, that is called out in the slot description rather than quietly fixed here. Validated against the live corpus via `linkml-validate -s schema/agentjobs-v1.yaml tasks/agentjobs/*.yaml`.

URI: https://github.com/jeffposey/agentjobs/schema/v1