#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""jqlsearch — query SLAC Jira live via the JQL search REST API.

Standard library only, so it runs either way:

  uv run jqlsearch.py text "epixuhr" --limit 10
  ./jqlsearch.py search 'project = FERMIS3DF ORDER BY updated DESC'   # uv via shebang
  python3 jqlsearch.py issue FERMIS3DF-43                             # python3 >= 3.9 only
  uv run jqlsearch.py projects

Auth: a Jira Data Center personal access token (Bearer), resolved in order from
$JIRA_TOKEN, $JIRA_TOKEN_FILE, then ~/.config/jira-search/token. Per-user by
design — there is no shared default, so a central install never authenticates
everyone as one account. Run the sibling `jira-login` to install your token,
`whoami` to see which identity you are using.

READ-ONLY BY CONSTRUCTION. A Jira personal access token carries its owner's full
permissions — there is no read-only PAT — so the read-only-ness lives here, in
the code. Every call in this file is a plain GET but one: Jira's JQL search
endpoint takes its query in a request body, so Client.search() is the single
write-shaped call in the skill, and it creates, edits and deletes nothing. The
comment at that call site says so, there is no second one, and a one-line grep
over scripts/ for write-shaped verbs is the audit that keeps it that way.
"""
from __future__ import annotations

import argparse
import atexit
import getpass
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    import pwd                      # unix only; absent on Windows
except ImportError:                 # pragma: no cover
    pwd = None

# A deployed skill directory is often shared and not the caller's to write to,
# and a stray __pycache__/ next to these two scripts is noise in every listing.
sys.dont_write_bytecode = True

BASE = os.environ.get("JIRA_URL", "https://jira.slac.stanford.edu").rstrip("/")
API = "/rest/api/2"
PAT_URL = (f"{BASE}/secure/ViewProfile.jspa?selectedTab="
           f"com.atlassian.pats.pats-plugin:jira-user-personal-access-tokens")

# Setup instructions must point at *this* file, not at install.sh: install.sh
# lives in the repo, and a user of a central deployment has only the deployed
# skill directory (SKILL.md, reference/, scripts/). Absolute, so the hint stays
# correct after the reader cd's somewhere else.
#
# __file__, deliberately, and NOT sys.argv[0]: the sibling `jira-login` wrapper
# imports this module and calls main(["login"] + argv), so under it argv[0] is
# the wrapper. A hint built from argv[0] then reads `jira-login whoami`, and the
# wrapper has already spent the subcommand slot on "login" — that command dies
# with `unrecognized arguments: whoami`. So: any hint that names a SUBCOMMAND
# must name jqlsearch.py, whichever entry point the user actually came in
# through. A hint with no subcommand after it has no such constraint and is free
# to name the friendlier wrapper — LOGIN_CMD, three lines down, is exactly that
# case and does.
SCRIPT = os.path.abspath(__file__) if "__file__" in globals() else "jqlsearch.py"

# The command to hand a user who needs a token. `jira-login` is a sibling
# wrapper shipped in the same scripts/ directory; fall back to the subcommand
# when jqlsearch.py was copied somewhere on its own. This one is a bare command
# with no subcommand after it, so the wrapper is the right thing to name.
_WRAPPER = os.path.join(os.path.dirname(SCRIPT), "jira-login")
LOGIN_CMD = _WRAPPER if os.path.exists(_WRAPPER) else f"{SCRIPT} login"


def home_dir() -> str:
    """The invoking user's home.

    $HOME is inherited, so it lies under sudo, cron, and service accounts. The
    passwd database does not.
    """
    if pwd is not None:
        try:
            return pwd.getpwuid(os.getuid()).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def default_token_file() -> str:
    """Per-user, by construction — never a shared path.

    A shared default would mean every user of a central install silently
    authenticating as whoever owns that file, seeing that account's view of
    Jira — and, because a Jira PAT carries full write permission, holding that
    account's ability to change things. Deliberately absent: point
    JIRA_TOKEN_FILE at a shared file if you actually want that.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home_dir(), ".config")
    return os.path.join(xdg, "jira-search", "token")


TOKEN_FILE = os.environ.get("JIRA_TOKEN_FILE") or default_token_file()

# S3DF nodes trust SLAC's internal CA through the system bundle; the uv-managed
# pythons look for /etc/ssl/cert.pem instead and fail verification without this.
CA_CANDIDATES = ("/etc/pki/tls/certs/ca-bundle.crt",
                 "/etc/ssl/certs/ca-certificates.crt")

MAX_LIMIT = 1000         # server hard cap: asking for 5000 comes back as 1000
PAGE_DELAY = 0.5         # between pages of an --all sweep; FillRate is 5/s
MAX_RETRY_WAIT = 300.0   # cap on ONE 429 sleep, whatever Retry-After claims

# ...and a cap on all of them together, because MAX_RETRY_WAIT on its own bounds
# nothing an agent cares about. `delay` is re-armed for every request and each of
# the four sleeps in a request is clamped separately, so a `Retry-After` of "inf"
# costs 4 x 300 s = 20 minutes PER REQUEST — and SKILL.md documents `--limit 1200
# --all` and `--limit 0 --all` as ordinary use: 13 requests for issuetype = Bug is
# 4.3 hours, 55 requests for the whole instance is 18.3 hours, all of it silent
# without -v. RETRY_BUDGET is what actually stops that. It counts only seconds
# spent asleep in the retry path, never wall clock, so a slow-but-healthy sweep is
# never cut short by it; raise it if you genuinely want to wait longer.
RETRY_BUDGET = 600.0     # total 429 backoff allowed for the WHOLE command
LOUD_SLEEP = 30.0        # a sleep at least this long is announced without -v
_retry_slept = 0.0       # seconds this process has already spent backing off

# Enough to render a hit line, and cheap enough that --limit 1000 stays one
# fast call. Jira search has no `excerpt` field, so there is nothing to show
# under a hit unless the caller asks for `description` (--excerpt / --fields).
DEFAULT_FIELDS = "summary,status,issuetype,assignee,updated,project,priority"

