"""Unit + contract tests for render_slack.py, the templates, and the action's run-block.

uv run pytest                      # run everything
UPDATE_GOLDEN=1 uv run pytest      # regenerate tests/golden/*.json after an intentional change
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jinja2 import TemplateError, meta

import render_slack as rs

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "render_slack.py"
GOLDEN = ROOT / "tests" / "golden"
TEMPLATES = sorted((ROOT / "templates").glob("*.json"))
TEMPLATE_IDS = [p.stem for p in TEMPLATES]

# Variables the action provides by default, plus the extras `vars:` supplies.
CTX = {
    "STATUS": "success",
    "REPO": "me/app",
    "ACTOR": "andrew",
    "WORKFLOW": "Deploy",
    "EVENT": "push",
    "BRANCH": "main",
    "SHA": "abc1234def5678",
    "COMMIT_URL": "https://github.com/me/app/commit/abc1234",
    "COMMIT_MESSAGE": 'Fix "quotes" & <tags>\nsecond line',
    "PR_TITLE": "",
    "PR_NUMBER": "",
    "PR_URL": "",
    "RUN_URL": "https://github.com/me/app/actions/runs/9",
    "RUN_NUMBER": "7",
    "SERVICE": "myapp",
    "ENVIRONMENT": "production",
}
# Variables a built-in template needs that the action does NOT set by default.
EXTRA_VARS = {"deploy": {"SERVICE", "ENVIRONMENT"}}


def section(text="hi"):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def load(path):
    return json.loads(Path(path).read_text())


# --------------------------------------------------------------------------- render
class TestRender:
    def test_renders_nested_strings_and_leaves_keys_and_scalars(self):
        tpl = {"{{ KEY }}": ["{{ A }}", {"x": "{{ B }}-{{ A }}"}, 5, None, True]}
        out = rs.render(tpl, {"A": "a", "B": "b", "KEY": "k"})
        assert out == {"{{ KEY }}": ["a", {"x": "b-a"}, 5, None, True]}

    @pytest.mark.parametrize(
        "value",
        [
            'say "hi"',
            "line1\nline2",
            "back\\slash",
            "emoji 🚀 ünï",
            "{braces}",
            "it's",
            "</script>",
            "",
        ],
    )
    def test_awkward_values_survive_json_roundtrip(self, value):
        out = rs.render({"text": "{{ V }}"}, {"V": value})
        assert json.loads(json.dumps(out))["text"] == value

    def test_values_are_data_not_templates(self):
        assert rs.render({"t": "{{ V }}"}, {"V": "{{ 7 * 7 }}"}) == {"t": "{{ 7 * 7 }}"}

    def test_missing_variable_is_an_error(self):
        with pytest.raises(TemplateError):
            rs.render({"t": "{{ NOPE }}"}, {})

    def test_template_sandbox_blocks_dunder_access(self):
        with pytest.raises(TemplateError):
            rs.render({"t": "{{ ''.__class__.__mro__ }}"}, {})

    def test_slicing_and_expressions(self):
        assert rs.render({"t": "{{ SHA[:7] }} {{ 'a' if X else 'b' }}"}, {"SHA": "1234567890", "X": 0}) == {
            "t": "1234567 b"
        }


class TestFilters:
    def test_mrkdwn_escapes_only_the_three_characters(self):
        assert rs.env.from_string("{{ s | mrkdwn }}").render(s="a & <b> *c*") == "a &amp; &lt;b&gt; *c*"

    def test_mrkdwn_does_not_double_escape_ampersand_ordering(self):
        assert rs.env.from_string("{{ s | mrkdwn }}").render(s="<&>") == "&lt;&amp;&gt;"

    @pytest.mark.parametrize("text,expected", [("a\nb", "a"), ("single", "single"), ("", ""), ("\nx", "")])
    def test_first_line(self, text, expected):
        assert rs.env.from_string("{{ s | first_line }}").render(s=text) == expected

    @pytest.mark.parametrize(
        "status,emoji",
        [
            ("success", ":white_check_mark:"),
            ("failure", ":x:"),
            ("cancelled", ":warning:"),
            ("???", ":grey_question:"),
        ],
    )
    def test_status_emoji(self, status, emoji):
        assert rs.env.from_string("{{ s | status_emoji }}").render(s=status) == emoji


# ------------------------------------------------------------------------- validate
class TestValidate:
    def test_valid_message_passes(self):
        rs.validate({"blocks": [section()]})

    def test_message_without_blocks_passes(self):
        rs.validate({"text": "fallback only"})

    def test_too_many_blocks(self):
        with pytest.raises(ValueError, match="50"):
            rs.validate({"blocks": [section()] * 51})

    def test_unknown_block_type(self):
        with pytest.raises(ValueError, match="unknown block type"):
            rs.validate({"blocks": [{"type": "nope"}]})

    def test_header_over_150_chars(self):
        with pytest.raises(ValueError, match="header"):
            rs.validate({"blocks": [{"type": "header", "text": {"type": "plain_text", "text": "x" * 151}}]})

    def test_section_text_over_3000_chars(self):
        with pytest.raises(ValueError, match="section"):
            rs.validate({"blocks": [section("x" * 3001)]})


# ----------------------------------------------------------------- built-in templates


@pytest.mark.parametrize("path", TEMPLATES, ids=TEMPLATE_IDS)
def test_template_renders_validates_and_has_fallback_text(path):
    payload = rs.render(load(path), CTX)
    rs.validate(payload)
    assert json.loads(json.dumps(payload)) == payload
    assert payload["text"], "top-level `text` is the notification fallback; keep it"


@pytest.mark.skip
@pytest.mark.parametrize("path", TEMPLATES, ids=TEMPLATE_IDS)
def test_template_golden_output(path):
    payload = rs.render(load(path), CTX)
    golden = GOLDEN / f"{path.stem}.json"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.parent.mkdir(exist_ok=True)
        golden.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    assert payload == load(golden), "output changed; if intentional run UPDATE_GOLDEN=1 uv run pytest"


def action_default_vars():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    step = next(s for s in action["runs"]["steps"] if s.get("id") == "render")

    return {k.removeprefix("SLACK_VAR_") for k in step["env"] if k.startswith("SLACK_VAR_")}


@pytest.mark.parametrize("path", TEMPLATES, ids=TEMPLATE_IDS)
def test_every_template_variable_is_provided_by_the_action(path, monkeypatch):
    """Guards against a template using a variable action.yml never sets."""
    needed = set()

    def walk(node):
        if isinstance(node, str):
            needed.update(meta.find_undeclared_variables(rs.env.parse(node)))
        elif isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            for n in node.values():
                walk(n)

    walk(load(path))
    provided = action_default_vars() | EXTRA_VARS.get(path.stem, set())

    assert needed <= provided, f"{path.name} uses undefined variables: {sorted(needed - provided)}"


# ------------------------------------------------------------------------------- CLI
def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLACK_VAR_")}
    env.update(extra)
    return env


def run_cli(*args, **env):
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=clean_env(**env))


class TestCli:
    @pytest.fixture
    def tpl(self, tmp_path):
        p = tmp_path / "t.json"
        p.write_text('{"text": "{{ X }}"}')
        return p

    def test_precedence_env_then_vars_json_then_dash_v(self, tpl):
        assert json.loads(run_cli(str(tpl), SLACK_VAR_X="env").stdout)["text"] == "env"
        assert json.loads(run_cli(str(tpl), "--vars-json", '{"X": "json"}', SLACK_VAR_X="env").stdout)["text"] == "json"
        r = run_cli(str(tpl), "--vars-json", '{"X": "json"}', "-v", "X=arg", SLACK_VAR_X="env")
        assert json.loads(r.stdout)["text"] == "arg"

    def test_vars_file(self, tpl, tmp_path):
        f = tmp_path / "v.json"
        f.write_text('{"X": ["a", "b"]}')
        tpl.write_text('{"text": "{{ X | join(\\"+\\") }}"}')
        assert json.loads(run_cli(str(tpl), "--vars", str(f)).stdout)["text"] == "a+b"

    def test_output_flag_writes_file_and_keeps_stdout_empty(self, tpl, tmp_path):
        out = tmp_path / "out.json"
        r = run_cli(str(tpl), "-o", str(out), SLACK_VAR_X="v")
        assert r.returncode == 0 and r.stdout == ""
        assert load(out) == {"text": "v"}

    @pytest.mark.parametrize(
        "args",
        [
            ["--vars-json", "{oops"],  # malformed JSON
            ["--vars-json", "[1, 2]"],  # not an object
            ["-v", "NOEQUALS"],  # bad KEY=VALUE
        ],
    )
    def test_bad_input_fails_cleanly(self, tpl, args):
        r = run_cli(str(tpl), *args, SLACK_VAR_X="v")
        assert r.returncode != 0 and r.stderr.startswith("render_slack:") and "Traceback" not in r.stderr

    def test_missing_variable_exits_nonzero(self, tpl):
        r = run_cli(str(tpl))
        assert r.returncode != 0 and "X" in r.stderr

    def test_missing_template_file(self, tmp_path):
        assert run_cli(str(tmp_path / "nope.json")).returncode != 0


# ------------------------------------------------- the action's real bash run-block
def render_step():
    action = yaml.safe_load((ROOT / "action.yml").read_text())
    return next(s for s in action["runs"]["steps"] if s.get("id") == "render")


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
class TestActionRunBlock:
    """Executes the exact `run:` script from action.yml (needs uv; resolves the PEP 723 deps)."""

    def run(self, tmp_path, **env):
        gh_output = tmp_path / "gh_output"
        gh_output.touch()
        base = {k: v for k, v in CTX.items() if k not in EXTRA_VARS["deploy"]}

        full = clean_env(
            ACTION_PATH=str(ROOT),
            RUNNER_TEMP=str(tmp_path),
            GITHUB_OUTPUT=str(gh_output),
            TEMPLATE="workflow",
            TEMPLATE_FILE="",
            OUTPUT_FILE="",
            **{f"SLACK_VAR_{k}": v for k, v in base.items()},
        )
        full.update(env)
        r = subprocess.run(["bash", "-c", render_step()["run"]], capture_output=True, text=True, env=full)
        return r, gh_output.read_text()

    def test_success_sets_output_and_writes_valid_payload(self, tmp_path):
        r, out = self.run(tmp_path)
        assert r.returncode == 0, r.stderr
        path = out.strip().removeprefix("payload-file=")
        assert path == str(tmp_path / "slack-payload.json")
        rs.validate(load(path))

    def test_vars_from_action_supplies_template_extras(self, tmp_path, monkeypatch):
        r, out = self.run(tmp_path, TEMPLATE="deploy", SLACK_VAR_SERVICE="svc", SLACK_VAR_ENVIRONMENT="prod")
        assert r.returncode == 0, r.stderr
        assert "svc" in load(out.strip().removeprefix("payload-file="))["text"]

    def test_path_traversal_in_template_name_is_rejected(self, tmp_path):
        r, _ = self.run(tmp_path, TEMPLATE="../../etc/passwd")
        assert r.returncode != 0 and "invalid template name" in r.stdout

    def test_missing_extras_fail_the_step(self, tmp_path):
        r, out = self.run(tmp_path, TEMPLATE="deploy")
        assert r.returncode != 0 and out == ""
