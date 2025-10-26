# Task 031 - Phase 3: Web GUI (Browser Interface)

---
## ⚠️ CRITICAL: WORKING DIRECTORY ⚠️

**YOU MUST WORK IN THE AGENTJOBS REPOSITORY:**
```bash
cd /mnt/c/projects/agentjobs
```

**DO NOT:**
- ❌ Work in `/mnt/c/projects/privateproject`
- ❌ Copy agentjobs files into privateproject
- ❌ Create `privateproject/src/agentjobs/` directory
- ❌ Make any changes to privateproject repo

**This prompt is stored in privateproject but the WORK is in agentjobs.**

---

**Context**: Phases 1 & 2 complete with core infrastructure, REST API, and Python client.
Now implement Phase 3: Browser-based GUI for humans to view and manage tasks.

---

## Objectives

Build a browser-based interface where humans can:
- View dashboard with active tasks and stats
- Browse/search/filter task list
- View detailed task information
- Track progress on phases and deliverables
- See agent prompts and status updates

**Design Goal**: Simple, clean, dark theme matching PrivateProject scanner reports.

---

## Tech Stack

- **Templates**: Jinja2 (already in dependencies)
- **CSS**: Tailwind CSS via CDN (no build step)
- **JavaScript**: Alpine.js via CDN (lightweight reactivity)
- **Icons**: Heroicons via CDN
- **Theme**: Dark mode with clean typography

---

## Implementation Steps

**Before you start:**
```bash
cd /mnt/c/projects/agentjobs
git status  # Verify you're in agentjobs, NOT privateproject
```

---

## 1. Template Structure

Create templates in `src/agentjobs/api/templates/`:

```
src/agentjobs/api/templates/
├── base.html              # Base layout with nav, Tailwind, Alpine
├── dashboard.html         # Main dashboard view
├── task_list.html         # Sortable/filterable task list
├── task_detail.html       # Full task information
├── components/
│   ├── task_card.html     # Reusable task card component
│   ├── status_badge.html  # Status badge component
│   └── priority_badge.html # Priority badge component
```

---

## 2. Base Template (`base.html`)

```html
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}AgentJobs{% endblock %}</title>

    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        dark: {
                            bg: '#0f172a',
                            surface: '#1e293b',
                            border: '#334155',
                            text: '#e2e8f0',
                            muted: '#94a3b8'
                        }
                    }
                }
            }
        }
    </script>

    <!-- Alpine.js -->
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>

    <style>
        body {
            background-color: #0f172a;
            color: #e2e8f0;
        }
    </style>
</head>
<body class="bg-dark-bg text-dark-text">
    <!-- Navigation -->
    <nav class="bg-dark-surface border-b border-dark-border">
        <div class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
            <div class="flex justify-between h-16">
                <div class="flex">
                    <div class="flex-shrink-0 flex items-center">
                        <h1 class="text-2xl font-bold">AgentJobs</h1>
                    </div>
                    <div class="ml-10 flex items-baseline space-x-4">
                        <a href="/" class="px-3 py-2 rounded-md text-sm font-medium hover:bg-dark-border">Dashboard</a>
                        <a href="/tasks" class="px-3 py-2 rounded-md text-sm font-medium hover:bg-dark-border">Tasks</a>
                        <a href="/docs" target="_blank" class="px-3 py-2 rounded-md text-sm font-medium hover:bg-dark-border">API Docs</a>
                    </div>
                </div>
            </div>
        </div>
    </nav>

    <!-- Main Content -->
    <main class="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        {% block content %}{% endblock %}
    </main>
</body>
</html>
```

---

## 3. Dashboard View (`dashboard.html`)

Show overview with:
- Active tasks count (in_progress, blocked)
- Recent updates
- Priority breakdown
- Quick actions

