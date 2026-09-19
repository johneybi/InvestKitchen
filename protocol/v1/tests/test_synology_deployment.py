from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEPLOY = ROOT / "deploy" / "synology"


def test_synology_compose_has_no_public_port_and_keeps_personal_data_read_only() -> None:
    text = (DEPLOY / "compose.yml").read_text(encoding="utf-8")
    assert "platform: linux/amd64" in text
    assert "build:" not in text
    assert "TRADEMIND_RUNTIME_IMAGE_REF:?" in text
    assert "ports:" not in text
    assert 'user: "${TRADEMIND_RUNTIME_UID:-1000}:${TRADEMIND_RUNTIME_GID:-1000}"' in text
    assert "personal_data:/var/lib/trademind/personal:ro" in text
    assert "INVESTKITCHEN_PORTFOLIO_AUTHORITY" in text
    assert "INVESTKITCHEN_CONNECTOR_WRITES" in text
    assert "INVESTKITCHEN_MARKET_PROVIDER" in text
    assert "toss_market_readonly" in text
    assert "INVESTKITCHEN_TOSS_MARKET_SECRET_HOST_FILE" in text
    assert "personal-data" in text
    assert "CONTROL_PLANE_API_KEY_FILE: /run/secrets/control_plane_api_key" in text
    assert "cap_drop:" in text and "no-new-privileges:true" in text
    assert "CONTROL_PLANE_API_KEY:" not in text


def test_synology_image_uses_verified_official_runtime_archive() -> None:
    dockerfile = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.14-alpine3.22@sha256:1a5f0303fd8941565b5bbda3fac2345148a9fd8245d575c2971ccefcd77c78f6" in dockerfile
    assert "tunnel-client-runtime-v0.0.14-linux-amd64.zip" in dockerfile
    assert "29d29cf860ada54e4d3c82c715f4fbfcff2abcdc2584c0fc26431308dfa2505b" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "ghcr.io/openai/tunnel-client" not in dockerfile
    assert "COPY --from=" not in dockerfile
    first_from = next(
        offset
        for offset in range(len(dockerfile))
        if dockerfile.startswith("FROM ", offset)
        and (offset == 0 or dockerfile[offset - 1] == "\n")
    )
    assert dockerfile.index("ARG PYTHON_IMAGE=") < first_from
    assert "ARG TRADEMIND_SOURCE_COMMIT=unknown" in dockerfile
    assert "org.opencontainers.image.revision" in dockerfile


def test_synology_bootstrap_uses_operator_uid_gid_without_requiring_chown() -> None:
    script = (DEPLOY / "bootstrap.sh").read_text(encoding="utf-8")
    assert "TRADEMIND_RUNTIME_UID:-$(id -u)" in script
    assert "TRADEMIND_RUNTIME_GID:-$(id -g)" in script
    assert 'if [ "$(id -u)" -eq 0 ]; then' in script


def test_container_entrypoint_keeps_key_out_of_mcp_command() -> None:
    script = (DEPLOY / "container-entrypoint.sh").read_text(encoding="utf-8")
    mcp_line = next(line for line in script.splitlines() if line.startswith("MCP_COMMAND="))
    assert "CONTROL_PLANE" not in mcp_line
    assert "--personal-data-root" in mcp_line
    assert "--native-store-root" in mcp_line
    assert "$CHECKPOINT_ARG" in mcp_line
    assert "INVESTKITCHEN_PORTFOLIO_AUTHORITY" in script
    assert "INVESTKITCHEN_CONNECTOR_WRITES" in script
    assert "INVESTKITCHEN_MARKET_PROVIDER" in script
    assert "--market-provider toss-native-subprocess" in script
    assert "--market-secret-file" in script
    assert "TOSSINVEST_CLIENT_SECRET" not in script
    assert "--approval-store-root" in script
    assert "--write-principal" in script
    assert "--write-grant" in script
    assert "portfolio_ids" in script
    assert "--portfolio-checkpoint-root" in script
    assert '--mcp.command "command=$MCP_COMMAND,channel=main"' in script