# What `issue` asks for. `comment` is here because Jira hands back the whole
# thread in one response — no second round trip per comment.
ISSUE_FIELDS = ("summary,description,status,issuetype,project,priority,"
                "resolution,labels,components,fixVersions,assignee,reporter,"
                "created,updated,comment")


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------

def ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile:
        cafile = next((c for c in CA_CANDIDATES if os.path.exists(c)), None)
    return ssl.create_default_context(cafile=cafile)


MINT_HELP = f"""\
You need a Jira personal access token of your own — search results are filtered
by *your* Jira permissions, so this is not a credential anyone can share with
you.

There is no API for the first one, so mint it in a browser, and set an expiry —
tokens here default to never expiring:
    {PAT_URL}"""

SETUP_HELP = f"""\
error: no Jira token.

Every user needs their own — results are filtered by *your* Jira permissions.

  1. mint one (browser; there is no API for the first token):
       {PAT_URL}
  2. install it — prompts without echoing, writes {TOKEN_FILE} mode 600:
       {LOGIN_CMD}

That is the whole setup, and it is yours alone to do: results are filtered by
your own Jira permissions, so nobody can install this token for you.

Or set JIRA_TOKEN / JIRA_TOKEN_FILE to override."""


def token_source() -> str:
    """Where the token came from. Never returns the token itself."""
    if os.environ.get("JIRA_TOKEN"):
        return "$JIRA_TOKEN"
    return TOKEN_FILE


def read_token_file(path: str) -> str:
    try:
        mode = os.stat(path).st_mode
    except OSError as e:
        sys.exit(f"error: cannot stat {path}: {e}")
    if mode & 0o077:
        # Group trees here are setgid and group-writable, so an inherited umask
        # leaks the token easily. Fail at setup rather than quietly. A leaked
        # Jira PAT is worse than a leaked Confluence one: it can write.
        sys.exit(f"error: {path} is readable by group or others "
                 f"(mode {mode & 0o777:03o}).\n"
                 f"  fix: chmod 600 {path}")
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        sys.exit(f"error: cannot read {path}: {e}")


def get_token() -> str:
    tok = os.environ.get("JIRA_TOKEN")
    if tok:
        return tok.strip()
    if not os.path.exists(TOKEN_FILE):
        sys.exit(SETUP_HELP)
    tok = read_token_file(TOKEN_FILE)
    if not tok:
        sys.exit(f"error: {TOKEN_FILE} is empty.\n\n{SETUP_HELP}")
    return tok


AUTH_FAIL = f"""\
error: Jira answered as ANONYMOUS (X-AUSERNAME: anonymous) — the token was not
       accepted, and what came back is the subset of Jira that logged-out
       visitors can see, not yours.
  token came from: {{src}}
  mint a new one:  {PAT_URL}
  then install it: {LOGIN_CMD} --force"""


def retry_after_seconds(value, fallback: float) -> float:
    """How long to wait, from a `Retry-After` that may be anything at all.

    RFC 9110 permits two forms — delta-seconds (`120`) and an HTTP-date
    (`Wed, 21 Oct 2015 07:28:00 GMT`) — and a bare float() on the second raises
    ValueError. Jira DC sends an integer today, so this never fired here, but a
    proxy or CDN in front of the instance is free to send the date form, and a
    traceback inside the retry path would turn a recoverable rate-limit into a
    crash at exactly the moment the tool is supposed to be coping.

    Nothing here is trusted; anything unusable falls back to the caller's own
    backoff schedule:

      missing / empty / unparseable   -> fallback
      "0", negative, or NaN           -> fallback  (this instance sends 0)
      "30"                            -> 30.0
      b"30"                           -> 30.0      (decoded, not stringified)
      "1_0"                           -> fallback  (float() would say 10.0)
      "0x10", "soon-ish"              -> fallback
      an HTTP-date in the future      -> seconds until then
      an HTTP-date already past       -> fallback
      absurdly large, or "inf"        -> clamped to MAX_RETRY_WAIT
    """
    if value is None:
        return fallback
    if isinstance(value, (bytes, bytearray)):
        # http.client hands headers back as str, but this function is also
        # called with whatever a caller has; str(b"30") is "b'30'", which parses
        # as nothing and would degrade to the fallback while looking handled.
        try:
            value = bytes(value).decode("latin-1")   # the header charset
        except (UnicodeDecodeError, ValueError):     # pragma: no cover
            return fallback
    raw = str(value).strip()
    if not raw:
        return fallback
    if "_" in raw:
        # float() honours PEP 515 underscores, so a header of "1_0" would be
        # read as 10 seconds. Retry-After has no such form — delta-seconds is
        # DIGITs — and an HTTP-date has no underscore either, so this is junk.
        return fallback
    try:
        secs = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError):
            return fallback
        if when is None:                     # older pythons answer None, not raise
            return fallback
        if when.tzinfo is None:              # a date with no zone means UTC here
            when = when.replace(tzinfo=timezone.utc)
        try:
            secs = (when - datetime.now(timezone.utc)).total_seconds()
        except (OverflowError, ValueError):  # pragma: no cover
            return fallback
    if secs != secs or secs <= 0:            # NaN, zero, or in the past
        return fallback
    return min(secs, MAX_RETRY_WAIT)


def budget_spent_message(spent: float, headers, body: str) -> str:
    """What to say when the whole-command 429 budget is gone.

    Names the time actually waited, because the alternative an agent is left
    with is guessing whether the tool hung.
    """
    return (f"error: HTTP 429 — Jira kept rate-limiting this client and the "
            f"whole-command retry budget is spent: {spent:.0f}s "
            f"({spent / 60:.1f} min) asleep across this run, out of "
            f"{RETRY_BUDGET:.0f}s. Giving up rather than waiting longer.\n"
            f"  server said:  {extract_message(body)}\n"
            f"  headers:      Retry-After: {headers.get('Retry-After')}  |  "
            f"X-RateLimit-Remaining: {headers.get('X-RateLimit-Remaining')}\n"
            f"  what to do:   wait a few minutes, then run fewer calls in "
            f"parallel — one --limit 100 beats 100 calls. Something that ran "
            f"out of budget here was being throttled the whole way; restarting "
            f"it immediately will not go faster.\n"
            f"  note:         this budget covers the whole command, not one "
            f"request. A `Retry-After` of 'inf' or a huge number would "
            f"otherwise buy 20 minutes per request, and an --all sweep is "
            f"dozens of requests. Raise RETRY_BUDGET in\n"
            f"                {SCRIPT}\n"
            f"                if waiting longer is really what you want.")


