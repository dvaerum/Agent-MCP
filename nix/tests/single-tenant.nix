# pkgs.nixosTest: end-to-end smoke for single-tenant router mode.
#
# Mirror of multi-tenant.nix with the opposite toggle wired in: the
# router boots with --single-tenant only-project --single-workspace
# /home/testuser/projects/only-project. The projects.local.json seed
# the home-manager module would normally ExecStartPre is inlined as
# its own systemd ExecStartPre on the router unit.
#
# Three assertions specific to single-tenant mode:
#
#   1. __create / __unregister / __rename all return 410 with the
#      documented JSON body shape.
#   2. /__dashboard/<wrong-name>/<section> → 302 Location:
#      /__dashboard/only-project/<section>  (W1 redirect; decision #9).
#   3. /__dashboard/only-project/ → 200 (sanity: the configured
#      project's URL is unaffected).
{ pkgs, lib, self, ... }:

let
  ports = import ./_ports.nix;
  packagedPkgs = import ../packages.nix {
    inherit pkgs lib;
    src = self;
  };
  singleName = "only-project";
  singleWorkspace = "/home/testuser/projects/${singleName}";
in
pkgs.testers.nixosTest {
  name = "agent-mcp-single-tenant";

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
      description = "Agent-MCP router (single-tenant test)";
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
        ExecStartPre = [
          "${pkgs.coreutils}/bin/mkdir -p /home/testuser/.config/agent-mcp /home/testuser/projects ${singleWorkspace}"
          # Seed projects.local.json with the single-tenant entry —
          # mirrors what the home-manager module's
          # singleProjectSeedScript does on production hosts.
          ''
            ${pkgs.bash}/bin/sh -c 'echo "{\"${singleName}\":\"${singleWorkspace}\"}" > /home/testuser/.config/agent-mcp/projects.local.json'
          ''
        ];
        ExecStart = ''
          ${packagedPkgs.agentMcpRouterWrapper}/bin/agent-mcp-router \
            --single-tenant ${singleName} \
            --single-workspace ${singleWorkspace}
        '';
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

    environment.systemPackages = [ pkgs.curl pkgs.jq ];
    networking.firewall.enable = false;
  };

  testScript = ''
    start_all()
    machine.wait_for_unit("fake-openai.service")
    machine.wait_for_unit("agent-mcp-router.service")
    machine.wait_for_open_port(${toString ports.routerPort})

    # 1. __create → 410.
    code_create = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "-F name=newproj "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
    )
    assert code_create == "410", (
        f"__create must 410 in single-tenant mode; got {code_create}"
    )

    # Body shape: {error, single_tenant_name}.
    body = machine.succeed(
        "curl -s -F name=newproj "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__create"
    )
    import json
    data = json.loads(body)
    assert data["error"] == "endpoint_disabled_in_single_tenant_mode", (
        f"bad error code: {data!r}"
    )
    assert data["single_tenant_name"] == "${singleName}", (
        f"bad single_tenant_name in body: {data!r}"
    )

    # 2. __unregister + __rename → 410.
    for endpoint_data in (
        ("__unregister", "name=${singleName}"),
        ("__rename", "old_name=${singleName}&new_name=other"),
    ):
        endpoint, payload = endpoint_data
        code = machine.succeed(
            "curl -s -o /dev/null -w '%{http_code}' "
            f"-X POST --data '{payload}' "
            f"-H 'Content-Type: application/x-www-form-urlencoded' "
            f"http://127.0.0.1:${toString ports.routerPort}/agent-mcp/{endpoint}"
        )
        assert code == "410", f"{endpoint} must 410 single-tenant; got {code}"

    # 3. W1 redirect on dashboard for a wrong project name.
    code = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/wrong-name/"
    )
    assert code == "302", f"expected 302 W1 redirect; got {code}"

    location = machine.succeed(
        "curl -s -D - -o /dev/null "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/wrong-name/tasks/"
        "| grep -i '^location:' | tr -d '\\r' | awk '{print $2}'"
    ).strip()
    assert location == "/agent-mcp/__dashboard/${singleName}/tasks/", (
        f"unexpected W1 redirect target: {location!r}"
    )

    # 4. Configured project's URL still 200s.
    code = machine.succeed(
        "curl -s -o /dev/null -w '%{http_code}' "
        "http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/${singleName}/"
    )
    assert code == "200", f"configured project dashboard should 200; got {code}"

    # 5. Phase 4 runtime asset-prefix substitution: same regression
    # guard as multi-tenant.nix. The dashboard build emits the
    # sentinel `__AGENT_MCP_ASSET_PREFIX__`; the router substitutes
    # the configured prefix on serve. Leak = blank dashboard.
    sentinel_count = machine.succeed(
        "curl -fsS http://127.0.0.1:${toString ports.routerPort}/agent-mcp/__dashboard/${singleName}/"
        " | grep -c '__AGENT_MCP_ASSET_PREFIX__' || true"
    ).strip()
    assert sentinel_count == "0", (
        "Phase 4 regression: the asset-prefix sentinel leaked into "
        f"served HTML (count={sentinel_count!r}) in single-tenant "
        "mode. Both modes share the same dashboard handler, so a "
        "leak here would also fail the multi-tenant test — but pin "
        "it explicitly so the failure surface tells the operator "
        "exactly which mode broke."
    )
  '';
}
