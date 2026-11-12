"""Bounded mapping of GetDelegate metadata, without changing mailbox access."""

from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
from typing import Any

from gucmail.errors import GUCmailError, UnsupportedFeatureError
from gucmail.services.delegates import MAX_DELEGATES, DelegateInfo, DelegatePermission

PERMISSION_FOLDERS = ("calendar", "tasks", "inbox", "contacts", "notes", "journal")


def delegate_reader(*, account: Any) -> Any:
    # Import only when a caller deliberately resolves the Exchange gateway.
    from exchangelib.services import GetDelegate
    from exchangelib.util import TNS

    class PresenceAwareGetDelegate(GetDelegate):  # type: ignore[misc]
        def _elem_to_obj(self, elem: Any) -> Any:
            permission_xml = elem.find(f"{{{TNS}}}DelegatePermissions")
            absent_permissions = [
                folder
                for folder in PERMISSION_FOLDERS
                if permission_xml is None
                or permission_xml.find(f"{{{TNS}}}{folder.title()}FolderPermissionLevel") is None
            ]
            flags = (
                ("receive_copies_of_meeting_messages", "ReceiveCopiesOfMeetingMessages"),
                ("view_private_items", "ViewPrivateItems"),
            )
            reported_flags: dict[str, bool | None] = {}
            for attribute, tag in flags:
                field = elem.find(f"{{{TNS}}}{tag}")
                if field is None:
                    reported_flags[attribute] = None
                else:
                    # XML Schema permits 1/0 as well as true/false. The installed
                    # library's boolean parser does not recognize the numeric forms.
                    text = (field.text or "").strip()
                    if text not in {"true", "false", "1", "0"}:
                        raise _invalid()
                    reported_flags[attribute] = text in {"true", "1"}
            value = super()._elem_to_obj(elem)
            if value.delegate_permissions is not None:
                for folder in absent_permissions:
                    setattr(value.delegate_permissions, f"{folder}_folder_permission_level", None)
            for attribute, reported in reported_flags.items():
                setattr(value, attribute, reported)
            return value

    return PresenceAwareGetDelegate(account=account)


def _invalid() -> GUCmailError:
    return GUCmailError("Exchange returned invalid delegate metadata. No permissions were changed.")


def _text(value: object, maximum: int = 4096) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > maximum:
        raise _invalid()
    return value


def _boolean(value: object) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise _invalid()


def map_delegate(value: Any) -> DelegateInfo:
    user = getattr(value, "user_id", None)
    if user is None:
        raise _invalid()
    email = _text(getattr(user, "primary_smtp_address", None), 1024)
    sid = _text(getattr(user, "sid", None), 1024)
    distinguished = _text(getattr(user, "distinguished_user", None), 128)
    external = _text(getattr(user, "external_user_identity", None))
    if not any((email, sid, distinguished, external)):
        raise _invalid()
    permissions = getattr(value, "delegate_permissions", None)
    return DelegateInfo(
        display_name=_text(getattr(user, "display_name", None)) or "",
        email_address=email,
        security_identifier=sid,
        distinguished_user=distinguished,
        external_user_identity=external,
        permissions=tuple(
            DelegatePermission(
                folder, _text(getattr(permissions, f"{folder}_folder_permission_level", None), 128)
            )
            for folder in PERMISSION_FOLDERS
        ),
        receive_meeting_copies=_boolean(getattr(value, "receive_copies_of_meeting_messages", None)),
        view_private_items=_boolean(getattr(value, "view_private_items", None)),
    )


def map_delegate_collection(values: Iterable[Any]) -> tuple[DelegateInfo, ...]:
    iterator = iter(values)
    try:
        bounded = list(islice(iterator, MAX_DELEGATES + 1))
        if len(bounded) > MAX_DELEGATES:
            raise UnsupportedFeatureError(
                "The delegate collection exceeds the 1000-entry safety limit. "
                "EWS does not paginate this operation; inspect it in Outlook."
            )
        return tuple(map_delegate(value) for value in bounded)
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
