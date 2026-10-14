#!/usr/bin/env python3
"""Claude Code token audit + unfinished-work finder.

Reads session transcripts (~/.claude/projects/<project>/<session>.jsonl) and answers:
  1. Which project / chat / tool / model burns the most tokens?
  2. Why (long contexts, huge tool outputs, repeated file reads, heavy model)?
  3. What should you do about it (auto-generated, per session)?
  4. Which chats are unfinished: waiting for your reply, interrupted, paused, or stale?

Usage:
  python3 tools/token_audit.py                       # terminal report
  python3 tools/token_audit.py --html report.html    # + self-contained dashboard
  python3 tools/token_audit.py --days 14 --stale-days 3 --root ~/.claude/projects
  python3 tools/token_audit.py --json out.json       # raw data

Cost model: "weighted tokens" = input-token equivalents using Anthropic's price ratios
(fresh input 1x, cache write 1.25x/2x, cache read 0.1x, output 5x). It ranks sessions by
cost, it is not a bill.
"""
import argparse, collections, datetime as dt, html, json, os, re, sys
from pathlib import Path

W_IN, W_CW5, W_CW1, W_CR, W_OUT = 1.0, 1.25, 2.0, 0.1, 5.0
BIG_RESULT_CHARS = 20_000      # a single tool result this big is a context bomb
LONG_CONTEXT = 150_000         # per-call context above this = expensive every turn
QUESTION_RE = re.compile(r"(\?\s*$|\b(let me know|which (one|option)|would you like|do you want|should i|shall i|please confirm|want me to)\b)", re.I)
INTERRUPT_RE = re.compile(r"\[Request interrupted", re.I)


def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict):
            if b.get("type") == "text":
                out.append(b.get("text", ""))
            elif b.get("type") == "tool_result":
                out.append(text_of(b.get("content")))
    return "\n".join(out)


def size_of(content):
    return len(text_of(content))


