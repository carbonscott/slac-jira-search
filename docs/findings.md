# Findings

Why this skill exists, and what was measured along the way. All numbers taken
2026-09-04 against `jira.slac.stanford.edu` with one LCLS account (`cwang31`,
Jira user key `JIRAUSER21902`).

The probe scripts that produced them are not in this repo — they were one-off
`curl` invocations against a rate-limited instance, and the results they captured
are written up below rather than left to be re-run.

The question: can an agent query SLAC Jira **live**, safely, with a credential
that is not read-only? Yes — Jira exposes a REST search API driven by JQL (Jira
Query Language), it answers in 130–250 ms, and read-only-ness can be enforced in
the client instead of in the credential. The rest of this document is the
evidence for that claim and the traps found on the way.

## 1. The instance

`GET /rest/api/2/serverInfo`, verbatim:

```json
{"baseUrl":"https://jira.slac.stanford.edu","version":"10.3.19",
 "versionNumbers":[10,3,19],"deploymentType":"Server","buildNumber":10030019,
 "buildDate":"2026-04-03T00:00:00.000-0700","databaseBuildNumber":10030019,
 "serverTime":"2026-09-04T16:07:42.487-0700",
 "scmInfo":"95508b3a08ac0f09fbef8676c203b44bdc6e1a12","serverTitle":"SLAC JIRA"}
```

**Jira 10.3.19, `deploymentType: Server`, build 10030019.** That version matters:
Jira Server 10.x still speaks the `/rest/api/2` API with `Authorization: Bearer
<PAT>`, and it is *not* Jira Cloud — none of the Cloud-only JQL (`cf[10010]`
ARI syntax, `parentEpic()`, `approver()`) applies here. Where Atlassian's
published docs and this instance disagree, the reference cheat sheet
(`claude/skills/jira-search/reference/jql-cheatsheet.md`) follows the instance.

195 projects are visible to this token; the whole-instance issue count
(`order by created DESC`) is **54,950**.

## 2. Endpoints and latency

| Endpoint | Purpose | Method |
|---|---|---|
| `/rest/api/2/serverInfo` | version banner | GET |
| `/rest/api/2/myself` | who this token is | GET |
| `/rest/api/2/project` | project list — a **bare JSON array**, no pagination envelope | GET |
| `/rest/api/2/search` | JQL search | POST (also answers GET) |
| `/rest/api/2/issue/{key}` | one issue + comments | GET |
| `/rest/api/2/mypermissions` | what this token may do | GET |
| `/rest/pat/latest/tokens` | this account's PAT metadata | GET |

Measured round trips:

```
GET  /rest/api/2/myself                time_total=0.129s  bytes=753
GET  /rest/api/2/project               time_total=0.247s  bytes=141759
GET  /rest/api/2/issue/FERMIS3DF-43    time_total=0.156s  bytes=12718
GET  /rest/api/2/serverInfo            time_total=0.155s  bytes=337
POST /rest/api/2/search  (25 hits)     time_total=0.243s  bytes=43351
```

`/rest/api/2/search` answers **both** GET (`?jql=…`) and POST. Confirmed: a GET
with `jql=text ~ "epixuhr"` returned `total = 165`, the same number the POST
returns. This skill uses POST anyway — a real JQL string is long, full of quotes,
parentheses and spaces, and a URL-encoded one lands in proxy logs and shell
history. POST is the **only** non-GET request this codebase ever makes.

## 3. The credential carries full write permission. The client is what is read-only.

This is the single most important thing to understand about the skill.

**Jira Server personal access tokens have no scopes.** A PAT inherits every
permission the account has; there is no "read-only token" checkbox on the
creation form and no scope parameter on the PAT API. First-hand proof, from
`GET /rest/api/2/mypermissions?projectKey=FERMIS3DF`:

```
BROWSE_PROJECTS      havePermission: true
CREATE_ISSUES        havePermission: true
EDIT_ISSUES          havePermission: true
ADD_COMMENTS         havePermission: true
TRANSITION_ISSUES    havePermission: true
RESOLVE_ISSUES       havePermission: true
MOVE_ISSUES          havePermission: true
LINK_ISSUES          havePermission: true
ASSIGN_ISSUES        havePermission: true
BULK_CHANGE          havePermission: true
DELETE_ISSUES        havePermission: false
ADMINISTER           havePermission: false
SYSTEM_ADMIN         havePermission: false
```

The token behind this repo can create issues, edit them, comment on them and
transition them across every project it can browse. It cannot delete them, and it
is not an admin — but "cannot delete" is a property of *this* account, not of the
token, and another user's PAT will report a different set.

So safety is a property of the code, not the credential:

