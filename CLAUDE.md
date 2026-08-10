<!--
Claude Code reads CLAUDE.md, not AGENTS.md, so this file is the bridge that makes the
repo's canonical agent docs load at session start.

The markdown links inside AGENTS.md are links, not imports -- Claude Code only expands
@path syntax. So ENGINEERING.md and ALLAGENTS.md are imported explicitly here rather
than relying on the index to pull them in.

Order mirrors AGENTS.md's own guidance: index, then engineering standards, then agent
workflow. If Claude-specific instructions are ever wanted, add them below the imports;
they will land after the shared docs and take precedence where they overlap.

This block is an HTML comment and is stripped before the file enters context, so it
costs no tokens.
-->

@AGENTS.md
@ENGINEERING.md
@ALLAGENTS.md
