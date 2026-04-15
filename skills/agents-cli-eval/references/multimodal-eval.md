# Multimodal Evaluation

> File paths below reference the scaffolded layout (`tests/eval/`). Adjust for your project structure if not using /agents-cli-scaffold.

## Schema Support

`Invocation.user_content` is `genai_types.Content`, which accepts multimodal `Part` types beyond text:

- **`inline_data`** — base64-encoded bytes with a mime type (images, audio, PDF)
- **`file_data`** — GCS URI reference (`gs://bucket/path`)

### Evalset example with image input

```json
{
  "eval_id": "describe_image",
  "conversation": [
    {
      "invocation_id": "inv_1",
      "user_content": {
        "parts": [
          { "text": "Describe this image" },
          { "inline_data": { "mime_type": "image/png", "data": "<base64>" } }
        ]
      },
      "final_response": {
        "role": "model",
        "parts": [{ "text": "The image shows a bar chart..." }]
      }
    }
  ]
}
```

For GCS-hosted files, use `file_data` instead:

```json
{ "file_data": { "mime_type": "image/jpeg", "file_uri": "gs://my-bucket/photos/test.jpg" } }
```

---

## What Works Out of the Box

- **`tool_trajectory_avg_score`** — evaluates tool call sequences, not content modality. Works fine with multimodal inputs.
- **Response/rubric evaluators** — evaluate the agent's *text response*. If the agent produces a text answer from multimodal input (e.g., describes an image), these metrics work normally.

---

## The Text-Only Gap

Built-in LLM-as-judge evaluators call `get_text_from_content()` (`llm_as_judge_utils.py:46-50`), which extracts only `.text` parts and skips `inline_data`/`file_data`. This means:

- The **judge never sees** the original image/audio/file content
- If the evaluation itself needs to reason about the multimodal input (e.g., "did the agent correctly describe this image?"), the built-in judge cannot verify it
- A **custom metric** is needed for true multimodal evaluation

---

## Custom Metric for Multimodal Evaluation

Custom metric functions receive full `Invocation` objects (`custom_metric_evaluator.py:54-76`), including all multimodal parts. Build a prompt that sends the original media + agent response to a vision-capable judge model.

```python
# my_app/eval/multimodal_metric.py
import os
from google import genai
from google.adk.evaluation.eval_case import Invocation
from google.adk.evaluation.eval_metrics import EvalMetric
from google.adk.evaluation.evaluator import EvaluationResult, PerInvocationResult, EvalStatus

def _get_genai_client() -> genai.Client:
    # genai.Client() does NOT auto-detect GOOGLE_GENAI_USE_VERTEXAI
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
        return genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
    return genai.Client()

async def multimodal_response_quality(
    eval_metric: EvalMetric,
    actual_invocations: list[Invocation],
    expected_invocations: list[Invocation] | None,
    conversation_scenario=None,
) -> EvaluationResult:
    client = _get_genai_client()
    threshold = eval_metric.threshold or 0.8
    per_invocation = []
    for actual in actual_invocations:
        if not actual.final_response or not actual.final_response.parts:
            per_invocation.append(PerInvocationResult(
                actual_invocation=actual, score=0.0, eval_status=EvalStatus.FAILED))
            continue
        agent_text = "\n".join(p.text for p in actual.final_response.parts if p.text)
        # Build prompt with original multimodal parts + agent response
        judge_parts = list(actual.user_content.parts) + [
            genai.types.Part.from_text(  # note: text= keyword is required
                text=f"\n\nAgent response: {agent_text}"
                "\n\nDoes the agent response accurately describe the content above? "
                "Reply with ONLY a single number from 0.0 to 1.0 (nothing else)."
            ),
        ]
        response = await client.aio.models.generate_content(
            model="gemini-3-flash-preview",
            contents=genai.types.Content(role="user", parts=judge_parts),
        )
        try:
            score = max(0.0, min(1.0, float(response.text.strip())))
        except (ValueError, AttributeError):
            score = 0.0
        per_invocation.append(PerInvocationResult(
            actual_invocation=actual, score=score,
            eval_status=EvalStatus.PASSED if score >= threshold else EvalStatus.FAILED,
        ))
    avg = sum(r.score for r in per_invocation) / len(per_invocation) if per_invocation else 0.0
    return EvaluationResult(
        overall_score=avg, per_invocation_results=per_invocation,
        overall_eval_status=EvalStatus.PASSED if avg >= threshold else EvalStatus.FAILED,
    )
```

### Wire it in `eval_config.json`

```json
{
  "criteria": {
    "multimodal_response_quality": 0.8
  },
  "custom_metrics": {
    "multimodal_response_quality": {
      "code_config": {
        "name": "my_app.eval.multimodal_metric.multimodal_response_quality"
      },
      "description": "Evaluates response accuracy against multimodal input"
    }
  }
}
```
