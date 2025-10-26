# Task 031 - Phase 4.1: Improve Task Rendering & Migration

---
## ⚠️ CRITICAL: WORKING DIRECTORY ⚠️

**YOU MUST WORK IN THE AGENTJOBS REPOSITORY:**
```bash
cd /mnt/c/projects/agentjobs
```

**DO NOT work in privateproject.**

---

## Problem

Task detail pages show raw markdown as unformatted text walls (see screenshot).

**Issues:**
1. Markdown not rendered (**, ##, bullets show as raw text)
2. Long phase descriptions are unreadable blobs
3. No syntax highlighting for code blocks
4. Description field too verbose (entire markdown content dumped in)

---

## Objectives

1. Render markdown as HTML in templates
2. Improve migration parser to better extract content
3. Add collapsible sections for long text
4. Better visual hierarchy

---

## Implementation

### 1. Add Markdown Rendering to Templates

Update `src/agentjobs/api/templates/base.html`:

```html
<!-- After Alpine.js, before </head> -->
<script src="https://cdn.jsdelivr.net/npm/marked@11.0.0/marked.min.js"></script>
<script>
    // Configure marked for safe rendering
    marked.setOptions({
        breaks: true,
        gfm: true,
    });

    // Alpine.js directive for markdown rendering
    document.addEventListener('alpine:init', () => {
        Alpine.directive('markdown', (el, { expression }, { evaluateLater, effect }) => {
            let getContent = evaluateLater(expression);
            effect(() => {
                getContent(content => {
                    if (content) {
                        el.innerHTML = marked.parse(content);
                    }
                });
            });
        });
    });
</script>
```

### 2. Update Task Detail Template

Replace raw text with markdown rendering in `src/agentjobs/api/templates/task_detail.html`:

**Description section:**
```html
<div class="bg-dark-surface rounded-lg border border-dark-border p-6">
    <h2 class="text-lg font-semibold mb-4">Description</h2>
    <div class="prose prose-invert max-w-none prose-headings:text-dark-text prose-p:text-dark-text prose-li:text-dark-text prose-code:text-blue-300 prose-pre:bg-dark-bg">
        <div x-data="{ content: `{{ task.description | replace('`', '\\`') }}` }" x-markdown="content"></div>
    </div>
</div>
```

**Phase notes (if present):**
```html
{% if phase.notes %}
<div class="prose prose-invert prose-sm max-w-none mt-1">
    <div x-data="{ notes: `{{ phase.notes | replace('`', '\\`') }}` }" x-markdown="notes"></div>
</div>
{% endif %}
```

**Prompts section:**
```html
<div class="prose prose-invert prose-sm max-w-none">
    <div x-data="{ prompt: `{{ task.prompts.starter | replace('`', '\\`') }}` }" x-markdown="prompt"></div>
</div>
```

**Add Tailwind prose styles to base.html:**
```html
<style>
    /* Prose styles for markdown content */
    .prose code {
        background-color: rgba(59, 130, 246, 0.1);
        padding: 0.125rem 0.25rem;
        border-radius: 0.25rem;
        font-size: 0.875em;
    }
    .prose pre {
        background-color: #0f172a;
        border: 1px solid #334155;
        border-radius: 0.5rem;
        padding: 1rem;
        overflow-x: auto;
    }
    .prose pre code {
        background-color: transparent;
        padding: 0;
    }
    .prose ul, .prose ol {
        padding-left: 1.5rem;
    }
    .prose li {
        margin: 0.25rem 0;
    }
    .prose h2, .prose h3, .prose h4 {
        margin-top: 1.5rem;
        margin-bottom: 0.75rem;
        font-weight: 600;
    }
    .prose hr {
        border-color: #334155;
        margin: 1.5rem 0;
    }
</style>
```

### 3. Improve Migration Parser

Update `src/agentjobs/migration/parser.py` to extract cleaner descriptions:

```python
def _extract_section(self, content: str, section_headings: List[str]) -> str:
    """Extract content from a markdown section."""
    for heading in section_headings:
        # Try ## Heading format
        pattern = rf"^##\s+{heading}[^\n]*\n(.*?)(?=^##|\Z)"
        match = re.search(pattern, content, re.MULTILINE | re.DOTALL | re.IGNORECASE)
        if match:
            section_content = match.group(1).strip()
            return section_content
    return ""

def _build_clean_description(self, parsed: ParsedTask) -> str:
    """Build clean description from parsed content."""
    # Start with explicit objective/description section
    desc = parsed.description

    # If description is too long (>500 chars) or contains phase headers,
    # just take the first paragraph or objective
    if desc and (len(desc) > 500 or "##" in desc or "Phase" in desc):
        # Try to extract just the objective/summary
        lines = desc.split('\n')
        clean_lines = []
        for line in lines:
            # Stop at phase markers or long technical blocks
            if re.match(r'^#+\s+Phase', line, re.IGNORECASE):
                break
            if line.strip().startswith('**') and len(line) > 100:
                continue
            clean_lines.append(line)
            if len('\n'.join(clean_lines)) > 300:
                break
        desc = '\n'.join(clean_lines).strip()

    # Fallback to raw content excerpt
    if not desc or len(desc) < 20:
        desc = parsed.raw_content[:300] + "..." if len(parsed.raw_content) > 300 else parsed.raw_content

    return desc
```

### 4. Add Collapsible Long Sections

For phases with long content, add collapse functionality:

```html
{% for phase in task.phases %}
<div class="p-4" x-data="{ expanded: false }">
    <div class="flex items-center justify-between">
        <div class="flex-1">
            <h3 class="font-medium">{{ phase.title }}</h3>
            {% if phase.notes and phase.notes|length > 200 %}
            <div class="mt-2">
                <div x-show="!expanded" class="prose prose-invert prose-sm max-w-none">
                    <div x-data="{ notes: `{{ (phase.notes[:200] + '...') | replace('`', '\\`') }}` }" x-markdown="notes"></div>
                </div>
                <div x-show="expanded" x-collapse class="prose prose-invert prose-sm max-w-none">
                    <div x-data="{ notes: `{{ phase.notes | replace('`', '\\`') }}` }" x-markdown="notes"></div>
                </div>
                <button @click="expanded = !expanded" class="text-xs text-blue-400 hover:text-blue-300 mt-2">
                    <span x-show="!expanded">Show more</span>
                    <span x-show="expanded">Show less</span>
                </button>
            </div>
            {% elif phase.notes %}
            <div class="mt-2 prose prose-invert prose-sm max-w-none">
                <div x-data="{ notes: `{{ phase.notes | replace('`', '\\`') }}` }" x-markdown="notes"></div>
            </div>
            {% endif %}
        </div>
        <div class="ml-4">
            <span class="px-2 py-1 text-xs rounded {% if phase.status == 'completed' %}bg-green-900 text-green-200{% else %}bg-gray-700 text-gray-300{% endif %}">
                {{ phase.status }}
            </span>
        </div>
    </div>
</div>
{% endfor %}
```

---

## Testing

```bash
cd /mnt/c/projects/agentjobs

# Re-migrate test tasks to pick up parser improvements
rm tasks/agentjobs/*.yaml
.venv/bin/agentjobs migrate \
  'tasks/privateproject/task-016-*.yaml' \
  tasks/agentjobs/ \
  --prompts-dir /mnt/c/projects/privateproject/ops/prompts

# Start server
.venv/bin/agentjobs serve

# Open browser to http://localhost:8765
# Check:
# ✓ Description renders with proper markdown formatting
# ✓ Headings, bullets, code blocks styled correctly
# ✓ Phase notes render as markdown
# ✓ Long content has "Show more" collapse
# ✓ Prompts render with markdown
# ✓ No raw ** or ## visible
```

---

## Acceptance Criteria

- [ ] Markdown renders as HTML (no raw ** or ##)
- [ ] Code blocks have syntax highlighting background
- [ ] Bullets and lists display correctly
- [ ] Headings have proper hierarchy
- [ ] Long phase notes are collapsible
- [ ] Description is clean (not entire markdown dump)
- [ ] Prompts render with markdown
- [ ] Dark theme preserved

---

## Commit

```bash
cd /mnt/c/projects/agentjobs

git add -A
git commit -m "feat(ui): add markdown rendering to task detail pages

Fix unreadable task detail views by rendering markdown as HTML.

Changes:
- Add marked.js for markdown parsing (CDN)
- Create Alpine.js x-markdown directive
- Update task_detail.html to render markdown
- Add Tailwind prose styles for dark theme
- Improve migration parser for cleaner descriptions
- Add collapsible sections for long content

Before: Raw markdown text walls
After: Formatted HTML with proper styling

Claude with Sonnet 4.5"

git push origin main
```

---

## Notes

- **marked.js** is lightweight (10KB) and works via CDN
- **x-markdown directive** integrates with Alpine.js for reactive rendering
- **Jinja escaping**: Use `| replace('`', '\\`')` to escape backticks in markdown
- **Prose styles**: Tailwind prose classes with dark theme overrides

Ready to make tasks readable! 📖
