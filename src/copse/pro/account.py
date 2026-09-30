"""The ``pro`` plugin in copse's ``copse.account`` entry-point group:
``copse account login|logout|status|upgrade|portal|org``."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

from copse.pro import auth, credentials, license

USAGE = """usage: copse account <command> [--base-url URL]

  login     log in to copse Pro in your browser (device code)
  logout    revoke this device's session and forget its credentials
  status    show your account, plan, features and when the entitlement expires
  upgrade   print the checkout URL for upgrading your plan
  portal    print the billing portal URL (invoices, seats, cancellation)
  org list          list the orgs you belong to
  org use <org_id>  work as a member of <org_id> (`personal` for your own)
  org policy        show the current org's policy (and refresh the cached copy)"""


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
        ok = (len(args) == 1 and args[0] in ("login", "logout", "status", "upgrade", "portal")) or \
            (args[:1] == ["org"] and (args[1:] in (["list"], ["policy"], [])
                                      or (len(args) == 3 and args[1] == "use")))
        if not ok:
            print(USAGE, file=self.err)
            return 2
        try:
            if args[0] == "org":
                sub = args[1] if len(args) > 1 else "list"
                return getattr(self, "cmd_org_" + sub)(base, *args[2:])
            return getattr(self, "cmd_" + args[0])(base)
        except (auth.AuthError, license.LicenseError, credentials.CredentialError) as e:
            print(f"copse account: {e}", file=self.err)
            return 1

    def cmd_login(self, base: str | None) -> int:
        client = auth.Client(base, transport=self.transport)
        ent = auth.login(client, self.store, show=self._say)
        self._say(f"Logged in as {ent.sub} ({ent.org_id}), plan {ent.plan}.")
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
            if e.code == "transport":
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
        self._say(f"  learning  {'cloud (hosted learning active)' if cloud else 'local only'}"
                  + ("" if cloud or 'learning' not in ent.features
                     else " -- offline; hosted learning resumes after a refresh"))
        self._say(f"  expires   {_when(ent.exp)}")
        if ent.in_grace:
            left = max(0, int((ent.grace_until or now) - now)) // 3600
            self._say(f"  grace     until {_when(ent.grace_until)} (~{left} h); "
                      "reconnect to refresh")
        return 0

    def cmd_upgrade(self, base: str | None) -> int:
        self._say(auth.checkout_url(self._client(base), self.store))
        return 0

    def cmd_portal(self, base: str | None) -> int:
        self._say(auth.portal_url(self._client(base), self.store))
        return 0


def make(repo_root: str | None = None) -> ProAccount:
    return ProAccount(repo_root)
