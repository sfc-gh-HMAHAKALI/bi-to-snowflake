-- =====================================================================
-- Call {{AGENT}} from SQL
-- =====================================================================
-- Why this exists: the Streamlit app calls the agent, and the container runtime cannot.
--
-- `_snowflake.send_snow_api_request` is the usual way to reach a Cortex Agent from
-- Streamlit in Snowflake without an External Access Integration. But `_snowflake` is a
-- private module available only inside UDFs and stored procedures. A warehouse-runtime
-- Streamlit app inherits it because the app itself runs as a UDF; a container-runtime
-- app runs in a compute pool and does not. So the agent embed that worked on warehouse
-- runtime silently has no route on container runtime.
--
-- Three options were available:
--   * go back to warehouse runtime -- gives up pinned dependencies, which is what fixed
--     the outage in the first place
--   * add an External Access Integration and call the REST API with `requests` -- needs
--     a token and an outbound network rule, both of which the reference model's platform team would
--     have to approve
--   * wrap the call in a stored procedure, which still has `_snowflake`
--
-- The third keeps the call inside Snowflake, needs no new infrastructure, and has a
-- useful side effect: the agent becomes reachable from ordinary SQL, so the React app
-- and any JDBC client can use exactly the same path rather than each implementing its
-- own REST plumbing.
-- =====================================================================

USE SCHEMA {{DB}}.ANALYTICS;

CREATE OR REPLACE PROCEDURE {{ASK_PROC}}(QUESTION STRING, CONTEXT STRING)
RETURNS VARIANT
LANGUAGE PYTHON
RUNTIME_VERSION = '3.11'
PACKAGES = ('snowflake-snowpark-python')
HANDLER = 'run'
COMMENT = 'Ask the {{AGENT}} Cortex Agent. Returns {answer, sql, error}. Callable from any SQL client.'
AS
$$
import json


def _parse(body):
    """Pull the answer text and any generated SQL out of the agent's event stream.

    The event contract, confirmed by inspecting a live response rather than assumed:

      response.status          progress messages -- ignore
      response.thinking[.delta] reasoning trace -- ignore, this is not the answer
      response.text.delta      answer fragments, many
      response.text            the same answer, complete, once
      response.tool_result     carries the SQL the agent ran
      response                 final envelope
      done                     data is the bare string "[DONE]", not a dict

    Three traps in that list. `done` carries a string where every other event carries a
    dict, so any parser that calls .get() on data unconditionally raises AttributeError
    on the very last event -- after doing all the work correctly. The answer arrives as
    deltas AND as a complete `response.text`. And it arrives a third time inside the
    final `response` envelope, so treating all three as additive returns the reply
    twice over, which reads as the agent stuttering.

    Precedence is therefore strict rather than additive: complete text if present, else
    the final envelope, else assembled deltas.
    """
    events = json.loads(body) if isinstance(body, str) else body
    if isinstance(events, dict):
        events = [events]

    text_events, envelope, deltas, sql = [], [], [], ""

    for event in events or []:
        if not isinstance(event, dict):
            continue
        name = event.get("event") or ""
        data = event.get("data")
        if not isinstance(data, dict):
            continue                      # 'done' and anything else non-dict

        if name == "response.text":
            text_events.append(data.get("text", ""))
        elif name == "response.text.delta":
            deltas.append(data.get("text", ""))
        elif name == "response.tool_result":
            for c in data.get("content") or []:
                if isinstance(c, dict):
                    sql = (c.get("json", {}) or {}).get("sql", "") or sql
        elif name == "response":
            for c in data.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "text":
                    envelope.append(c.get("text", ""))

    for candidate in (text_events, envelope, deltas):
        answer = "".join(candidate).strip()
        if answer:
            return answer, sql
    return "", sql


def run(session, question, context):
    if not question or not question.strip():
        return {"answer": "", "sql": "", "error": "No question supplied."}

    import _snowflake

    payload = {
        "model": "claude-4-sonnet",
        "messages": [{
            "role": "user",
            "content": [{"type": "text",
                         "text": f"{context or ''}\n\n{question}".strip()}],
        }],
    }

    try:
        resp = _snowflake.send_snow_api_request(
            "POST",
            "/api/v2/databases/{{DB}}/schemas/ANALYTICS/agents/{{AGENT}}:run",
            {}, {}, payload, None, 120000,
        )
    except Exception as exc:
        return {"answer": "", "sql": "", "error": f"Agent call failed: {exc}"}

    body = resp.get("content") if isinstance(resp, dict) else None
    if body is None:
        return {"answer": "", "sql": "",
                "error": f"Unexpected agent response shape: {resp!r}"[:800]}

    try:
        answer, sql = _parse(body)
    except Exception as exc:
        return {"answer": "", "sql": "",
                "error": f"Could not parse agent response: {exc}"}

    if not answer:
        return {"answer": "", "sql": sql,
                "error": "The agent returned no text."}
    return {"answer": answer, "sql": sql, "error": ""}
$$;
