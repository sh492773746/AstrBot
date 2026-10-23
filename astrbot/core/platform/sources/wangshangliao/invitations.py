"""Resolve current membership evidence without trusting retained NIM inviters."""

from datetime import datetime, timezone

from .wire import ProtocolError


def latest_membership_events(response: dict, group: str) -> dict[str, dict]:
    """Select the newest signed membership event for each exact business user.

    Args:
        response: Initial, account-visible business application-log response.
        group: Exact business group ID.

    Returns:
        Latest event per member, including departures and natural applications.
        Equal-time conflicting events are deliberately left ambiguous.

    Raises:
        ProtocolError: If relevant evidence cannot be ordered or identified safely.
    """
    if (
        not isinstance(response, dict)
        or not isinstance(response.get("list"), list)
        or len(response["list"]) > 4096
    ):
        raise ProtocolError("invitation_response")
    latest, seen = {}, set()
    for row in response["list"]:
        if not isinstance(row, dict):
            raise ProtocolError("invitation_response")
        if str(row.get("groupId")) != group:
            continue
        applicant = row.get("applicant")
        if not isinstance(applicant, dict):
            raise ProtocolError("invitation_response")
        uid, event_id = applicant.get("userId"), row.get("applyId")
        for value in (uid, event_id):
            if (
                type(value) not in (str, int)
                or not str(value).isascii()
                or not str(value).isdigit()
                or len(str(value)) > 20
                or int(value) <= 0
            ):
                raise ProtocolError("invitation_response")
        if str(event_id) in seen:
            raise ProtocolError("invitation_response")
        seen.add(str(event_id))
        try:
            raw_time = row["applyTime"]
            if not isinstance(raw_time, str) or not 1 <= len(raw_time) <= 64:
                raise ValueError
            stamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
                raise ValueError
            stamp = stamp.timestamp()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ProtocolError("invitation_response") from None
        uid = str(uid)
        previous = latest.get(uid)
        if previous is None or stamp > previous["timestamp"]:
            latest[uid] = {"timestamp": stamp, "row": row, "ambiguous": False}
        elif stamp == previous["timestamp"]:
            previous["ambiguous"] = True
    return latest