def analyze_session(path, now, stale_days):
    s = dict(id=path.stem, project=path.parent.name, path=str(path), title="", first_ts=None, last_ts=None,
             calls=0, in_=0, cw=0, cr=0, out=0, weighted=0.0, peak_ctx=0, sub_weighted=0.0,
             models=collections.Counter(), tool_chars=collections.Counter(), tool_calls=collections.Counter(),
             big_results=[], rereads=collections.Counter(), open_todos=[], cwd="", user_msgs=0)
    usage_by_msg = {}      # message id -> (usage, model, sidechain, ts)  (blocks of one reply repeat usage)
    tool_names = {}        # tool_use id -> (name, detail)
    todos = {}             # task id/content -> status
    last = None            # last conversational event: (kind, text, ts, extra)
    pending_tool = {}      # tool_use ids not yet answered
    first_user = ""

    for line in open(path, encoding="utf-8", errors="replace"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        ts = parse_ts(o.get("timestamp", "")) if o.get("timestamp") else None
        if ts:
            s["first_ts"] = s["first_ts"] or ts
            s["last_ts"] = ts
        s["cwd"] = o.get("cwd") or s["cwd"]
        if t in ("ai-title", "summary", "custom-title"):
            s["title"] = o.get("aiTitle") or o.get("summary") or o.get("customTitle") or s["title"]
        msg = o.get("message") or {}
        side = bool(o.get("isSidechain"))

        if t == "assistant":
            mid = msg.get("id") or o.get("uuid")
            usage_by_msg[mid] = (msg.get("usage") or {}, msg.get("model") or "?", side)
            blocks = msg.get("content") or []
            txt = []
            for b in blocks if isinstance(blocks, list) else []:
                if b.get("type") == "tool_use":
                    name, inp = b.get("name", "?"), b.get("input") or {}
                    s["tool_calls"][name] += 1
                    detail = inp.get("file_path") or inp.get("command") or inp.get("pattern") or ""
                    tool_names[b["id"]] = (name, detail)
                    pending_tool[b["id"]] = name
                    if name == "Read" and inp.get("file_path"):
                        s["rereads"][inp["file_path"]] += 1
                    if name == "TodoWrite":
                        todos = {x.get("content", ""): x.get("status", "") for x in inp.get("todos", [])}
                    if name == "TaskCreate":
                        todos[inp.get("subject", "")] = "pending"
                    if name == "TaskUpdate" and inp.get("status"):
                        for k in list(todos):
                            if inp.get("subject") == k:
                                todos[k] = inp["status"]
                elif b.get("type") == "text":
                    txt.append(b.get("text", ""))
            if not side:
                kind = "assistant_tool" if any(b.get("type") == "tool_use" for b in blocks if isinstance(b, dict)) else "assistant_text"
                if kind == "assistant_text" or txt:
                    last = (kind, "\n".join(txt).strip(), ts, msg.get("stop_reason"))
                else:
                    last = (kind, "", ts, msg.get("stop_reason"))

        elif t == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = b.get("tool_use_id")
                        pending_tool.pop(tid, None)
                        n = size_of(b.get("content"))
                        name, detail = tool_names.get(tid, ("?", ""))
                        s["tool_chars"][name] += n
                        if n >= BIG_RESULT_CHARS:
                            s["big_results"].append((name, detail[:80], n))
            if not o.get("isMeta") and not side:
                txt = text_of(content).strip()
                is_tool_result = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
                if txt and not is_tool_result and not txt.startswith("<"):
                    s["user_msgs"] += 1
                    first_user = first_user or txt
                    last = ("user", txt, ts, None)
                elif is_tool_result:
                    last = ("tool_result", "", ts, None)
                elif INTERRUPT_RE.search(txt):
                    last = ("user", txt, ts, None)

    # tokens (deduped per reply)
    for mid, (u, model, side) in usage_by_msg.items():
        cc = u.get("cache_creation") or {}
        cw1 = cc.get("ephemeral_1h_input_tokens", 0)
        cw5 = cc.get("ephemeral_5m_input_tokens", 0) or (0 if cw1 else u.get("cache_creation_input_tokens", 0))
        i, cr, o_ = u.get("input_tokens", 0), u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0)
        w = i * W_IN + cw5 * W_CW5 + cw1 * W_CW1 + cr * W_CR + o_ * W_OUT
        s["calls"] += 1
        s["in_"] += i; s["cw"] += cw1 + cw5; s["cr"] += cr; s["out"] += o_
        s["weighted"] += w
        if side:
            s["sub_weighted"] += w
        s["models"][model] += w
        s["peak_ctx"] = max(s["peak_ctx"], i + cr + cw1 + cw5)

    s["title"] = (s["title"] or first_user or "(untitled)").replace("\n", " ")[:90]
    s["open_todos"] = [k for k, v in todos.items() if v in ("pending", "in_progress")]

    # ---- status ----
    idle = (now - s["last_ts"]).total_seconds() / 86400 if s["last_ts"] else 0
    s["idle_days"] = idle
    kind, text, _, stop = last or ("none", "", None, None)
    if kind == "assistant_text" and QUESTION_RE.search(text[-400:]) and any(x in text[-400:] for x in ("?", "let me know", "confirm")):
        status, why = "needs_reply", "Claude asked you something and is waiting for your answer"
    elif kind == "assistant_tool" or pending_tool:
        status, why = "interrupted", "Stopped mid-action (tool call never completed or session was cut off)"
    elif kind == "tool_result":
        status, why = "interrupted", "Tool finished but Claude never responded (crashed / cut off)"
    elif kind == "user":
        status, why = "unanswered", "Your last message got no reply" + (" (you interrupted it)" if INTERRUPT_RE.search(text) else "")
    elif s["open_todos"]:
        status, why = "unfinished", f"{len(s['open_todos'])} to-do item(s) still open"
    else:
        status, why = "done", "Last reply was a final answer"
    if status == "done" and idle >= stale_days and s["open_todos"]:
        status = "unfinished"
    if s["open_todos"] and status == "done":
        status, why = "unfinished", f"{len(s['open_todos'])} to-do item(s) still open"
    s["status"], s["status_why"] = status, why
    s["last_text"] = text[-200:].replace("\n", " ") if text else ""
    s["stale"] = status != "done" and idle >= stale_days

    # ---- why expensive + what to do ----
    tips = []
    if s["peak_ctx"] > LONG_CONTEXT:
        tips.append(f"Context reached {s['peak_ctx']:,} tokens; every turn re-reads it. Run /compact at ~50% or /clear when switching task.")
    elif s["calls"] > 60:
        tips.append(f"{s['calls']} model calls in one chat. Split into focused sessions; start fresh per task.")
    seen = set()
    for name, det, n in sorted(s["big_results"], key=lambda x: -x[2]):
        if (name, det) in seen or len(seen) >= 3:
            continue
        seen.add((name, det))
        tips.append(f"{name} returned {n:,} chars ({det or 'no detail'}). Narrow it: grep/head/tail, line ranges, `| head -100`.")
    rr = [(p, c) for p, c in s["rereads"].items() if c >= 3]
    for p, c in sorted(rr, key=lambda x: -x[1])[:2]:
        tips.append(f"{os.path.basename(p)} read {c}x. Read once, or keep key facts in CLAUDE.md.")
    top_model = s["models"].most_common(1)[0][0] if s["models"] else ""
    if "opus" in top_model and s["user_msgs"] <= 3 and s["weighted"] > 200_000:
        tips.append("Opus used for a short task. Use Sonnet/Haiku for routine edits (/model).")
    if s["weighted"] and s["sub_weighted"] / s["weighted"] > 0.4:
        tips.append("Over 40% of cost is in sub-agents. Run them only for broad searches; give them tight scopes.")
    if s["cr"] and s["cw"] and s["cw"] / (s["cw"] + s["cr"]) > 0.5 and s["calls"] > 10:
        tips.append("High cache-write share: prompt cache keeps being rebuilt (idle >5 min or changing context). Batch work; avoid long pauses.")
    s["tips"] = tips
    return s


