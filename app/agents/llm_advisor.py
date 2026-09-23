import json

from app.config import Settings
from app.models import StrategySignal


class LLMAdvisor:
    """Optional advisory agent. It cannot execute orders and can only adjust confidence by +/- 0.10."""

    def __init__(self, settings: Settings):
        self.settings = settings

    async def confidence_adjustment(self, signal: StrategySignal) -> tuple[float, str]:
        if not self.settings.enable_llm_advisor or not self.settings.openai_api_key:
            return 0.0, "LLM advisor disabled"

        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(api_key=self.settings.openai_api_key)
            prompt = {
                "task": "Review a technical trading signal. Return JSON only.",
                "constraints": [
                    "Do not place orders.",
                    "Do not change risk limits.",
                    "adjustment must be between -0.10 and 0.10.",
                ],
                "signal": signal.side.value,
                "base_confidence": signal.confidence,
                "reason": signal.reason,
                "features": signal.features,
                "output_schema": {"adjustment": 0.0, "reason": "short explanation"},
            }
            response = await client.responses.create(
                model=self.settings.openai_model,
                input=json.dumps(prompt),
            )
            text = response.output_text.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:].strip()
            data = json.loads(text)
            adjustment = max(-0.10, min(0.10, float(data.get("adjustment", 0))))
            return adjustment, str(data.get("reason", "LLM review"))[:300]
        except Exception as exc:
            return 0.0, f"LLM advisor unavailable: {exc}"