class Client:
    """Thin read-only client with 429 backoff. The SLAC instance rate-limits.

    Two call sites reach the network: get(), which never sends a body and is
    therefore a GET, and search(), the one exception documented there.
    """

    def __init__(self, verbose: bool = False, token: str = ""):
        self.ctx = ssl_context()
        # `login` passes the token it just wrote, so its check tests that token
        # and not whatever $JIRA_TOKEN would otherwise win with.
        self.token = token or get_token()
        self.verbose = verbose

    def headers(self, extra: dict = None) -> dict:
        h = {"Authorization": f"Bearer {self.token}",
             "Accept": "application/json"}
        h.update(extra or {})
        return h

    def get(self, path: str, params: dict = None, tries: int = 5) -> object:
        url = f"{BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        # No body and no explicit verb, which is exactly what makes urllib send
        # a GET. Do not add either; see the module docstring.
        req = urllib.request.Request(url, headers=self.headers())
        return self._send(req, path, tries)

    def search(self, jql: str, fields, start: int, limit: int,
               tries: int = 5) -> object:
        """Run one page of a JQL query.

        THE ONE WRITE-SHAPED REQUEST IN THIS SKILL, and it is a read: Jira's
        JQL search takes its query in a request body, so this is the single
        call here that is not a GET. It creates, edits, transitions and deletes
        nothing.
        """
        body = json.dumps({"jql": jql, "startAt": max(0, int(start)),
                           "maxResults": min(int(limit), MAX_LIMIT),
                           "fields": list(fields)}).encode("utf-8")
        # This is that one call. Keep it the only one, and keep it here so the
        # read-only audit is a one-line grep. Jira Data Center 10.3 serves
        # POST /rest/api/2/search normally — Atlassian's deprecation of it is a
        # Jira *Cloud* change and does not apply to this instance — so do not
        # "fix" this into something else, and do not add a second body-bearing
        # request anywhere in scripts/.
        req = urllib.request.Request(f"{BASE}{API}/search", data=body, method="POST",
                                     headers=self.headers(
                                         {"Content-Type": "application/json"}))
        return self._send(req, f"{API}/search", tries)

    def log_rate(self, headers) -> None:
        """The rate-limit budget on stderr, from whatever headers arrived.

        Every piece is optional. `/rest/api/2/serverInfo` sends none of these at
        all, and a proxy is free to forward some and drop others, so the line is
        assembled from what is actually present instead of being formatted
        blind — the old version printed `69 of None left, refills None/Nones`
        the moment anything was missing, which reads like a bug in the client
        rather than a quiet endpoint.
        """
        rem = headers.get("X-RateLimit-Remaining")
        if rem is None:                      # nothing useful to say
            return
        limit = headers.get("X-RateLimit-Limit")
        fill = headers.get("X-RateLimit-FillRate")
        interval = headers.get("X-RateLimit-Interval-Seconds")
        budget = f"{rem} of {limit} left" if limit is not None else f"{rem} left"
        if fill is not None and interval is not None:
            budget += f", refills {fill}/{interval}s"
        elif fill is not None:
            budget += f", refills {fill} per interval"
        print(f"  (rate limit: {budget})", file=sys.stderr)

    def _send(self, req, path: str, tries: int = 5) -> object:
        """Send a prepared request, honour 429, and turn Jira errors into text.

        Shared by get() and search() so both fail the same readable way.
        Returns whatever the endpoint answers: /rest/api/2/project is a bare
        JSON array, everything else here is an object.
        """
        global _retry_slept
        # At least one attempt, always. The loop below has no normal exit — every
        # path inside it returns or exits — so this clamp is what makes that true
        # rather than nearly true, and it is why there is no "retries exhausted"
        # line after the loop: with tries >= 1 that line was unreachable, and
        # with tries = 0 it was the only thing a caller got instead of a request.
        tries = max(1, int(tries))
        delay = 10.0
        for attempt in range(tries):
            try:
                with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
                    # Jira answers 200 with the anonymous view when a Bearer is
                    # missing, malformed, or revoked — a shorter list, not an
                    # error. X-AUSERNAME is the only thing that tells them
                    # apart, so every response is checked, not just /myself.
                    who = (r.headers.get("X-AUSERNAME") or "").strip().lower()
                    if who == "anonymous":
                        sys.exit(AUTH_FAIL.format(src=token_source()))
                    if self.verbose:
                        self.log_rate(r.headers)
                    raw = r.read().decode("utf-8", "replace")
                    return json.loads(raw) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 429 and attempt < tries - 1:
                    # This instance sends `Retry-After: 0`, and honouring that
                    # literally is a hot loop against a server that is already
                    # saying stop — so 0, junk and a missing header all fall
                    # back to the doubling schedule. See retry_after_seconds().
                    wait = retry_after_seconds(e.headers.get("Retry-After"),
                                               delay)
                    # The whole-command budget, consulted here and nowhere else.
                    # Without it MAX_RETRY_WAIT bounds one sleep and nothing
                    # else: `delay` resets per request, so a pathological header
                    # scales straight up with the number of requests a sweep
                    # makes. See RETRY_BUDGET.
                    left = RETRY_BUDGET - _retry_slept
                    if left <= 0:
                        sys.exit(budget_spent_message(_retry_slept, e.headers,
                                                      body))
                    wait = min(wait, left)
                    # Loud even without -v once the pause is long. A silent
                    # multi-minute stall is indistinguishable from a hang, and
                    # an agent watching it will kill the command rather than
                    # wait — which is the wrong lesson to teach it.
                    if self.verbose or wait >= LOUD_SLEEP:
                        print(f"  (429 rate limited, sleeping {wait:.0f}s; "
                              f"{_retry_slept + wait:.0f}s of the "
                              f"{RETRY_BUDGET:.0f}s retry budget used)",
                              file=sys.stderr)
                    time.sleep(wait)
                    _retry_slept += wait
                    delay *= 2
                    continue
                if e.code == 400:
                    sys.exit(f"JQL error (HTTP 400): {extract_message(body)}")
                if e.code in (401, 403):
                    sys.exit(f"error: HTTP {e.code} — token rejected. It is "
                             f"invalid, expired, revoked, or lacks permission.\n"
                             f"  server said:     {extract_message(body)}\n"
                             f"  token came from: {token_source()}\n"
                             f"  mint a new one:  {PAT_URL}\n"
                             f"  then install it: {LOGIN_CMD} --force")
                if e.code == 404:
                    sys.exit(f"error: HTTP 404 for {path} — "
                             f"{extract_message(body)}")
                if e.code == 429:
                    # Retries are spent. Say what happened in words, because the
                    # body here is a Tomcat HTML page, not Jira JSON, and the
                    # generic line below would print a paragraph of stylesheet.
                    plural = "attempt" if tries == 1 else "attempts"
                    sys.exit(f"error: HTTP 429 — Jira is rate-limiting this "
                             f"client, and {tries} {plural} did not get "
                             f"through.\n"
                             f"  server said:  {extract_message(body)}\n"
                             f"  headers:      Retry-After: "
                             f"{e.headers.get('Retry-After')}  |  "
                             f"X-RateLimit-Remaining: "
                             f"{e.headers.get('X-RateLimit-Remaining')}\n"
                             f"  what to do:   wait a minute, then run fewer "
                             f"calls in parallel — one --limit 100 beats 100 "
                             f"calls.\n"
                             f"  note:         a `Retry-After` of 0 — which "
                             f"this instance sends, on 429s and on healthy "
                             f"responses alike — is not an instruction to "
                             f"retry at once. Unusable values are ignored and "
                             f"this client backs off on its own schedule.")
                sys.exit(f"HTTP {e.code} for {path}: {extract_message(body)}")
            except urllib.error.URLError as e:
                sys.exit(f"network/TLS error for {path}: {e.reason}\n"
                         f"hint: export SSL_CERT_FILE=/etc/pki/tls/certs/ca-bundle.crt")


