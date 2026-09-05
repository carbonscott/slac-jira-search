---
name: jira-search
description: Search SLAC Jira live using JQL (Jira Query Language) via the REST search API. Covers every project your account can see (195 projects, ~55,000 issues), with each issue's description and full comment thread. Use for questions about LCLS/TID/ECS/DAQ tickets, detector and hardware builds, experiment tooling, "what is the status of X", "who is working on X", "what broke and how was it fixed", "find the ticket about X", or reading one issue end to end.
---

# Jira live search (JQL)

Query `jira.slac.stanford.edu` directly — Jira Server 10.3.19. This searches
**every project the user's own account can see, always current**: 195 projects
and roughly 55,000 issues for a typical LCLS account, including each issue's
description and its whole comment thread.

Results are filtered by the token owner's permissions, so coverage differs
between users. `jqlsearch.py whoami` says which identity is in play.

## Running the script

No venv, no `pip install`, no dependencies — the script carries PEP 723 inline
metadata and uses only the standard library, so all three of these work. (A
token is the one thing you do need; see **Auth** below.)

```bash
SKILL_DIR=~/.claude/skills/jira-search                # wherever this SKILL.md lives
source "$SKILL_DIR/env.sh"                            # puts the shared uv on PATH
JQL="$SKILL_DIR/scripts/jqlsearch.py"                 # see below
uv run --script "$JQL" text "detector calibration"    # preferred
"$JQL"          text "detector calibration"           # shebang runs it through uv
python3 "$JQL"  text "detector calibration"           # only if python3 is >= 3.9
```

Source `env.sh` in the *same* bash command as the script — each command runs in a
fresh shell, so a `source` from an earlier one is already gone. It puts the
facility's shared `uv` on `PATH` (S3DF `/sdf/group/lcls/ds/dm/apps/dev/bin`, OLCF
`/ccs/home/cwang31/.local/bin`) and sets a per-user uv cache, so these commands
work without a personal `~/.local/bin/uv`.

**Use `uv` unless you know the local `python3` is 3.9 or newer.** The script
declares `requires-python = ">=3.9"` and uv provisions that automatically; the
system python on SLAC login nodes is 3.6 and cannot parse the file at all
(`SyntaxError: future feature annotations is not defined`).

`$SKILL_DIR` is the directory holding this `SKILL.md` — wherever this skill was
deployed, usually `~/.claude/skills/jira-search`:

```bash
JQL=~/.claude/skills/jira-search/scripts/jqlsearch.py
```

Examples below use `uv run --script`. Copy `jqlsearch.py` anywhere you like — it
is standard-library-only and has no repo-relative dependencies.

## Auth

Each user needs their own personal access token. It is resolved in this order:

1. `$JIRA_TOKEN`
2. `$JIRA_TOKEN_FILE`
3. `~/.config/jira-search/token` — the default

There is deliberately **no shared fallback path**, so a central install never
authenticates everyone as one account. If the token is missing the script prints
setup instructions; if it is group- or world-readable the script refuses to use
it. `JIRA_URL` points at a different instance.

### The token is not read-only — this code is

A Jira Server PAT carries the **full permissions of the account that minted it**,
write included. There is no read-only PAT to ask for. The read-only-ness of this
skill therefore lives in `scripts/jqlsearch.py`: every call it makes is a GET,
with one exception — Jira takes JQL in a request body, so searching is
`POST /rest/api/2/search`, which creates nothing. Nothing here creates, edits,
transitions, assigns, comments on, links, or deletes anything. A first look at
that is one line:

```bash
grep -nE 'urlopen|Request\(|data=|method=|POST|PUT|DELETE|PATCH' "$SKILL_DIR"/scripts/*
```

**Four hits**, and every one of them has to be accountable:

| What the line is | Why it is allowed |
|---|---|
| `req = urllib.request.Request(url, headers=self.headers())` | the GET builder — **no `data=`, no `method=`**, which is exactly what makes urllib send a GET |
| `# POST /rest/api/2/search normally — …` | the comment above the search call |
| `req = urllib.request.Request(…, data=body, method="POST", …)` | the one search call: Jira takes JQL in a request body |
| `with urllib.request.urlopen(req, …) as r:` | the single place either request is actually sent |

Do not settle for the narrower `grep -nE 'method|POST|PUT|DELETE|PATCH'`. It
finds only the last two, and it would **miss a future
`urllib.request.Request(url, data=payload)`** — urllib makes any request that
carries a body a POST whether or not anyone wrote `method=`. That is precisely
the change that would quietly give this skill the ability to write, so `data=`,
`Request(` and `urlopen` are in the pattern on purpose.