def test_compose_config_parses_without_docker_daemon() -> None:
    env = dict(os.environ)
    env["TRADEMIND_TUNNEL_ID"] = "tunnel_0123456789abcdef0123456789abcdef"
    env["TRADEMIND_RUNTIME_IMAGE_REF"] = "trademind-runtime:synology-fixture"
    completed = subprocess.run(
        ["docker", "compose", "-f", str(DEPLOY / "compose.yml"), "config", "--quiet"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_synology_compose_wrapper_uses_env_file_instead_of_sudo_environment() -> None:
    script = (DEPLOY / "runtime-compose.sh").read_text(encoding="utf-8")
    assert '--env-file "$ENV_FILE"' in script
    assert "runtime-deploy.env" in script
    assert "source " not in script


def test_synology_installer_and_cutover_are_fail_closed() -> None:
    installer = (DEPLOY / "install-image.sh").read_text(encoding="utf-8")
    cutover = (DEPLOY / "cutover.sh").read_text(encoding="utf-8")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "\"$DOCKER_BIN\" load -i" in installer
    assert "Unexpected image platform" in installer
    assert "tunnel-client-runtime" in installer
    assert "secure_tunnel.py preflight" in installer
    assert "get_transactions" in installer
    assert "INVESTKITCHEN_PORTFOLIO_AUTHORITY" in installer
    assert "--portfolio-checkpoint-root" in installer
    assert "--no-build runtime" in cutover
    assert "TradeMind runtime API key is missing" in cutover
    assert ".git" in dockerignore
    assert "runtime-data/" in dockerignore
    assert "secrets/" in dockerignore


def test_synology_shell_scripts_parse() -> None:
    for name in (
        "bootstrap.sh",
        "runtime-compose.sh",
        "build-image.sh",
        "install-image.sh",
        "cutover.sh",
        "container-entrypoint.sh",
        "seed-portfolio-authority.sh",
        "portfolio-authority-cutover.sh",
        "portfolio-authority-rollback.sh",
        "deploy-portfolio-authority.sh",
        "advisory-write.sh",
        "codex-mcp.sh",
    ):
        completed = subprocess.run(
            ["sh", "-n", str(DEPLOY / name)],
            text=True,
            capture_output=True,
            check=False,
        )
        assert completed.returncode == 0, f"{name}: {completed.stderr}"


def test_codex_mcp_launcher_is_narrow_server_owned_and_checkpoint_bound() -> None:
    script = (DEPLOY / "codex-mcp.sh").read_text(encoding="utf-8")
    assert '"client_id": "client:codex-ssh"' in script
    assert '"credential_binding_id": "credential:investkitchen-codex-ssh"' in script
    assert '"knowledge.commit", "portfolio.update", "operation.approve"' in script
    assert '"instance_id": instance_id' in script
    assert '"preview_knowledge_update"' in script
    assert '"apply_knowledge_update"' in script
    assert '"preview_portfolio_update"' in script
    assert '"apply_portfolio_update"' in script
    assert "decision.commit" not in script
    assert "transaction.commit" not in script
    assert "order." not in script
    assert "market.write" not in script
    assert "portfolio_ids" in script
    assert "--portfolio-checkpoint-root" in script
    assert "Portfolio checkpoint journal missing" in script
    assert "--approval-store-root" in script
    assert "tunnel_stdio_launcher.py" in script
    assert "toss-native-subprocess" in script
    assert "toss-market-readonly.json" in script
    assert "runtime-deploy.env" in script
    assert "INVESTKITCHEN_TOSS_MARKET_SECRET_HOST_FILE" in script
    assert "MARKET_PROVIDER=${MARKET_PROVIDER:-none}" in script
    assert "TOSSINVEST_CLIENT_SECRET" not in script


def test_synology_builder_pins_commit_and_exports_archive() -> None:
    script = (DEPLOY / "build-image.sh").read_text(encoding="utf-8")
    assert "--platform linux/amd64" in script
    assert "--provenance=false" in script
    assert "--sbom=false" in script
    assert '--build-arg TRADEMIND_SOURCE_COMMIT="$COMMIT"' in script
    assert '--output="type=docker,dest=$ARCHIVE"' in script
    assert 'docker load -i "$ARCHIVE"' in script
    assert "application/vnd.oci.empty.v1+json" in script
    assert "shasum -a 256" in script
    assert "Image revision mismatch" in script


def test_portfolio_authority_deployment_is_seeded_and_rollback_safe() -> None:
    seed = (DEPLOY / "seed-portfolio-authority.sh").read_text(encoding="utf-8")
    cutover = (DEPLOY / "portfolio-authority-cutover.sh").read_text(encoding="utf-8")
    rollback = (DEPLOY / "portfolio-authority-rollback.sh").read_text(encoding="utf-8")
    orchestrator = (DEPLOY / "deploy-portfolio-authority.sh").read_text(encoding="utf-8")
    assert "portfolio_authority_cli.py seed" in seed
    assert "portfolio_authority_cli.py verify" in seed
    assert '"ready_for_cutover":true' in seed
    assert "storage_recovery_cli.py backup" in cutover
    assert "pre-portfolio-authority" in cutover
    assert "rolling back to personal-data authority" in cutover
    assert "INVESTKITCHEN_PORTFOLIO_AUTHORITY=personal-data" in rollback
    assert "install-image.sh" in orchestrator
    assert "seed-portfolio-authority.sh" in orchestrator
    assert "portfolio-authority-cutover.sh" in orchestrator


def test_synology_host_scripts_do_not_depend_on_sudo_path_for_docker() -> None:
    for name in (
        "cutover.sh",
        "install-image.sh",
        "portfolio-authority-cutover.sh",
        "seed-portfolio-authority.sh",
        "runtime-compose.sh",
    ):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        assert "DOCKER_BIN=$(resolve_docker_bin)" in text
        assert "/usr/local/bin/docker" in text
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("docker "), f"{name}: bare docker command: {stripped}"
            assert ' timeout "${DOCKER_TIMEOUT}s" docker ' not in line
