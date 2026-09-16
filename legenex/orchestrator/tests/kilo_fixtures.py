"""Request fixtures shaped like real Kilo Code traffic.

Kilo Code (like Cline and Roo) sends every turn with:

* a system prompt of tens of kilobytes (rules, modes, tool usage guide);
* a native `tools` array describing its whole coding toolbox;
* `max_tokens` equal to the model's advertised output window;
* a user turn wrapped as `<task>` (first turn) or `<user_message>` /
  `<feedback>` (follow-ups), followed by an `<environment_details>` block that
  lists open tabs, terminals and the workspace file tree.

The file tree is deliberately full of words the old classifier scored as
complexity (api, endpoint, debug, workflow, agent, test, http, implement).

Used by tests/test_classifier.py (hermetic) and by the live gx-auto
acceptance script (legenex/tests/gx-auto-acceptance.py), so the live check
routes exactly the payloads the unit tests pin.
"""

from __future__ import annotations

import copy
from typing import Any

_TOOL_SPECS: tuple[tuple[str, str], ...] = (
    ("read_file", "Read one or more files and return their contents with line numbers."),
    ("write_to_file", "Write full content to a file, creating directories as needed."),
    ("apply_diff", "Apply a search/replace diff block to an existing file."),
    ("insert_content", "Insert new lines into a file at a given line number."),
    ("search_and_replace", "Find and replace text or regex matches inside a file."),
    ("execute_command", "Execute a CLI command in the workspace terminal and return output."),
    ("list_files", "List files and directories, optionally recursively."),
    ("search_files", "Regex search across files in a directory, returning context."),
    ("list_code_definition_names", "List classes, functions and methods defined in source files."),
    ("codebase_search", "Semantic search over the indexed codebase."),
    ("browser_action", "Drive a headless browser: launch, click, type, scroll, close."),
    ("ask_followup_question", "Ask the user a clarifying question with suggested answers."),
    ("attempt_completion", "Present the final result of the task to the user."),
    ("switch_mode", "Request switching to a different mode (code, architect, ask, debug)."),
    ("new_task", "Create a new sub-task in a chosen mode with an initial message."),
    ("update_todo_list", "Replace the task todo list with an updated checklist."),
    ("use_mcp_tool", "Call a tool exposed by a connected MCP server."),
    ("access_mcp_resource", "Read a resource exposed by a connected MCP server."),
    ("fetch_instructions", "Fetch instructions for creating an MCP server or a mode."),
    ("run_slash_command", "Run a workspace slash command and return its output."),
)


def _tool(name: str, description: str) -> dict[str, Any]:
    long_description = (
        description
        + " "
        + (
            "Use this tool when it is the most direct way to make progress. Always provide "
            "absolute or workspace-relative paths. Never guess file contents; read before you "
            "edit. The result is returned to you in the next message. "
        )
        * 12
    )
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": long_description,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path."},
                    "content": {"type": "string", "description": "Content or diff body."},
                    "args": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional arguments for the operation.",
                    },
                    "line": {"type": "integer", "description": "1-based line number."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    }


KILO_TOOLS: list[dict[str, Any]] = [_tool(n, d) for n, d in _TOOL_SPECS]

KILO_SYSTEM_PROMPT = (
    "You are Kilo Code, a highly skilled software engineer with extensive knowledge in many "
    "programming languages, frameworks, design patterns, and best practices.\n\n"
    "====\n\nMARKDOWN RULES\n\nALL responses MUST show ANY `language construct` OR filename "
    "reference as clickable.\n\n====\n\nTOOL USE\n\nYou have access to a set of tools that are "
    "executed upon the user's approval. You must use exactly one tool per message.\n\n"
    + (
        "# Tool Use Guidelines\n1. Assess what information you already have and what you need. "
        "2. Choose the most appropriate tool. 3. If multiple actions are needed, use one tool at "
        "a time. 4. Wait for the user's confirmation after each tool use. Debug failures by "
        "reading the error output carefully; design, refactor and architect only when asked. "
        "Prove assumptions by reading code, derive conclusions from evidence.\n"
    )
    * 120
    + "\n====\n\nRULES\n- The project base directory is: /home/dev/projects/webshop\n"
    "- Do not ask for more information than necessary.\n"
)

