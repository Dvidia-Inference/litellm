"""Live e2e: POST /v1/messages to a Bedrock Claude deployment must sanitize
Anthropic-native extensions Bedrock's invoke schema rejects.

Claude Code emits per-message ``output_config`` (effort), a ``display`` value on
adaptive thinking, and ``tool_addition`` content blocks; api.anthropic.com
accepts all three while Bedrock InvokeModel 400s. The gateway is expected to
strip or map them the same way the existing allowlist strips top-level extras,
so the customer sees a 200 either way.
"""

from __future__ import annotations

from typing import cast

import pytest
from anthropic.types import Message, MessageParam, TextBlock, ThinkingConfigParam
from e2e_config import unique_marker
from lifecycle import ResourceManager
from models import LiteLLMParamsBody
from proxy_client import ProxyClient
from sdk_clients import NO_PROXY_CACHE, SdkClients

pytestmark = pytest.mark.e2e

BEDROCK_BACKEND = "bedrock/us.anthropic.claude-sonnet-5"


def _register(proxy: ProxyClient, resources: ResourceManager) -> tuple[str, str]:
    model = f"e2e-bedrock-msgs-ext-{unique_marker()}"
    model_id = proxy.create_model(
        model,
        LiteLLMParamsBody(
            model=BEDROCK_BACKEND,
            aws_access_key_id="os.environ/AWS_BEDROCK_TEST_ACCESS_KEY_ID",
            aws_secret_access_key="os.environ/AWS_BEDROCK_TEST_SECRET_ACCESS_KEY",
            aws_region_name="us-east-1",
        ),
    )
    resources.defer(lambda: proxy.delete_model(model_id))
    return model, resources.key()


def _text(message: Message) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextBlock))


class TestBedrockMessagesNativeExtensions:
    @pytest.mark.covers("llm.messages.bedrock.native_extensions.output_config_dropped")
    def test_nested_output_config_dropped(
        self, proxy: ProxyClient, resources: ResourceManager, sdk: SdkClients
    ) -> None:
        model, key = _register(proxy, resources)
        client = sdk.anthropic(key)

        # Claude Code stamps output_config on assistant turns; Bedrock rejects it
        # ("messages.1.output_config: Extra inputs are not permitted"), so the cast
        # models the wire shape the SDK types do not know about yet.
        message = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[
                {"role": "user", "content": "read the file /tmp/a.txt"},
                cast(
                    MessageParam,
                    {
                        "role": "assistant",
                        "output_config": {"effort": "high"},
                        "content": [
                            {"type": "text", "text": "Reading it now."},
                            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "/tmp/a.txt"}},
                        ],
                    },
                ),
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "hello world"}],
                },
            ],
            extra_body=NO_PROXY_CACHE,
        )
        assert message.role == "assistant", f"unexpected role: {message.role!r}"
        assert _text(message).strip(), f"/v1/messages returned no text: {message.content!r}"

    @pytest.mark.covers("llm.messages.bedrock.native_extensions.thinking_display_mapped")
    def test_thinking_display_updates_mapped(
        self, proxy: ProxyClient, resources: ResourceManager, sdk: SdkClients
    ) -> None:
        model, key = _register(proxy, resources)
        client = sdk.anthropic(key)

        # Anthropic accepts display="updates" on adaptive thinking; Bedrock only
        # allows 'summarized'/'omitted', so the gateway must map it.
        message = client.messages.create(
            model=model,
            max_tokens=300,
            thinking=cast(ThinkingConfigParam, {"type": "adaptive", "display": "updates"}),
            messages=[{"role": "user", "content": "what is 2+2? think briefly"}],
            extra_body=NO_PROXY_CACHE,
        )
        assert message.role == "assistant", f"unexpected role: {message.role!r}"
        assert _text(message).strip(), f"/v1/messages returned no text: {message.content!r}"

    @pytest.mark.covers("llm.messages.bedrock.native_extensions.tool_addition_dropped")
    def test_tool_addition_block_dropped(self, proxy: ProxyClient, resources: ResourceManager, sdk: SdkClients) -> None:
        model, key = _register(proxy, resources)
        client = sdk.anthropic(key)

        # tool_addition blocks carry deferred tool references Anthropic accepts;
        # Bedrock's content-block tag allowlist has no 'tool_addition' entry.
        message = client.messages.create(
            model=model,
            max_tokens=300,
            messages=[
                {"role": "user", "content": "read /tmp/a.txt"},
                cast(
                    MessageParam,
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_addition",
                                "tool_reference": {"type": "tool_reference", "tool_name": "Read"},
                            },
                            {"type": "text", "text": "ok"},
                        ],
                    },
                ),
                {"role": "user", "content": "continue"},
            ],
            extra_body=NO_PROXY_CACHE,
        )
        assert message.role == "assistant", f"unexpected role: {message.role!r}"
        assert _text(message).strip(), f"/v1/messages returned no text: {message.content!r}"
