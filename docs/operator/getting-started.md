# Getting Started with Agent-MCP

Welcome to Agent-MCP! This guide will take you from installation to your first successful multi-agent collaboration workflow.

## 📋 Prerequisites

### Required Knowledge
- **Basic programming experience** (any language)
- **Command line familiarity** (basic terminal commands)
- **Git basics** (clone, commit, push)
- **AI assistant experience** (Claude Code, Cursor, or similar)

### Required Software
- **Python 3.8+** with pip/uv
- **Node.js 18+** with npm
- **Git** for version control
- **AI Coding Assistant** (Claude Code recommended)

### Recommended Setup
- **VS Code** with SQLite Viewer extension
- **Terminal multiplexer** (tmux or similar)
- **OpenAI API key** for embeddings

---

## ⚡ Quick Install (5 Minutes)

### 1. Clone and Setup
```bash
# Clone the repository
git clone https://github.com/rinadelph/Agent-MCP.git
cd Agent-MCP

# Install dependencies
uv venv && uv pip install -e .

# (Optional) Switch to OpenAI cloud. When OPENAI_API_KEY is unset, the
# server defaults to a local Ollama endpoint (qwen3:1.7b) — see the
# "Environment variables" section below.
# export OPENAI_API_KEY=sk-...

# Configure Claude Code hooks for multi-agent file locking
./setup-claude-hooks.sh
```

### 2. Start MCP Server
```bash
# Start the MCP server (replace with your project path)
uv run -m agent_mcp.cli --project-dir /path/to/your/project

# You'll see output like:
# 📡 Server running on http://localhost:8080
# 📊 Dashboard: Start with 'cd agent_mcp/dashboard && npm run dev'
```

> The project-wide "admin token" that used to be printed at
> startup was retired in PRs #208–#211. External MCP clients
> authenticate with a per-agent bearer token provisioned from
> the dashboard — see
> [`docs/integrations/external-mcp-client.md`](../integrations/external-mcp-client.md).

### 3. Configure Multi-Agent File Locking

Agent-MCP requires Claude Code hooks for proper multi-agent coordination. The setup script configures these automatically:

```bash
# If you missed it in step 1, run:
./setup-claude-hooks.sh
```

**What this does:**
- Configures PreToolUse hooks to check file locks before editing
- Configures PostToolUse hooks to log activity and release locks
- Creates necessary directories (`.agent-locks`, `.agent-activity`)
- Prevents file conflicts when multiple agents work simultaneously

**Troubleshooting:**
- If you see "MODULE_NOT_FOUND" errors when editing files, the hooks aren't configured properly
- Run the setup script again: `./setup-claude-hooks.sh`
- Ensure you're running Claude Code from the Agent-MCP project root directory

### 4. Launch Dashboard (Optional but Recommended)
```bash
# In a new terminal
cd agent_mcp/dashboard
npm install  # First time only
npm run dev

# Dashboard available at http://localhost:3847
```

### 5. Connect AI Assistant

After provisioning a per-agent token from the dashboard (see
[`docs/integrations/external-mcp-client.md`](../integrations/external-mcp-client.md)),
add to your `mcp.json`:

```json
{
  "mcpServers": {
    "Agent-MCP": {
      "url": "http://localhost:5454/agent-mcp/mcp/<project>",
      "headers": {
        "Authorization": "Bearer <per-agent-token>"
      }
    }
  }
}
```

**You're ready!** The server is running and your AI assistant can connect.

---

## First-boot setup (operator login)

As of v5.0.59 the dashboard requires operator login (Phase 1 of the
operator-login plan; ADR-0013). The agent-side MCP transport
(`/agent-mcp/mcp/<project>`) is unchanged — agents authenticate with
their per-agent token via the `Authorization: Bearer` header. Only
the dashboard surface uses cookie sessions.

