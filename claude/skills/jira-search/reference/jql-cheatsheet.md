# JQL cheat sheet

**Verified against `jira.slac.stanford.edu` (Jira Server 10.3.19) on 2026-09-04**
by running every query below and recording the real answer. Where this instance
disagrees with Atlassian's published documentation, this file follows the
instance and says so.

Numbers are `total` from `POST /rest/api/2/search` for the account that ran the
probes. Jira filters by project permission, so your totals will differ; the
*shapes* and the error messages will not.

## Endpoints

| Endpoint | Returns | Notes |
|---|---|---|
| `POST /rest/api/2/search` | `{startAt, maxResults, total, issues[]}` | Body `{"jql":…, "startAt":…, "maxResults":…, "fields":[…]}`. **Use this.** |
| `GET /rest/api/2/search?jql=…` | same | Works too, but a real JQL string URL-encodes badly and lands in proxy logs. |
| `GET /rest/api/2/issue/{key}` | one issue | `fields=…`, `expand=renderedFields` (wiki markup → HTML), `expand=changelog`. |
| `GET /rest/api/2/project` | **bare JSON array** of projects | No pagination envelope. 195 entries here. |
| `GET /rest/api/2/myself` | `name`, `key`, `displayName`, `emailAddress` | **No `username` field.** |
| `GET /rest/api/2/priority`, `/status`, `/issuetype` | the instance's value lists | Useful when a `=` value is rejected. |

Auth header: `Authorization: Bearer <personal-access-token>`.

**There is no `excerpt` in a Jira search result** and the result's `self` is a
REST URL. Build the human link yourself: `https://jira.slac.stanford.edu/browse/{KEY}`.

## Anatomy

```
project = FERMIS3DF AND text ~ "detector calibration" AND status != Done ORDER BY updated DESC
└─field─┘ │ └value┘      └field┘ │ └────── value ──────┘                        └─── sort ───┘
          └ operator                └ operator
```

Keywords (`AND`, `OR`, `NOT`, `ORDER BY`, `IS`, `EMPTY`, `IN`, `WAS`, `CHANGED`)
are case-insensitive. **So are project keys and issue keys on this instance** —
`project = fermis3df` and `key = fermis3df-43` both work. *(Deliberate contrast
with the sibling CQL skill, where Confluence space keys are case-sensitive.)*

## Fields

| Field | Example | Measured |
|---|---|---|
| `text` | `text ~ "epixuhr"` | 165 — searches summary + description + comments + more |
| `summary` | `summary ~ "epix"` | 89 |
| `description` | `description ~ "epix"` | 146 |
| `comment` | `comment ~ "epix"` | 142 |
| `environment` | `environment ~ "epix"` | 0 — field exists, unused here |
| `project` | `project = FERMIS3DF` | 42 |
| `key` / `issue` | `key = FERMIS3DF-43` | 1 |
| `issuetype` | `issuetype = Bug` / `= "Sub-task"` | 12914 / 3945 |
| `status` | `status = Open` | 1851 |
| `statusCategory` | `statusCategory = Done` | 46119 |
| `resolution` | `resolution IS EMPTY` | 9846 (identical to `resolution = Unresolved`) |
| `priority` | `priority = Major` | 35670 — read the warning below |
| `assignee` | `assignee IS EMPTY` | 10078 |
| `reporter` / `creator` | `reporter = currentUser()` | 1 |
| `labels` | `labels = psana` | 2 |
| `component` | `component IS NOT EMPTY` | 31570 |
| `fixVersion` | `fixVersion IS NOT EMPTY` | 12689 |
| `affectedVersion` | `affectedVersion IS NOT EMPTY` | 5369 |
| `created` `updated` `due` `resolved` | `updated > "2026-01-01"` | 11930 |
| `attachments` | `attachments IS NOT EMPTY` | 11898 |
| `votes` / `timespent` | `votes > 0` | 100 / 1043 |
| `sprint` | `sprint in openSprints()` | 77 |
| `"Epic Link"` | `"Epic Link" IS NOT EMPTY` | 3859 — quote multi-word field names |
| `parent` | `parent = FERMIS3DF-43` | 0 — sub-tasks of that issue |
| `level` | `level IS NOT EMPTY` | 0 — issue security unused here |
| `issueFunction` | `issueFunction in hasComments()` | 41808 — **ScriptRunner IS installed here** |

**`status` exists and works.** *(Deliberate contrast: the sibling Confluence
skill records that CQL on that instance rejects `status` entirely. Do not carry
that limitation across — filtering by workflow state is normal here.)*

**Priority is a trap on this instance.** Two priority schemes coexist:

```
GET /rest/api/2/priority
  id=1     Blocker      id=10000 High      id=10100 Standard    id=4  Minor
  id=2     Critical     id=10001 Medium    id=10200 Normal      id=10002 Low
  id=3     Major                                                id=5  Trivial
```

`priority = High` matches **1,060** issues; `priority = Major` matches **35,670**.
Asking for "high priority issues" with `= High` silently misses almost
everything. `priority > Medium` (41,094) spans both schemes; `priority < Medium`
is 12,672.

## Operators

| Operator | Meaning | Measured caveat |
|---|---|---|
| `=` `!=` | exact value | **Not valid on `text`** → 400 `The operator '=' is not supported by the 'text' field.` |
| `~` | contains | The one you want for prose |
| `!~` | does not contain | Works on `summary` (54861) — **400 on `text`**: `The operator '!~' is not supported by the 'text' field.` |
| `>` `>=` `<` `<=` | ranges | dates, numbers, priority sequence |
| `IN` `NOT IN` | set membership | `status in (Open, "In Progress")` = 3058 |
| `IS EMPTY` `IS NOT EMPTY` | unset / set | `assignee IS EMPTY` = 10078 |
| `WAS` | historical value | `status WAS "In Progress"` = 16680 |
| `CHANGED` | field ever changed | `status CHANGED` = 49575; `status CHANGED FROM "Open" TO "In Progress"` = 8379 |

`NOT project = FERMIS3DF` (54908) and parentheses both work:
`summary ~ "epix" AND (status = Open OR statusCategory = "To Do")` = 13.

**`WAS` will not take `currentUser()`** here: `assignee WAS currentUser()` →
400 `A value provided by the function 'currentUser' is invalid for the field 'assignee'.`

## Functions

Verified working on this instance:

| Function | Measured |
|---|---|
| `currentUser()` | `assignee = currentUser()` 0, `reporter = currentUser()` 1 |
| `startOfDay()` `startOfWeek()` `startOfMonth()` `startOfYear()` | `created >` 21 / 147 / 119 / 6111 |
| `endOfDay()` `endOfMonth("-1M")` `endOfWeek()` | `updated < endOfDay()` 54950; `updated > endOfMonth("-1M")` 396 |
| `now()` | `created > now()` 0 |
| `membersOf("group")` | `assignee in membersOf("jira-users")` 42055 |
| `issueHistory()` `watchedIssues()` `linkedIssues("KEY")` | 0 / 1 / 0 |
| `openSprints()` `closedSprints()` | 77 / 1043 |
| `projectsLeadByUser()` | 0 |
| ScriptRunner `issueFunction in hasComments()` etc. | 41808 |

**Date offsets are bare, not wrapped in `now()`.** Write `created > -7d` (157) or
`created > -30d`. *(Deliberate contrast: CQL spells this `now("-30d")`. Jira
rejects that outright — `Function 'now' expected '0' arguments but received '1'.`)*
Units: `w` weeks, `d` days, `h` hours, `m` minutes.

Date literals: `"2026-01-01"` and `"2026-01-01 12:00"` both parse (both 11930
for `updated >`).

An unknown function is named in the error:
`issueFunction in bogusFunctionName()` → 400 `Unable to find JQL function 'bogusFunctionName()'.`
A real function with a bad argument is too:
`membersOf("nosuchgroup")` → 400 `Function 'membersOf' can not generate a list of usernames for group 'nosuchgroup'; the group does not exist.`

## Text-search syntax (inside `~ "…"`)

**Multiple words are AND-ed, not OR-ed.** Proven by arithmetic:

| Query | total |
|---|---|
| `text ~ "detector"` | 1556 |
| `text ~ "calibration"` | 854 |
| `text ~ "detector calibration"` | **86** |
| `text ~ "detector AND calibration"` | **86** |
| `text ~ "+detector +calibration"` | **86** |
| `text ~ "detector OR calibration"` | 2282 |
| `text ~ "\"detector calibration\""` | **6** |

86 is smaller than either single term, so a space cannot mean OR; it is exactly
the explicit-AND figure. Inner double quotes narrow it further to an adjacent
phrase (6). If you want either word, say `OR` inside the value.

- **Trailing `*` widens:** `text ~ "epix"` 297 → `text ~ "epix*"` 926.
- **A leading `*` is accepted here**, contrary to the usual "no leading
  wildcards" advice: `text ~ "*epix"` returned 312, not an error. It is slow and
  its semantics are not documented; prefer a trailing wildcard.
- **`?` is a single-character wildcard:** `text ~ "epi?"` 2324.
- **`-` at the start of a term excludes it:** `text ~ "detector -calibration"`
  1509. **A `-` inside a word does not** — `text ~ "lcls-ii"` (676) is a
  hyphenated term, matched roughly as the adjacent phrase (`"lcls-ii"` quoted:
  661), not as "lcls minus ii". Hyphens need no escaping.