def collect(root, days, stale_days):
    now = dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=days) if days else None
    sessions = []
    for p in Path(root).expanduser().glob("*/*.jsonl"):
        if cutoff and dt.datetime.fromtimestamp(p.stat().st_mtime, dt.timezone.utc) < cutoff:
            continue
        s = analyze_session(p, now, stale_days)
        if s["calls"] or s["user_msgs"]:
            if s["cwd"]:
                _LABELS[s["project"]] = s["cwd"]
            sessions.append(s)
    return sessions



# ---------------------------------------------------------------- claude.ai data export
def _load_export(path):
    """Return (conversations, projects) from an export folder, zip, or conversations.json."""
    import zipfile
    path = Path(path).expanduser()
    found = {}
    def pick(name, data):
        base = os.path.basename(name).lower()
        if base in ("conversations.json", "projects.json"):
            found[base] = json.loads(data.decode("utf-8", "replace") if isinstance(data, bytes) else data)
    if path.is_file() and path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                pick(n, z.read(n))
    elif path.is_file():
        pick(path.name, path.read_bytes())
        for sib in ("conversations.json", "projects.json"):
            f = path.parent / sib
            if f.exists() and sib not in found:
                pick(sib, f.read_bytes())
    else:
        for f in path.rglob("*.json"):
            pick(f.name, f.read_bytes())
    unwrap = lambda x, k: x.get(k, []) if isinstance(x, dict) else (x or [])
    return unwrap(found.get("conversations.json"), "conversations"), unwrap(found.get("projects.json"), "projects")


