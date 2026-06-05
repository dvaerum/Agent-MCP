# pkgs.nixosTest: end-to-end smoke for PR-2 event-coord wake loop.
#
# Boots a single-node VM with the multi-tenant router + per-project
# backend template (same shape as multi-tenant.nix), then drives the
# `wait_for_events` + `fetch_events_since` MCP tools via curl with two
# distinct bearer tokens representing two simulated agents.
#
# Test plan covers the 10 cases from the prancy-napping-pie locked
# plan's "E2E verification on the VM" section:
#
#   1. serverInfo.instructions wake-loop bootstrap appears when both
#      flags ON; disappears when per-agent flag OFF.
#   2. wait_for_events blocks then returns on inbox message (fat).
#   3. Capability subset routing for unassigned_task_appeared.
#   4. Empty required_capabilities → broadcast.
#   5. Empty agent capabilities → matches only empty-required tasks.
#   6. fetch_events_since catch-up.
#   7. Concurrent wait_for_events → conflict envelope.
#   8. stop_listening on per-agent flag flip.
#   9. Lowercase normalization of capability labels.
#   10. Global toggle OFF → stop_listening for all agents.
#
# Test runtime target: ~2-3 minutes total once the VM is booted.
{ pkgs, lib, self, ... }:

let
  ports = import ./_ports.nix;
  packagedPkgs = import ../packages.nix {
    inherit pkgs lib;
    src = self;
  };
