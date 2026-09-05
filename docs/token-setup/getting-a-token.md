# Getting a Jira personal access token

Every user of `jira-search` needs their own token. This is not a credential
anyone can hand you: Jira filters search results by the token owner's project
permissions, so a shared token would show you someone else's view of the tracker
— hiding issues you are entitled to read, and showing you issues from projects
you are not on.

It takes about a minute. It has to be done in a browser: the PAT REST API
(`/rest/pat/latest/tokens`) can list and create tokens, but only for a caller who
already holds one, so it cannot mint your first.

## Before you start

- You need a working SLAC Jira login at `jira.slac.stanford.edu`.
- From outside SLAC, you will likely need the VPN before the site loads.

## 1. Open the token page

Go straight to:

```
https://jira.slac.stanford.edu/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens
```

That is one URL on one line — the `selectedTab=` query parameter is what selects
the **Personal Access Tokens** tab of your profile, so do not let your terminal
or mail client break it.

Or navigate by hand: your **avatar (top right) → Profile → Personal Access
Tokens**. The tab sits alongside the other profile tabs; it is not under the
admin cog.

**If you are not signed in, the URL does not 404 — it redirects you to the SLAC
login page.** Verified 2026-09-04 with no cookies:

```
$ curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' \
    "https://jira.slac.stanford.edu/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens"
302 https://jira.slac.stanford.edu/login.jsp?permissionViolation=true&os_destination=%2Fsecure%2FViewProfile.jspa%3FselectedTab%3Dcom.atlassian.pats.pats-plugin%3Ajira-user-personal-access-tokens&page_caps=&user_role=
```

That tells you the site is up and the link is live rather than dead, and Jira
carries your destination in `os_destination`, so signing in lands you back on
the token tab.

**It does not tell you the URL is correct.** Signed out, *everything* under
`/secure/` answers `302 → login.jsp`, including URLs that do not exist. Same day,
same cookieless `curl`:

```
/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens
  -> 302 .../login.jsp?permissionViolation=true&os_destination=...   (the real one)
/secure/ViewProfile.jspa?selectedTab=com.bogus.plugin:nonexistent-tab
  -> 302 .../login.jsp?permissionViolation=true&os_destination=...   (invented tab key)
/secure/NoSuchPage.jspa
  -> 302 .../login.jsp?permissionViolation=true&os_destination=...   (invented page)
/secure/TotalGarbage12345.jspa?selectedTab=lol
  -> 302 .../login.jsp?permissionViolation=true&os_destination=...   (pure garbage)
```

Jira checks *permission* before it checks whether the page exists, so a redirect
is its answer to anything an anonymous visitor asks for. A `302` therefore means
"you are not signed in" and nothing more. **The only test of the tab key itself
is to sign in and look at the page.**

### The check that can actually fail

The `selectedTab` key above names Atlassian's PAT plugin. Whether that plugin is
really installed on this instance is testable, because its REST endpoint answers
only when it exists — and, unlike `/secure/`, it distinguishes its failures:

```
$ curl -s -o /dev/null -w 'HTTP %{http_code}\n' \
    https://jira.slac.stanford.edu/rest/pat/latest/tokens
HTTP 401                                        # no Bearer

$ curl -s -w '\nHTTP %{http_code}\n' -K <file-holding-your-Authorization-header> \
    https://jira.slac.stanford.edu/rest/pat/latest/tokens
[{"id":58,"name":"test",
  "createdAt":"2026-09-04T22:35:14.728+00:00",
  "lastAccessedAt":"2026-09-04T23:51:56.862+00:00",
  "expiringAt":"2026-12-03T22:35:14.728+00:00"}]
HTTP 200                                        # body first, then the code

$ curl -s -o /dev/null -w 'HTTP %{http_code}\n' -K <same file> \
    https://jira.slac.stanford.edu/rest/nosuchplugin/latest/tokens
HTTP 302                                        # -> login.jsp, even authenticated
```

`401` without a token, `200` with one, and `302` for a plugin that does not
exist. The `200` is the proof: the PAT plugin is installed, and the tab key in
the URL above is that plugin's.

All three commands print their code — that is the only reason any of this is
checkable, and the middle one is the odd shape for exactly that reason: it is the
one whose *body* you also want, so it keeps the body (no `-o /dev/null`) and lets
`-w` put the status line after it. Copy any of the three and you get its code.

Note the metadata is name, id, created, expiry and last-used only — **the
endpoint never returns token values**, which is why it is safe to run and paste.

(Put the header in a `curl` config file — `header = "Authorization: Bearer …"`,
mode 600 — rather than on the command line, where other users on a shared host
can read it out of the process list.)

Verified 2026-09-04. This is also the answer when someone says "your link is
broken": ask whether they were signed in, because the redirect alone cannot
distinguish a good link from a typo'd one.

The page lists any tokens you already have — name, created date, expiry date,
and when each was last used — and each row has a **Revoke** link. Click **Create
token**.

## 2. Fill in the form