def _msg_text(m):
    parts = [m.get("text") or ""]
    for b in m.get("content") or []:
        if isinstance(b, dict) and b.get("type") == "text" and b.get("text") not in parts:
            parts.append(b.get("text") or "")
    return max(parts, key=len)


def _att_chars(m):
    n = 0
    for a in (m.get("attachments") or []):
        n += len(a.get("extracted_content") or "")
    return n


def analyze_export(path, now, stale_days):
    convs, projects = _load_export(path)
    pname, pdocs = {}, {}
    for pr in projects:
        pname[pr.get("uuid")] = pr.get("name") or "(unnamed project)"
        pdocs[pr.get("uuid")] = sum(len(d.get("content") or "") for d in pr.get("docs", []) or []) + len(pr.get("prompt_template") or "")
    sessions = []
    for c in convs:
        msgs = c.get("chat_messages") or []
        if not msgs:
            continue
        puid = c.get("project_uuid") or (c.get("project") or {}).get("uuid")
        proj = pname.get(puid) or (c.get("project") or {}).get("name") or "(no project)"
        kb = pdocs.get(puid, 0) / 4
        ctx = kb; processed = 0.0; out = 0.0; calls = 0; peak = 0; big = []; users = 0
        for m in msgs:
            chars = len(_msg_text(m)) + _att_chars(m)
            tok = chars / 4
            if _att_chars(m) >= BIG_RESULT_CHARS:
                big.append(("Attachment/paste", (m.get("attachments") or [{}])[0].get("file_name", "")[:60], _att_chars(m)))
            if m.get("sender") == "human":
                users += 1; ctx += tok
            else:
                calls += 1; processed += ctx; out += tok; ctx += tok; peak = max(peak, ctx)
        first = parse_ts(c.get("created_at", "")); last = parse_ts(msgs[-1].get("created_at") or c.get("updated_at", ""))
        idle = (now - last).total_seconds() / 86400 if last else 0
        lm = msgs[-1]; ltxt = _msg_text(lm).strip()
        if lm.get("sender") == "human":
            status, why = "unanswered", "Your last message got no reply"
        elif QUESTION_RE.search(ltxt[-400:]):
            status, why = "needs_reply", "Claude asked you something and is waiting for your answer"
        else:
            status, why = "done", "Last reply was a final answer"
        tips = []
        if calls > 30:
            tips.append(f"{calls} replies in one chat; each re-reads the whole history. Start a new chat per task and paste a short summary.")
        if kb > 20_000:
            tips.append(f"Project '{proj}' carries ~{kb/1000:.0f}k tokens of knowledge into every reply. Trim docs or move rarely-used ones out.")
        for n_, d_, ch in big[:3]:
            tips.append(f"Large attachment/paste ({ch:,} chars{', ' + d_ if d_ else ''}) stays in context for every later reply. Paste only the needed part.")
        if peak > 100_000:
            tips.append(f"Conversation reached ~{peak/1000:.0f}k tokens. Summarise and restart.")
        sessions.append(dict(
            id=c.get("uuid", "?"), project=proj, path="", title=((c.get("name") or "").strip() or _msg_text(msgs[0]).strip() or "(untitled)").replace("\n", " ")[:90],
            first_ts=first, last_ts=last, calls=calls, in_=int(processed), cw=0, cr=0, out=int(out),
            weighted=processed + out * W_OUT, peak_ctx=int(peak), sub_weighted=0.0, models=collections.Counter({c.get("model") or "claude.ai": processed + out * W_OUT}),
            tool_chars=collections.Counter(), tool_calls=collections.Counter(), big_results=big, rereads=collections.Counter(),
            open_todos=[], cwd="", user_msgs=users, idle_days=idle, status=status, status_why=why,
            last_text=ltxt[-200:].replace("\n", " "), stale=status != "done" and idle >= stale_days, tips=tips, estimated=True))
    return sessions


def resume_hint(s):
    return f"https://claude.ai/chat/{s['id']}" if s.get("estimated") else f"claude --resume {s['id']}"