_TAG_RE = re.compile(r"<[^>]+>")

# How much of a non-JSON error body is worth reading at all. This is the *error*
# path: it runs when the server is already misbehaving, on a body nobody vetted,
# so the work done here must not scale with whatever the other end felt like
# sending. A <title>, an <h1> and an XML <message> all sit in the first few
# hundred bytes of any page that has one, so 8 KB is already far more than a
# message needs. Applied BEFORE anything scans the body — belt to _ELEMENT's
# braces, and it also bounds the tag-strip and unescape passes below.
_MARKUP_SCAN = 8192

# How much of the extracted sentence to keep. 300, which is what this code gave
# before the error-parser was added, and dropping it to 200 was an accident of
# that rewrite rather than a decision. The parser exists because 300 characters
# of *raw body* was a screenful of stylesheet with the message still off the end
# — not because 300 characters of the actual message is too many.
_MSG_CLIP = 300

# The opening tag only. The closing tag is found with str.find, not with a
# regex — see _element_text(), where that split is the entire point.
_OPEN_RE = {t: re.compile(rf"<{t}[^>]*>", re.I) for t in ("title", "h1", "message")}


def _element_text(body: str, tag: str):
    """Inner text of the first <tag ...>...</tag>, or None. Linear in len(body).

    Deliberately NOT `<tag[^>]*>(.*?)</tag>` with re.S. That pattern is
    catastrophic on the one input this function has to survive — an error body
    carrying many OPENING tags and no closing one — because `.*?` rescans to
    end-of-body from every opener in turn: O(n^2). Measured on the previous
    version: 500 bare <title>s in a 99 KB body took 0.77 s, 4000 took 5.8 s,
    20000 in 232 KB took 44.7 s, all on the error path, all on a body chosen by
    whoever is misbehaving.

    Unrolling the inner class (`[^<]*(?:<(?!/title)[^<]*)*`) does NOT fix it,
    which is worth writing down because it looks like it should: the negative
    lookahead only refuses `</title`, so the group walks straight over every
    other `<title>` to the end of the body exactly as `.*?` did — remeasured at
    193 ms for 1168 openers in 8 KB. Finding the CLOSING tag first does fix it,
    because str.find scans forward from a cursor that never rewinds: no closing
    tag means no match, found in one pass, with nothing to backtrack into.
    """
    low = body.lower()
    close = "</" + tag
    start = 0
    while True:
        c = low.find(close, start)
        if c < 0:                            # no closer left: nothing can match
            return None
        m = _OPEN_RE[tag].search(body, start, c)
        if m:
            return body[m.end():c]
        start = c + len(close)               # a closer with no opener; keep going


def markup_message(body: str) -> str:
    """A readable sentence out of an error body that is not JSON.

    Jira's own errors are JSON, but not every error on this path comes from
    Jira. A 429 is answered by Tomcat *before* the application is reached, as an
    HTML status page; the PAT endpoint answers an unauthenticated call in XML.
    Collapsing either one and clipping to 300 characters yields a screenful of
    stylesheet with the actual message still off the end, so pull out the
    elements that carry the text: <title>/<h1> for HTML, <message> for XML.

    An empty or whitespace-only <title> falls through to <h1>, which is what
    Tomcat's 429 page needs: its <title> is the status line, its <h1> is the
    sentence. Nested markup inside the element is stripped, not skipped.
    """
    body = (body or "")[:_MARKUP_SCAN]
    for tag in ("title", "h1", "message"):
        inner = _element_text(body, tag)
        if inner is not None:
            out = collapse(html.unescape(_TAG_RE.sub(" ", inner)), _MSG_CLIP)
            if out:
                return out
    if "<" in body and ">" in body:          # some other markup — strip the tags
        return (collapse(html.unescape(_TAG_RE.sub(" ", body)), _MSG_CLIP)
                or "(markup response with no readable text)")
    return collapse(body, _MSG_CLIP) or "(empty response body)"