**Token Name.** Name it after the thing that will use it, not after yourself —
you will eventually have several, and the name is how you tell them apart when
revoking. `claude-code`, `opencode`, and `laptop` are good; `token1` is not.

**Permissions.** There is nothing to choose, and this is the part worth
understanding: **a Jira personal access token carries your account's full
permissions.** There is no read-only PAT scope on Jira Server. Whatever your
account can create, edit, comment on, transition or delete, the token can too.

Measured on this instance for the account behind this repo, against project
`FERMIS3DF`:

```
CREATE_ISSUES     havePermission: true
EDIT_ISSUES       havePermission: true
ADD_COMMENTS      havePermission: true
TRANSITION_ISSUES havePermission: true
```

So treat the value exactly as you would your password. `jira-search` is
read-only because **its code only ever issues `GET` requests plus the single
`POST /rest/api/2/search` that Jira requires for a query** — not because the
credential is restricted. That distinction matters if you ever paste the same
token into another tool.

**Expiry date.** Set one. 90 days is a reasonable default; the form shows you
the resulting date. Set it deliberately — **you cannot change the expiry after
the token is created**, and a token with no expiry is a permanent credential
that will outlive your interest in it.

Click **Create**.

## 3. Copy the token — you get exactly one chance

Jira shows the new token value once, in a dialog, and copies it to your
clipboard. The value is unrecoverable after you dismiss that dialog; your only
option then is to revoke the token and create another.

Do not screenshot this screen. A picture of that dialog is a picture of a working
password, and screenshots of it are a common way tokens leak. With the value on
your clipboard, go straight to the next step — do not park it in a text file, a
chat message, a shell command, or your shell profile on the way.

## 4. Install it

`jira-login` is not on your `PATH` — typing it bare gives `command not found`.
It ships inside the skill, so you run it by path, and the path depends on where
the skill was deployed for you:

```bash
# a personal Claude Code install
~/.claude/skills/jira-search/scripts/jira-login

# the shared LCLS opencode install on S3DF
/sdf/group/lcls/ds/dm/apps/dev/opencode/skills/jira-search/scripts/jira-login
```

Both are the same program. If you use both tools, you still install the token
only once — it goes to a single per-user file, not into either skill directory.

If you would rather type a short command, link it onto your `PATH` once:

```bash
mkdir -p ~/.local/bin
ln -s /sdf/group/lcls/ds/dm/apps/dev/opencode/skills/jira-search/scripts/jira-login \
      ~/.local/bin/jira-login
```

It prompts without echoing, so nothing appears as you paste. It writes the token
to `~/.config/jira-search/token` with mode **600**, creating the directory as
700, and then makes one live call to confirm the token works. Seeing your own
name back means you are done.

## 5. Check the expiry actually took

Go back to the token page and look at the **Expiry date** column for your new
token. If it says **Never**, the automatic-expiry box was not ticked when you
submitted. You cannot fix that on an existing token — revoke it and create a
replacement.

## When it expires

Commands start failing with an HTTP 401 and this body:

```json
{"message":"Client must be authenticated to access this resource.","status-code":401}
```

Mint a new token exactly as above, then install it over the old one:

```bash
<path-from-step-4>/jira-login --force
```

`--force` is required because the command refuses to overwrite an existing token
by accident.

**Check who you are** at any time:

```bash
<skill-dir>/scripts/jqlsearch.py whoami
```

One Jira-specific trap: a **missing or malformed** `Authorization` header does
not always produce a 401. Jira can answer `200` with the anonymous user's much
smaller view of the tracker, and marks it with an `X-AUSERNAME: anonymous`
response header. If searches suddenly return far fewer issues than you expect,
run `whoami` before you blame the query.

## Housekeeping

- **Revoke tokens you no longer use**, from the same page. A revoked token stops
  working immediately.
- **One token per tool.** If a laptop is lost or a service is retired, you revoke
  one token instead of re-minting everything.
- **Never put the token on a command line.** Process arguments are readable by
  other users on shared machines like the S3DF login nodes.
- **Never commit it, paste it into a ticket, or screenshot the creation dialog.**
  It is not a read-only credential; see step 2.

## Other ways to supply the token

`jira-search` looks for the token in this order:

1. `$JIRA_TOKEN`
2. `$JIRA_TOKEN_FILE` — a path to a file holding the token
3. `~/.config/jira-search/token` — the default, written by `jira-login`

The lookup is `XDG_CONFIG_HOME`-aware, and the token file is rejected if it is
group- or world-readable; the error tells you the `chmod` to run.

If you keep secrets in a password manager, you can skip the prompt and pipe it
in, which avoids the clipboard entirely:

```bash
pass show jira | <path-from-step-4>/jira-login
```

Setting `JIRA_TOKEN` in a shell profile also works, but puts the secret in a
dotfile that is easy to commit to a dotfiles repo by mistake. The token file is
the safer default.

For everything the skill can then do with that token, see the skill's own
`SKILL.md`.