If a fifth hit appears, or the GET builder above ever grows a `data=`, this skill
can change Jira.

### The grep is the summary; the criterion is enumerating egress sites

The pattern above is wider than the obvious one and it is still **not
sufficient**, because a grep can only look for verbs someone thought to write.
This line writes to Jira and matches none of its eight alternatives — no
`urlopen`, no `Request(`, no `data=`, no `method=`, no verb at all:

```python
urllib.request.build_opener().open(url, b'{}')
```

So audit the *call graph*, not the text. Every `Call` node the parser found,
intersected with the ways this standard library can reach the network or the
shell:

```bash
uv run --python '>=3.9' python - "$SKILL_DIR"/scripts/jqlsearch.py <<'PY'
import ast, sys
EGRESS = {"urllib.request.Request", "urllib.request.urlopen",
          "urllib.request.urlretrieve", "urllib.request.build_opener",
          "urllib.request.OpenerDirector", "http.client.HTTPSConnection",
          "http.client.HTTPConnection", "subprocess.run", "subprocess.Popen"}
calls = [ast.unparse(n.func) for n in ast.walk(ast.parse(open(sys.argv[1]).read()))
         if isinstance(n, ast.Call)]
print("egress:", sorted(set(calls) & EGRESS))
print("opens :", sorted({c for c in calls if c.endswith(".open")}))
PY
```

A clean audit prints exactly this, and any other line is a finding:

```
egress: ['urllib.request.Request', 'urllib.request.urlopen']
opens : ['os.open']
```

Two entry points, both `urllib`, both accounted for in the table above. The
`opens` line is there because `.open` is how an opener is actually fired; the
only two here are filesystem, not network — `os.open(path, O_CREAT|O_EXCL|O_WRONLY,
0o600)` writing the token file, and `os.open(os.devnull)` for a stdout redirect.
`subprocess` and `http.client` never appear, and neither does any import of
them:

```bash
grep -nE '^(import|from) ' "$SKILL_DIR"/scripts/jqlsearch.py
```

**`uv` is not optional here.** `ast.unparse` needs Python 3.9 or newer and the
system `python3` on a SLAC login node is 3.6, where `hasattr(ast, "unparse")` is
`False` — so run under `uv` or this check silently cannot be performed at all.

To convince yourself the enumeration can fail, append that `build_opener` line
to a **copy** and re-run: `egress` gains `urllib.request.build_opener` and
`opens` gains `urllib.request.build_opener().open`, while the grep on that same
line returns zero hits. That gap between the two checks is the whole reason this
section exists.

### Never announce a missing token you have not observed

**Do not tell the user to set up a token unless a command you actually ran just
failed with a token error.** Most users already have one, and being told to redo
setup they completed is worse than useless — it makes them doubt a working
install. The instructions below are a *reaction* to a failure, never a
precondition to check first.

If you are unsure whether auth works, do not guess and do not read it off this
file. Run one cheap command and look at the output:

```bash
uv run --script "$JQL" whoami
```

```
Cong Wang <cwang31@slac.stanford.edu>
  instance: https://jira.slac.stanford.edu
  account:  cwang31@slac.stanford.edu (key JIRAUSER21902)
  token:    ~/.config/jira-search/token
  note:     search results are filtered by your own permissions —
            another account sees a different set of projects.
  note:     a Jira PAT carries this account's FULL permissions,
            including write. This tool only ever reads.
```

The two `note:` blocks are part of the output, not commentary added here.

A name and email means auth is fine — proceed with the real query. Only if a
command exits with an error mentioning the token do the following apply.

### When a command really did fail on the token

Stop and tell the user to set one up themselves. The command ships in this skill
directory, so it works even for someone who only ever received the deployed
skill:

```bash
$SKILL_DIR/scripts/jira-login           # prompts, hidden, writes mode 600
$SKILL_DIR/scripts/jira-login --force   # replace an expired or wrong token
```

They mint the token first, in a browser, at
`https://jira.slac.stanford.edu/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens`
— there is no API for the first one.

Quote the error you actually got. Three different failures, three different fixes:

- `error: no Jira token.` — nothing is installed yet; plain `jira-login`.
- `error: HTTP 401 — token rejected.` — a token exists but is invalid, expired,
  or revoked; `jira-login --force`.
- `error: Jira answered as ANONYMOUS (X-AUSERNAME: anonymous)` — the Bearer was
  not accepted at all and Jira served the logged-out view. Same fix as 401, and
  note that **any result you already showed from that call was the public
  subset, not the user's**.

