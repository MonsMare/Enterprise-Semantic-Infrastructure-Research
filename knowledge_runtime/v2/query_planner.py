from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class ConversationState:
    turns: tuple[dict[str, str], ...] = ()
    anchors: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()

    @classmethod
    def from_turns(cls, turns: Sequence[dict[str, Any]]) -> "ConversationState":
        normalized = tuple(
            {"role": str(turn.get("role", "")), "content": str(turn.get("content", ""))}
            for turn in turns
            if str(turn.get("content", "")).strip()
        )
        anchor_values: list[str] = []
        for turn in normalized:
            content = turn["content"]
            # Preserve standards, named documents, identifiers, and compact
            # noun phrases that are useful retrieval anchors.
            anchor_values.extend(re.findall(r"\b[A-Z]{2,}(?:\s*\d+[A-Z]*)?\b", content))
            anchor_values.extend(re.findall(r"《[^》]{2,}》", content))
        deduped = tuple(dict.fromkeys(value.strip() for value in anchor_values if value.strip()))
        return cls(turns=normalized, anchors=deduped)

    def latest_user_text(self) -> str:
        for turn in reversed(self.turns):
            if turn["role"] == "user":
                return turn["content"]
        return ""


@dataclass(frozen=True)
class QueryPlan:
    original: str
    query: str
    rewrites: tuple[str, ...] = ()
    anchors: tuple[str, ...] = ()
    clarification: bool = False
    clarification_reason: str | None = None


class QueryPlanner:
    def __init__(self, *, max_rewrites: int = 3) -> None:
        if max_rewrites <= 0:
            raise ValueError("max_rewrites must be positive")
        self.max_rewrites = max_rewrites

    def plan(self, question: str, *, conversation: ConversationState | None = None) -> QueryPlan:
        original = " ".join(str(question).split())
        state = conversation or ConversationState()
        if not original:
            return QueryPlan(original, "", clarification=True, clarification_reason="empty_query")
        if self._ambiguous_without_anchor(original) and not state.anchors:
            return QueryPlan(
                original,
                original,
                anchors=state.anchors,
                clarification=True,
                clarification_reason="pronoun_without_anchor",
            )

        rewrites: list[str] = []
        query = original
        if self._needs_anchor(original) and state.anchors:
            anchor_prefix = " ".join(state.anchors[:2])
            query = f"{anchor_prefix} {original}".strip()
            rewrites.append(query)
        # One focused lexical rewrite helps terse follow-ups without fanning
        # out into an unbounded broad search.
        if len(original) <= 12 and state.latest_user_text() and query == original:
            prior = state.latest_user_text()
            candidate = f"{prior} {original}".strip()
            if candidate != query:
                rewrites.append(candidate)
                query = candidate
        return QueryPlan(original, query, tuple(rewrites[: self.max_rewrites]), state.anchors)

    @staticmethod
    def _ambiguous_without_anchor(text: str) -> bool:
        compact = re.sub(r"\s+", "", text.casefold())
        return bool(
            re.fullmatch(r"(?:那它|这个它|那个它|那|这|它|这个|那个)(?:的)?(?:呢|怎么样|是什么|有哪些|要求)?[?？]?", compact)
        )

    @staticmethod
    def _needs_anchor(text: str) -> bool:
        compact = re.sub(r"\s+", "", text.casefold())
        return any(marker in compact for marker in ("它", "这个", "那个", "那", "上述", "上面"))