```html
{% extends "base.html" %}

{% block title %}Dashboard - AgentJobs{% endblock %}

{% block content %}
<div class="space-y-6">
    <!-- Stats Grid -->
    <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
        <div class="bg-dark-surface p-6 rounded-lg border border-dark-border">
            <div class="text-sm text-dark-muted">In Progress</div>
            <div class="text-3xl font-bold mt-2">{{ stats.in_progress }}</div>
        </div>
        <div class="bg-dark-surface p-6 rounded-lg border border-dark-border">
            <div class="text-sm text-dark-muted">Blocked</div>
            <div class="text-3xl font-bold mt-2 text-red-400">{{ stats.blocked }}</div>
        </div>
        <div class="bg-dark-surface p-6 rounded-lg border border-dark-border">
            <div class="text-sm text-dark-muted">Completed</div>
            <div class="text-3xl font-bold mt-2 text-green-400">{{ stats.completed }}</div>
        </div>
        <div class="bg-dark-surface p-6 rounded-lg border border-dark-border">
            <div class="text-sm text-dark-muted">Total</div>
            <div class="text-3xl font-bold mt-2">{{ stats.total }}</div>
        </div>
    </div>

    <!-- Active Tasks -->
    <div class="bg-dark-surface rounded-lg border border-dark-border">
        <div class="p-6 border-b border-dark-border">
            <h2 class="text-xl font-semibold">Active Tasks</h2>
        </div>
        <div class="divide-y divide-dark-border">
            {% for task in active_tasks %}
            <a href="/tasks/{{ task.id }}" class="block p-4 hover:bg-dark-border transition">
                <div class="flex items-center justify-between">
                    <div class="flex-1">
                        <h3 class="font-medium">{{ task.title }}</h3>
                        <p class="text-sm text-dark-muted mt-1">{{ task.description[:100] }}...</p>
                    </div>
                    <div class="ml-4 flex items-center space-x-2">
                        {% include "components/priority_badge.html" %}
                        {% include "components/status_badge.html" %}
                    </div>
                </div>
            </a>
            {% endfor %}
        </div>
    </div>

    <!-- Recent Updates -->
    <div class="bg-dark-surface rounded-lg border border-dark-border">
        <div class="p-6 border-b border-dark-border">
            <h2 class="text-xl font-semibold">Recent Updates</h2>
        </div>
        <div class="divide-y divide-dark-border">
            {% for update in recent_updates %}
            <div class="p-4">
                <div class="flex items-start">
                    <div class="flex-1">
                        <div class="flex items-center">
                            <span class="font-medium">{{ update.task_title }}</span>
                            <span class="mx-2 text-dark-muted">•</span>
                            <span class="text-sm text-dark-muted">{{ update.timestamp }}</span>
                        </div>
                        <p class="text-sm text-dark-muted mt-1">{{ update.summary }}</p>
                    </div>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
</div>
{% endblock %}
```

---

## 4. Task List View (`task_list.html`)

Browsable list with:
- Search box
- Status/priority filters
- Sortable columns
- Links to detail pages

