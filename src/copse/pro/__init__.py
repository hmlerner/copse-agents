"""copse Pro and Team: the client side of copse's paid tiers (open source).

Licensing (offline verification of signed entitlements), the account
commands, hosted learning, and the team policy and audit-event plugins live
here, registered as copse's own entry points. Everything here is inert
without a verified entitlement: the policy plugin allows everything, the
events plugin sends nothing, and learning stays off.

What ever leaves the machine is spelled out in each module: coarse task
features (a kind and a size bucket) and keyed hashes of the repo, agent and
branch identities. Never task text, file names, paths, prompts or diffs.

Other modules gate features with::

    from copse.pro.license import features, has, require
"""

from __future__ import annotations