def extract_message(body: str) -> str:
    """The human sentence out of a Jira error body.

    Jira uses two shapes and a blind `.get("message")` misses the important
    one: 400/404 carry {"errorMessages": [...], "errors": {...}} while 401
    carries {"message": "...", "status-code": 401}. A body that is not JSON at
    all is a third case — see markup_message().
    """
    try:
        d = json.loads(body)
    except Exception:  # noqa: BLE001
        return markup_message(body or "")
    if isinstance(d, dict):
        parts = [str(m) for m in (d.get("errorMessages") or []) if m]
        parts += [f"{k}: {v}" for k, v in (d.get("errors") or {}).items()]
        if parts:
            return "  ".join(parts)
        if d.get("message"):
            return str(d["message"])
    return collapse(body, _MSG_CLIP) or "(empty response body)"


# --------------------------------------------------------------------------
# identity, login, and the commands that need no search
# --------------------------------------------------------------------------

def print_identity(me: dict, source: str) -> None:
    """/rest/api/2/myself has no `username` field — the login name is `name`."""
    email = me.get("emailAddress") or me.get("name") or "?"
    print(f"{me.get('displayName') or '?'} <{email}>")
    print(f"  instance: {BASE}")
    print(f"  account:  {me.get('name') or '?'} (key {me.get('key') or '?'})")
    print(f"  token:    {source}")
    print("  note:     search results are filtered by your own permissions —")
    print("            another account sees a different set of projects.")
    print("  note:     a Jira PAT carries this account's FULL permissions,")
    print("            including write. This tool only ever reads.")


def collect_token(from_file: str) -> str:
    """The token, from a file, a pipe, or a hidden prompt. Never echoed."""
    if from_file:
        try:
            with open(from_file) as f:
                raw = f.read()
        except OSError as e:
            sys.exit(f"error: cannot read {from_file}: {e}")
    elif not sys.stdin.isatty():
        # `pass show jira | jqlsearch.py login` — and how install.sh forwards
        # a piped token.
        raw = sys.stdin.read()
    else:
        print(MINT_HELP)
        print()
        try:
            raw = getpass.getpass("Paste token (input hidden, Enter when done): ")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("error: aborted; nothing was written.")
    tok = raw.strip()
    if not tok:
        sys.exit("error: empty token; nothing was written.")
    return tok


