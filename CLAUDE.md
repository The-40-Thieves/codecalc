<!-- entire-agent:begin -->
Read .entire/agent-guide.md for this repository's workflow, source inspection, and verification guidance.
@.entire/agent-guide.md
<!-- entire-agent:end -->

## Boundaries

- Do not edit `.entire/agent-guide.md` or anything between the `entire-agent` markers: `entire brain setup` regenerates them.
- Never commit secrets, tokens, `.env` files or `.entire/settings.local.json`.
- Never push Entire checkpoints or session transcripts to this repository's origin; they belong only in the private `The-40-Thieves/entire-checkpoints` repo.
- Do not merge to `main`, force-push, or rewrite published history without the owner's approval.
