"""Read-only admission gate for the fixed three-account raid rehearsal."""

from .open_store import OpenError

GROUP = "1459282"
RECEIVER = "43506527"
TARGETS = ("31846829", "23691273")


async def preflight(service, actor):
    """Require two genuinely ordinary targets before any live-test permission.

    Args:
        service: Original integrated runtime, not arbitrary supplied connections.
        actor: Authenticated dashboard principal.

    Returns:
        Metadata-only eligibility report. This never creates a test permit.
    """
    if not actor.startswith("dashboard/") or not actor[10:]:
        raise OpenError("raid_arguments")
    accounts = {str(adapter.account): adapter for adapter in service.adapters()}
    receiver = accounts.get(RECEIVER)
    if receiver is None:
        raise OpenError("account_unavailable")
    frozen = (receiver, receiver.account, receiver.business, receiver.config["id"])
    roster = await service.roster(receiver, GROUP)
    if frozen != (receiver, receiver.account, receiver.business, receiver.config["id"]):
        raise OpenError("operation_revoked")
    rows = []
    for uid in TARGETS:
        member, adapter = roster.get(uid, {}), accounts.get(uid)
        ordinary = member.get("groupRole") == "GROUP_ROLE_MEMBER"
        bound = bool(
            adapter and str(member.get("nimId", "")) == str(adapter.nim_account)
        )
        rows.append(
            {
                "member": uid,
                "role": member.get("groupRole", "absent"),
                "ordinary": ordinary,
                "transport_bound": bound,
            }
        )
    return {
        "group": GROUP,
        "receiver": RECEIVER,
        "targets": rows,
        "eligible": all(row["ordinary"] and row["transport_bound"] for row in rows),
        "permit_created": False,
        "live_acceptance_passed": False,
    }