- **A colon needs no escaping either:** `text ~ "psana:detector"` and
  `text ~ "psana\\:detector"` both return 79.
- **A bare Lucene keyword is a parse error.** `text ~ "AND"` → 400
  `Unable to parse the text 'AND' for field 'text'.` Wrap it in inner quotes:
  `text ~ "\"AND\""` → 37343.
- **An empty term is rejected:** `text ~ ""` → 400
  `The field 'text' does not support searching for an empty string.`

## Sorting

`ORDER BY <field> ASC|DESC`, appended last. `ORDER BY Rank` works. An unsortable
name is rejected: `ORDER BY bogus` → 400 `Not able to sort using field 'bogus'.`

**With no `ORDER BY`, a text query comes back by relevance, not by date.**
Measured — first five keys of `text ~ "epixuhr"`:

```
no ORDER BY          TIDIDECS-356(c2025-06-12), TIDAT-416(c2024-03-05), TIDAT-469(c2024-03-21), …
ORDER BY created DESC TIDAT-5564(c2026-09-03), TIDAT-5563(c2026-09-03), TIDAT-5554(c2026-09-02), …
```

The unordered list is not date-sorted. Add `ORDER BY updated DESC` when you want
recency; leave it off when you want the best match.

## Result shape

```json
{ "startAt": 0, "maxResults": 25, "total": 165,
  "issues": [ { "key": "TIDAT-5564", "id": "…",
                "self": "https://jira.slac.stanford.edu/rest/api/2/issue/…",
                "fields": { "summary": "…", "status": {"name": "Open"},
                            "issuetype": {"name": "Bug"},
                            "project": {"key": "TIDAT"},
                            "assignee": {"displayName": "…"},
                            "updated": "2026-09-03T…" } } ] }
```

`total` is the true match count; `issues` holds only this page. Nested values are
objects — `fields.status.name`, not `fields.status`. Issue **descriptions are
Jira wiki markup** (`h2.`, `{code}`, `* bullets`, `[text|url]`), not HTML and not
Markdown; `expand=renderedFields` converts to HTML if you need it.

## Errors and limits

| Symptom | Cause / fix |
|---|---|
| 400 `Field 'x' does not exist or you do not have permission to view it.` | typo'd field name — or a real field in a project you cannot browse |
| 400 `The operator '=' is not supported by the 'text' field.` | use `~`. Same for `!~` on `text` |
| 400 `The value 'X' does not exist for the field 'project'` / `'component'` | validated value list — check `/rest/api/2/project`, or the project's components |
| 400 `Unable to parse the text 'AND' for field 'text'.` | reserved word as a search term — wrap in inner quotes |
| 400 `Not able to sort using field 'bogus'.` | unsortable `ORDER BY` field |
| 400 `Function 'now' expected '0' arguments but received '1'.` | CQL habit — write `-30d`, not `now("-30d")` |
| **Silently truncated results** | `maxResults` is capped at **1000** and defaults to **50**. Asking for 5000 returns 1000 with no error. Page with `startAt` while `startAt + len(issues) < total` |
| HTTP 429 | token bucket: `X-RateLimit-Limit: 70`, `X-RateLimit-FillRate: 5` per `X-RateLimit-Interval-Seconds: 1`. 20 back-to-back GETs never tripped it. Honour `Retry-After` |
| HTTP 200 but almost no results | check the `X-AUSERNAME` response header. `anonymous` means the Bearer token was missing or bad — Jira does **not** always 401 |
| `CERTIFICATE_VERIFY_FAILED` | uv-managed pythons miss SLAC's CA. `export SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt` |

401 bodies carry `.message`; 400 bodies carry `.errorMessages[]` and **no
`.message`**. Read both shapes.

## Measured scope (2026-09-04)

| Query | `total` |
|---|---|
| `order by created DESC` (whole instance) | 54,950 |
| projects visible to this token | 195 |
| `issuetype = Bug` | 12,914 |
| `issuetype IN (Bug, Task, Story)` | 34,432 |
| `issuetype = "Sub-task"` | 3,945 |
| `resolution IS EMPTY` (unresolved) | 9,846 |
| `statusCategory = Done` | 46,119 |
| `status = Open` | 1,851 |
| `component IS NOT EMPTY` | 31,570 |
| `attachments IS NOT EMPTY` | 11,898 |
| `issueFunction in hasComments()` | 41,808 |
| `created > startOfYear()` | 6,111 |
| `created > -30d` | 1,016 |
| `project = FERMIS3DF` | 42 |
| `text ~ "epixuhr"` | 165 |
| `text ~ "detector calibration"` | 86 |

For what the CLI can do with these queries, see `SKILL.md`.
