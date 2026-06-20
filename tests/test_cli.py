"""CLI smoke tests for the command surface."""

from __future__ import annotations

import textwrap

from click.testing import CliRunner

from lynx.cli.main import cli


def test_version_shows_current_v2() -> None:
    """The CLI reports the package's __version__; we don't pin a specific
    minor here so the test doesn't lock in a release number."""
    from lynx import __version__

    runner = CliRunner()
    res = runner.invoke(cli, ["--version"])
    assert res.exit_code == 0
    assert __version__ in res.output
    assert __version__.startswith("2.")


def test_init_writes_policy_only(tmp_path) -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["init", "--dir", str(tmp_path)])
    assert res.exit_code == 0
    assert (tmp_path / "policy.yaml").exists()
    # init writes ONLY the policy. No state dir, no toml.
    assert not (tmp_path / ".lynx").exists()
    assert not (tmp_path / "lynx.toml").exists()


def test_init_does_not_overwrite_without_force(tmp_path) -> None:
    (tmp_path / "policy.yaml").write_text("# pre-existing")
    runner = CliRunner()
    res = runner.invoke(cli, ["init", "--dir", str(tmp_path)])
    assert res.exit_code == 1
    combined = (res.output or "") + (res.stderr or "")
    assert "already exists" in combined


def test_init_force_overwrites(tmp_path) -> None:
    (tmp_path / "policy.yaml").write_text("# pre-existing")
    runner = CliRunner()
    res = runner.invoke(cli, ["init", "--dir", str(tmp_path), "--force"])
    assert res.exit_code == 0
    text = (tmp_path / "policy.yaml").read_text()
    assert "rules:" in text


def test_policy_lint_clean(tmp_path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "version: 1\n"
        "defaults: { on_no_match: allow }\n"
        "rules:\n"
        "  - id: r1\n"
        "    match: { tool: shell }\n"
        "    decision: allow\n"
    )
    runner = CliRunner()
    res = runner.invoke(cli, ["policy", "lint", str(policy)])
    assert res.exit_code == 0
    assert "1 rules" in res.output


def test_policy_bundle_id_deterministic(tmp_path) -> None:
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\ndefaults: { on_no_match: deny }\nrules: []\n")
    runner = CliRunner()
    res1 = runner.invoke(cli, ["policy", "bundle-id", str(policy)])
    res2 = runner.invoke(cli, ["policy", "bundle-id", str(policy)])
    assert res1.exit_code == 0
    assert res1.output == res2.output


def test_policy_lint_reports_compile_error(tmp_path) -> None:
    policy = tmp_path / "broken.yaml"
    policy.write_text(
        "version: 1\n"
        "rules:\n"
        "  - id: r1\n"
        "    match: { tool: shell, args.cmd.matchess: 'x' }\n"  # typo'd operator
        "    decision: deny\n"
    )
    runner = CliRunner()
    res = runner.invoke(cli, ["policy", "lint", str(policy)])
    assert res.exit_code == 1
    err = res.stderr or res.output
    assert "PolicyCompileError" in err or "matches" in err


def test_policy_bundle_id_reports_compile_error(tmp_path) -> None:
    policy = tmp_path / "broken.yaml"
    policy.write_text("this is: not valid: yaml: <<<")
    runner = CliRunner()
    res = runner.invoke(cli, ["policy", "bundle-id", str(policy)])
    assert res.exit_code == 1
    err = res.stderr or res.output
    assert "PolicyCompileError" in err or "YAML" in err


def test_run_subcommand_executes_async_main(tmp_path) -> None:
    script = tmp_path / "ran.py"
    marker = tmp_path / "MARKER"
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio

            async def main():
                with open({str(marker)!r}, 'w') as f:
                    f.write('ok')
            """
        )
    )
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(script)])
    assert res.exit_code == 0
    assert marker.read_text() == "ok"


def test_run_subcommand_rejects_script_without_async_main(tmp_path) -> None:
    script = tmp_path / "noasync.py"
    script.write_text("def main():\n    return 1\n")
    runner = CliRunner()
    res = runner.invoke(cli, ["run", str(script)])
    assert res.exit_code == 1
    err = res.stderr or res.output
    assert "async" in err


def test_init_creates_missing_directory(tmp_path) -> None:
    target = tmp_path / "sub" / "dir"
    runner = CliRunner()
    res = runner.invoke(cli, ["init", "--dir", str(target)])
    assert res.exit_code == 0
    assert (target / "policy.yaml").exists()
