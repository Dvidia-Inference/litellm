import enum
import types
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final, NoReturn, TypeAlias, assert_never

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

import litellm
from litellm.constants import (
    BEDROCK_INVOKE_SUPPORTED_THINKING_DISPLAY_VALUES,
    BEDROCK_INVOKE_UNSUPPORTED_MESSAGE_CONTENT_BLOCK_TYPES,
)
from litellm.litellm_core_utils.prompt_templates.factory import DEFAULT_USER_CONTINUE_MESSAGE

_KEY_MESSAGES: Final = "messages"
_KEY_THINKING: Final = "thinking"
_KEY_DISPLAY: Final = "display"
_KEY_CONTENT: Final = "content"
_KEY_TYPE: Final = "type"
_KEY_TEXT: Final = "text"
_KEY_OUTPUT_CONFIG: Final = "output_config"

_PLACEHOLDER_TEXT_BLOCK: Final = types.MappingProxyType(
    {_KEY_TYPE: "text", _KEY_TEXT: DEFAULT_USER_CONTINUE_MESSAGE["content"]}
)


class OptInKnob(enum.Enum):
    DROP_PARAMS = "drop_params"
    MODIFY_PARAMS = "modify_params"


@dataclass(frozen=True, slots=True)
class OptIns:
    drop_params: bool
    modify_params: bool

    def allows(self, knob: OptInKnob) -> bool:
        match knob:
            case OptInKnob.DROP_PARAMS:
                return self.drop_params
            case OptInKnob.MODIFY_PARAMS:
                return self.modify_params
            case _:
                assert_never(knob)


@dataclass(frozen=True, slots=True)
class Offender:
    path: str
    knob: OptInKnob


@dataclass(frozen=True, slots=True)
class Sanitized:
    messages: tuple[Mapping[str, object] | JsonValue, ...] | None
    thinking: Mapping[str, JsonValue] | None
    removed: tuple[Offender, ...]


@dataclass(frozen=True, slots=True)
class Refused:
    offenders: tuple[Offender, ...]


SanitizeOutcome = Sanitized | Refused


