#!/usr/bin/env python3
"""Invoke a Cortex Agent over REST.

Written as a reusable module rather than a test script because Path 4 embeds the
same call in the Streamlit and React front ends. One implementation of the
request shape, the SSE parsing, and the auth means the chat in the app and the
chat in a test harness cannot drift apart.

Auth uses the programmatic access token already configured for the snow CLI
connection, so no new credential is introduced.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import config


def load_connection(name: str) -> dict:
    """Read a connection definition from the standard snow CLI config."""
    # tomllib is standard library only from Python 3.11; tomli is the backport.
    # Supporting both keeps this runnable on an older local interpreter.
    try:
        import tomllib as toml_reader
    except ModuleNotFoundError:
        import tomli as toml_reader

    path = pathlib.Path.home() / ".snowflake" / "connections.toml"
    with open(path, "rb") as f:
        cfg = toml_reader.load(f)
    if name not in cfg:
        raise SystemExit(f"connection {name!r} not found in {path}")
    return cfg[name]


def agent_endpoint(account: str, agent_fqn: str) -> str:
    """Build the agent:run URL.

    The account identifier uses an underscore between org and account in the
    hostname form, whereas connections.toml stores it with a hyphen.
    """
    host = account.lower().replace("_", "-")
    db, schema, name = agent_fqn.split(".")
    return (
        f"https://{host}.snowflakecomputing.com"
        f"/api/v2/databases/{db}/schemas/{schema}/agents/{name}:run"
    )


def read_token(conn: dict) -> str:
    """Resolve the PAT from wherever the connection keeps it.

    The snow CLI supports an inline `token`, a `token_file_path`, or an
    environment variable. Handling all three means this module works against a
    connection configured any of the usual ways rather than only the one in front
    of us, and it never prints the token.
    """
    env = os.environ.get("SNOWFLAKE_PAT") or os.environ.get("SNOWFLAKE_TOKEN")
    if env:
        return env.strip()
    if conn.get("token"):
        return str(conn["token"]).strip()
    tfp = conn.get("token_file_path")
    if tfp:
        p = pathlib.Path(os.path.expanduser(str(tfp)))
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
        raise SystemExit(f"token_file_path points at a missing file: {p}")
    raise SystemExit(
        "No token found. Set `token` or `token_file_path` on the connection, "
        "or export SNOWFLAKE_PAT."
    )


def run_agent(
    agent_fqn: str,
    question: str,
    connection_name: str = "my-demo-account",
    timeout: int = 180,
) -> dict:
    """Send one question to an agent and collect the streamed response.

    The endpoint returns server-sent events. Text arrives incrementally as
    ``response.text.delta`` events, tool activity as its own event types, so the
    transcript is reassembled here rather than assuming a single JSON body.
    """
    conn = load_connection(connection_name)
    token = read_token(conn)

    url = agent_endpoint(conn["account"], agent_fqn)
    payload = {
        "thread_id": None,
        "parent_message_id": None,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": question}]}
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {token}",
            "X-Snowflake-Authorization-Token-Type": "PROGRAMMATIC_ACCESS_TOKEN",
        },
        method="POST",
    )

    text_parts: list[str] = []
    tool_uses: list[dict] = []
    sql_statements: list[str] = []
    charts: list[dict] = []
    errors: list[str] = []

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            event_type = None
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                body = line.split(":", 1)[1].strip()
                if body in ("", "[DONE]"):
                    continue
                try:
                    ev = json.loads(body)
                except json.JSONDecodeError:
                    continue

                if event_type == "response.text.delta":
                    text_parts.append(ev.get("text", ""))
                elif event_type == "response.text":
                    # Full-text event; prefer it if no deltas were seen.
                    if not text_parts:
                        text_parts.append(ev.get("text", ""))
                elif event_type == "response.tool_use":
                    tool_uses.append(
                        {"name": ev.get("name"), "input": ev.get("input")}
                    )
                elif event_type == "response.tool_result":
                    content = ev.get("content") or []
                    for c in content:
                        j = (c or {}).get("json") or {}
                        if j.get("sql"):
                            sql_statements.append(j["sql"])
                        if j.get("chart_spec"):
                            charts.append(j["chart_spec"])
                elif event_type in ("error", "response.error"):
                    errors.append(json.dumps(ev))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1500]
        return {"ok": False, "status": e.code, "error": detail, "text": ""}
    except Exception as e:  # network, timeout
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "text": ""}

    return {
        "ok": not errors,
        "text": "".join(text_parts).strip(),
        "tools_used": [t["name"] for t in tool_uses if t.get("name")],
        "sql": sql_statements,
        "charts": charts,
        "errors": errors,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Invoke a Cortex Agent")
    ap.add_argument("question", nargs="?", help="Question to ask")
    config.add_arguments(ap)
    ap.add_argument("--agent",
                    help="Fully-qualified agent name; defaults to the one "
                         "the naming flags imply")
    ap.add_argument("--connection", default="my-demo-account")
    ap.add_argument("--suite", action="store_true", help="Run the standard test suite")
    args = ap.parse_args(argv)

    # The suite deliberately includes questions the model should NOT be able to
    # answer. An agent evaluation that only asks answerable questions tells you
    # nothing about how it behaves at the edge of its scope.
    suite = [
        (
            "in-scope structured",
            "What is our fiscal year to date sales amount by product line?",
            "should return four product lines with fiscal YTD figures",
        ),
        (
            "definitional",
            "What does backlog mean in this model and which metric should I use for it?",
            "should use the glossary tool, not general knowledge",
        ),
        (
            "fiscal trap",
            "What were sales year to date? Say explicitly which period you used.",
            "should use FISCAL year to date, July onwards, not calendar January",
        ),
        (
            "out of scope",
            "What is the average patient adherence rate for our CPAP devices by month?",
            "should decline and name what is missing, not approximate it",
        ),
        (
            "cross-fact",
            "Which sales territories have the largest gap between bookings and sales?",
            "should combine both fact tables",
        ),
    ]

    if args.suite:
        results = []
        for label, q, expectation in suite:
            print(f"\n{'=' * 70}\n[{label}] {q}\n  expectation: {expectation}\n{'-' * 70}")
            r = run_agent(args.agent, q, args.connection)
            if not r["ok"] and r.get("error"):
                print(f"  ERROR: {str(r['error'])[:600]}")
            print(f"  tools: {r.get('tools_used')}")
            print("  " + (r.get("text") or "(no text)")[:1400].replace("\n", "\n  "))
            results.append({"label": label, "question": q, **r})
        os.makedirs("out", exist_ok=True)
        with open("out/agent_test_results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print("\nwrote out/agent_test_results.json")
        return 0

    if not args.question:
        ap.error("supply a question or use --suite")
    r = run_agent(args.agent, args.question, args.connection)
    print(json.dumps(r, indent=2)[:4000])
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