def write_token_file(path: str, token: str, force: bool) -> None:
    """Write the token so it is never, even briefly, readable by anyone else.

    O_CREAT|O_EXCL with mode 600 rather than write-then-chmod: on a shared
    filesystem the gap between those two is a window in which the token is
    world-readable. --force unlinks first so the new file really is created
    here — O_CREAT leaves an existing file's mode alone.
    """
    if os.path.lexists(path) and not force:
        sys.exit(f"error: {path} already exists; nothing was written.\n"
                 f"  re-run with --force to replace it "
                 f"(the old token is not recoverable afterwards).")
    old_umask = os.umask(0o077)          # covers the parent dirs makedirs creates
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, mode=0o700, exist_ok=True)
        if os.path.lexists(path):
            os.unlink(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
    except OSError as e:
        sys.exit(f"error: cannot write {path}: {e}")
    finally:
        os.umask(old_umask)


def cmd_login(args) -> int:
    """Install this user's token.

    Lives in this script rather than only in install.sh because this script is
    the one file that exists wherever the skill is deployed; install.sh stays
    behind in the repo, which end users of a central deployment never see.
    """
    token = collect_token(args.from_file)
    write_token_file(TOKEN_FILE, token, args.force)
    # flush: the verify call below writes to stderr, and a piped install log
    # otherwise shows the failure before the write it refers to.
    print(f"wrote {TOKEN_FILE} (mode 600)", flush=True)

    if os.environ.get("JIRA_TOKEN"):
        print("warning: $JIRA_TOKEN is set and takes precedence over the file.\n"
              "         Unset it, or the token just written is ignored.",
              file=sys.stderr)

    if args.no_verify:
        return 0
    try:
        me = Client(verbose=args.verbose, token=token).get(f"{API}/myself")
    except SystemExit as e:      # Client._send() exits on HTTP/TLS failure
        print(f"\n{e}", file=sys.stderr)
        print(f"\nwarning: {TOKEN_FILE} was written, but the check above failed.\n"
              f"         The token may be wrong (a truncated paste is the usual\n"
              f"         cause) — or this instance rate-limited the check; it\n"
              f"         does that readily. Try `{SCRIPT} whoami` in a minute.",
              file=sys.stderr)
        return 1
    print()
    print_identity(me, TOKEN_FILE)
    return 0


def cmd_whoami(args) -> int:
    """One cheap call that proves the token works and says who it belongs to.

    Also the cheapest way to catch a dud token on this instance: an
    unauthenticated GET here is answered 401, and a bad Bearer elsewhere can be
    answered 200-as-anonymous, which Client._send() rejects.
    """
    me = Client(verbose=args.verbose).get(f"{API}/myself")
    print_identity(me, token_source())
    return 0


def cmd_projects(args) -> int:
    """Every project this token can see — the way to find a project KEY.

    /rest/api/2/project answers a bare JSON array of every visible project:
    there is no {"values": ..., "isLast": ...} envelope and nothing to page
    through, so one call is the whole answer.
    """
    rows = Client(verbose=args.verbose).get(f"{API}/project")
    if not isinstance(rows, list):
        sys.exit(f"unexpected response from {API}/project: {str(rows)[:200]}")
    total = len(rows)
    if args.filter:
        needle = args.filter.lower()
        rows = [r for r in rows if needle in json.dumps(r).lower()]
    rows.sort(key=lambda r: (r.get("key") or "").upper())
    if args.json:
        json.dump(rows, sys.stdout, indent=2)
        print()
        return 0
    for r in rows:
        print(f"{r.get('key', ''):<16} {r.get('projectTypeKey', ''):<12} "
              f"{r.get('name', '')}")
    if args.filter:
        print(f"\n{len(rows)} of {total} projects visible to this token "
              f"match {args.filter!r}")
    else:
        print(f"\n{total} projects visible to this token")
    return 0


# --------------------------------------------------------------------------
# formatting
# --------------------------------------------------------------------------

def collapse(text: str, limit: int = 0) -> str:
    """Whitespace-collapsed, optionally clipped. Jira text is full of newlines."""
    out = " ".join((text or "").split())
    if limit and len(out) > limit:
        out = out[:limit].rstrip() + "..."
    return out


def named(v) -> str:
    """Jira nests almost every scalar one level down: {"name": "Bug"}."""
    if isinstance(v, dict):
        return v.get("name") or v.get("displayName") or v.get("key") or ""
    return str(v) if v else ""


def browse_url(key: str) -> str:
    """The human URL for an issue.

    Constructed, not read off the response: an issue's `self` is the REST URL
    (/rest/api/2/issue/12345), which is not what a person wants pasted into
    chat, and Jira search returns no browse link of its own.
    """
    return f"{BASE}/browse/{key}"


def print_hit(i: int, issue: dict) -> None:
    """One search hit. Always the key and always the browse URL.

    Jira search has no `excerpt` field — unlike Confluence's, which ships a
    highlighted snippet with every hit. Everything printed here comes out of
    the `fields` that were requested, so a description line appears only when
    `description` was among them (--excerpt, or --fields ...,description).
    """
    f = issue.get("fields") or {}
    key = issue.get("key") or "?"
    print(f"\n[{i}] {key}  {collapse(f.get('summary')) or '(no summary)'}")
    proj = f.get("project") or {}
    meta = [named(f.get("issuetype")), named(f.get("status"))]
    if isinstance(proj, dict) and proj.get("key"):
        meta.append(f"project={proj['key']}")
    if f.get("priority"):
        meta.append(f"priority={named(f.get('priority'))}")
    if f.get("resolution"):
        meta.append(f"resolution={named(f.get('resolution'))}")
    if f.get("updated"):
        meta.append(f"updated {str(f['updated'])[:10]}")
    if "assignee" in f:
        meta.append(f"assignee={named(f.get('assignee')) or 'unassigned'}")
    meta = [b for b in meta if b]        # --fields can leave nothing to say
    if meta:
        print(f"    {'  |  '.join(meta)}")
    print(f"    {browse_url(key)}")
    desc = collapse(f.get("description") or "", 300)
    if desc:
        print(f"    {desc}")


# --------------------------------------------------------------------------
# the search commands
# --------------------------------------------------------------------------

def run_search(args, jql: str) -> int:
    """Shared by `search` (raw JQL) and `text` (JQL built for you)."""
    client = Client(verbose=args.verbose)
    fields = [x.strip() for x in (args.fields or DEFAULT_FIELDS).split(",")
              if x.strip()]
    if args.excerpt and "description" not in fields:
        fields.append("description")

    # --limit is the number of rows you get back, always. The server caps one
    # response at MAX_LIMIT, so a bigger --limit only means anything together
    # with --all, which pages until it is filled. --limit 0 means "no cap",
    # which is the only way to ask for all 12,914 of something.
    cap = max(0, args.limit)
    data = client.search(jql, fields, args.start,
                         min(cap, MAX_LIMIT) if cap else MAX_LIMIT)
    issues = data.get("issues", []) or []
    total = data.get("total")

    if args.all and total:
        avail = max(0, total - args.start)
        want = min(cap, avail) if cap else avail
        start = args.start + len(issues)
        while len(issues) < want and start < total:
            time.sleep(PAGE_DELAY)          # FillRate is 5/s; stay well under
            batch = (client.search(jql, fields, start,
                                   min(want - len(issues), MAX_LIMIT))
                     .get("issues", []) or [])
            if not batch:
                break
            issues.extend(batch)
            start += len(batch)
        issues = issues[:want]

    if args.json:
        json.dump({"jql": jql, "total": total, "returned": len(issues),
                   "issues": issues}, sys.stdout, indent=2)
        print()
        return 0

    print(f"jql: {jql}")
    print(f"{total} match(es); showing {len(issues)}")
    for i, issue in enumerate(issues, 1):
        print_hit(i, issue)
    # What is left *after* this window, not total-minus-shown: with --start 40
    # of 42 there are two rows shown and nothing more, not forty more.
    seen = args.start + len(issues)
    if total and total > seen:
        # Do not suggest --all to someone who already passed it.
        nxt = "--limit 0" if args.all else "--all"
        print(f"\n... {total - seen} more; use --start {seen} or {nxt}")
    return 0


def cmd_search(args) -> int:
    return run_search(args, args.jql)


# A JQL function call — currentUser(), startOfMonth(), now() — and a relative
# date like -30d or 2w. Both are syntax, not data: quoting either turns it into
# a literal string and the query then matches nothing (or 400s).
JQL_FUNC_RE = re.compile(r"^[A-Za-z]\w*\([^()]*\)$")
JQL_RELDATE_RE = re.compile(r"^-?\d+[mhdwMy]$")


def jql_value(v: str) -> str:
    """A user-supplied value, safe to paste into a JQL clause.

    Quoted by default — `status = "In Review"` needs it and `issuetype = "Bug"`
    is harmless — with the two syntactic forms above left bare. Embedded quotes
    are backslash-escaped, which is what this JQL parser expects.
    """
    v = v.strip()
    if JQL_FUNC_RE.match(v) or JQL_RELDATE_RE.match(v):
        return v
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def cmd_text(args) -> int:
    """Convenience wrapper that builds the JQL for you.

    The built query is printed as the first line of the output, so a user who
    wants to widen or tweak it can paste it straight into `search`.
    """
    clauses = [f'{args.field} ~ {jql_value(" ".join(args.words))}']
    if args.project:
        keys = [jql_value(k) for k in args.project]
        clauses.append(f"project = {keys[0]}" if len(keys) == 1
                       else f"project in ({', '.join(keys)})")
    if args.type:
        clauses.append(f"issuetype = {jql_value(args.type)}")
    if args.status:
        clauses.append(f"status = {jql_value(args.status)}")
    if args.assignee:
        clauses.append(f"assignee = {jql_value(args.assignee)}")
    if args.since:
        # argparse rejects a bare `-30d` — it is not a negative number, so it
        # is taken for an unknown flag. `30d` is therefore accepted and given
        # its sign here; `--since=-30d` (with the equals sign) works too.
        since = args.since.strip()
        if re.match(r"^\d+[mhdwMy]$", since):
            since = "-" + since
        clauses.append(f"updated > {jql_value(since)}")
    if args.open:
        # Unquoted on purpose: Unresolved is a JQL keyword here, not a
        # resolution named "Unresolved" — quoting it is an error.
        clauses.append("resolution = Unresolved")
    jql = " AND ".join(clauses)
    if args.recent:
        jql += " ORDER BY updated DESC"
    return run_search(args, jql)


# A Jira issue key (FERMIS3DF-43), bare or inside a URL. Both URL shapes this
# instance produces are covered: /browse/KEY, and the ?selectedIssue=KEY that
# the board and backlog views put in the address bar.
ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
ISSUE_IN_URL_RE = re.compile(r"(?:/browse/|selectedIssue=)([A-Za-z][A-Za-z0-9_]*-\d+)")


def parse_issue_key(target: str) -> str:
    t = (target or "").strip()
    m = ISSUE_IN_URL_RE.search(t)
    if m:
        return m.group(1).upper()
    if ISSUE_KEY_RE.match(t):
        return t.upper()
    sys.exit(f"error: {target!r} is not an issue key or issue URL.\n"
             f"  expected PROJECT-123, {BASE}/browse/PROJECT-123,\n"
             f"  or a board URL containing ?selectedIssue=PROJECT-123.\n"
             f"  to search by words instead: {SCRIPT} text \"{target}\"")


def stamp(ts: str) -> str:
    """2026-09-04T12:33:41.000-0700 -> 2026-09-04 12:33:41."""
    return str(ts or "")[:19].replace("T", " ")


def cmd_issue(args) -> int:
    """One issue, whole: header, description, and the comment thread.

    Search hits are one line of metadata; anything a person actually asked
    about lives in the description and the comments, so fetch this before
    answering from a summary line.
    """
    key = parse_issue_key(args.target)
    params = {"fields": ISSUE_FIELDS}
    if args.rendered:
        # renderedFields is the server's own wiki-markup -> HTML pass. Useful
        # when a table or a macro matters; the raw markup is the default.
        params["expand"] = "renderedFields"
    data = Client(verbose=args.verbose).get(f"{API}/issue/{key}", params)

    if args.json:
        json.dump(data, sys.stdout, indent=2)
        print()
        return 0

    f = data.get("fields") or {}
    rendered = data.get("renderedFields") or {}
    proj = f.get("project") or {}
    lines = [f"# {data.get('key', key)}  {collapse(f.get('summary'))}", ""]
    rows = [("type", named(f.get("issuetype"))),
            ("status", named(f.get("status"))),
            ("resolution", named(f.get("resolution")) or "Unresolved"),
            ("project", collapse(f"{proj.get('key', '')} — "
                                 f"{proj.get('name', '')}")),
            ("priority", named(f.get("priority"))),
            ("labels", ", ".join(f.get("labels") or [])),
            ("components", ", ".join(named(c) for c in (f.get("components") or []))),
            ("fixVersions", ", ".join(named(v) for v in (f.get("fixVersions") or []))),
            ("assignee", named(f.get("assignee")) or "unassigned"),
            ("reporter", named(f.get("reporter"))),
            ("created", stamp(f.get("created"))),
            ("updated", stamp(f.get("updated"))),
            ("url", browse_url(data.get("key", key)))]
    lines += [f"- {k}: {v}" for k, v in rows if v]
    lines += ["", "---", "", "## Description", ""]
    desc = (rendered.get("description") if args.rendered
            else f.get("description")) or "(no description)"
    # Verbatim. This instance stores descriptions as Jira *wiki markup* (h2.,
    # {code}, [text|url], * bullets) — not HTML and not Markdown. Converting it
    # would destroy link targets, heading levels and code-block boundaries,
    # which is exactly what an agent reading this needs; --rendered asks the
    # server for HTML when that is wanted instead.
    lines += [desc.rstrip()]

    comments = ((f.get("comment") or {}).get("comments") or [])
    rendered_comments = ((rendered.get("comment") or {}).get("comments") or [])
    if not args.no_comments:
        shown = comments
        skipped = 0
        if args.comments is not None and args.comments < len(comments):
            skipped = len(comments) - args.comments      # keep the newest ones
            shown = comments[skipped:]
        lines += ["", f"## Comments ({len(comments)})"]
        if skipped:
            lines += [f"", f"(showing the {len(shown)} most recent; "
                           f"{skipped} older not shown — raise --comments)"]
        for i, c in enumerate(shown, skipped + 1):
            body = c.get("body") or ""
            if args.rendered:
                match = next((rc for rc in rendered_comments
                              if rc.get("id") == c.get("id")), None)
                body = (match or {}).get("body") or body
            who = (c.get("author") or {}).get("displayName") or "?"
            lines += ["", f"### {i}. {who} — {stamp(c.get('created'))}", "",
                      body.rstrip()]

    text = "\n".join(lines) + "\n"
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text)
        print(f"wrote {args.out} ({len(text)} chars)")
    else:
        sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jqlsearch",
        description="Query SLAC Jira live via the JQL search API (read-only).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="progress on stderr: rate-limit headers per response "
                        "and every 429 backoff. Accepted before or after the "
                        "subcommand.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_search_flags(sp):
        sp.add_argument("--limit", type=int, default=25,
                        help=f"rows to return (default 25). One response is "
                             f"capped at {MAX_LIMIT} by the server, so a "
                             f"larger --limit needs --all. 0 = no cap.")
        sp.add_argument("--start", type=int, default=0, help="startAt offset")
        sp.add_argument("--all", action="store_true",
                        help="keep paging until --limit rows are collected "
                             "(with --limit 0, until there are no more)")
        sp.add_argument("--json", action="store_true", help="raw JSON results")
        sp.add_argument("--fields", default="",
                        help=f"comma-separated field list to request "
                             f"(default {DEFAULT_FIELDS})")
        sp.add_argument("--excerpt", action="store_true",
                        help="also request `description` and print ~300 chars "
                             "of it under each hit (Jira sends no excerpt)")

    se = sub.add_parser("search", help="run a raw JQL query")
    se.add_argument("jql")
    add_search_flags(se)
    se.set_defaults(func=cmd_search)

    t = sub.add_parser("text", help="free-text search; builds the JQL for you")
    t.add_argument("words", nargs="+", help='e.g. "detector calibration"')
    t.add_argument("--project", action="append", metavar="KEY",
                   help="project key; repeatable (see `projects`)")
    t.add_argument("--type", help="issue type: Bug, Task, Story, Epic, ...")
    t.add_argument("--status", help='e.g. "To Do", "In Progress", Done')
    t.add_argument("--assignee",
                   help="account name, or currentUser() for yourself")
    t.add_argument("--since", metavar="WHEN",
                   help='updated after: "2026-01-01", 30d (i.e. -30d; a bare '
                        '-30d is eaten by argparse), startOfMonth()')
    t.add_argument("--open", action="store_true",
                   help="unresolved issues only (resolution = Unresolved)")
    t.add_argument("--field", default="text",
                   choices=["text", "summary", "description", "comment"],
                   help="text (default) spans summary, description, "
                        "environment and comments")
    t.add_argument("--recent", action="store_true",
                   help="ORDER BY updated DESC instead of relevance")
    add_search_flags(t)
    t.set_defaults(func=cmd_text)

    g = sub.add_parser("issue", help="fetch one issue: header, description, comments")
    g.add_argument("target", metavar="KEY|URL",
                   help="FERMIS3DF-43, or a /browse/ or ?selectedIssue= URL")
    g.add_argument("--json", action="store_true", help="raw JSON")
    g.add_argument("--no-comments", action="store_true",
                   help="header and description only")
    g.add_argument("--comments", type=int, metavar="N",
                   help="show only the N most recent comments (default: all)")
    g.add_argument("--rendered", action="store_true",
                   help="server-rendered HTML instead of raw wiki markup")
    g.add_argument("--out", metavar="FILE",
                   help="write to FILE instead of stdout")
    g.set_defaults(func=cmd_issue)

    lg = sub.add_parser("login", help=f"install your token into {TOKEN_FILE}")
    lg.add_argument("--from", dest="from_file", metavar="FILE",
                    help="read the token from FILE instead of prompting")
    lg.add_argument("--force", action="store_true",
                    help="replace an existing token file")
    lg.add_argument("--no-verify", action="store_true",
                    help="skip the one live call that confirms the token works")
    lg.set_defaults(func=cmd_login)

    w = sub.add_parser("whoami", help="who this token authenticates as")
    w.set_defaults(func=cmd_whoami)

    pr = sub.add_parser("projects", help="list projects this token can see")
    pr.add_argument("--filter", help="substring filter, matched against the "
                                     "whole project record (key, name, lead)")
    pr.add_argument("--json", action="store_true", help="raw JSON")
    pr.set_defaults(func=cmd_projects)

    # -v is a top-level flag, but `text timing -v` is what a person actually
    # types, and argparse answers that with `unrecognized arguments: -v`. Give
    # every subcommand its own copy so both positions work.
    #
    # default=argparse.SUPPRESS is load-bearing. Since Python 3.7 a subparser
    # parses into a *fresh* namespace and copies every key it holds back over
    # the parent's, so a plain `default=False` here would silently undo the True
    # that `jqlsearch.py -v text timing` had already set. SUPPRESS leaves the
    # key absent unless the flag was actually given, so the parent's value
    # survives and either position — or both — turns verbosity on.
    for sp in sub.choices.values():
        sp.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS,
                        help="progress and rate-limit headers on stderr")

    return p