```html
{% extends "base.html" %}

{% block title %}Tasks - AgentJobs{% endblock %}

{% block content %}
<div x-data="{
    search: '',
    statusFilter: 'all',
    priorityFilter: 'all'
}">
    <!-- Header with Search and Filters -->
    <div class="bg-dark-surface rounded-lg border border-dark-border p-6 mb-6">
        <div class="flex flex-col md:flex-row gap-4">
            <!-- Search -->
            <div class="flex-1">
                <input
                    type="text"
                    x-model="search"
                    placeholder="Search tasks..."
                    class="w-full bg-dark-bg border border-dark-border rounded-lg px-4 py-2 text-dark-text focus:outline-none focus:border-blue-500"
                >
            </div>

            <!-- Status Filter -->
            <select x-model="statusFilter" class="bg-dark-bg border border-dark-border rounded-lg px-4 py-2">
                <option value="all">All Status</option>
                <option value="planned">Planned</option>
                <option value="in_progress">In Progress</option>
                <option value="blocked">Blocked</option>
                <option value="under_review">Under Review</option>
                <option value="completed">Completed</option>
            </select>

            <!-- Priority Filter -->
            <select x-model="priorityFilter" class="bg-dark-bg border border-dark-border rounded-lg px-4 py-2">
                <option value="all">All Priorities</option>
                <option value="critical">Critical</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
            </select>
        </div>
    </div>

    <!-- Tasks Table -->
    <div class="bg-dark-surface rounded-lg border border-dark-border overflow-hidden">
        <table class="min-w-full divide-y divide-dark-border">
            <thead>
                <tr class="bg-dark-bg">
                    <th class="px-6 py-3 text-left text-xs font-medium text-dark-muted uppercase tracking-wider">Task</th>
                    <th class="px-6 py-3 text-left text-xs font-medium text-dark-muted uppercase tracking-wider">Status</th>
                    <th class="px-6 py-3 text-left text-xs font-medium text-dark-muted uppercase tracking-wider">Priority</th>
                    <th class="px-6 py-3 text-left text-xs font-medium text-dark-muted uppercase tracking-wider">Assigned</th>
                    <th class="px-6 py-3 text-left text-xs font-medium text-dark-muted uppercase tracking-wider">Updated</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-dark-border">
                {% for task in tasks %}
                <tr
                    class="hover:bg-dark-border transition cursor-pointer"
                    onclick="window.location.href='/tasks/{{ task.id }}'"
                    x-show="
                        (search === '' || '{{ task.title }}'.toLowerCase().includes(search.toLowerCase())) &&
                        (statusFilter === 'all' || statusFilter === '{{ task.status }}') &&
                        (priorityFilter === 'all' || priorityFilter === '{{ task.priority }}')
                    "
                >
                    <td class="px-6 py-4">
                        <div class="font-medium">{{ task.title }}</div>
                        <div class="text-sm text-dark-muted">{{ task.category }}</div>
                    </td>
                    <td class="px-6 py-4">
                        {% include "components/status_badge.html" %}
                    </td>
                    <td class="px-6 py-4">
                        {% include "components/priority_badge.html" %}
                    </td>
                    <td class="px-6 py-4 text-sm">{{ task.assigned_to or '-' }}</td>
                    <td class="px-6 py-4 text-sm text-dark-muted">{{ task.updated.strftime('%Y-%m-%d %H:%M') }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
</div>
{% endblock %}
```

---

## 5. Task Detail View (`task_detail.html`)

Full task information with:
- All metadata
- Phases with progress
- Status updates timeline
- Deliverables checklist
- Prompts (collapsible)

