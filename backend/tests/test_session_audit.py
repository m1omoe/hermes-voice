import pytest

from app import session_audit


def test_latest_assistant_text_extracts_text_blocks_and_ignores_non_text_blocks():
    messages = [
        {
            "role": "assistant",
            "timestamp": 10,
            "content": [
                {"type": "thinking", "thinking": "internal reasoning"},
                {"type": "tool_use", "name": "terminal", "input": {"command": "pwd"}},
                {"type": "text", "text": "Visible answer."},
            ],
        }
    ]

    assert session_audit._latest_assistant_text(messages, since_ts=10) == "Visible answer."
    assert session_audit._latest_assistant_text(
        [messages[0] | {"content": [{"type": "thinking", "thinking": "only reasoning"}]}],
        since_ts=10,
    ) == ""


def test_latest_assistant_text_preserves_timestamp_filter_and_last_assistant_semantics():
    messages = [
        {"role": "assistant", "timestamp": 9, "content": "Old answer."},
        {"role": "assistant", "timestamp": 10, "content": {"content": "First answer."}},
        {"role": "assistant", "timestamp": 11, "content": "Last answer."},
    ]

    assert session_audit._latest_assistant_text(messages, since_ts=10) == "Last answer."


def test_latest_assistant_text_returns_empty_when_all_assistant_messages_are_stale():
    messages = [
        {"role": "assistant", "timestamp": 9, "content": "Old answer."},
        {"role": "assistant", "timestamp": 9.5, "content": [{"type": "text", "text": "Also old."}]},
    ]

    assert session_audit._latest_assistant_text(messages, since_ts=10) == ""


def test_latest_assistant_text_accepts_sole_typed_text_block():
    messages = [
        {"role": "assistant", "timestamp": 10, "content": {"type": "text", "text": "Only answer."}},
    ]

    assert session_audit._latest_assistant_text(messages, since_ts=10) == "Only answer."


def test_latest_assistant_text_rejects_typed_non_text_blocks_with_visible_fields():
    for block in (
        {"type": "thinking", "text": "internal thinking"},
        {"type": "reasoning", "content": "internal reasoning"},
        {"type": "future_block", "text": "not an answer"},
    ):
        messages = [{"role": "assistant", "timestamp": 10, "content": block}]

        assert session_audit._latest_assistant_text(messages, since_ts=10) == ""


@pytest.mark.asyncio
async def test_fetch_turn_result_extracts_structured_text_and_preserves_tool_summaries(monkeypatch):
    messages = [
        {
            "role": "assistant",
            "timestamp": 10,
            "content": [{"type": "text", "text": "Done."}],
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {"name": "terminal", "arguments": '{"command":"pwd"}'},
                }
            ],
        },
        {
            "role": "tool",
            "timestamp": 10,
            "tool_call_id": "call-1",
            "content": '{"exit_code":0}',
        },
    ]

    async def export_messages(settings, session_id):
        return messages

    monkeypatch.setattr(session_audit, "_export_messages", export_messages)

    text, tools = await session_audit.fetch_turn_result(object(), "session-id", since_ts=10)

    assert text == "Done."
    assert [tool.as_dict() for tool in tools] == [
        {"name": "terminal", "preview": "pwd", "ok": True}
    ]
