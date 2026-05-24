from core.ai_engines.anthropic_claude import AnthropicKYCAgent
from core.ai_engines.base import KYCReviewer
from core.ai_engines.openai_vision import OpenAIVisionReviewer


def get_kyc_reviewer(engine_name: str) -> KYCReviewer:
    if engine_name == "openai_vision":
        return OpenAIVisionReviewer()
    if engine_name == "anthropic_claude":
        return AnthropicKYCAgent()
    raise ValueError(f"Unknown KYC AI engine: {engine_name}")
