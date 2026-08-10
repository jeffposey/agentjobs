# agentjobs-v2 

The proposed next iteration of the task schema, transcribed from docs/schema-design.md (ACCEPTED, D1-D3 resolved 2026-07-29) into LinkML so it can be rendered, browsed, validated and diffed against v1 before any Python is written.
This is a PRESCRIPTIVE schema: nothing here is implemented yet. It is the machine- readable companion to the design document, and the intended input to task-050's model implementation. Section references below point at docs/schema-design.md.
Three of v2's design tenets are enforced structurally rather than by convention: every open task names who acts next (`ball`, required while open), every handoff carries its ask (`ball_prompt`, required whenever the ball is set), and each concept has exactly one mechanism (phases, prompts, issues and Comment are gone).
NULL AND ABSENT ARE EQUIVALENT (decided 2026-07-29). `ball: null` and an omitted `ball` mean the same thing, as do `outcome: null` and an omitted `outcome`. This matters because hand-editing YAML in git is a first-class interface (D2): a human writing `outcome: null` is being explicit, not wrong.
LinkML cannot express that. It has no null type, so an optional enum slot compiles to a bare `$ref` and the generated JSON Schema rejects an explicit null. The consequence, recorded so it is not mistaken for a design position:

  * OMISSION is the canonical on-disk form -- what the migrator and the manager
    write, and the only form the generated JSON Schema accepts.
  * EXPLICIT NULL is equally valid input and must be accepted by the loader.
    Pydantic's `Optional[Ball] = None` does this natively, so task-050 gets it for
    free -- but the JSON Schema artifact in schema/generated/ is therefore
    STRICTER than the runtime, and must not be used as the sole validator for
    hand-edited files.
  * The `value_presence: ABSENT` conditions in the rules below should be read as
    "absent or null" throughout.

URI: https://github.com/jeffposey/agentjobs/schema/v2