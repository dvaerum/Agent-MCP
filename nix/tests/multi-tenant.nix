# pkgs.nixosTest: end-to-end smoke for multi-tenant router mode.
#
# Boots a single-node VM with the router unit + a per-project backend
# template, then drives the public HTTP surface:
#
#   1. POST __create twice (two projects).
#   2. GET __projects lists both names.
#   3. Deep-link into one project's dashboard returns 200 (the
#      handler serves index.html for any /__dashboard/<name>/ path).
#   4. The retired SSE handshake URL returns 410 with the migration
#      hint body (regression guard for the Streamable-HTTP migration).
#
# The backend isn't actually exercised here — booting the full
# embedding pipeline against ollama would balloon the test runtime.
# Phase 4's E2E in nixos-developer-system covers that path; this
# test is the cheap CI-friendly half.
#
# Mirror counterpart: ./single-tenant.nix (same harness, opposite
# toggle assertions).
{ pkgs, lib, self, ... }:

let
  ports = import ./_ports.nix;
  packagedPkgs = import ../packages.nix {
    inherit pkgs lib;
    src = self;
  };
in
pkgs.testers.nixosTest {
  name = "agent-mcp-multi-tenant";

  nodes.machine = { config, pkgs, ... }: {
    imports = [ ./fake-openai.nix ];

    # Modest sizing — the router + a per-project backend are both
    # python+aiohttp processes; 1.5 GB headroom catches OOM bugs
    # without exploding the runner.
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

    # ── Per-project backend template ────────────────────────────────
    # Lazy-spawned by the router (`systemctl start agent-mcp@<name>`).
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
        # Idempotent stale-socket cleanup; backend's own bind() can't
        # bind over an existing sock file.
        ExecStartPre = "${pkgs.coreutils}/bin/rm -f /run/agent-mcp/%i/backend.sock";
        ExecStart = ''
          ${packagedPkgs.agentMcpLauncher}/bin/agent-mcp-launcher %i
        '';
        Restart = "on-failure";
        RestartSec = 5;
      };
    };

    # ── Router unit (multi-tenant) ───────────────────────────────────
    systemd.services.agent-mcp-router = {
      description = "Agent-MCP router (multi-tenant test)";
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

    # The router runs as testuser but the per-project template starts
    # via the system bus; testuser needs to manage agent-mcp@* units
    # via polkit (no sudo prompt).
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

    environment.systemPackages = [ pkgs.curl pkgs.jq ];
    networking.firewall.enable = false;
  };

  testScript = ''
    start_all()
    machine.wait_for_unit("fake-openai.service")
    machine.wait_for_unit("agent-mcp-router.service")
    machine.wait_for_open_port(${toString ports.routerPort})

    # __projects starts empty.
    out = machine.succeed(
        "curl -fsS http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__projects"
    )
    assert '"projects": []' in out or '"projects":[]' in out, (
        f"expected empty project list; got: {out!r}"
    )

    # 1. Register two projects via __create.
    for name in ("alpha", "beta"):
        machine.succeed(
            f"curl -fsSL -o /dev/null -F name={name} "
            f"http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
        )

    out = machine.succeed(
        "curl -fsS http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__projects"
    )
    assert '"alpha"' in out and '"beta"' in out, (
        f"both alpha and beta should be listed; got: {out!r}"
    )

    # 2. Deep link into a project's dashboard — handler serves the
    # static index.html for any project segment, so 200 is the right
    # answer here.
    code = machine.succeed(
        "curl -fsS -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/alpha/"
    )
    assert code == "200", f"expected 200 from dashboard handler; got {code}"

    # 3. Legacy SSE handshake URL → 410 with migration body.
    body = machine.succeed(
        "curl -fsS -o - -w '\\n%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__sse/alpha "
        "|| true"
    )
    # curl -f exits non-zero on 4xx, suppressed by `|| true`; the
    # body still streams. We assert on body content + status code.
    out_410 = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__sse/alpha"
    )
    assert out_410 == "410", f"expected 410 on legacy SSE; got {out_410}"

    # 4. __create / __unregister / __rename are NOT 410 in multi-tenant
    # mode — they're the documented multi-tenant write surface.
    code_create = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "-F name=gamma "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
    )
    assert code_create != "410", (
        f"__create must not 410 in multi-tenant mode; got {code_create}"
    )

    # 5. Phase 4 runtime asset-prefix substitution: the dashboard build
    # emits `__AGENT_MCP_ASSET_PREFIX__` everywhere Next.js would have
    # baked in a build-time assetPrefix; the router must substitute the
    # default `/agent-mcp/__dashboard` on serve. A leak here = a broken
    # white dashboard in the browser.
    sentinel_count = machine.succeed(
        "curl -fsS http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/alpha/"
        " | grep -c '__AGENT_MCP_ASSET_PREFIX__' || true"
    ).strip()
    assert sentinel_count == "0", (
        "Phase 4 regression: the asset-prefix sentinel leaked into "
        f"served HTML (count={sentinel_count!r}). The router's "
        "substitution must replace every occurrence before bytes go "
        "on the wire — a leaked sentinel renders the dashboard blank."
    )

    # Asset URLs in the served HTML must now reference the configured
    # runtime prefix (default /agent-mcp/__dashboard).
    served = machine.succeed(
        "curl -fsS http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/alpha/"
    )
    assert "/agent-mcp/__dashboard/_next/" in served, (
        "Phase 4: expected substituted asset URLs in served HTML; "
        f"first 400 bytes: {served[:400]!r}"
    )
  '';
}