- `jqlsearch.py` exposes exactly two request paths — a `get()` that builds a
  `urllib` request with no body (hence GET), and a `search()` that POSTs to
  `/rest/api/2/search`. There is no other `method=`, no PUT, no DELETE, no PATCH.
- Nothing in the skill touches `/rest/api/2/issue/{key}/transitions`,
  `/comment`, `/issueLink`, `/worklog`, or any other mutating endpoint.

That is a design constraint worth restating in review whenever this code changes,
because the credential will not stop a mistake.

Two side notes from the same probe:

- The `permissions=` query parameter was **ignored**. Asking for
  `permissions=CREATE_ISSUES,EDIT_ISSUES,ADD_COMMENTS,DELETE_ISSUES` returned all
  **81** permission keys anyway. Filter client-side.
- Several keys are returned twice under a deprecated alias (`EDIT_ISSUE` and
  `EDIT_ISSUES`, `CREATE_ISSUE` and `CREATE_ISSUES`, …), flagged with
  `"deprecatedKey": true`. Match on the plural form.

## 4. `maxResults` — the server silently overrides what you ask for

Two POSTs to `/rest/api/2/search` with `{"jql":"issuetype = Bug","fields":[]}`:

| Request body | Echoed `maxResults` | `issues` returned | `total` |
|---|---|---|---|
| `"maxResults": 5000` | **1000** | 1000 | 12914 |
| `maxResults` omitted | **50** | 50 | 12914 |

So: the server-side hard cap is **1000** and the default is **50**. It does not
error on 5000, it just quietly gives you 1000 — which is exactly the shape of bug
where a script "sweeps everything" and silently sees the first 1000 of 12,914.
`total` is always the true match count and is unaffected by the cap; page with
`startAt` when `total > maxResults`. `jqlsearch.py` sets `MAX_LIMIT = 1000` and
clamps rather than letting the server decide.

## 5. Rate limiting is a token bucket, and it is generous

Responses carry these headers by name:

```
X-RateLimit-Limit: 70
X-RateLimit-Remaining: 69
X-RateLimit-FillRate: 5
X-RateLimit-Interval-Seconds: 1
Retry-After: 0
```

Read that as: a bucket of **70** tokens that refills at **5 per 1 second**.
`Retry-After: 0` is present on healthy responses too — it is not an error signal
by itself.

Measured directly: **20 back-to-back `GET /rest/api/2/myself` calls with no sleep
between them** all returned HTTP 200, and `X-RateLimit-Remaining` drifted only
from 69 to 64 — the bucket refills faster than a serial client on a ~130 ms round
trip can drain it. To actually trip this you need concurrency, not a loop.

`X-RateLimit-*` is **not emitted on every endpoint**. Measured:

| Endpoint | `X-RateLimit-*` present? |
|---|---|
| `GET /rest/api/2/serverInfo` | **no** |
| `GET /rest/api/2/myself` | yes |
| `GET /rest/api/2/project` | yes |
| `GET /rest/api/2/issue/{key}` | yes |
| `GET /rest/api/2/mypermissions` | yes |
| `GET /rest/pat/latest/tokens` | yes |
| `POST /rest/api/2/search` | yes |

A client that reads its budget from the headers must cope with their absence, not
assume "no header means no limit".

*Deliberate comparison, since this repo mirrors `slac-confluence-search`:* the
Confluence instance behaved completely differently — sustained ~3 s-spaced
requests tripped `HTTP 429` after roughly ten calls and stayed tripped through a
30 s pause. Jira here does not. **Do not carry the Confluence pacing advice
across**; `PAGE_DELAY = 0.5 s` between pages of a sweep is ample against a
5/s fill rate.

## 6. Jira-specific traps a Confluence port walks straight into

These are the places where mirroring the CQL skill produces a subtly broken Jira
skill. Each was hit for real.

**A bad token can still return HTTP 200.** Jira answers an absent or malformed
`Authorization` header with the *anonymous* user's view rather than a 401, and
marks the response with an `X-AUSERNAME` header. On a good call it reads
`X-AUSERNAME: cwang31%40slac.stanford.edu` (URL-encoded); anonymous access reads
`X-AUSERNAME: anonymous`. Treat that as an auth failure and say so loudly —
otherwise the failure mode is "the search worked, there just aren't many results",
which is the worst possible way to lose data.

**Two different error body shapes.** A 401 is
`{"message":"Client must be authenticated to access this resource.","status-code":401}`
— it has `.message`. A 400 is `{"errorMessages":[...],"errors":{}}` — it has **no
`.message` key at all**. Code that only reads `.message` prints an empty error on
every JQL mistake, which is the one case where the server's text is actually
useful (`The operator '=' is not supported by the 'text' field.`).

**`/rest/api/2/project` is a flat array.** No `{"values": [...], "isLast": …}`
envelope, no `_links.next`. 195 entries, 141 KB, one call. Any pagination loop
ported from the Confluence space listing is dead code here.

