# Claude Code Rebuild Prompt

Paste this into a new Claude Code session to recreate the AI research agent from scratch.

---

Build a Python script called `research_agent.py` that uses the Anthropic Python SDK (`anthropic`) with server-side web search tools to automatically research VW dealers.

Requirements:
- Use `web_search_20260209` and `web_fetch_20260209` as server-side tools (Claude handles searching automatically, no client-side tool execution needed)
- Use `claude-sonnet-4-6` model
- Load `dealers.csv` (columns: name, url, platform, state) as context in the user message so Claude knows existing dealers
- Accept a research question as a CLI argument: `.venv/bin/python research_agent.py "your question here"`
- Stream text output to the terminal in real-time using `client.messages.stream()`
- Handle `pause_turn` stop reason by re-sending messages so Claude can continue searching
- Include a system prompt telling Claude it's a VW dealer research assistant focused on finding scheduler URLs, platforms (Xtime, Tekion, DealerFX), reviews, and oil change pricing
- Print token usage at the end
- Install with: `uv venv && uv pip install anthropic`
- Requires `ANTHROPIC_API_KEY` env var

Also add an "AI Research Agent" section to the README with setup and usage examples.