> **Note (2026-06-23, `retire-system-token` complete after Wave 5,
> PRs #208 / #209 / #210 / #211 / Wave 5):** the project-wide
> `admin_token` / `system_token` no longer exists. External MCP
> clients (Claude Code, IDE plugins, ad-hoc scripts) must
> **provision a per-agent worker or manager agent in the dashboard
> and use that agent's `token` as the bearer.** See
> [`docs/integrations/external-mcp-client.md`](../integrations/external-mcp-client.md) for the
> walkthrough.

Pick the bootstrap path that matches your deploy shape:

### Wizard (browser, easiest)

```bash
# Start the router (multi-tenant), then browse to the dashboard.
uv run -m agent_mcp.router
# Open http://localhost:5454/agent-mcp/
```

The empty-users state redirects you to `/agent-mcp/setup`. Pick a
username + password; that account becomes the first operator and
inherits membership in every existing project.

### Env vars (NixOS, Docker, declarative deploys)

```bash
export AGENT_MCP_BOOTSTRAP_USERNAME="dennis"
# Pass the password via a sops-decrypted env file or systemd
# `EnvironmentFile=` — anything that doesn't leak into the
# command line / `ps`-readable args.
export AGENT_MCP_BOOTSTRAP_PASSWORD="$(cat /run/secrets/agent-mcp-bootstrap-pw)"
uv run -m agent_mcp.router
```

The router creates the first operator on startup, then unsets both
env vars in-process so they don't leak into spawned backend
subprocesses (per agent.create_user → init_router_db).

### CLI (ops fallback, subsequent operators)

```bash
# Interactive password prompt:
uv run -m agent_mcp.router create-operator --username alice

# Non-interactive (piped):
echo "$NEW_PW" | uv run -m agent_mcp.router create-operator \
    --username alice --password-stdin
```

After first boot, log in at `http://localhost:5454/agent-mcp/login`.
The session cookie is `agent_mcp_session=<opaque>; HttpOnly; Secure;
SameSite=Lax; Path=/agent-mcp/`. Sessions live 30 days idle, sliding
on every dashboard request; revoke immediately via `agent-mcp router
delete-operator <username>` (Phase 2) or directly with a SQL `DELETE
FROM sessions WHERE user_id = ...` against `/var/lib/agent-mcp/router.db`.

---

## Environment variables

Agent-MCP defaults are designed to work out of the box — none of the
following are required.

| Variable                          | Default                          | Notes |
| --------------------------------- | -------------------------------- | ----- |
| `OPENAI_API_KEY`                  | `ollama` (auto-seeded)           | Set to a real OpenAI key to use the cloud. |
| `OPENAI_BASE_URL`                 | `http://127.0.0.1:11434/v1`      | Override only if Ollama is not on localhost. |
| `OPENAI_MODEL`                    | `qwen3:1.7b`                     | Chat-completions model. |
| `AGENT_MCP_EMBEDDING_MODEL`       | `qwen3-embedding:0.6b`           | RAG embedding model. |
| `AGENT_MCP_EMBEDDING_DIMENSION`   | `1024`                           | Must match the embedding model. |
| `MCP_PROJECT_DIR`                 | (set by `--project-dir`)         | **Advanced.** The CLI sets this from `--project-dir`. Only export manually for cases like running Alembic migrations outside the CLI (see `agent_mcp/db/README.md`). |

Pre-v5.0.53 wirings used a `.env.example` checked into the repo
referencing `MCP_SERVER_URL` and `MCP_ADMIN_TOKEN`. Both are
**removed**: `MCP_SERVER_URL` is no longer read, and the project-wide
"admin token" was retired entirely (`retire-system-token`,
PRs #208 / #209 / #210 / #211 / Wave 5). Per-agent bearer tokens
have taken its place — provision a worker or manager agent in the
dashboard ("Create Agent" panel) and use that row's `token` to
authenticate external MCP clients. See
[`docs/integrations/external-mcp-client.md`](../integrations/external-mcp-client.md) for the
walkthrough.

A handful of legacy `--admin-token-*` / `--system-token-*` CLI flags
existed transitionally between Phase 2 Wave 1b and `retire-system-
token` Wave 3; they're all gone. Spawned agents receive their token
via the `MCP_AGENT_TOKEN` env var stamped into the tmux session by
`create_agent` — no global token file is written anywhere.

---

## 🎯 Your First Multi-Agent Project

Let's build a simple task management system to demonstrate Agent-MCP's capabilities.

### Step 1: Create Your Project Directory
```bash
mkdir my-task-manager
cd my-task-manager

# Initialize basic structure
touch README.md
mkdir src docs tests
```

### Step 2: Create Your First MCD

Create `MCD.md` in your project root:

```markdown
# Task Manager MCD

## 🎯 Overview & Goals  
**Project Vision**: Build a simple task management web application where users can create, update, and delete tasks with a clean, responsive interface.

**Target Users**: Individual users who need a simple, distraction-free task tracker

**Core Features**: 
1. Create tasks with title and description
2. Mark tasks as complete/incomplete
3. Delete tasks
4. Responsive web interface
5. Local storage persistence

**Success Criteria**: 
- Users can add a task in under 5 seconds
- Task status updates are immediate
- Interface works on mobile and desktop
- No data loss on page refresh

## 🏗️ Technical Architecture
**Frontend**: 
- Vanilla JavaScript (no framework complexity)
- HTML5 with semantic structure
- CSS3 with Flexbox/Grid for responsive design
- LocalStorage for data persistence

**Backend**: 
- None required (client-side only for simplicity)
- Future: Node.js + Express for multi-user features

**APIs**: 
- LocalStorage API for data persistence
- Future: REST API for server features

**Technology Justification**: 
- Vanilla JS for simplicity and learning
- LocalStorage for immediate functionality without backend complexity
- Responsive design for universal accessibility

## 📋 Detailed Implementation

### Data Structure
```javascript
// Task object structure
interface Task {
  id: string;          // UUID for unique identification
  title: string;       // Required, max 100 characters
  description: string; // Optional, max 500 characters  
  completed: boolean;  // Task completion status
  createdAt: Date;     // Creation timestamp
  updatedAt: Date;     // Last modification timestamp
}

// Storage structure
const tasks = []; // Array of Task objects in localStorage
```

### Core Functions
```javascript
// Required functions to implement
function createTask(title, description) { }
function updateTask(id, updates) { }
function deleteTask(id) { }
function toggleTaskComplete(id) { }
function loadTasks() { }
function saveTasks() { }
function renderTasks() { }
```

### HTML Structure
```html
<!DOCTYPE html>
<html>
<head>
  <title>Task Manager</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <div class="container">
    <header>
      <h1>My Task Manager</h1>
    </header>
    
    <section class="task-form">
      <input type="text" id="task-title" placeholder="Task title...">
      <textarea id="task-description" placeholder="Description (optional)"></textarea>
      <button id="add-task">Add Task</button>
    </section>
    
    <section class="task-list">
      <div id="tasks-container">
        <!-- Tasks rendered here -->
      </div>
    </section>
  </div>
</body>
</html>
```

## 📁 File Structure & Organization
```
my-task-manager/
├── index.html           # Main HTML file
├── css/
│   └── styles.css      # All styling
├── js/
│   ├── app.js          # Main application logic
│   ├── storage.js      # LocalStorage utilities
│   └── utils.js        # Helper functions
├── tests/
│   └── app.test.html   # Simple HTML-based tests
└── docs/
    └── README.md       # Usage instructions
```

## ✅ Task Breakdown & Implementation Plan

### Phase 1: Core Structure (30 minutes)
**1.1 HTML Foundation**
- Create semantic HTML structure with proper accessibility
- Include meta tags for responsive design
- **Acceptance**: HTML validates and displays correctly

**1.2 CSS Styling**
- Implement responsive layout with Flexbox
- Create clean, modern styling with good contrast
- **Acceptance**: Interface looks good on mobile and desktop

### Phase 2: JavaScript Functionality (45 minutes)
**2.1 Data Management**
- Implement localStorage utilities for data persistence
- Create task CRUD operations
- **Acceptance**: Tasks persist across page refreshes

**2.2 User Interface**
- Implement task creation form handling
- Create dynamic task list rendering
- Add task completion toggle functionality
- **Acceptance**: All core features work without errors

### Phase 3: Polish (15 minutes)
**3.1 User Experience**
- Add form validation and user feedback
- Implement keyboard shortcuts (Enter to add task)
- **Acceptance**: Interface is intuitive and responsive

## 🔗 Integration & Dependencies
**Internal Dependencies**: 
- app.js depends on storage.js and utils.js
- All JS files depend on DOM structure in index.html

**External Dependencies**: 
- None (vanilla JavaScript only)

## 🧪 Testing & Validation Strategy
**Manual Testing**:
- Create task and verify it appears
- Mark task complete and verify status change
- Delete task and verify removal
- Refresh page and verify persistence
- Test on mobile device for responsiveness

**Acceptance Criteria**:
- All CRUD operations work correctly
- Data persists across browser sessions
- No JavaScript errors in console
- Interface is fully responsive
```

### Step 3: Provision a Manager Agent and Initialize It

In the dashboard's **Create Agent** panel, create an agent with the
role `manager` (call it `mgr` or similar). Copy its `token` from the
agents list. In your AI assistant (Claude Code/Cursor), use that
token to initialize the manager session:

```
You are the manager agent for the Task Manager project.
Agent Token: "<your_manager_token_from_the_dashboard>"

TASK: Add the entire MCD to project context - every detail, don't summarize anything.

[Paste your complete MCD here]

After adding context, create a worker agent to start implementation.
```

See [`docs/integrations/external-mcp-client.md`](../integrations/external-mcp-client.md) for
the full client-config setup (downloading `.mcp.json`, headers,
multi-tenant vs single-tenant URL shape).

### Step 4: Create Worker Agent

When the manager agent creates a worker, copy that worker's `token`
from the dashboard's agents list and initialize it in a new window:

```
You are frontend-worker agent.
Your Agent Token: "<worker_token_from_the_dashboard>"

Look at your assigned tasks and ask the project RAG agent 5-7 critical questions to understand:
- What exactly needs to be implemented
- What file structure to use  
- How the task management functionality should work

Think critically about each question before asking.

AUTO --worker --memory
```

### Step 5: Watch the Magic Happen

The worker agent will:
1. ✅ Query the project RAG for context
2. ✅ Review its assigned tasks  
3. ✅ Create the HTML structure
4. ✅ Implement CSS styling
5. ✅ Build JavaScript functionality
6. ✅ Test the implementation
7. ✅ Update task status and document progress

### Step 6: Review and Iterate

Use the dashboard to:
- 📊 Monitor agent progress in real-time
- 📋 Track task completion
- 🧠 View project context and agent communications
- 🔍 Debug any issues that arise

---

## 🎓 Learning Path

### After Your First Project
1. **Study the MCD** - Review how it guided the agent's work
2. **Explore the Dashboard** - Understand the visualizations
3. **Try Variations** - Modify the MCD and see how agents adapt
4. **Join Community** - Share your experience and learn from others

### Next Steps
1. **[The Complete MCD Guide](../mcd-example/mcd-guide.md)** - Master MCD creation
2. **[Example MCD](../mcd-example/README.md)** - Study a worked example
3. **[Connecting external MCP clients](../integrations/external-mcp-client.md)** - Wire Claude Code / IDE plugins / scripts to a project

---

## 🔧 Troubleshooting Common Issues

### "Cannot find admin token"
**Solution**: The project-wide admin token was retired in PRs
#208–#211. Provision a per-agent token from the dashboard
instead — see
[`docs/integrations/external-mcp-client.md`](../integrations/external-mcp-client.md).

### "Agent can't access project context"
**Solution**: Make sure the manager agent successfully added the MCD to project context. Check the dashboard's Memory section.

### "Worker agent doesn't understand tasks"
**Solution**: Your MCD might be too vague. Add more specific implementation details and acceptance criteria.

### "Agents are not coordinating"
**Solution**: Ensure each agent is initialized with its own per-agent token from the dashboard's `agents` table and the `--worker` flag is included in worker initialization.

### ❌ "Dashboard won't load"
**Solution**: 
```bash
# Check Node.js version (needs 18+)
node --version  

# Reinstall dependencies in dashboard directory
cd agent_mcp/dashboard
rm -rf node_modules package-lock.json
npm install
npm run dev
```

### ❌ "MCP server connection failed"
**Solution**: Verify the server is running on the correct port and that your AI assistant can reach `http://localhost:8080/sse`.

---

## 💡 Pro Tips for Success

### 1. Start Simple
- Begin with small, focused projects
- Master the basic workflow before attempting complex systems
- Use the provided examples as templates

### 2. Write Detailed MCDs
- The more specific your MCD, the better your results
- Include exact file names, function signatures, and acceptance criteria
- Don't assume the AI knows your preferences

### 3. Use the Dashboard
- Monitor agent activity in real-time
- Review project context to ensure it's complete
- Watch for patterns in agent coordination

### 4. Iterate and Improve
- Start with a basic MCD and refine it
- Learn from agent questions and confusion
- Update MCDs based on implementation experience

### 5. Join the Community
- Share your MCDs and get feedback
- Learn from other developers' experiences
- Contribute improvements and patterns

---

## 🚀 What's Next?

### Expand Your Skills
1. **Create More Complex Projects** - Try building APIs, databases, or full-stack applications
2. **Experiment with Agent Specialization** - Create frontend-only, backend-only, or testing-focused agents
3. **Explore Advanced Patterns** - Use multiple coordinated agents for larger projects

### Contribute to Agent-MCP
1. **Share Your MCDs** - Help others learn from your examples
2. **Report Issues** - Help improve the platform
3. **Suggest Features** - Shape the future of AI collaboration

### Stay Connected
- **[Discord Community](https://discord.gg/7Jm7nrhjGn)** - Daily discussions and support
- **[GitHub](https://github.com/rinadelph/Agent-MCP)** - Source code and issue tracking
- **[Documentation](../README.md)** - Comprehensive guides and references

---

**Congratulations! You've successfully set up Agent-MCP and completed your first multi-agent project. You're now ready to build amazing things with coordinated AI intelligence.**

**[Continue with The Complete MCD Guide →](../mcd-example/mcd-guide.md)**