# Token audit

`tools/token_audit.py` reads your Claude Code transcripts (`~/.claude/projects/*/*.jsonl`) and reports:

- **Where tokens go**: by project, chat, tool output, model, sub-agents (cost-weighted: cache read 0.1x, output 5x).
- **Why and what to do**: per-chat fixes (long context -> /compact, huge tool output, repeated file reads, Opus on small tasks, cache churn).
- **Unfinished work**: chats *needing your reply*, *interrupted*, *unanswered*, or with *open to-dos*, with idle time, STALE flag and the `claude --resume <id>` command.

```
python3 tools/token_audit.py                      # terminal report, last 30 days
python3 tools/token_audit.py --html audit.html    # + dashboard
python3 tools/token_audit.py --days 0 --stale-days 3 --json audit.json
```

No dependencies (Python 3.8+). Reads local files only. Run it with `--root` to point at another transcript folder.
Cloud-session transcripts live inside each container, so run it where the sessions ran, or copy the `.jsonl` files over.

## Windows (PowerShell)

```
git clone -b claude/gallant-archimedes-rtjmjg https://github.com/saransthinking/virtual-lab-wsd-ergonomics.git
cd virtual-lab-wsd-ergonomics
py tools\token_audit.py --html audit.html
start audit.html
```

Install Python from python.org (tick "Add python.exe to PATH") if `py` is not found. Use `py`, not `python3`.

## claude.ai chats and projects (all conversations)

1. claude.ai -> Settings -> Privacy -> **Export data**. Wait for the email, download the zip.
2. Run on the zip (no need to unzip):

```
py tools\token_audit.py --export "C:\Users\You\Downloads\data-xxxx.zip" --days 0 --html chats.html
start chats.html
```

Exports have no token counts, so figures are **estimates** (characters / 4, with every reply re-reading the whole chat plus the
project's knowledge files). Use them to rank chats and projects, not to bill. Unfinished chats (waiting on your reply, or your
last message unanswered) are listed with a direct chat link.