def _devnull_stdout() -> None:
    """Point fd 1 at /dev/null. Called once a write to stdout has already failed.

    Python flushes sys.stdout during interpreter shutdown, and a flush to a pipe
    whose reader has gone prints `Exception ignored in: <_io.TextIOWrapper
    name='<stdout>'>` *after* the program has otherwise finished — the exact
    noise all of this exists to suppress. Redirecting the fd gives that flush
    somewhere harmless to land.

    os.open() returns a descriptor of its own and os.dup2() only duplicates it
    onto fd 1, so the original is spare and is closed here rather than leaked.
    """
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:                                    # pragma: no cover
        return
    try:
        os.dup2(fd, sys.stdout.fileno())
    except OSError:                                    # pragma: no cover
        pass
    finally:
        os.close(fd)


def _flush_stdout_quietly() -> None:
    """Flush stdout at exit, and swallow the broken pipe if there is one.

    Registered with atexit, which runs before the interpreter's own flush of the
    standard streams. That ordering is what makes this cover the paths main()
    cannot: `--help` prints and then raises SystemExit from inside argparse, and
    every sys.exit("error: ...") in this file leaves buffered stdout behind the
    same way.
    """
    try:
        sys.stdout.flush()
    except (BrokenPipeError, ValueError, OSError):
        _devnull_stdout()


