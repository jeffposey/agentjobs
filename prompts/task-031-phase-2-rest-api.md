# Task 031 - Phase 2: REST API and Python Client Library

**Context**: Phase 1 is complete with core infrastructure, models, storage, and CLI.
Now implement Phase 2: REST API endpoints and Python client library.

**Reference**: See `tasks/agentjobs/task-031-tasks-2-0-system.yaml` section "Phase 2: REST API"

---

## Objectives

### 1. Implement REST API Endpoints

Expand `src/agentjobs/api/main.py` with full REST API:

**Task Management:**
```
GET    /api/tasks                    # List all tasks (with filters)
GET    /api/tasks/{task_id}          # Get specific task
GET    /api/tasks/next               # Get highest priority available task
POST   /api/tasks                    # Create new task
PUT    /api/tasks/{task_id}          # Update task (full replace)
PATCH  /api/tasks/{task_id}          # Partial update
DELETE /api/tasks/{task_id}          # Archive task
```

**Status Updates:**
```
POST   /api/tasks/{task_id}/status   # Update status with note
POST   /api/tasks/{task_id}/progress # Add progress update
```

**Prompts:**
```
GET    /api/tasks/{task_id}/prompts/starter  # Get starter prompt
POST   /api/tasks/{task_id}/prompts          # Add followup prompt
```

**Deliverables:**
```
PATCH  /api/tasks/{task_id}/deliverables/{deliverable_id}  # Mark complete
```

**Search:**
```
GET    /api/search?q={query}         # Full-text search
```

### 2. Create Python Client Library

Create `src/agentjobs/client.py` with `TaskClient` class:

```python
from agentjobs import TaskClient

client = TaskClient(base_url="http://localhost:8765")

# Core operations
task = client.get_next_task()
client.mark_in_progress(task.id, agent="codex")
client.add_progress_update(task.id, summary="...", details="...")
client.mark_deliverable_complete(task.id, deliverable_path="...")
client.mark_completed(task.id)

# Prompt access
prompt = client.get_starter_prompt(task.id)
```

**Implementation Requirements:**
- Use `httpx` for HTTP requests (already in dependencies)
- Handle connection errors gracefully
- Proper error messages from API responses
- Type hints throughout
- Timeout configuration (default 30s)

**Methods to implement:**
```python
class TaskClient:
    def __init__(self, base_url: str = "http://localhost:8765"):
        """Initialize client with API base URL."""

    # Task queries
    def list_tasks(self, status: Optional[str] = None,
                   priority: Optional[str] = None) -> List[Task]:
        """List tasks with optional filters."""

    def get_task(self, task_id: str) -> Task:
        """Get specific task by ID."""

    def get_next_task(self, priority: Optional[str] = None) -> Optional[Task]:
        """Get highest priority available task."""

    # Task creation/updates
    def create_task(self, title: str, description: str,
                   priority: str = "medium", **kwargs) -> Task:
        """Create new task."""

    def update_task(self, task_id: str, **updates) -> Task:
        """Partial update of task fields."""

    # Status management
    def mark_in_progress(self, task_id: str, agent: str,
                        summary: str = "") -> Task:
        """Mark task as in progress."""

    def mark_completed(self, task_id: str, agent: str = "",
                      summary: str = "") -> Task:
        """Mark task as completed."""

    def mark_blocked(self, task_id: str, reason: str, agent: str = "") -> Task:
        """Mark task as blocked with reason."""

    # Progress tracking
    def add_progress_update(self, task_id: str, summary: str,
                          details: Optional[str] = None,
                          agent: str = "") -> Task:
        """Add progress update to task."""

    # Deliverables
    def mark_deliverable_complete(self, task_id: str,
                                 deliverable_path: str) -> Task:
        """Mark a deliverable as completed."""

    # Prompts
    def get_starter_prompt(self, task_id: str) -> str:
        """Get starter prompt for task."""

    def add_followup_prompt(self, task_id: str, content: str,
                           author: str, context: str = "") -> Task:
        """Add followup prompt to task."""

    # Search
    def search_tasks(self, query: str) -> List[Task]:
        """Search tasks by query string."""
```

### 3. API Route Organization

Organize routes into separate files:

```
src/agentjobs/api/
├── main.py              # FastAPI app, CORS, middleware
├── routes/
│   ├── __init__.py
│   ├── tasks.py         # Task CRUD endpoints
│   ├── status.py        # Status update endpoints
│   ├── prompts.py       # Prompt endpoints
│   ├── search.py        # Search endpoints
│   └── health.py        # Health check (existing)
```

### 4. Request/Response Models

Create proper Pydantic models for API contracts:

```python
# In src/agentjobs/api/models.py

class TaskCreateRequest(BaseModel):
    title: str
    description: str
    priority: Priority = Priority.MEDIUM
    category: str = "general"
    # ... other optional fields

class TaskUpdateRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[Priority] = None
    # ... all fields optional for PATCH

class StatusUpdateRequest(BaseModel):
    status: TaskStatus
    author: str
    summary: str
    details: Optional[str] = None

class ProgressUpdateRequest(BaseModel):
    author: str
    summary: str
    details: Optional[str] = None

class PromptAddRequest(BaseModel):
    author: str
    content: str
    context: Optional[str] = None
```

### 5. API Documentation

- Ensure FastAPI auto-generates docs at `/docs` (Swagger UI)
- Add proper docstrings to all endpoint functions with examples
- Include request/response examples in docstrings
- Add OpenAPI tags for grouping endpoints