class _ContentBlockView(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    type: str


_CONTENT_BLOCK_UNION: TypeAlias = Annotated[_ContentBlockView | JsonValue, Field(union_mode="left_to_right")]


class _MessageView(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    content: str | tuple[_CONTENT_BLOCK_UNION, ...] | None = None
    output_config: JsonValue | None = None


_MESSAGE_UNION: TypeAlias = Annotated[_MessageView | JsonValue, Field(union_mode="left_to_right")]


class _ThinkingView(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    display: str | None = None


class _RequestView(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    messages: tuple[_MESSAGE_UNION, ...] | None = None
    thinking: JsonValue | None = None


_REQUEST_VIEW_ADAPTER: Final = TypeAdapter(_RequestView)
_THINKING_VIEW_ADAPTER: Final = TypeAdapter(_ThinkingView)


def _thinking_view(raw_thinking: JsonValue | None) -> _ThinkingView | None:
    try:
        return _THINKING_VIEW_ADAPTER.validate_python(raw_thinking)
    except ValidationError:
        return None


def _message_param_offenders(message_index: int, message: _MessageView | JsonValue) -> tuple[Offender, ...]:
    if not isinstance(message, _MessageView) or _KEY_OUTPUT_CONFIG not in message.model_fields_set:
        return ()
    return (Offender(path=f"{_KEY_MESSAGES}[{message_index}].{_KEY_OUTPUT_CONFIG}", knob=OptInKnob.DROP_PARAMS),)


def _message_block_offenders(message_index: int, message: _MessageView | JsonValue) -> tuple[Offender, ...]:
    if not isinstance(message, _MessageView) or not isinstance(message.content, tuple):
        return ()
    return tuple(
        Offender(
            path=f"{_KEY_MESSAGES}[{message_index}].{_KEY_CONTENT}[{block_index}] ({_KEY_TYPE} '{block.type}')",
            knob=OptInKnob.MODIFY_PARAMS,
        )
        for block_index, block in enumerate(message.content)
        if isinstance(block, _ContentBlockView) and block.type in BEDROCK_INVOKE_UNSUPPORTED_MESSAGE_CONTENT_BLOCK_TYPES
    )


def _param_offenders(view: _RequestView, thinking: _ThinkingView | None) -> tuple[Offender, ...]:
    display: Final = thinking.display if thinking is not None else None
    return (
        tuple(
            offender
            for index, message in enumerate(view.messages or ())
            for offender in _message_param_offenders(index, message)
        )
    ) + (
        (Offender(path=f"{_KEY_THINKING}.{_KEY_DISPLAY} (value '{display}')", knob=OptInKnob.DROP_PARAMS),)
        if isinstance(display, str) and display not in BEDROCK_INVOKE_SUPPORTED_THINKING_DISPLAY_VALUES
        else ()
    )


def _block_offenders(view: _RequestView) -> tuple[Offender, ...]:
    return tuple(
        offender
        for index, message in enumerate(view.messages or ())
        for offender in _message_block_offenders(index, message)
    )


def _sanitize_message(message: JsonValue, opt_ins: OptIns) -> Mapping[str, object] | JsonValue:
    if not isinstance(message, Mapping):
        return message
    cleaned: Final = types.MappingProxyType(
        {k: v for k, v in message.items() if not (k == _KEY_OUTPUT_CONFIG and opt_ins.drop_params)}
    )
    content: Final = cleaned.get(_KEY_CONTENT)
    if not opt_ins.modify_params or not isinstance(content, list):
        return cleaned
    remaining: Final = tuple(
        block
        for block in content
        if not (
            isinstance(block, Mapping)
            and isinstance(block.get(_KEY_TYPE), str)
            and block[_KEY_TYPE] in BEDROCK_INVOKE_UNSUPPORTED_MESSAGE_CONTENT_BLOCK_TYPES
        )
    )
    if remaining or not content:
        return types.MappingProxyType({**cleaned, _KEY_CONTENT: remaining})
    return types.MappingProxyType({**cleaned, _KEY_CONTENT: (_PLACEHOLDER_TEXT_BLOCK,)})


def sanitize_for_bedrock_invoke(request: Mapping[str, JsonValue], opt_ins: OptIns) -> SanitizeOutcome:
    try:
        view: Final = _REQUEST_VIEW_ADAPTER.validate_python(request)
    except ValidationError:
        return Sanitized(messages=None, thinking=None, removed=())

    thinking_view: Final = _thinking_view(view.thinking)
    offenders: Final = _param_offenders(view, thinking_view) + _block_offenders(view)
    blocked: Final = tuple(offender for offender in offenders if not opt_ins.allows(offender.knob))
    if blocked:
        return Refused(offenders=blocked)
    if not offenders:
        return Sanitized(messages=None, thinking=None, removed=())

    raw_messages: Final = request.get(_KEY_MESSAGES)
    sanitized_messages: Final = (
        tuple(_sanitize_message(message, opt_ins) for message in raw_messages)
        if isinstance(raw_messages, list)
        else None
    )
    raw_thinking: Final = request.get(_KEY_THINKING)
    display: Final = thinking_view.display if thinking_view is not None else None
    sanitized_thinking: Final = (
        types.MappingProxyType({k: v for k, v in raw_thinking.items() if k != _KEY_DISPLAY})
        if (
            opt_ins.drop_params
            and isinstance(raw_thinking, Mapping)
            and isinstance(display, str)
            and display not in BEDROCK_INVOKE_SUPPORTED_THINKING_DISPLAY_VALUES
        )
        else None
    )
    return Sanitized(
        messages=sanitized_messages,
        thinking=sanitized_thinking,
        removed=offenders,
    )


def raise_refusal(refused: Refused, model: str) -> NoReturn:
    param_offenders: Final = tuple(
        offender.path for offender in refused.offenders if offender.knob is OptInKnob.DROP_PARAMS
    )
    block_offenders: Final = tuple(
        offender.path for offender in refused.offenders if offender.knob is OptInKnob.MODIFY_PARAMS
    )
    param_error: Final = (
        f"Bedrock Invoke does not accept {', '.join(param_offenders)}. "
        "Set `litellm_settings.drop_params: true` on the proxy or `litellm.drop_params = True` "
        "in the SDK to have LiteLLM drop them (an unsupported thinking.display falls back to the "
        "model default), or remove them from the request."
    )
    block_error: Final = (
        f"Bedrock Invoke does not accept {', '.join(block_offenders)}. "
        "Set `litellm_settings.modify_params: true` on the proxy or `litellm.modify_params = True` "
        "in the SDK to have LiteLLM remove those blocks (a message left empty gets the placeholder "
        f"text '{DEFAULT_USER_CONTINUE_MESSAGE['content']}'), or remove them from the request."
    )
    match (bool(param_offenders), bool(block_offenders)):
        case (True, True):
            raise litellm.UnsupportedParamsError(
                message=f"{param_error} {block_error}",
                model=model,
                llm_provider="bedrock",
            )
        case (True, False):
            raise litellm.UnsupportedParamsError(
                message=param_error,
                model=model,
                llm_provider="bedrock",
            )
        case (False, True):
            raise litellm.BadRequestError(
                message=block_error,
                model=model,
                llm_provider="bedrock",
            )
        case (False, False):
            raise litellm.UnsupportedParamsError(
                message="Bedrock Invoke request was refused with no recorded offenders.",
                model=model,
                llm_provider="bedrock",
            )
