# AI incidents

AI never writes to a patient record on its own: summaries are drafts until a physician approves them, and the
assistant's writes need the user's confirmation. An AI incident is therefore about availability, cost, or the
quality of drafts physicians are being asked to review.

## Summaries failing (502 "AI summary generation failed")

Every attempt is recorded in `ai_generations`, failures included:

```sql
SELECT created_at, status, provider, model_id, prompt_version, attempts, error
FROM ai_generations WHERE purpose = 'discharge_summary' ORDER BY created_at DESC LIMIT 20;
```

- `status = failed`, `error` like `provider returned 429/5xx` or `provider unreachable`: provider outage or rate
  limit. Check the provider's status page. Physicians can write the summary manually meanwhile; nothing is lost.
- `status = failed`, `model declined the request`: a safety refusal. There is deliberately no automatic fallback to
  another model (provenance must name the model that wrote the text). Look at the inputs for that workflow with the
  physician, never by copying them out of the system.
- `status = invalid_output`: the model returned JSON that didn't match the schema twice. A spike right after a
  prompt or model change means the change should be reverted (point `ACTIVE` back to the previous prompt version in
  `app/ai/prompts/__init__.py`, or change `LLM_SUMMARY_MODEL`).

## Disabling AI quickly

Set the API task's `LLM_PROVIDER` to `none` and force a new deployment. Summaries then return 503 ("AI provider not
configured") and the assistant falls back to offline rule-based routing over the same permission-checked tools.

## Suspect drafts (hallucinations, wrong patient details)

1. Grounding warnings on drafts (`ai_generations.grounding_warnings`) flag medications or diagnoses not found in the
   input; a rise in them is the early signal.
2. `GET /api/ai/metrics` shows approval rate and edit similarity per prompt version. Falling similarity means
   physicians are rewriting more of each draft.
3. Reproduce offline with the golden set: `python -m app.ai.evals --provider anthropic --model <model>`. Add the failure
   pattern as a new synthetic case in `app/ai/evals/golden_set.json`, never real patient data, so CI guards against
   it from then on.
4. Fix with a new prompt version (published versions are immutable), and ship it through the normal PR and eval gate.

## Cost spike

Token counts are in `ai_generations` (`input_tokens`, `output_tokens`). Group by `user_id` and day to find the source.
Per-user rate limits and a monthly budget alarm are planned (ADR-001 guardrails) but not built yet.

## PHI concerns

Prompts contain de-identified text only (`app/ai/deid.py`), and neither prompts nor outputs are stored in
`ai_generations`. If identifiers did reach a provider (for example a name format the de-identifier missed), treat it
as a potential disclosure: disable AI (above), add the case to the de-identification tests and the golden set, and
follow the HIPAA breach assessment process. Using the Anthropic API with real patient data requires a BAA; Bedrock
is covered by the AWS BAA.