Example endpoint with docs:
```python
@router.get("/tasks/{task_id}", response_model=Task, tags=["tasks"])
async def get_task(task_id: str):
    """
    Get a specific task by ID.

    **Parameters:**
    - task_id: Unique task identifier (e.g., "task-001")

    **Returns:**
    - Task object with all fields

    **Errors:**
    - 404: Task not found
    """
    task = manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task
```

### 6. Error Handling

Implement consistent error responses:

```python
# 404 - Not Found
{
  "detail": "Task task-001 not found"
}

# 400 - Bad Request (validation error)
{
  "detail": "Invalid priority value. Must be: low, medium, high, critical"
}

# 500 - Internal Server Error
{
  "detail": "Failed to save task: [error details]"
}
```

Use FastAPI's HTTPException throughout.

### 7. CORS Configuration

Add CORS middleware for browser access:

```python
from fastapi.middleware.cors import CORSMiddleware

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8765", "http://127.0.0.1:8765"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### 8. Testing

**Create comprehensive tests:**

**tests/test_api.py:**
```python
def test_list_tasks_empty()
def test_list_tasks_with_filters()
def test_get_task_success()
def test_get_task_not_found()
def test_create_task_success()
def test_create_task_validation_error()
def test_update_task_status()
def test_get_next_task()
def test_add_progress_update()
def test_mark_deliverable_complete()
def test_search_tasks()
```

**tests/test_client.py:**
```python
def test_client_get_next_task()
def test_client_mark_in_progress()
def test_client_add_progress_update()
def test_client_mark_completed()
def test_client_connection_error()
def test_client_404_error()
```

**Testing Strategy:**
- Use FastAPI TestClient for API tests
- Use pytest fixtures for test tasks
- Mock httpx for client library tests
- Test both success and error paths
- Maintain >80% coverage

### 9. Update Documentation

**docs/api-reference.md:**
- Full endpoint documentation
- Request/response examples
- Authentication notes (none for Phase 2)
- Error response formats

**docs/quickstart.md:**
- Add Python client library examples
- Show agent workflow with client
- Include error handling examples

**README.md:**
- Update installation instructions if needed
- Add link to API docs
- Show client library import

### 10. Export TaskClient in __init__.py

Update `src/agentjobs/__init__.py`:

```python
from .models import Task, TaskStatus, Priority, ...
from .manager import TaskManager
from .storage import TaskStorage
from .client import TaskClient  # ADD THIS

__all__ = [
    "Task",
    "TaskStatus",
    "Priority",
    ...,
    "TaskManager",
    "TaskStorage",
    "TaskClient",  # ADD THIS
]
```

---

## Acceptance Criteria

- [ ] All REST endpoints functional (GET/POST/PUT/PATCH/DELETE)
- [ ] Python client library (`TaskClient`) implements all methods
- [ ] Client library works end-to-end with live server
- [ ] API documentation accessible at `/docs` with examples
- [ ] Test coverage remains >80%
- [ ] Error handling with proper HTTP status codes (404, 400, 500)
- [ ] CORS configured for browser access
- [ ] Documentation updated (api-reference.md, quickstart.md)
- [ ] Client exported in `__init__.py` for easy import

---

## Validation Steps

```bash
# 1. Run all tests
cd ~/projects/agentjobs
.venv/bin/pytest tests/ -v

# 2. Check coverage
.venv/bin/pytest --cov=src/agentjobs --cov-report=term-missing

# 3. Start server in background
.venv/bin/agentjobs serve &
SERVER_PID=$!

# 4. Test endpoints with curl
curl http://localhost:8765/api/tasks
curl http://localhost:8765/docs
curl -X POST http://localhost:8765/api/tasks \
  -H "Content-Type: application/json" \
  -d '{"title": "Test Task", "description": "Testing API"}'

# 5. Test Python client
python3 << EOF
from agentjobs import TaskClient

client = TaskClient()
task = client.create_task(
    title="Test from client",
    description="Testing Python client"
)
print(f"Created task: {task.id}")

next_task = client.get_next_task()
print(f"Next task: {next_task.title if next_task else 'None'}")
EOF

# 6. Kill server
kill $SERVER_PID

# 7. Commit and push
git add -A
git commit -m "feat(api): implement Phase 2 REST API and Python client

Complete REST API implementation:
- Task CRUD endpoints (GET/POST/PUT/PATCH/DELETE)
- Status update endpoints
- Prompt management endpoints
- Search endpoint
- Proper error handling (404, 400, 500)
- CORS middleware

Python client library (TaskClient):
- All API methods implemented
- Connection error handling
- Type hints throughout
- Tested end-to-end

Testing:
- Comprehensive API endpoint tests
- Client library tests with mocked httpx
- Maintained >80% coverage

Documentation:
- API reference with examples
- Updated quickstart guide
- Swagger UI at /docs

Claude with Sonnet 4.5"

git push origin main
```

---

## Notes

- Keep API responses JSON-formatted (no HTML)
- Use Pydantic for automatic validation
- FastAPI will auto-generate OpenAPI schema
- httpx is similar to requests but async-capable
- Consider rate limiting in future phases
- Authentication/authorization deferred to Phase 6

---

## Success Indicators

✅ `curl http://localhost:8765/api/tasks` returns JSON
✅ `from agentjobs import TaskClient` works
✅ All pytest tests pass with >80% coverage
✅ `/docs` shows interactive API documentation
✅ Client can create task, mark in progress, and complete it

Ready to implement! 🚀