```html
{% extends "base.html" %}

{% block title %}{{ task.title }} - AgentJobs{% endblock %}

{% block content %}
<div class="space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
        <div>
            <h1 class="text-3xl font-bold">{{ task.title }}</h1>
            <div class="flex items-center space-x-3 mt-2">
                {% include "components/status_badge.html" %}
                {% include "components/priority_badge.html" %}
                <span class="text-sm text-dark-muted">{{ task.category }}</span>
            </div>
        </div>
        <a href="/tasks" class="px-4 py-2 bg-dark-surface border border-dark-border rounded-lg hover:bg-dark-border transition">
            ← Back to Tasks
        </a>
    </div>

    <!-- Description -->
    <div class="bg-dark-surface rounded-lg border border-dark-border p-6">
        <h2 class="text-lg font-semibold mb-4">Description</h2>
        <p class="text-dark-text whitespace-pre-wrap">{{ task.description }}</p>
    </div>

    <!-- Metadata Grid -->
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="bg-dark-surface rounded-lg border border-dark-border p-4">
            <div class="text-xs text-dark-muted mb-1">Created</div>
            <div class="text-sm">{{ task.created.strftime('%Y-%m-%d') }}</div>
        </div>
        <div class="bg-dark-surface rounded-lg border border-dark-border p-4">
            <div class="text-xs text-dark-muted mb-1">Updated</div>
            <div class="text-sm">{{ task.updated.strftime('%Y-%m-%d') }}</div>
        </div>
        <div class="bg-dark-surface rounded-lg border border-dark-border p-4">
            <div class="text-xs text-dark-muted mb-1">Assigned To</div>
            <div class="text-sm">{{ task.assigned_to or 'Unassigned' }}</div>
        </div>
        <div class="bg-dark-surface rounded-lg border border-dark-border p-4">
            <div class="text-xs text-dark-muted mb-1">Effort</div>
            <div class="text-sm">{{ task.estimated_effort or 'Not estimated' }}</div>
        </div>
    </div>

    <!-- Phases (if any) -->
    {% if task.phases %}
    <div class="bg-dark-surface rounded-lg border border-dark-border">
        <div class="p-6 border-b border-dark-border">
            <h2 class="text-lg font-semibold">Phases</h2>
        </div>
        <div class="divide-y divide-dark-border">
            {% for phase in task.phases %}
            <div class="p-4">
                <div class="flex items-center justify-between">
                    <div>
                        <h3 class="font-medium">{{ phase.title }}</h3>
                        {% if phase.notes %}
                        <p class="text-sm text-dark-muted mt-1">{{ phase.notes }}</p>
                        {% endif %}
                    </div>
                    <span class="px-2 py-1 text-xs rounded {% if phase.status == 'completed' %}bg-green-900 text-green-200{% else %}bg-gray-700 text-gray-300{% endif %}">
                        {{ phase.status }}
                    </span>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    <!-- Deliverables (if any) -->
    {% if task.deliverables %}
    <div class="bg-dark-surface rounded-lg border border-dark-border">
        <div class="p-6 border-b border-dark-border">
            <h2 class="text-lg font-semibold">Deliverables</h2>
        </div>
        <div class="divide-y divide-dark-border">
            {% for deliverable in task.deliverables %}
            <div class="p-4 flex items-center">
                <span class="mr-3">
                    {% if deliverable.status == 'completed' %}✓{% else %}○{% endif %}
                </span>
                <div class="flex-1">
                    <code class="text-sm">{{ deliverable.path }}</code>
                    {% if deliverable.description %}
                    <p class="text-sm text-dark-muted mt-1">{{ deliverable.description }}</p>
                    {% endif %}
                </div>
                <span class="text-xs text-dark-muted">{{ deliverable.status }}</span>
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    <!-- Status Updates Timeline -->
    {% if task.status_updates %}
    <div class="bg-dark-surface rounded-lg border border-dark-border">
        <div class="p-6 border-b border-dark-border">
            <h2 class="text-lg font-semibold">Status Updates</h2>
        </div>
        <div class="p-6 space-y-4">
            {% for update in task.status_updates|reverse %}
            <div class="border-l-2 border-blue-500 pl-4">
                <div class="flex items-center text-sm text-dark-muted">
                    <span>{{ update.author }}</span>
                    <span class="mx-2">•</span>
                    <span>{{ update.timestamp.strftime('%Y-%m-%d %H:%M') }}</span>
                    <span class="mx-2">•</span>
                    <span class="px-2 py-1 text-xs rounded bg-dark-bg">{{ update.status }}</span>
                </div>
                <p class="mt-1 font-medium">{{ update.summary }}</p>
                {% if update.details %}
                <p class="mt-1 text-sm text-dark-muted">{{ update.details }}</p>
                {% endif %}
            </div>
            {% endfor %}
        </div>
    </div>
    {% endif %}

    <!-- Prompts (Collapsible) -->
    <div x-data="{ open: false }" class="bg-dark-surface rounded-lg border border-dark-border">
        <button
            @click="open = !open"
            class="w-full p-6 border-b border-dark-border flex items-center justify-between hover:bg-dark-border transition"
        >
            <h2 class="text-lg font-semibold">Agent Prompts</h2>
            <span x-text="open ? '▼' : '▶'"></span>
        </button>
        <div x-show="open" x-collapse class="p-6">
            {% if task.prompts.starter %}
            <div class="mb-6">
                <h3 class="text-sm font-medium text-dark-muted mb-2">Starter Prompt</h3>
                <pre class="bg-dark-bg p-4 rounded text-sm overflow-x-auto">{{ task.prompts.starter }}</pre>
            </div>
            {% endif %}
            {% if task.prompts.followups %}
            <div>
                <h3 class="text-sm font-medium text-dark-muted mb-2">Follow-up Prompts</h3>
                {% for followup in task.prompts.followups %}
                <div class="mb-4 pb-4 border-b border-dark-border last:border-0">
                    <div class="text-xs text-dark-muted mb-2">
                        {{ followup.author }} • {{ followup.timestamp.strftime('%Y-%m-%d %H:%M') }}
                        {% if followup.context %} • {{ followup.context }}{% endif %}
                    </div>
                    <pre class="bg-dark-bg p-4 rounded text-sm overflow-x-auto">{{ followup.content }}</pre>
                </div>
                {% endfor %}
            </div>
            {% endif %}
        </div>
    </div>
</div>
{% endblock %}
```