**Do not** read, print, guess, or type a token yourself, and do not run
`jira-login` for the user — it prompts them, deliberately, so the secret never
passes through you or through a command line.

## Commands

| Command | Use it for |
|---|---|
| `text "<words>"` | Free-text search. Builds the JQL for you. **Start here.** |
| `search "<jql>"` | A raw JQL query when you need full control. |
| `issue <key\|url>` | One issue: header, description, and every comment. |
| `projects` | List the projects this account can see (find the right key). |
| `whoami` | Which identity the token belongs to. One cheap call; good first check. |
| `login` | Install a token. The user runs this — see **Auth**, never you. |

`text` and `search` share `--limit N` (default 25, `0` = no cap), `--start N`,
`--all`, `--json`, `--fields CSV`, and `--excerpt`.

### `-v` — see what the network is doing

`-v` / `--verbose` works on **every** subcommand and writes to **stderr** only:
the rate-limit budget left after each response, and a line for each 429 backoff
while it waits. Nothing on stdout changes, so adding it never disturbs a pipe or
`--json`.

```bash
uv run --script "$JQL" -v text "epixuhr" --limit 1     # before the subcommand
uv run --script "$JQL" text "epixuhr" -v --limit 1     # after it — also works
```

Both positions are accepted. `-v` is how you see the whole rate-limit story
described under **Rules** below — every 429 sleep, however short. Without it,
only sleeps of 30 s or more announce themselves, so a *long* wait is never
mistaken for a hang, but a run of short ones is still invisible.

### text — the common case

```bash
uv run --script "$JQL" text "epixuhr"                              # whole instance
uv run --script "$JQL" text "detector calibration" --limit 3
uv run --script "$JQL" text "epixuhr" --project TIDAT --recent --limit 3
uv run --script "$JQL" text "timing" --open --since 30d --limit 3  # updated in 30 days
uv run --script "$JQL" text "QCFG_parse" --field summary           # summary only
uv run --script "$JQL" text "epixuhr" --limit 2 --excerpt          # + description snippet
uv run --script "$JQL" text "epixuhr" --assignee 'currentUser()' --limit 1
```

The first line of the output is the JQL that was built, so you can widen or
tweak it and re-run it through `search`:

```
jql: text ~ "epixuhr" AND project = "TIDAT" ORDER BY updated DESC
111 match(es); showing 3

[1] TIDAT-5193  (Dawood) ePixUHR 3x2 PL516 PDU panel adapter assembly x12
    Assembly  |  CABLE Fabrication Only  |  project=TIDAT  |  priority=Standard  |  updated 2026-09-03  |  assignee=unassigned
    https://jira.slac.stanford.edu/browse/TIDAT-5193
```

Flags: `--project KEY` (repeatable), `--type Bug`, `--status "To Do"`,
`--assignee NAME|currentUser()`, `--since WHEN`, `--open`, `--recent`,
`--field text|summary|description|comment` (default `text` is Jira's
multi-field alias: summary, description, environment, comments and text custom
fields — which is why `text ~ "epixuhr"` finds 165 issues where `summary ~` finds
94, `description ~` 47 and `comment ~` 25).

`--since` takes `"2026-01-01"`, `startOfMonth()`, or `30d`. Write `30d`, **not**
`-30d`: a bare leading dash is eaten by the argument parser as an unknown flag.
`--since=-30d` (with the equals sign) also works.

### search — raw JQL

```bash
uv run --script "$JQL" search 'project = FERMIS3DF ORDER BY updated DESC'
uv run --script "$JQL" search 'reporter = currentUser() ORDER BY created DESC'
uv run --script "$JQL" search 'text ~ "epix*" AND created > -30d' --limit 3
uv run --script "$JQL" search 'project = FERMIS3DF AND status = "To Do"' --limit 2 --fields summary,updated
uv run --script "$JQL" search 'key = FERMIS3DF-43' --json --fields summary,status
uv run --script "$JQL" search 'issuetype = Bug ORDER BY created ASC' --limit 1200 --all --fields summary
```

Every hit prints the issue key and its `browse` URL — cite that URL, not the
REST `self` link the API returns.

One response is capped at **1000 rows** by the server however large `--limit`
is; `--all` pages until `--limit` is filled, 1000 rows at a time with a half
second between pages. `--limit 0 --all` really does fetch everything — that is
12,914 rows and 13 requests for `issuetype = Bug`, so mean it when you ask.

`--fields` replaces the default
`summary,status,issuetype,assignee,updated,project,priority`. Jira sends **no
excerpt with a hit** — unlike Confluence — so a snippet needs `description`
fetched explicitly, which is what `--excerpt` does.