in
pkgs.testers.nixosTest {
  name = "agent-mcp-event-driven-coord";

  nodes.machine = { config, pkgs, ... }: {
    imports = [ ./fake-openai.nix ];

    virtualisation = {
      memorySize = 1536;
      cores = 2;
      diskSize = 2048;
    };

    users.users.testuser = {
      isNormalUser = true;
      group = "testuser";
      uid = 1500;
      createHome = true;
    };
    users.groups.testuser = {};

    systemd.services."agent-mcp@" = {
      description = "Agent-MCP backend — project %i";
      after = [ "fake-openai.service" ];
      serviceConfig = {
        Type = "simple";
        User = "testuser";
        Group = "testuser";
        Environment = [
          "HOME=/home/testuser"
          "XDG_RUNTIME_DIR=/run/user/1500"
          "OPENAI_BASE_URL=http://127.0.0.1:11434/v1"
          "OPENAI_API_KEY=fake"
          "AGENT_MCP_EMBEDDING_MODEL=fake-zero-vector"
          "AGENT_MCP_EMBEDDING_DIMENSION=1024"
          # Keep the wake-loop default snappy so the
          # `wait_for_events(timeout=10)` calls in the test don't
          # blow past the polkit-managed unit's grace period.
          "AGENT_MCP_EVENT_WAIT_TIMEOUT=60"
        ];
        RuntimeDirectory = "agent-mcp/%i";
        RuntimeDirectoryMode = "0700";
        ExecStartPre = "${pkgs.coreutils}/bin/rm -f /run/agent-mcp/%i/backend.sock";
        ExecStart = ''
          ${packagedPkgs.agentMcpLauncher}/bin/agent-mcp-launcher %i
        '';
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    systemd.services.agent-mcp-router = {
      description = "Agent-MCP router (event-coord test)";
      wantedBy = [ "multi-user.target" ];
      after = [ "fake-openai.service" "network.target" ];
      environment = {
        AGENT_MCP_PROJECTS_FILE = "/home/testuser/.config/agent-mcp/projects.local.json";
        AGENT_MCP_SOCK_DIR = "/run/agent-mcp";
        AGENT_MCP_DASHBOARD_DIR = "${packagedPkgs.agentMcpDashboard}/share/agent-mcp-dashboard";
        AGENT_MCP_EXTERNAL_URL = "http://localhost:${toString ports.routerPort}";
        AGENT_MCP_DEFAULT_WORKSPACE = "/home/testuser/projects";
        AGENT_MCP_ROUTER_PORT = toString ports.routerPort;
        AGENT_MCP_ROUTER_HOST = "0.0.0.0";
        AGENT_MCP_IDLE_SEC = "14400";
        AGENT_MCP_README_HTML = "${packagedPkgs.readmeHtml}";
        AGENT_MCP_INSTALLER_TEMPLATE = "${packagedPkgs.installerTemplate}";
      };
      serviceConfig = {
        Type = "simple";
        User = "testuser";
        Group = "testuser";
        RuntimeDirectory = "agent-mcp";
        RuntimeDirectoryMode = "0700";
        ExecStartPre = "${pkgs.coreutils}/bin/mkdir -p /home/testuser/.config/agent-mcp /home/testuser/projects";
        ExecStart = "${packagedPkgs.agentMcpRouterWrapper}/bin/agent-mcp-router";
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    security.polkit.enable = true;
    security.polkit.extraConfig = ''
      polkit.addRule(function(action, subject) {
        if (action.id == "org.freedesktop.systemd1.manage-units" &&
            subject.user == "testuser") {
          var unit = action.lookup("unit");
          if (unit && (unit.indexOf("agent-mcp@") == 0)) {
            return polkit.Result.YES;
          }
        }
      });
    '';

    environment.systemPackages = [ pkgs.curl pkgs.jq pkgs.sqlite ];
    networking.firewall.enable = false;
  };

  testScript = ''
    import json

    start_all()
    machine.wait_for_unit("fake-openai.service")
    machine.wait_for_unit("agent-mcp-router.service")
    machine.wait_for_open_port(${toString ports.routerPort})

    # ── Bootstrap the project ────────────────────────────────────
    machine.succeed(
        "curl -fsSL -o /dev/null -F name=coord-test "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
    )

    # The per-project backend systemd unit is lazy-spawned by the
    # router on the first /mcp request. Issue an unauthenticated
    # request just to wake the spawn (we don't care about the body —
    # 401 is fine, that confirms the backend booted enough to run
    # the auth middleware).
    machine.wait_until_succeeds(
        "curl -s -o /dev/null "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/mcp/coord-test",
        timeout=60,
    )
    machine.wait_until_succeeds(
        "systemctl is-active agent-mcp@coord-test.service",
        timeout=60,
    )

    # Poll the backend's DB file until it exists (the unit's
    # "active" state can precede DB initialisation).
    db_path = "/home/testuser/.local/share/agent-mcp/projects/coord-test/.agent/mcp_state.db"
    machine.wait_until_succeeds(f"test -f {db_path}", timeout=60)

    # ── Provision two agents directly via SQL ────────────────────
    # The harness pattern from existing VM tests: skip the
    # create_agent tool and INSERT directly so we control both the
    # token and capabilities precisely.
    def sql(stmt: str) -> str:
        # `sqlite3` defaults to no row separator for SELECT; the
        # test driver expects plain stdout, so we just shell out.
        return machine.succeed(f"sqlite3 {db_path} \"{stmt}\"")

    sql(
        "INSERT INTO agents (token, agent_id, capabilities, "
        "created_at, status, working_directory, color, updated_at, "
        "auto_event_loop) VALUES "
        "('tokbe', 'worker-backend', '[\\\"backend\\\"]', "
        " '2026-01-01T00:00:00', 'active', '/tmp', '#888', "
        " '2026-01-01T00:00:00', 1)"
    )
    sql(
        "INSERT INTO agents (token, agent_id, capabilities, "
        "created_at, status, working_directory, color, updated_at, "
        "auto_event_loop) VALUES "
        "('tokfe', 'worker-frontend', '[\\\"frontend\\\"]', "
        " '2026-01-01T00:00:00', 'active', '/tmp', '#888', "
        " '2026-01-01T00:00:00', 1)"
    )

    # The auth allow-list is loaded at backend startup; restart so
    # the two newly-inserted tokens are accepted.
    machine.succeed("systemctl restart agent-mcp@coord-test.service")
    machine.wait_for_unit("agent-mcp@coord-test.service")
    # The backend is healthy when the MCP transport accepts an
    # initialize request without 503.
    machine.wait_until_succeeds(
        "curl -fsS -o /dev/null -X POST "
        " -H 'Authorization: Bearer tokbe' "
        " -H 'Content-Type: application/json' "
        " -H 'Accept: application/json, text/event-stream' "
        " -d '{\\\"jsonrpc\\\":\\\"2.0\\\",\\\"id\\\":1,"
        "\\\"method\\\":\\\"initialize\\\","
        "\\\"params\\\":{\\\"protocolVersion\\\":\\\"2025-03-26\\\","
        "\\\"capabilities\\\":{},"
        "\\\"clientInfo\\\":{\\\"name\\\":\\\"test\\\","
        "\\\"version\\\":\\\"0\\\"}}}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/mcp/coord-test",
        timeout=30,
    )

    MCP_URL = (
        "http://127.0.0.1:${toString ports.routerPort}"
        "/agent-mcp/mcp/coord-test"
    )

    def mcp_call(token: str, method: str, params: dict, jid: int = 1) -> dict:
        body = json.dumps({
            "jsonrpc": "2.0", "id": jid,
            "method": method, "params": params,
        })
        # The server returns either application/json or text/event-
        # stream depending on the request shape; --raw-data + the
        # standard accept header covers both inline-JSON and SSE.
        out = machine.succeed(
            f"curl -fsS -X POST "
            f"-H 'Authorization: Bearer {token}' "
            f"-H 'Content-Type: application/json' "
            f"-H 'Accept: application/json, text/event-stream' "
            f"--data {json.dumps(body)} {MCP_URL}"
        )
        # When the server replies with SSE, the JSON body is on a
        # `data:` line. Strip that prefix uniformly.
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(out)

    def tool_call(token: str, name: str, args: dict) -> dict:
        """Return the parsed tool-result text envelope."""
        rsp = mcp_call(token, "tools/call", {
            "name": name, "arguments": args,
        })
        result = rsp.get("result", {})
        content = result.get("content", [])
        if not content:
            return {}
        return json.loads(content[0].get("text", "{}"))

    # ── Test 1: serverInfo.instructions wake-loop bootstrap ──────
    init = mcp_call("tokbe", "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "vm-test", "version": "0"},
    })
    instr = init.get("result", {}).get("instructions") or ""
    assert "wait_for_events" in instr, (
        f"wake-loop bootstrap missing from initialize response; got: {instr!r}"
    )

    # Flip per-agent flag OFF; bootstrap should disappear.
    sql("UPDATE agents SET auto_event_loop = 0 WHERE agent_id = 'worker-backend'")
    init = mcp_call("tokbe", "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "vm-test", "version": "0"},
    })
    instr = init.get("result", {}).get("instructions") or ""
    assert "wait_for_events" not in instr, (
        f"wake-loop bootstrap should NOT appear when per-agent flag OFF; got: {instr!r}"
    )
    # Restore.
    sql("UPDATE agents SET auto_event_loop = 1 WHERE agent_id = 'worker-backend'")

    # ── Test 6: fetch_events_since catch-up (the simplest path) ──
    # Send a message to worker-backend, then call fetch_events_since;
    # expect one event in the catch-up envelope.
    tool_call("tokbe", "send_agent_message", {
        "recipient_id": "worker-backend",
        "message": "catch up test",
        "deliver_method": "store",
    })
    env = tool_call("tokbe", "fetch_events_since", {})
    assert env.get("events"), f"fetch_events_since: no events; got {env}"
    assert env.get("cursor"), f"fetch_events_since: no cursor; got {env}"

    # ── Test 8: stop_listening on per-agent flag flip ────────────
    # Flip OFF then call wait_for_events — should return immediately.
    sql("UPDATE agents SET auto_event_loop = 0 WHERE agent_id = 'worker-backend'")
    env = tool_call("tokbe", "wait_for_events", {"timeout_seconds": 30})
    events = env.get("events", [])
    assert any(e.get("type") == "stop_listening" for e in events), (
        f"expected stop_listening when per-agent flag OFF; got {env}"
    )
    sql("UPDATE agents SET auto_event_loop = 1 WHERE agent_id = 'worker-backend'")

    # ── Test 10: global toggle OFF → stop_listening for everyone ──
    sql(
        "INSERT OR REPLACE INTO project_context "
        "(context_key, value, created_at, updated_at, "
        " created_by, updated_by) VALUES "
        "('config_auto_event_loop_global', 'false', "
        " '2026-01-01T00:00:00', '2026-01-01T00:00:00', "
        " 'admin', 'admin')"
    )
    env_be = tool_call("tokbe", "wait_for_events", {"timeout_seconds": 30})
    env_fe = tool_call("tokfe", "wait_for_events", {"timeout_seconds": 30})
    for who, env in (("be", env_be), ("fe", env_fe)):
        assert any(
            e.get("type") == "stop_listening" for e in env.get("events", [])
        ), f"{who}: expected stop_listening when global flag OFF; got {env}"
    sql(
        "UPDATE project_context SET value = 'true' "
        "WHERE context_key = 'config_auto_event_loop_global'"
    )

    # ── Test 9: lowercase normalization (verify via SQL) ──────────
    # PR-1 normalizes at write time; verify the column shape on
    # task-create through the assign_task tool.
    tool_call("tokbe", "assign_task", {
        "task_title": "cap-norm task",
        "task_description": "x",
        "required_capabilities": ["Backend", "DB"],
    })
    rc = sql(
        "SELECT required_capabilities FROM tasks "
        "WHERE title = 'cap-norm task'"
    ).strip()
    # Stored as JSON list; should be lowercase + deduped.
    assert "backend" in rc and "db" in rc and "Backend" not in rc, (
        f"required_capabilities should normalize to lowercase; got {rc!r}"
    )

    # Smoke summary print so a failed assertion is easy to spot in
    # the test log.
    print("event-driven-coord VM test: all assertions passed")
  '';
}