_FILE_TREE = "\n".join(
    [
        "api/",
        "api/endpoints/orders.py",
        "api/endpoints/payments.py",
        "api/http_client.py",
        "agents/workflow_runner.py",
        "agents/orchestrator.py",
        "debug/debug.log",
        "docs/architecture.md",
        "docs/design-system-architecture.md",
        "scripts/implement_migration.sh",
        "src/components/Checkout.tsx",
        "src/components/Cart.tsx",
        "src/lib/proof_of_concept.ts",
        "tests/test_orders.py",
        "tests/test_payments.py",
        "tests/stack_trace_fixture.txt",
        "README.md",
    ]
    * 60
)

ENVIRONMENT_DETAILS = (
    "<environment_details>\n"
    "# VSCode Visible Files\nsrc/components/Checkout.tsx\n\n"
    "# VSCode Open Tabs\napi/endpoints/orders.py,src/components/Checkout.tsx,tests/test_orders.py\n\n"
    "# Actively Running Terminals\n## Terminal 1 (Active)\n### Working Directory: /home/dev/projects/webshop\n"
    "### Original command: `npm run dev -- --debug`\n\n"
    "# Current Time\n2026-09-17T09:30:00+02:00\n\n"
    "# Current Cost\n$0.00\n\n"
    "# Current Mode\n<slug>code</slug>\n<name>Code</name>\n<model>gx-auto</model>\n\n"
    "# Current Workspace Directory (/home/dev/projects/webshop) Files\n"
    f"{_FILE_TREE}\n"
    "</environment_details>"
)


def kilo_request(
    task: str,
    *,
    tag: str = "task",
    max_tokens: int = 32_768,
    stream: bool = True,
) -> dict[str, Any]:
    """A first-turn Kilo Code request for `task`."""
    return {
        "model": "gx-auto",
        "stream": stream,
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": KILO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"<{tag}>\n{task}\n</{tag}>"},
                    {"type": "text", "text": ENVIRONMENT_DETAILS},
                ],
            },
        ],
        "tools": copy.deepcopy(KILO_TOOLS),
        "tool_choice": "auto",
    }


def kilo_continuation(task: str, *, native_tool_role: bool = True) -> dict[str, Any]:
    """A mid-loop Kilo request: the newest turn is a tool result."""
    req = kilo_request(task)
    req["messages"].append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "api/endpoints/orders.py"}'},
                }
            ],
        }
    )
    body = "1 | from fastapi import APIRouter\n2 | router = APIRouter()\n"
    if native_tool_role:
        req["messages"].append({"role": "tool", "tool_call_id": "call_1", "content": body})
    else:
        req["messages"].append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "[read_file for 'api/endpoints/orders.py'] Result:\n" + body},
                    {"type": "text", "text": ENVIRONMENT_DETAILS},
                ],
            }
        )
    return req


#: (task text, expected tier, why) for first-turn Kilo requests.
KILO_ROUTING_CASES: tuple[tuple[str, str, str], ...] = (
    ("are you there?", "gx-mini", "presence check with the whole toolbox attached"),
    ("hello", "gx-mini", "greeting"),
    ("what can you help me with in this repo?", "gx-mini", "capability question, no task"),
    ("who are you and which model are you?", "gx-mini", "identity question"),
    ("thanks, that's all", "gx-mini", "acknowledgement"),
    (
        "Fix the failing test in tests/test_orders.py and update the orders endpoint so the "
        "total includes tax.",
        "gx-fast",
        "real repo modification",
    ),
    ("add a dark mode toggle to the Checkout component", "gx-fast", "feature work"),
    ("run the unit tests and fix whatever breaks", "gx-fast", "agentic coding loop"),
    (
        "Debug the intermittent race condition in agents/orchestrator.py: two workers sometimes "
        "commit the same order. Find the root cause and explain step by step why the locking "
        "design fails before proposing a fix.",
        "gx-reason",
        "hard concurrency debugging",
    ),
    (
        "Design the architecture for splitting payments into a separate service: the protocol, "
        "the schema changes, failure modes and the trade-offs. Prove the idempotency invariant "
        "holds under retries.",
        "gx-reason",
        "architecture + proof",
    ),
)
