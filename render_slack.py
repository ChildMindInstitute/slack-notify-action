# /// script
# requires-python = ">=3.11"
# dependencies = ["jinja2>=3.1", "slack-sdk>=3.33"]
# ///
"""Render a Slack Block Kit JSON template.

    uv run render_slack.py TEMPLATE.json [--vars FILE] [--vars-json JSON] [-v KEY=VALUE ...] [-o OUT.json]

Variables, later sources win:
  1. env vars prefixed SLACK_VAR_   (SLACK_VAR_SERVICE -> {{ SERVICE }})
  2. --vars FILE / --vars-json JSON (JSON objects; values may be lists/dicts)
  3. -v KEY=VALUE

Every string value in the template is a Jinja2 template, rendered *after* the
JSON is parsed, so values can never break the JSON. Missing variables are an
error. Extra filters: mrkdwn (escape & < >), first_line, status_emoji.
The result is validated against Slack's Block Kit models before it is written.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment
from slack_sdk.errors import SlackObjectFormationError
from slack_sdk.models.blocks import Block

ENV_PREFIX = "SLACK_VAR_"
STATUS_EMOJI = {
    "success": ":white_check_mark:",
    "failure": ":x:",
    "cancelled": ":warning:",
    "skipped": ":fast_forward:",
}

env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
env.filters["mrkdwn"] = lambda s: str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
env.filters["first_line"] = lambda s: (str(s).splitlines() or [""])[0]
env.filters["status_emoji"] = lambda s: STATUS_EMOJI.get(str(s), ":grey_question:")


def render(node, ctx):
    """Recursively render every string value (keys are left alone)."""
    if isinstance(node, str):
        return env.from_string(node).render(ctx) if ("{{" in node or "{%" in node) else node
    if isinstance(node, list):
        return [render(n, ctx) for n in node]
    if isinstance(node, dict):
        return {k: render(v, ctx) for k, v in node.items()}
    return node


def validate(payload):
    blocks = payload.get("blocks", [])
    if len(blocks) > 50:
        raise ValueError(f"{len(blocks)} blocks (Slack max is 50)")
    for i, raw in enumerate(blocks):
        block = Block.parse(raw)
        if block is None:
            raise ValueError(f"blocks[{i}]: unknown block type {raw.get('type')!r}")
        try:
            block.validate_json()
        except SlackObjectFormationError as e:
            raise ValueError(f"blocks[{i}] ({raw.get('type')}): {e}") from e


def as_object(value, source):
    if not isinstance(value, dict):
        raise ValueError(f"{source} must be a JSON object")
    return value


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template")
    ap.add_argument("--vars", help="JSON file of variables")
    ap.add_argument("--vars-json", help="JSON object of variables, as a string")
    ap.add_argument("-v", "--var", action="append", default=[], metavar="KEY=VALUE")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    args = ap.parse_args()

    try:
        ctx = {k.removeprefix(ENV_PREFIX): v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)}

        if args.vars:
            with Path.open(args.vars) as f:
                ctx.update(as_object(json.load(f), "--vars file"))

        if args.vars_json:
            ctx.update(as_object(json.loads(args.vars_json), "--vars-json"))

        for item in args.var:
            key, sep, value = item.partition("=")
            if not sep:
                raise ValueError(f"--var expects KEY=VALUE, got {item!r}")
            ctx[key] = value
        # print(ctx)

        with Path.open(args.template) as f:
            template = json.load(f)
        payload = render(template, ctx)
        validate(payload)
    except (OSError, json.JSONDecodeError, TemplateError, ValueError) as e:
        sys.exit(f"render_slack: {type(e).__name__}: {e}")

    out = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        with Path.open(args.output, "w") as f:
            f.write(out)
    else:
        sys.stdout.write(out)


if __name__ == "__main__":
    main()
