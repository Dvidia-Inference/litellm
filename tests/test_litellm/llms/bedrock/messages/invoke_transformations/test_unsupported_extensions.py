import copy

import pytest

from litellm.llms.bedrock.messages.invoke_transformations.unsupported_extensions import (
    Offender,
    OptInKnob,
    OptIns,
    Refused,
    Sanitized,
    Unchanged,
    sanitize_for_bedrock_invoke,
)


@pytest.mark.parametrize(
    "request_body",
    [
        {"messages": "not a list", "thinking": "not a dict"},
        {"messages": ["bare string", 7, None], "thinking": {"type": "adaptive"}},
        {"messages": [{"role": "assistant", "content": ["bare string", 7, {"type": "text", "text": "ok"}]}]},
    ],
)
def test_sanitize_leaves_non_json_object_shapes_alone(request_body):
    snapshot = copy.deepcopy(request_body)

    outcome = sanitize_for_bedrock_invoke(request_body, OptIns(drop_params=True, modify_params=False))

    assert isinstance(outcome, Unchanged)
    assert request_body == snapshot


def test_sanitize_drops_output_config_without_mutating_the_request():
    request_body = {
        "messages": [{"role": "assistant", "content": "plain string content", "output_config": {"effort": "high"}}],
    }
    snapshot = copy.deepcopy(request_body)

    outcome = sanitize_for_bedrock_invoke(request_body, OptIns(drop_params=True, modify_params=False))

    assert isinstance(outcome, Sanitized)
    assert request_body == snapshot
    assert outcome.request["messages"] == [{"role": "assistant", "content": "plain string content"}]


def test_opt_ins_allows_gates_each_knob_independently():
    both_off = OptIns(drop_params=False, modify_params=False)
    drop_only = OptIns(drop_params=True, modify_params=False)
    modify_only = OptIns(drop_params=False, modify_params=True)

    assert not both_off.allows(OptInKnob.DROP_PARAMS) and not both_off.allows(OptInKnob.MODIFY_PARAMS)
    assert drop_only.allows(OptInKnob.DROP_PARAMS) and not drop_only.allows(OptInKnob.MODIFY_PARAMS)
    assert not modify_only.allows(OptInKnob.DROP_PARAMS) and modify_only.allows(OptInKnob.MODIFY_PARAMS)


def test_refused_lists_only_the_blocked_offenders_with_their_knob():
    request = {
        "messages": [
            {
                "role": "assistant",
                "output_config": {"effort": "high"},
                "content": [
                    {"type": "tool_addition", "tool_reference": {"type": "tool_reference", "tool_name": "Read"}},
                    {"type": "text", "text": "ok"},
                ],
            },
        ],
        "thinking": {"type": "adaptive", "display": "updates"},
    }

    outcome = sanitize_for_bedrock_invoke(request, OptIns(drop_params=True, modify_params=False))

    assert isinstance(outcome, Refused)
    assert outcome.offenders == (
        Offender(path="messages[0].content[0] (type 'tool_addition')", knob=OptInKnob.MODIFY_PARAMS),
    )


def test_refused_lists_both_groups_when_both_knobs_off():
    request = {
        "messages": [
            {
                "role": "assistant",
                "output_config": {"effort": "high"},
                "content": [
                    {"type": "tool_addition", "tool_reference": {"type": "tool_reference", "tool_name": "Read"}}
                ],
            },
        ],
    }

    outcome = sanitize_for_bedrock_invoke(request, OptIns(drop_params=False, modify_params=False))

    assert isinstance(outcome, Refused)
    assert outcome.offenders == (
        Offender(path="messages[0].output_config", knob=OptInKnob.DROP_PARAMS),
        Offender(path="messages[0].content[0] (type 'tool_addition')", knob=OptInKnob.MODIFY_PARAMS),
    )