_LABELS = {}


def pretty_project(name):
    return _LABELS.get(name) or name.lstrip("-").replace("-", "/")[-60:]


def fmt(n):
    return f"{n/1e6:.2f}M" if n >= 1e6 else f"{n/1e3:.1f}k" if n >= 1e3 else f"{n:.0f}"


def rollups(sessions):
    proj = collections.defaultdict(lambda: dict(weighted=0.0, sessions=0, calls=0, out=0))
    tools = collections.Counter(); tcalls = collections.Counter(); models = collections.Counter()
    for s in sessions:
        p = proj[s["project"]]
        p["weighted"] += s["weighted"]; p["sessions"] += 1; p["calls"] += s["calls"]; p["out"] += s["out"]
        tools.update(s["tool_chars"]); tcalls.update(s["tool_calls"]); models.update(s["models"])
    return proj, tools, tcalls, models


def terminal(sessions, stale_days):
    proj, tools, tcalls, models = rollups(sessions)
    total = sum(s["weighted"] for s in sessions) or 1
    bar = lambda f: "#" * max(1, int(f * 30))
    print(f"\n=== TOKEN AUDIT: {len(sessions)} chats, {fmt(total)} weighted tokens ===\n")
    print("-- By project --")
    for k, v in sorted(proj.items(), key=lambda x: -x[1]["weighted"]):
        print(f"{v['weighted']/total:6.1%} {fmt(v['weighted']):>8}  {bar(v['weighted']/total):<30} {pretty_project(k)} ({v['sessions']} chats)")
    print("\n-- Top chats by cost --")
    for s in sorted(sessions, key=lambda s: -s["weighted"])[:10]:
        print(f"{fmt(s['weighted']):>8}  calls={s['calls']:<4} peak_ctx={fmt(s['peak_ctx']):>7}  {s['title']}  [{s['id'][:8]}]")
        for t in s["tips"]:
            print(f"            -> {t}")
    print("\n-- Tool output volume (what fills the context) --")
    for k, v in tools.most_common(6):
        print(f"{fmt(v/4):>8} tok  {k} ({tcalls[k]} calls)")
    print("\n-- Model spend --")
    for k, v in models.most_common():
        print(f"{v/total:6.1%}  {k}")
    print(f"\n=== UNFINISHED / NEEDS ATTENTION (stale = idle >= {stale_days}d) ===")
    labels = dict(needs_reply="NEEDS YOUR REPLY", interrupted="INTERRUPTED", unanswered="UNANSWERED", unfinished="UNFINISHED TO-DOS")
    open_ = [s for s in sessions if s["status"] != "done"]
    if not open_:
        print("Nothing pending.")
    for st in labels:
        grp = sorted([s for s in open_ if s["status"] == st], key=lambda s: s["idle_days"])
        if not grp: continue
        print(f"\n[{labels[st]}] {len(grp)}")
        for s in grp:
            print(f"  {s['idle_days']:5.1f}d idle{' STALE' if s['stale'] else '      '}  {s['title']}  [{s['id'][:8]}]  {pretty_project(s['project'])}")
            print(f"      {s['status_why']}" + (f"; last: \"{s['last_text'][:100]}\"" if s["last_text"] else ""))
            for t in s["open_todos"][:5]:
                print(f"      [ ] {t}")
            print(f"      resume: {resume_hint(s)}")
    print("\n-- General ways to cut usage --")
    for g in GENERAL_TIPS:
        print(f"  * {g}")


GENERAL_TIPS = [
    "One task per chat. /clear between tasks; /compact before context passes ~50%.",
    "Long chats cost most: each turn re-reads the whole history (cache reads) and idle >5 min rebuilds the cache.",
    "Keep tool output small: grep/head/line ranges instead of reading whole files or dumping logs.",
    "Put stable facts (commands, conventions) in CLAUDE.md instead of re-explaining / re-reading.",
    "Use Sonnet/Haiku for routine edits; reserve Opus for hard reasoning. Lower effort/thinking when not needed.",
    "Use sub-agents only for broad searches; their context is billed separately.",
    "Disable unused MCP servers/plugins: their tool schemas are sent on every call.",
    "Be specific in prompts (file + goal) so Claude doesn't explore the whole repo.",
]