---

## 6. Component Templates

**`components/status_badge.html`:**
```html
<span class="px-2 py-1 text-xs rounded
    {% if task.status == 'in_progress' %}bg-blue-900 text-blue-200
    {% elif task.status == 'completed' %}bg-green-900 text-green-200
    {% elif task.status == 'blocked' %}bg-red-900 text-red-200
    {% elif task.status == 'under_review' %}bg-yellow-900 text-yellow-200
    {% else %}bg-gray-700 text-gray-300{% endif %}">
    {{ task.status.replace('_', ' ').title() }}
</span>
```

**`components/priority_badge.html`:**
```html
<span class="px-2 py-1 text-xs rounded
    {% if task.priority == 'critical' %}bg-red-900 text-red-200
    {% elif task.priority == 'high' %}bg-orange-900 text-orange-200
    {% elif task.priority == 'medium' %}bg-blue-900 text-blue-200
    {% else %}bg-gray-700 text-gray-300{% endif %}">
    {{ task.priority.title() }}
</span>
```

---

## 7. Web Routes (`src/agentjobs/api/routes/web.py`)

Create new route file for HTML views:

```python
"""Web UI routes for browser-based task management."""

from datetime import datetime
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..dependencies import get_manager, get_templates
from ...models import TaskStatus

router = APIRouter()
templates = Jinja2Templates(directory="src/agentjobs/api/templates")


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, manager=Depends(get_manager)):
    """Dashboard view with stats and active tasks."""
    tasks = manager.list_tasks()

    # Calculate stats
    stats = {
        "total": len(tasks),
        "in_progress": len([t for t in tasks if t.status == TaskStatus.IN_PROGRESS]),
        "blocked": len([t for t in tasks if t.status == TaskStatus.BLOCKED]),
        "completed": len([t for t in tasks if t.status == TaskStatus.COMPLETED]),
    }

    # Get active tasks
    active_tasks = [
        t for t in tasks
        if t.status in [TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.UNDER_REVIEW]
    ]

    # Get recent updates (flatten all status_updates and sort)
    recent_updates = []
    for task in tasks:
        for update in task.status_updates:
            recent_updates.append({
                "task_id": task.id,
                "task_title": task.title,
                "timestamp": update.timestamp,
                "summary": update.summary,
                "author": update.author,
            })
    recent_updates.sort(key=lambda x: x["timestamp"], reverse=True)
    recent_updates = recent_updates[:10]  # Latest 10

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "stats": stats,
            "active_tasks": active_tasks,
            "recent_updates": recent_updates,
        }
    )


@router.get("/tasks", response_class=HTMLResponse)
async def task_list(request: Request, manager=Depends(get_manager)):
    """Task list view with search and filters."""
    tasks = manager.list_tasks()

    return templates.TemplateResponse(
        "task_list.html",
        {
            "request": request,
            "tasks": tasks,
        }
    )


@router.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_detail(request: Request, task_id: str, manager=Depends(get_manager)):
    """Task detail view with full information."""
    task = manager.get_task(task_id)

    if not task:
        # Return 404 page (create simple one)
        return templates.TemplateResponse(
            "404.html",
            {"request": request, "task_id": task_id},
            status_code=404
        )

    return templates.TemplateResponse(
        "task_detail.html",
        {
            "request": request,
            "task": task,
        }
    )
```