atexit.register(_flush_stdout_quietly)


def main(argv=None) -> int:
    # `projects | head -1` and `text foo | grep bar` are how this tool is
    # actually used, and the reader exits first every time. Python's default
    # turns the resulting EPIPE into a BrokenPipeError traceback *and* a second
    # complaint when the interpreter flushes stdout on the way out — eight lines
    # of noise and a non-zero exit for something that is not an error.
    #
    # Emphatically NOT `signal.signal(SIGPIPE, SIG_DFL)`. A signal disposition is
    # process-global: it applies to every descriptor, the TLS socket included, so
    # an EPIPE on a socket write stops being a catchable BrokenPipeError and
    # becomes death by signal 13 with nothing printed at all — measured, exit
    # status -13 and an empty stderr, on the one path where an explanation
    # matters most. Catching BrokenPipeError is what the signal module's own
    # "Note on SIGPIPE" recommends, and unlike the disposition it affects only
    # the pipe that actually broke.
    try:
        args = build_parser().parse_args(argv)
        code = args.func(args)
        sys.stdout.flush()          # here, where the failure is still catchable
        return code
    except BrokenPipeError:
        _devnull_stdout()
        return 141                  # 128 + SIGPIPE, as a shell reports it


if __name__ == "__main__":
    sys.exit(main())
