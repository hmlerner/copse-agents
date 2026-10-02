"""The ``pro`` plugin in copse's ``copse.account`` entry-point group:
``copse account [features]|login|logout|status|upgrade|portal|org|license``."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from copse.pro import auth, credentials, license, loopback

PRICING_URL = "https://pawdelta.com/copse#pricing"

# The paid features: (entitlement feature, cheapest plan with it, what it is, how to use it).
FEATURES = (
    ("learning", "pro", "hosted learning: picks the best profile per task",
     "on by itself; see `copse learning`"),
    ("services", "pro", "per-worktree Docker services (db, cache)",
     '"services" in .copse/config.json'),
    ("team", "team", "org policies + team audit feed", "`copse account org policy`"),
    ("ci", "team", "Copse-CI: issues into pull requests", "`copse ci init`"),
    ("audit", "enterprise", "tamper-evident local audit log", "`copse audit verify`"),
    ("airgap", "enterprise", "air-gapped mode, local models only",
     '"airgap": true in .copse/config.json'),
)

USAGE = """usage: copse account [<command>] [--base-url URL]

  (none)    what copse Pro/Team add, which you have, and how to get the rest
  features  the same
  login     log in to copse Pro: opens your browser and waits for it to come back
  login --device
            show a code to enter in a browser elsewhere instead (SSH, no browser here)
  logout    revoke this device's session and forget its credentials
  status    show your account, plan, features and when the entitlement expires
  upgrade   open the checkout for copse Pro (your personal org); prints the URL too
  upgrade --team --seats N [--org ORG]
            print the checkout URL for copse Team on a team org you administer
            (default: the current org)
  portal [--org ORG]  open the billing portal (invoices, seats, cancellation)
  sync      sync your user-wide settings (~/.copse/config.json) with your account now
  org list          list the orgs you belong to
  org create <name> create a team org you own (then `upgrade --team`)
  org invite <email> [--admin]  invite someone to the current org; prints the code
  org join <code>   accept an invite code and join that org
  org use <org_id>  work as a member of <org_id> (`personal` for your own)
  org policy        show the current org's policy (and refresh the cached copy)
  org ci-token create <name> [--org ORG]
                    create a CI token for `copse ci run` (admin+); shown once
  org ci-token list [--org ORG]           list the org's CI tokens
  org ci-token revoke <token_id> [--org ORG]  revoke a CI token
  license install <file>  install an offline (copse Enterprise) license; verified with the
                    pinned keys, no network; used in air-gap mode and when not logged in
  license status    show the installed offline license and the air-gap status
  license remove    remove the installed offline license"""


def _take(args: list[str], flag: str, value: bool = True):
    """Remove ``flag`` (and its value) from ``args``; return the value, True
    for a bare flag, or None if absent. Raises ValueError if the value is missing."""
    if flag not in args:
        return None
    i = args.index(flag)
    if not value:
        del args[i]
        return True
    if i + 1 >= len(args) or args[i + 1].startswith("--"):
        raise ValueError(flag)
    v = args[i + 1]
    del args[i:i + 2]
    return v


def _when(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class _OrgCommands:
    def cmd_org_list(self, base: str | None) -> int:
        client = self._client(base)
        status, body = auth.authed(client, self.store, "GET", "/orgs")
        if status != 200:
            raise auth._error(status, body)
        current = self._current_org()
        orgs = body.get("orgs") if isinstance(body.get("orgs"), list) else []
        if not orgs:
            self._say("You don't belong to any orgs.")
            return 0
        for o in orgs:
            if not isinstance(o, dict):
                continue
            org_id = auth._sanitize(o.get("org_id", ""), 64)
            mark = "*" if org_id == current else " "
            kind = "personal" if o.get("personal") else "team"
            self._say(f"{mark} {org_id:<28} {auth._sanitize(o.get('name', ''), 40):<32} "
                      f"{kind:<8} {auth._sanitize(o.get('role', ''), 8):<7} "
                      f"{auth._sanitize(o.get('plan', ''), 12)}")
        return 0

    def _current_org(self) -> str | None:
        try:
            creds = self.store.load() or {}
        except credentials.CredentialError:
            return None
        if creds.get("org_id"):
            return creds["org_id"]
        try:
            return license.verify(creds.get("entitlement") or "", issuer=auth.base_url(
                creds.get("base_url")), grace=license.MAX_GRACE).org_id
        except (license.LicenseError, auth.AuthError):
            return None

    def _team_org(self, org: str | None) -> str:
        """The team org a command acts on: ``org`` or the current one."""
        org = org or (self.store.load() or {}).get("org_id")
        if not org:
            raise auth.AuthError("no team org selected: pass --org ORG or run "
                                 "`copse account org use <org_id>` (see `copse account org list`)",
                                 code="no_team_org")
        if not auth.ORG_ID_RE.match(org):
            raise auth.AuthError("invalid org id", code="bad_request")
        return org

    def cmd_org_create(self, base: str | None, *name_words: str) -> int:
        name = " ".join(name_words).strip()
        if not name or len(name) > 100:
            raise auth.AuthError("org name must be 1-100 characters", code="bad_request")
        org = auth.create_org(self._client(base), self.store, name)
        self._say(f"Created team org {org['org_id']} ({org['name']}); you are its owner.")
        self._say(f"Next: copse account upgrade --team --seats N --org {org['org_id']}")
        return 0

    def cmd_org_invite(self, base: str | None, email: str, org: str | None = None,
                       admin: bool = False) -> int:
        org_id = self._team_org(org)
        inv = auth.create_invite(self._client(base), self.store, org_id, email,
                                 "admin" if admin else "member")
        self._say(f"Invited {inv['email']} to {inv['org_id']} as {inv['role']}.")
        self._say(f"Send them this command (the code is shown once): "
                  f"copse account org join {inv['invite_code']}")
        return 0

    def cmd_org_join(self, base: str | None, code: str) -> int:
        got = auth.accept_invite(self._client(base), self.store, code.strip())
        self._say(f"Joined {got['org_id']} as {got['role']}. "
                  f"Run `copse account org use {got['org_id']}` to work as a member.")
        return 0

    def cmd_org_ci_token(self, base: str | None, action: str, *rest: str, org: str | None = None) -> int:
        org_id = self._team_org(org)
        client = self._client(base)
        if action == "create":
            name = " ".join(rest).strip()
            if not name or len(name) > 100:
                raise auth.AuthError("CI token name must be 1-100 characters", code="bad_request")
            got = auth.create_ci_token(client, self.store, org_id, name)
            self._say(f"Created CI token {got['token_id']} ({got['name']}) for {got['org_id']}.")
            self._say("Store it as the COPSE_PRO_TOKEN repository secret (it is shown once):")
            self._say(f"  {got['token']}")
            self._say("  e.g. gh secret set COPSE_PRO_TOKEN   (then paste it)")
            return 0
        if action == "list":
            tokens = auth.list_ci_tokens(client, self.store, org_id)
            if not tokens:
                self._say(f"Org {org_id} has no CI tokens.")
                return 0
            for t in tokens:
                self._say(f"{t['token_id']:<36} {t['name'][:32]:<32} {t['status']:<8} "
                          f"created {_when(t['created_at'])} by {t['created_by']}, "
                          f"last used {_when(t['last_used_at'])}")
            return 0
        auth.revoke_ci_token(client, self.store, org_id, rest[0])
        self._say(f"Revoked CI token {rest[0]}; runs using it stop at their next start.")
        return 0

    def cmd_org_use(self, base: str | None, org_id: str) -> int:
        target = None if org_id == "personal" else org_id
        ent = auth.switch_org(self._client(base), self.store, target)
        self._say(f"Now using org {ent.org_id} as {ent.role or 'member'} (plan {ent.plan}).")
        return 0

    def cmd_org_policy(self, base: str | None) -> int:
        from copse.pro import team_policy

        client = self._client(base)
        ent = license.current(store=self.store, client=client)
        if "team" not in ent.features:
            self._say(f"Org {ent.org_id} has no team policy (plan {ent.plan}); nothing is enforced.")
            return 0
        try:
            p = team_policy.fetch_policy(ent.org_id, client, self.store)
            note = ""
        except team_policy.PolicyUnavailable as e:
            p = team_policy.load_cached(ent.org_id)
            if p is None:
                self._say(f"copse account: couldn't fetch the policy for {ent.org_id} ({e}), "
                          "and none is cached; delegations and merges are refused until it is.")
                return 1
            note = f" (cached; couldn't refresh: {e})"
        fmt = lambda v: "any" if v is None else (", ".join(v) or "none")  # noqa: E731
        self._say(f"Policy for org {p.org_id}, version {p.version}{note}")
        self._say(f"  providers              {fmt(p.allowed_providers)}")
        self._say(f"  models                 {fmt(p.allowed_models)}")
        self._say(f"  require human review   {'yes' if p.require_human_review else 'no'}")
        self._say(f"  max parallel workers   {p.max_parallel_workers or 'no limit'}")
        return 0


class ProAccount(_OrgCommands):
    def __init__(self, repo_root: str | None = None, store=None, transport=None,
                 out=None, err=None) -> None:
        self.repo_root = repo_root
        self._store, self.transport = store, transport
        self.out, self.err = out or sys.stdout, err or sys.stderr

    @property
    def store(self):
        if self._store is None:
            self._store = credentials.default_store()
        return self._store

    def _say(self, msg: str) -> None:
        print(msg, file=self.out)

    def _client(self, base: str | None) -> auth.Client:
        if base is None:
            try:
                base = (self.store.load() or {}).get("base_url")
            except credentials.CredentialError:
                base = None
        return auth.Client(base, transport=self.transport)

    def run(self, args: list[str]) -> int:
        args = list(args)
        base = None
        if "--base-url" in args:
            i = args.index("--base-url")
            if i + 1 >= len(args):
                print(USAGE, file=self.err)
                return 2
            base = args[i + 1]
            del args[i:i + 2]
        if args and args[0] in ("-h", "--help"):
            print(USAGE, file=self.out)
            return 0
        try:
            opts = {"team": _take(args, "--team", value=False), "seats": _take(args, "--seats"),
                    "org": _take(args, "--org"), "admin": _take(args, "--admin", value=False),
                    "device": _take(args, "--device", value=False)}
            seats = int(opts["seats"]) if opts["seats"] is not None else None
        except ValueError:
            print(USAGE, file=self.err)
            return 2
        cmd, rest = (args[0] if args else "features"), args[1:]
        sub = None
        if cmd == "org":
            sub = rest[0] if rest else "list"
        elif cmd == "license":
            sub = rest[0] if rest else "status"
        ok = {
            "features": not rest, "login": not rest, "logout": not rest, "status": not rest,
            "upgrade": not rest and (bool(opts["team"]) == (seats is not None)) and (seats or 1) >= 1
            and (opts["org"] is None or bool(opts["team"])),
            "portal": not rest, "sync": not rest,
            "org": (sub in ("list", "policy") and len(rest) <= 1)
            or (sub in ("use", "invite", "join") and len(rest) == 2)
            or (sub == "create" and len(rest) >= 2)
            or (sub == "ci-token" and len(rest) >= 2 and (
                (rest[1] == "create" and len(rest) >= 3) or (rest[1] == "list" and len(rest) == 2)
                or (rest[1] == "revoke" and len(rest) == 3))),
            "license": (sub in ("status", "remove") and len(rest) <= 1)
            or (sub == "install" and len(rest) == 2),
        }.get(cmd, False)
        flags_ok = {"upgrade": ("team", "seats", "org"), "portal": ("org",), "login": ("device",),
                    "org": ("org", "admin") if sub == "invite"
                    else ("org",) if sub == "ci-token" else ()}.get(cmd, ())
        if not ok or any(v is not None and k not in flags_ok for k, v in opts.items()):
            print(USAGE, file=self.err)
            return 2
        try:
            if cmd == "license":
                return getattr(self, "cmd_license_" + sub)(base, *rest[1:])
            if cmd == "org":
                if sub == "invite":
                    return self.cmd_org_invite(base, rest[1], opts["org"], bool(opts["admin"]))
                if sub == "ci-token":
                    return self.cmd_org_ci_token(base, *rest[1:], org=opts["org"])
                return getattr(self, "cmd_org_" + sub)(base, *rest[1:])
            if cmd == "upgrade":
                return self.cmd_upgrade(base, team=bool(opts["team"]), seats=seats, org=opts["org"])
            if cmd == "portal":
                return self.cmd_portal(base, org=opts["org"])
            if cmd == "login":
                return self.cmd_login(base, device=bool(opts["device"]))
            return getattr(self, "cmd_" + cmd)(base)
        except (auth.AuthError, license.LicenseError, credentials.CredentialError) as e:
            print(f"copse account: {e}", file=self.err)
            return 1

    def _open(self, url: str) -> None:
        """Print ``url``; also open it in the browser when talking to a terminal."""
        self._say(url)
        if getattr(self.out, "isatty", lambda: False)():
            import webbrowser

            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - the printed URL is enough
                pass

    def cmd_sync(self, base: str | None) -> int:
        from copse.pro import settings_sync

        r = settings_sync.sync(client=self._client(base), store=self.store)
        if r.action == "skipped":
            self._say(f"Settings not synced: {r.reason}")
            return 0
        verb = {"pulled": "Updated from your account", "pushed": "Sent to your account",
                "unchanged": "Settings already in sync"}[r.action]
        self._say(verb + (":" if r.changes else "."))
        for key, (old, new) in sorted(r.changes.items()):
            self._say(f"  {key}: {'-' if old is None else old} -> {'-' if new is None else new}")
        return 0

    def cmd_features(self, base: str | None) -> int:
        try:
            ent = license.current(store=self.store, client=self._client(base))
            err = None
        except (license.LicenseError, auth.AuthError, credentials.CredentialError) as e:
            ent, err = None, e
        have = ent.features if ent else frozenset()
        if ent:
            self._say(f"copse {ent.plan.capitalize()}: org {ent.org_id}"
                      + (" (offline grace)" if ent.in_grace else ""))
        elif err and "not logged in" not in str(err):
            self._say(f"copse Pro: {err}")
        else:
            self._say("copse Pro: not logged in. copse is complete without it; paid plans add:")
        self._say("")
        for feature, plan, what, how in FEATURES:
            if feature in have:
                self._say(f"  ✓ {feature:<9} {what:<50} {how}")
            else:
                self._say(f"    {feature:<9} {what:<50} needs {plan.capitalize()}")
        self._say("")
        if ent is None:
            self._say("Next: `copse account login`, then `copse account upgrade`. "
                      f"Plans: {PRICING_URL}")
        elif not have & {"learning", "services"}:
            self._say(f"Next: `copse account upgrade` opens the checkout. Plans: {PRICING_URL}")
        elif "team" not in have:
            self._say("Next, for a team: `copse account org create NAME`, then "
                      "`copse account upgrade --team --seats N --org ORG`. "
                      f"Plans: {PRICING_URL}")
        elif "audit" not in have:
            self._say(f"Enterprise (audit log, air-gap) is sales-led: {PRICING_URL}")
        self._say("More: `copse account status` (your plan), `copse account --help` (all commands).")
        return 0

    def cmd_login(self, base: str | None, device: bool = False) -> int:
        """Browser sign-in when a browser can open here (a terminal, not SSH,
        a display on Linux), else, or with ``--device``, the device code."""
        client = auth.Client(base, transport=self.transport)
        browser = not device and loopback.can_open_browser(self.out)
        ent = auth.login(client, self.store, show=self._say, browser=browser)
        self._say(f"Logged in as {ent.sub} ({ent.org_id}), plan {ent.plan}.")
        self._say("See what your plan includes: `copse account`"
                  + ("" if ent.features else "; get copse Pro: `copse account upgrade`"))
        return 0

    def cmd_logout(self, base: str | None) -> int:
        try:
            client = self._client(base)
        except auth.AuthError:
            client = None
        auth.logout(client, self.store)
        self._say("Logged out of copse Pro.")
        return 0

    def cmd_status(self, base: str | None) -> int:
        client = self._client(base)
        try:
            ent = license.current(store=self.store, client=client)
        except license.LicenseError as e:
            self._say(f"copse Pro: {e}")
            return 1
        try:
            who = auth.me(client, self.store)
        except auth.AuthError as e:
            who = {}
            if e.code == "airgap":
                self._say("(air-gap mode: showing the offline license)")
            elif e.code == "transport":
                self._say("(offline: showing the stored entitlement)")
            else:
                self._say(f"(could not load account details: {e.code})")
        now = time.time()
        state = "offline grace" if ent.in_grace else ent.status
        self._say(f"copse Pro: {state}")
        self._say(f"  account   {who.get('email') or ent.sub}")
        self._say(f"  org       {ent.org_id}" + (f" ({ent.role})" if ent.role else ""))
        self._say(f"  plan      {ent.plan} ({ent.seats} seat(s))")
        if who.get("plan") and who.get("plan") != ent.plan:
            self._say(f"            (account now shows plan {who['plan']}; "
                      "the entitlement updates on its next refresh)")
        self._say(f"  features  {', '.join(sorted(ent.features)) or '-'}")
        cloud = "learning" in ent.features and not ent.in_grace
        self._say(f"  learning  {'cloud (hosted learning active)' if cloud else 'off (no hosted learning)'}"
                  + ("" if cloud or 'learning' not in ent.features
                     else " -- offline; hosted learning resumes after a refresh"))
        self._say(f"  expires   {_when(ent.exp)}")
        if ent.in_grace:
            left = max(0, int((ent.grace_until or now) - now)) // 3600
            self._say(f"  grace     until {_when(ent.grace_until)} (~{left} h); "
                      "reconnect to refresh")
        self._airgap_lines()
        return 0

    # -- the offline license ----------------------------------------------------------------

    def _airgap_lines(self) -> None:
        from copse import airgap

        if not airgap.enabled():
            return
        self._say(f"  air-gap   on ({airgap.source()}): no outbound traffic, local models only")
        warning = airgap.warning()
        if warning:
            self._say(f"            ! {warning}")

    def cmd_license_install(self, base: str | None, path: str) -> int:
        from pathlib import Path

        try:
            data = Path(path).read_bytes()
        except OSError as e:
            raise license.LicenseError(f"cannot read {path}: {e.strerror or e}") from e
        if len(data) > license.MAX_LICENSE_FILE:
            raise license.LicenseError(f"{path} is too large to be a license file")
        ent = license.install(data)
        self._say(f"Installed offline license for org {ent.org_id} (plan {ent.plan}, "
                  f"{ent.seats} seat(s)); expires {_when(ent.exp)}.")
        self._say(f"  features  {', '.join(sorted(ent.features)) or '-'}")
        from copse import airgap

        if airgap.FEATURE in ent.features:
            self._say('  air-gap mode is included: turn it on with "airgap": true in '
                      f".copse/config.json or {airgap.ENV}=1")
        else:
            self._say(f"  this license doesn't include air-gap mode ({airgap.FEATURE!r})")
        return 0

    def cmd_license_status(self, base: str | None) -> int:
        try:
            ent = license.installed()
        except license.LicenseError as e:
            self._say(f"offline license: {e}")
            self._airgap_lines()
            return 1
        if ent is None:
            self._say("No offline license installed (`copse account license install <file>`).")
            self._airgap_lines()
            return 1
        state = "offline grace" if ent.in_grace else ent.status
        self._say(f"offline license: {state}")
        self._say(f"  org       {ent.org_id}")
        self._say(f"  plan      {ent.plan} ({ent.seats} seat(s))")
        self._say(f"  features  {', '.join(sorted(ent.features)) or '-'}")
        self._say(f"  expires   {_when(ent.exp)}")
        if ent.in_grace:
            self._say(f"  grace     until {_when(ent.grace_until)}; install a renewed license")
        self._airgap_lines()
        return 0

    def cmd_license_remove(self, base: str | None) -> int:
        self._say("Removed the offline license." if license.uninstall()
                  else "No offline license was installed.")
        return 0

    def cmd_upgrade(self, base: str | None, team: bool = False, seats: int | None = None,
                    org: str | None = None) -> int:
        if not team:
            self._open(auth.checkout_url(self._client(base), self.store))
            return 0
        self._open(auth.checkout_url(self._client(base), self.store, plan="team", seats=seats,
                                    org_id=self._team_org(org)))
        return 0

    def cmd_portal(self, base: str | None, org: str | None = None) -> int:
        if org is not None and not auth.ORG_ID_RE.match(org):
            raise auth.AuthError("invalid org id", code="bad_request")
        org = org or (self.store.load() or {}).get("org_id")
        self._open(auth.portal_url(self._client(base), self.store, org))
        return 0


def make(repo_root: str | None = None) -> ProAccount:
    return ProAccount(repo_root)