---

## 8. Update Main App (`src/agentjobs/api/main.py`)

Mount web routes and configure templates:

```python
# Add to imports
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

# After existing router includes
from .routes import web

# Mount web routes BEFORE API routes (so / goes to dashboard, not API)
app.include_router(web.router, tags=["web"])
app.include_router(router, prefix="/api")

# Serve static files (optional, for future CSS/JS files)
# app.mount("/static", StaticFiles(directory="src/agentjobs/api/static"), name="static")
```

---

## 9. Dependencies Update (`src/agentjobs/api/dependencies.py`)

Add templates dependency:

```python
from fastapi.templating import Jinja2Templates
from pathlib import Path

# Templates singleton
_templates = None

def get_templates() -> Jinja2Templates:
    """Get Jinja2 templates instance."""
    global _templates
    if _templates is None:
        template_dir = Path(__file__).parent / "templates"
        _templates = Jinja2Templates(directory=str(template_dir))
    return _templates
```

---

## 10. Create 404 Template (`templates/404.html`)

Simple not found page:

```html
{% extends "base.html" %}

{% block title %}Task Not Found - AgentJobs{% endblock %}

{% block content %}
<div class="text-center py-12">
    <h1 class="text-4xl font-bold mb-4">Task Not Found</h1>
    <p class="text-dark-muted mb-6">Task "{{ task_id }}" does not exist.</p>
    <a href="/tasks" class="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg">
        View All Tasks
    </a>
</div>
{% endblock %}
```

---

## Acceptance Criteria

- [ ] Dashboard shows stats and active tasks
- [ ] Task list displays all tasks with filters working
- [ ] Task detail shows full information
- [ ] Dark theme matches PrivateProject aesthetic
- [ ] Alpine.js filters work (search, status, priority)
- [ ] Responsive on mobile/tablet/desktop
- [ ] Navigation works between pages
- [ ] 404 page for missing tasks

---

## Testing

```bash
cd /mnt/c/projects/agentjobs

# Create some test tasks via CLI or API first
.venv/bin/agentjobs create --title "Test Task 1" --description "Testing GUI" --priority high
.venv/bin/agentjobs create --title "Test Task 2" --description "Another test" --priority medium

# Start server
.venv/bin/agentjobs serve

# Open browser
# Visit: http://localhost:8765 (dashboard)
# Visit: http://localhost:8765/tasks (task list)
# Click a task to see detail page

# Manual checks:
# ✓ Dashboard loads with stats
# ✓ Task list shows all tasks
# ✓ Search box filters tasks
# ✓ Status/priority dropdowns filter
# ✓ Click task goes to detail page
# ✓ Detail page shows all info
# ✓ Navigation works
# ✓ Dark theme looks good
# ✓ Responsive on mobile (resize browser)
```

---

## Commit When Done

**VERIFY YOU ARE IN THE CORRECT REPOSITORY:**
```bash
pwd  # Should show: /mnt/c/projects/agentjobs
cd /mnt/c/projects/agentjobs

git add -A
git commit -m "feat(ui): implement Phase 3 browser-based GUI

Add Jinja2 templates for dashboard, task list, and task detail views.

Features:
- Dashboard with stats and active tasks
- Task list with search and filters (Alpine.js)
- Task detail with phases, deliverables, status timeline
- Dark theme matching PrivateProject
- Responsive design with Tailwind CSS
- Web routes for HTML views

Templates:
- base.html (layout with nav)
- dashboard.html
- task_list.html
- task_detail.html
- components (status/priority badges)

Claude with Sonnet 4.5"

git push origin main
```

---

## Notes

- **No build step required** - Everything via CDN (Tailwind, Alpine, Heroicons)
- **Templates are server-rendered** - No client-side routing needed
- **Alpine.js for interactivity** - Lightweight, no React/Vue overhead
- **Dark theme only** - Matches PrivateProject, simpler than light/dark toggle
- **Board view and Reports** - Optional, can be added in Phase 6 (Advanced Features)

Ready to build! 🎨