def html_report(sessions, stale_days):
    proj, tools, tcalls, models = rollups(sessions)
    total = sum(s["weighted"] for s in sessions) or 1
    e = html.escape

    def bars(rows):
        mx = max([v for _, v, _ in rows] or [1]) or 1
        return "".join(f'<div class="row"><span class="lbl" title="{e(l)}">{e(l)}</span><span class="bar"><i style="width:{v/mx*100:.1f}%"></i></span><span class="val">{e(x)}</span></div>' for l, v, x in rows)

    pr = [(pretty_project(k), v["weighted"], f"{fmt(v['weighted'])} · {v['weighted']/total:.0%} · {v['sessions']} chats") for k, v in sorted(proj.items(), key=lambda x: -x[1]["weighted"])]
    tr = [(k, v, f"{fmt(v/4)} tok · {tcalls[k]} calls") for k, v in tools.most_common(8)]
    mr = [(k, v, f"{v/total:.0%}") for k, v in models.most_common()]
    top = "".join(
        f'<tr><td>{e(s["title"])}<br><small>{e(pretty_project(s["project"]))} · <code>{e(s["id"][:8])}</code></small></td>'
        f'<td class="n">{fmt(s["weighted"])}</td><td class="n">{s["calls"]}</td><td class="n">{fmt(s["peak_ctx"])}</td>'
        f'<td>{"".join("<div class=tip>" + e(t) + "</div>" for t in s["tips"]) or "<small>looks efficient</small>"}</td></tr>'
        for s in sorted(sessions, key=lambda s: -s["weighted"])[:15])
    groups = [("needs_reply", "Needs your reply"), ("interrupted", "Interrupted / cut off"), ("unanswered", "Your message unanswered"), ("unfinished", "Unfinished to-dos")]
    cards = ""
    for st, label in groups:
        grp = sorted([s for s in sessions if s["status"] == st], key=lambda s: s["idle_days"])
        items = "".join(
            f'<li class="{st}"><b>{e(s["title"])}</b>{" <span class=stale>STALE</span>" if s["stale"] else ""}'
            f'<br><small>{s["idle_days"]:.1f}d idle · {e(pretty_project(s["project"]))}</small><br>{e(s["status_why"])}'
            + (f'<br><small class="last">“{e(s["last_text"][:140])}”</small>' if s["last_text"] else "")
            + "".join(f"<br><small>☐ {e(t)}</small>" for t in s["open_todos"][:5])
            + f'<br><code>{e(resume_hint(s))}</code></li>' for s in grp)
        cards += f'<section><h3>{label} <span class="count">{len(grp)}</span></h3><ul>{items or "<li class=none>None</li>"}</ul></section>'
    tips = "".join(f"<li>{e(g)}</li>" for g in GENERAL_TIPS)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Token Audit</title><style>