### issue — read the actual ticket

A search hit is one line of metadata. The answer to a real question is almost
always in the description and the comments:

```bash
uv run --script "$JQL" issue FERMIS3DF-43
uv run --script "$JQL" issue https://jira.slac.stanford.edu/browse/FERMIS3DF-43 --no-comments
uv run --script "$JQL" issue FERMIS3DF-43 --comments 1     # newest N comments
uv run --script "$JQL" issue FERMIS3DF-43 --out FERMIS3DF-43.md
uv run --script "$JQL" issue FERMIS3DF-43 --rendered       # server-rendered HTML
```

It accepts a bare key, a `/browse/KEY` URL, or a board URL carrying
`?selectedIssue=KEY`. Output is a Markdown header, then `## Description`, then
`## Comments (N)`:

```
# FERMIS3DF-43  QCFG_parse fails in 64-bit container

- type: Bug
- status: To Do
- resolution: Unresolved
- project: FERMIS3DF — Fermi-S3DF
- priority: Major
- assignee: unassigned
- reporter: jgt@slac.stanford.edu
- created: 2026-09-04 15:19:31
- updated: 2026-09-04 15:20:18
- url: https://jira.slac.stanford.edu/browse/FERMIS3DF-43
```

The description and comments come back as **Jira wiki markup** — `h2.`,
`{code:java}...{code}`, `* bullets`, `[label|url]` — not HTML and not Markdown.
Read it as it is; the script prints it verbatim, because flattening it destroys
exactly what you need: link targets to cite, heading levels, and code-block
boundaries. `--rendered` asks the server for its own HTML rendering instead
(3,224 chars of markup become 3,893 chars of HTML on FERMIS3DF-43) — worth it
for a table or a macro, not otherwise.

### projects — find a key

```bash
uv run --script "$JQL" projects
uv run --script "$JQL" projects --filter lcls
```

```
XROBCS           software     LCLS X-Ray Optics 5kW BCS

36 of 195 projects visible to this token match 'lcls'
```

## Workflow

1. **Search broadly first** — `text "<words>"` with no `--project`. Jira here is
   195 projects wide and the interesting ticket is often in a project you would
   not have guessed (TID hardware, ECS, LCLSESH, ...).
2. **Read the match count.** `165 match(es)` is workable; thousands means the
   query is too vague — add `--project`, `--type`, `--open`, or better words.
3. **Open the promising issues** with `issue KEY`. The hit line carries no
   description at all; the resolution of a bug is usually in the last comment.
4. **Cite the browse URL** printed with each hit so the user can open the source.

## Rules

- **`~` for text, `=` for exact.** `summary ~ "psana"` matches substrings;
  `summary = "psana"` demands the whole summary. Using `=` on `text` is an
  error — the server answers *"The operator '=' is not supported by the 'text'
  field."*
- **Project keys are uppercase and are not the display names** — the key is
  `FERMIS3DF`, the name is `Fermi-S3DF`. Use `projects` to find them.
- **Quote multi-word values**: `status = "To Do"`, `text ~ "detector
  calibration"`. `text` builds this for you.
- **Open work is `resolution = Unresolved`**, not a status name — status names
  differ per project (`To Do`, `Backlog`, `In Preparation`, `Stalled`, ...).
  `text --open` writes that clause for you.
- **Rate limits are real**: 70 requests of burst, refilling 5/s. The script backs
  off on HTTP 429 — don't wrap it in a tight loop, and prefer one `--limit 100`
  call over 100 calls. Short backoffs are invisible unless you pass `-v`, so a
  command that seems to hang is often sleeping off a 429; any sleep of 30 s or
  more says so on stderr with or without `-v`. Total backoff for one command is
  capped at 10 minutes (`RETRY_BUDGET`), after which it gives up and tells you
  how long it waited rather than stalling an `--all` sweep for hours. This
  instance sends `Retry-After: 0`, which is **not** obeyed literally — see
  `docs/findings.md`.
- **A 200 response is not proof of auth.** A bad Bearer gets Jira's anonymous
  view, not an error; the script checks `X-AUSERNAME` on every response and
  fails loudly instead of quietly showing you a stranger's smaller Jira.
- **Never print the token** or paste it into a command line.
- **This skill never writes to Jira.** If a user asks you to file, edit,
  transition, assign, or comment on an issue, say that this tool is read-only
  and hand them the browse URL — do not reach for `curl`.

## Reference

`reference/jql-cheatsheet.md` (next to this file) — fields, operators,
functions, and the quirks verified against *this* instance. Read it before
writing a non-obvious JQL query.
