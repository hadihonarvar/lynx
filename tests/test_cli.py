"""Smoke tests for the CLI via click's test runner."""

from __future__ import annotations

from click.testing import CliRunner

from gazelle.cli.main import cli


def test_version_command():
    runner = CliRunner()
    res = runner.invoke(cli, ["--version"])
    assert res.exit_code == 0
    assert "0.1.0" in res.output


def test_init_creates_policy_and_config(tmp_path):
    runner = CliRunner()
    res = runner.invoke(cli, ["init", "--dir", str(tmp_path)])
    assert res.exit_code == 0
    assert (tmp_path / "policy.yaml").exists()
    assert (tmp_path / "gazelle.toml").exists()
    assert (tmp_path / ".gazelle").is_dir()


def test_policy_lint_clean(tmp_path):
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
    assert "1 rules compiled" in res.output


def test_policy_bundle_id_is_deterministic(tmp_path):
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\ndefaults: { on_no_match: deny }\nrules: []\n")
    runner = CliRunner()
    res1 = runner.invoke(cli, ["policy", "bundle-id", str(policy)])
    res2 = runner.invoke(cli, ["policy", "bundle-id", str(policy)])
    assert res1.exit_code == 0
    assert res1.output == res2.output