:root{{--bg:#fff;--fg:#1d2127;--mut:#667;--card:#f5f6f8;--line:#e1e4e8;--acc:#3b6fd4;--warn:#c2410c;--bad:#b91c1c}}
@media(prefers-color-scheme:dark){{:root{{--bg:#14171c;--fg:#e6e8eb;--mut:#9aa3ae;--card:#1c2027;--line:#2c323b;--acc:#6b98ee;--warn:#fb923c;--bad:#f87171}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:24px 16px}}h1{{margin:0 0 4px}}h2{{margin:32px 0 12px}}
.sub{{color:var(--mut)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}}
section,.panel{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}}
.row{{display:grid;grid-template-columns:minmax(80px,1.2fr) 2fr auto;gap:8px;align-items:center;margin:6px 0;font-size:13px}}
.lbl{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.bar{{background:var(--line);border-radius:4px;height:10px}}.bar i{{display:block;height:100%;background:var(--acc);border-radius:4px}}
.val{{color:var(--mut);white-space:nowrap}}table{{width:100%;border-collapse:collapse;font-size:13px}}th,td{{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}}
.n{{text-align:right;white-space:nowrap}}.tip{{color:var(--warn);margin-bottom:4px}}small,.last{{color:var(--mut)}}code{{font-size:12px;word-break:break-all}}
ul{{list-style:none;padding:0;margin:0}}li{{padding:10px 0;border-bottom:1px solid var(--line);font-size:14px}}li:last-child{{border:0}}
.count{{background:var(--acc);color:#fff;border-radius:10px;padding:0 8px;font-size:12px}}.stale{{color:var(--bad);font-size:11px;border:1px solid var(--bad);border-radius:4px;padding:0 4px}}
.needs_reply{{border-left:3px solid var(--acc);padding-left:8px}}.interrupted,.unanswered{{border-left:3px solid var(--bad);padding-left:8px}}.unfinished{{border-left:3px solid var(--warn);padding-left:8px}}
@media(max-width:700px){{table{{display:block;overflow-x:auto}}}}
</style></head><body><main>
<h1>Token Audit</h1><p class="sub">{len(sessions)} chats · {fmt(total)} weighted tokens (input-equivalent; cache read 0.1×, output 5×) · stale = idle ≥ {stale_days}d · generated {dt.datetime.now():%Y-%m-%d %H:%M}</p>
<h2>Needs attention</h2><div class="grid">{cards}</div>
<h2>Where tokens go</h2><div class="grid">
<div class="panel"><h3>By project</h3>{bars(pr)}</div>
<div class="panel"><h3>Tool output (context filler)</h3>{bars(tr)}</div>
<div class="panel"><h3>By model</h3>{bars(mr)}</div></div>
<h2>Most expensive chats &amp; how to cut them</h2>
<div class="panel"><table><tr><th>Chat</th><th>Cost</th><th>Calls</th><th>Peak ctx</th><th>Fix</th></tr>{top}</table></div>
<h2>General countermeasures</h2><div class="panel"><ul>{tips}</ul></div>
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="~/.claude/projects")
    ap.add_argument("--days", type=int, default=30, help="only chats touched in last N days (0 = all)")
    ap.add_argument("--stale-days", type=float, default=2, help="unfinished chat idle this long is flagged STALE")
    ap.add_argument("--export", help="claude.ai data export: folder, .zip, or conversations.json (estimates tokens from text)")
    ap.add_argument("--html"); ap.add_argument("--json")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):   # Windows consoles default to cp1252
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.export:
        now = dt.datetime.now(dt.timezone.utc)
        sessions = analyze_export(a.export, now, a.stale_days)
        if a.days:
            sessions = [x for x in sessions if x["last_ts"] and (now - x["last_ts"]).days <= a.days]
        print("NOTE: claude.ai exports have no token counts. Figures are ESTIMATES (chars/4; every reply re-reads the chat + project knowledge).")
    else:
        sessions = collect(a.root, a.days, a.stale_days)
    if not sessions:
        sys.exit("No conversations found in the export" if a.export else f"No transcripts found under {a.root}")
    terminal(sessions, a.stale_days)
    if a.html:
        Path(a.html).write_text(html_report(sessions, a.stale_days), encoding="utf-8"); print(f"\nHTML dashboard: {a.html}")
    if a.json:
        def enc(o):
            return o.isoformat() if isinstance(o, dt.datetime) else dict(o) if isinstance(o, collections.Counter) else str(o)
        Path(a.json).write_text(json.dumps(sessions, default=enc, indent=1), encoding="utf-8"); print(f"JSON: {a.json}")


if __name__ == "__main__":
    main()
