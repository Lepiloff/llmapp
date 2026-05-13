"""Pure discovery classification helpers."""
from __future__ import annotations

from dataclasses import dataclass

from apps.agent.llm.client import LLMCallMetadata, LLMProvider
from apps.agent.llm.prompts import discovery_classify_prompt
from apps.agent.llm.schemas import DiscoveryDecision
from apps.agent.sources.base import DiscoveryCandidate


@dataclass(frozen=True)
class DiscoveryResult:
    candidate: DiscoveryCandidate
    decision: DiscoveryDecision
    call_meta: LLMCallMetadata

    def as_dict(self) -> dict:
        return {
            "candidate": {
                "external_id": self.candidate.external_id,
                "url": self.candidate.url,
                "title": self.candidate.title,
                "summary": self.candidate.summary,
                "source_name": self.candidate.source_name,
                "raw_payload": self.candidate.raw_payload,
            },
            "decision": self.decision.model_dump(),
            "llm": {
                "provider": self.call_meta.provider,
                "model": self.call_meta.model,
                "prompt_version": self.call_meta.prompt_version,
                "input_tokens": self.call_meta.input_tokens,
                "output_tokens": self.call_meta.output_tokens,
                "cached_tokens": self.call_meta.cached_tokens,
                "cost_usd": self.call_meta.cost_usd,
                "is_mock": self.call_meta.is_mock,
            },
        }


def classify_candidate(
    candidate: DiscoveryCandidate,
    llm: LLMProvider,
) -> DiscoveryResult:
    prompt = discovery_classify_prompt(candidate.llm_context())
    response = llm.complete(
        system=prompt.system,
        messages=prompt.messages,
        schema=DiscoveryDecision,
        prompt_version=prompt.version,
    )
    decision = response.data
    assert isinstance(decision, DiscoveryDecision), (
        "LLMProvider returned wrong schema for discovery classification."
    )
    return DiscoveryResult(
        candidate=candidate,
        decision=decision,
        call_meta=response.meta,
    )