**`/rest/api/2/myself` has no `username` field.** It returns `name`, `key`,
`displayName` and `emailAddress`. A blind port of Confluence's identity printer
looks up `username` and prints `Cong Wang <?>`.

**Search results carry no excerpt.** Confluence's `/rest/api/search` hands back a
ready-made `excerpt` with `@@@hl@@@` highlight markers. Jira has no equivalent:
per-hit output has to be assembled from the `fields` you asked for, and the
result's `self` is a REST URL, not a human one — the browse link must be
constructed as `{BASE}/browse/{KEY}`.

**Descriptions are Jira wiki markup, not HTML and not Markdown.** `h2.`,
`{code}`, `* bullets`, `[text|url]`. Print it verbatim. Adding
`expand=renderedFields` converts it to HTML — 3,224 raw characters become 3,893
rendered on `FERMIS3DF-43` — which is available behind a flag but is not the
default, because the raw form is shorter and an LLM reads it fine.

**Jira *does* have a `status` field.** *Deliberate comparison:* the Confluence
findings record that `status = current` is rejected there, so archived content
cannot be filtered in CQL. That limitation does **not** transfer. `status`,
`statusCategory` and `resolution` are all live, indexed JQL fields here, and
`resolution = Unresolved` (9,846 issues) is the normal way to mean "still open".

## 7. Measured scope

| Query / call | Result |
|---|---|
| `order by created DESC` (whole instance) | 54,950 issues |
| `GET /rest/api/2/project` | 195 projects |
| `issuetype = Bug` | 12,914 |
| `resolution = Unresolved` | 9,846 |
| `component is not EMPTY` | 31,570 |
| `fixVersion is not EMPTY` | 12,689 |
| `labels is not EMPTY` | 12,869 |
| `project = FERMIS3DF` | 42 |
| `text ~ "epixuhr"` | 165 |
| `created > -30d` | 1,016 |

Jira filters by permission, so these are that account's numbers, not properties
of the instance — another user sees a different set. *These are issue counts and
project counts; they are not comparable to the Confluence skill's page and space
counts, and no comparison is intended.*

## 8. Tokens

`GET /rest/pat/latest/tokens` returns metadata for the calling account's tokens —
no secret values — which is how this repo confirmed the Atlassian PAT plugin
whose tab key `docs/token-setup/getting-a-token.md` cites is actually installed:

```json
[{"id":58,"name":"test","createdAt":"2026-09-04T22:35:14.728+00:00",
  "lastAccessedAt":"2026-09-04T22:55:16.025+00:00",
  "expiringAt":"2026-12-03T22:35:14.728+00:00"}]
```

The PAT page itself is a profile tab, not a plugin URL:

```
/secure/ViewProfile.jspa?selectedTab=com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens
```

Signed out, that URL **redirects rather than 404s** — `302` to
`/login.jsp?permissionViolation=true&os_destination=…`, verified with a
cookieless `curl`. Useful when someone reports "your link is broken": a 302 to
`login.jsp` means the link is fine and they are not signed in.

The API can list and create tokens, but only for a caller who already holds one,
so **the first token has to come from a browser.** There is no bootstrap.

## 9. TLS

*Carried over from `slac-confluence-search` and NOT re-verified on this host —
the measurements above were taken from a VM with a stock CA bundle, not from an
S3DF login node.* uv-managed pythons on S3DF look for `/etc/ssl/cert.pem` and
fail with `CERTIFICATE_VERIFY_FAILED: self-signed certificate in certificate
chain`; SLAC's CA lives in `/etc/pki/tls/certs/ca-bundle.crt`. `jqlsearch.py`
probes that path and `/etc/ssl/certs/ca-certificates.crt`, honouring
`$SSL_CERT_FILE` first. If a user reports a certificate error on S3DF, that is
the knob.

## 10. Where this could go

- **`WAS` / `CHANGED` history queries** are supported by this instance (see the
  cheat sheet) and would let an agent answer "what moved this sprint" without a
  changelog crawl. Not surfaced in the CLI yet.
- **`--all` sweeps** are cheap given the real rate limit; the 1000-row cap and
  `startAt` paging are the only constraint.
- **Attachments** are visible in the issue payload but not fetched. Anyone adding
  that must keep the GET-only rule intact.

## Running the scripts

`jqlsearch.py` carries [PEP 723](https://peps.python.org/pep-0723/) inline
metadata and a `#!/usr/bin/env -S uv run --script` shebang, so nothing needs a
venv or a `requirements.txt`. It is deliberately **stdlib-only** so it can be
copied anywhere. That is not the same as running under *any* python: it declares
`requires-python = ">=3.9"`, and the system python on SLAC login nodes is 3.6,
which cannot parse the file at all.
