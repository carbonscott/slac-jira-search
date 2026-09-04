# jira-search

Ask Claude about anything in SLAC Jira and get answers with links to the real
issues.

It queries Jira **live** with your own account's token, so it sees every project
you can see and it is never out of date. Works in Claude Code and in the shared
LCLS opencode install.

It is also **read-only by construction** — see [Why this is safe](#why-this-is-safe),
because that is not the same thing as "the token is read-only", and the
difference matters.

---

## Quick start

### 1. Get the skill

**Using the shared opencode install on S3DF?** It is already there. Skip to
step 2.

**Setting up your own Claude Code?** Clone and deploy:

```bash
git clone https://github.com/carbonscott/slac-jira-search.git
cd slac-jira-search
./install.sh
```

That links the skill into `~/.claude/skills/`. It never touches credentials.

### 2. Get a token

You need your own Jira personal access token. Nobody can give you one: results
are filtered by *your* project permissions, so a shared token would show you
someone else's view of the tracker.

Mint it in a browser, on your profile's **Personal Access Tokens** tab:

**https://jira.slac.stanford.edu/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens**

(If you are not signed in, that URL redirects you to the SLAC login page rather
than 404-ing. Click path if you would rather navigate: avatar, top right →
**Profile** → **Personal Access Tokens**.)

Set an expiry — 90 days is reasonable — and copy the value. It is shown **once**.

→ [Step-by-step walkthrough](docs/token-setup/getting-a-token.md), including
what to do when it expires.

### 3. Register the token

Run `jira-login` and paste it at the prompt. It is not on your `PATH`; the path
depends on where the skill was deployed:

```bash
# shared opencode install on S3DF
/sdf/group/lcls/ds/dm/apps/dev/opencode/skills/jira-search/scripts/jira-login

# your own Claude Code
~/.claude/skills/jira-search/scripts/jira-login
```

Input is hidden. The token is saved to `~/.config/jira-search/token` with mode
600, and one live call confirms it works. Seeing your own name come back means
you are done.

**You register the token once**, even if you use both Claude Code and opencode —
it lives in your home directory, not inside either skill.

### 4. Ask it something

> `@jira-search` find issues about epixuhr

> `@jira-search` what's still unresolved in FERMIS3DF?

> `@jira-search` show me FERMIS3DF-43 and its comments

Claude builds the JQL, runs the search, reads the promising issues, and cites the
`browse/` URLs so you can open the source.

---

## Why this is safe

**A Jira Server personal access token carries your account's full permissions.
There is no read-only PAT scope.** Whatever your account can create, edit,
comment on or transition, the token can too. Measured on this instance for the
account behind this repo, against a project it can browse:

```
CREATE_ISSUES     havePermission: true
EDIT_ISSUES       havePermission: true
ADD_COMMENTS      havePermission: true
TRANSITION_ISSUES havePermission: true
```

So the safety guarantee is not in the credential. It is in the code:

- The whole skill is one stdlib-only Python file with **exactly two request
  paths** — a `GET` builder with no request body, and the single
  `POST /rest/api/2/search` that Jira requires in order to run a JQL query.
- There is no `PUT`, no `DELETE`, no `PATCH`, and nothing that touches
  `/transitions`, `/comment`, `/issueLink` or `/worklog`.

That is a small enough surface to audit in one sitting, which is the point. If
you are handing an agent a credential that *could* file tickets in your name,
"the tool physically cannot" beats "the tool is asked not to". Read the two
methods and satisfy yourself; details in
[docs/findings.md](docs/findings.md#3-the-credential-carries-full-write-permission-the-client-is-what-is-read-only).

---

## What it can do

Six subcommands: `whoami`, `projects`, `text`, `search`, `issue`, `login`.

`text` builds the JQL for you from plain words and prints the query it built, so
you can copy it into `search` and refine it. `search` takes raw JQL. `issue`
prints one issue with its description and comments.

The exact flags, and worked examples of each, are in the skill's own
[`SKILL.md`](claude/skills/jira-search/SKILL.md). The query language itself is
covered by
[`reference/jql-cheatsheet.md`](claude/skills/jira-search/reference/jql-cheatsheet.md)
— every entry in it was verified against this instance, including the error
messages.

---

## When it stops working

**"no Jira token"** — you have not done step 3, or you are on a machine where you
have not done it yet.

**HTTP 401** — the token expired or was revoked. Mint a new one (step 2) and
install it over the old one:

```bash
<same path as step 3>/jira-login --force
```

**Results look suspiciously thin, but nothing errored** — Jira answers a missing
or malformed token with the *anonymous* view and HTTP 200 rather than a 401.
Check who you are:

```bash
<skill-dir>/scripts/jqlsearch.py whoami
```

---

## For maintainers: central deployment

One clone, many users, two jobs that belong to different people.

```bash
# once: clone somewhere group-readable
git clone https://github.com/carbonscott/slac-jira-search.git \
  /sdf/group/lcls/ds/dm/apps/dev/tools/jira-search

# deploy the skill — never touches a credential
/sdf/group/.../jira-search/install.sh
/sdf/group/.../jira-search/install.sh --dir /some/shared/skills

# each user, once: their own token (step 3 above)
```

`install.sh` deploys and nothing else; `jira-login` registers a token and nothing
else. Neither can do the other's job, and you cannot register tokens on your
users' behalf in any case.

| Flag | Effect |
|---|---|
| `--copy` / `--symlink` | force one mode; the default is chosen from the destination |
| `--dir DIR` | a skills directory other than `~/.claude/skills` |
| `--force` | replace an existing entry that isn't ours |
| `--uninstall` | remove the deployed skill (the user's token stays) |

**Symlink or copy is decided by where the destination is.** Under your home it
symlinks, so `git pull` updates you immediately. Anywhere else it copies, and
says why: a symlink in a shared tree points back into one person's clone, which
everyone else usually cannot read.

**For the LCLS shared opencode tree, prefer `deploy.sh` and the manifest** in
`deploy-opencode` over this script — it also fixes group ownership and creates
the `agents/` symlink. Manifest entry:

```json
{
  "name": "jira-search",
  "repo": "carbonscott/slac-jira-search",
  "ref": "main",
  "cron": null,
  "central_data": null
}
```

---

## What is in this repo

```
claude/skills/jira-search/     for Claude Code users
opencode/skills/jira-search/   rsynced into the shared opencode tree
install.sh                     deploys the skill
docs/token-setup/              the token walkthrough
docs/findings.md               how it works, what was measured, gotchas
```

`claude/` and `opencode/` hold **identical, duplicated** content — the layout the
LCLS deploy manifest expects. Any edit must be applied to both; `diff -r` between
them should be empty before you commit.

---

## Under the hood

[docs/findings.md](docs/findings.md) covers what was measured live against
`jira.slac.stanford.edu` on 2026-09-04: the instance version (Jira Server
10.3.19), the `X-RateLimit-*` token-bucket headers and what actually trips them,
the silent `maxResults` cap at 1000, the permission probe above, and the handful
of places where porting a Confluence/CQL client to Jira produces something that
looks like it works and does not.

The short version: it queries Jira's REST search API directly, with your token,
and hands the agent the issue fields and wiki-markup descriptions as they come.
Nothing is cached, nothing is synced, and there is no database to keep up to
date.
