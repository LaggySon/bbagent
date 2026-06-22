# bbagent — deterministic, API, and Claude Code run modes in one container.
FROM python:3.12-slim

WORKDIR /app

# Node + the Claude Code CLI, so the 3.2 (Claude Code) run mode works in-container
# too. Auth is headless: inject CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`,
# uses your Pro/Max subscription) or ANTHROPIC_API_KEY at run time — no interactive
# login needed. See README §3.6.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g @anthropic-ai/claude-code \
    && apt-get purge -y npm \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first so the layer caches across source edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Source + non-secret config. Secrets (.env, tokens) are injected at run time,
# never baked in — see .dockerignore.
COPY espn_agent.py bbagent_mcp.py scouting.py projections.py run-loop.sh ./
COPY bbagent.config.json .mcp.json CLAUDE.md ./

# Default: the self-scheduling loop (poll → full pass → sleep, log to loop.log).
# Override RUN_CMD at `docker run` for one-shots or to pick a run mode:
#   docker run --rm --env-file .env bbagent python espn_agent.py plan       # deterministic
#   docker run --rm --env-file .env -e RUN_CMD="claude -p '...' \
#       --mcp-config .mcp.json --permission-mode acceptEdits" bbagent       # Claude Code
CMD ["./run-loop.sh"]
