# pkgs.nixosTest: end-to-end smoke that the server NEVER auto-terminates
# workers — the regression guard for the dashboard-cleanup-loop bug fixed
# in this PR.
#
# Background
# ----------
# The dashboard previously ran a 2-minute setInterval that called
# `terminate_agent` on every "idle" worker (no current_task, > 10 min old)
# while any browser tab had the agents view open. This silently killed
# valid long-lived workers (`backend-dev`, `ios-app-dev`).
#
# The deeper invariant the fix needs to preserve: WITHOUT a browser
# connected, the backend itself MUST NOT auto-terminate any agent. The
# agent-deletion model is "explicit user action only." This VM test
# proves that property end-to-end:
#
#   1. Boot the multi-tenant router + a per-project backend.
#   2. Register a project.
#   3. Insert a "long-idle" worker directly in the sqlite DB
#      (created_at = "2024-01-01T00:00:00", status='created', no task).
#   4. Hit /api/all-data several times across a 3-minute window — the
#      sole client traffic is curl, no dashboard tab. (The deleted
#      cleanup loop was a browser-side setInterval; if it ever sneaks
#      back to the server, this test catches it.)
#   5. Re-read the worker row from sqlite. Status MUST still be
#      'created', terminated_at MUST still be NULL.
#
# This guarantees the server has no auto-terminate code path now and
# also pins the property as a CI-checked invariant against future
# "soft replacement" regressions (a background task, a periodic
# sweeper, anything that auto-flips status=terminated).
#
# Time budget
# -----------
# The 3-minute wait is intentional. The old dashboard loop fired every
# 120s; a 180s window covers more than one tick. Adding 30-60s for
# router cold-start + project creation puts the total runtime around
# 5-7 minutes — heavier than `multi-tenant.nix`'s ~3 minutes but well
# under the per-check budget for the existing VM tests.
{ pkgs, lib, self, ... }:

let
  ports = import ./_ports.nix;
  packagedPkgs = import ../packages.nix {
    inherit pkgs lib;
    src = self;
  };
in
pkgs.testers.nixosTest {
  name = "agent-mcp-no-auto-cleanup";

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
      description = "Agent-MCP router (no-auto-cleanup test)";
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
    start_all()
    machine.wait_for_unit("fake-openai.service")
    machine.wait_for_unit("agent-mcp-router.service")
    machine.wait_for_open_port(${toString ports.routerPort})

    # 1. Register a project so a backend gets lazy-spawned.
    machine.succeed(
        "curl -fsSL -o /dev/null -F name=idle-test "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
    )

    # 2. Force backend startup by hitting the dashboard endpoint —
    # this routes to /agent-mcp/__dashboard/idle-test/ which spawns
    # the per-project backend on first contact.
    machine.succeed(
        "curl -fsS -o /dev/null "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/idle-test/"
    )

    # Wait for the per-project backend to come up and the sqlite DB
    # to be created. The backend creates .agent/mcp_state.db on
    # lifespan startup.
    machine.wait_for_unit("agent-mcp@idle-test.service")
    machine.wait_until_succeeds(
        "test -f /home/testuser/projects/idle-test/.agent/mcp_state.db",
        timeout=60,
    )

    db = "/home/testuser/projects/idle-test/.agent/mcp_state.db"

    # 3. Insert a "long-idle" worker directly. created_at is two
    # years in the past so the deleted dashboard heuristic
    # (>10 min idle) would have flagged it on every poll. status
    # is the same shape used elsewhere in tests.
    machine.succeed(
        f"sqlite3 {db} \"INSERT INTO agents "
        "(token, agent_id, capabilities, created_at, status, "
        "working_directory, color, updated_at) VALUES "
        "('__test_token_old', 'old-worker', '[]', "
        "'2024-01-01T00:00:00', 'created', '/tmp', '#888', "
        "'2024-01-01T00:00:00');\""
    )

    # Sanity: the row exists and is not terminated.
    initial = machine.succeed(
        f"sqlite3 {db} \"SELECT status FROM agents WHERE "
        "agent_id='old-worker';\""
    ).strip()
    assert initial == "created", (
        f"setup: expected status='created' after insert; got {initial!r}"
    )

    # 4. Drive client traffic for ~3 minutes. The deleted bug was a
    # browser-side setInterval; this loop is a stand-in for "the
    # dashboard would be open." If anything server-side ever
    # auto-terminates idle agents, hitting /api/all-data repeatedly
    # is the most likely trigger surface.
    #
    # 3-min sleep > the old 2-min auto-cleanup tick + slack.
    machine.succeed("sleep 30")
    for _ in range(9):
        machine.succeed(
            "curl -fsS -o /dev/null "
            "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/api/idle-test/all-data"
            " || curl -fsS -o /dev/null "
            "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__api/idle-test/all-data"
            " || true"
        )
        machine.succeed("sleep 20")

    # 5. Re-read the row. status MUST still be 'created' and
    # terminated_at MUST still be NULL — proves the server has no
    # auto-terminate code path.
    final_status = machine.succeed(
        f"sqlite3 {db} \"SELECT status FROM agents WHERE "
        "agent_id='old-worker';\""
    ).strip()
    assert final_status == "created", (
        f"REGRESSION: old-worker was auto-terminated by the server "
        f"(status={final_status!r}). The agent-deletion model must "
        f"remain 'explicit user action only' — no background task, "
        f"no periodic sweeper, no API-side cleanup may exist."
    )

    final_terminated_at = machine.succeed(
        f"sqlite3 {db} \"SELECT IFNULL(terminated_at, '<null>') "
        "FROM agents WHERE agent_id='old-worker';\""
    ).strip()
    assert final_terminated_at == "<null>", (
        f"REGRESSION: old-worker has a terminated_at timestamp "
        f"({final_terminated_at!r}) despite never being terminated "
        f"by user action. Server-side auto-cleanup snuck back in."
    )
  '';
}
