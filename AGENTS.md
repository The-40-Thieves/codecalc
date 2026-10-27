<!-- entire-agent:begin -->
Read .entire/agent-guide.md for this repository's workflow, source inspection, and verification guidance.
@.entire/agent-guide.md
<!-- entire-agent:end -->

## Boundaries

- Leave `.entire/agent-guide.md` and the `entire-agent` blocks to `entire brain setup`, which regenerates them; to change them, re-run it rather than editing by hand.
- Don't commit secrets, tokens, `.env` files or `.entire/settings.local.json`. If one is committed by mistake, stop and tell the owner so it can be rotated.
- Session transcripts and checkpoints go only to the private `The-40-Thieves/entire-checkpoints` repo, not this origin. If `entire status` shows another destination, stop and ask before pushing.
- Merging to `main`, force-pushing or rewriting published history needs the owner's go-ahead in the current conversation.
