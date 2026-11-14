"""Refresh tests/fixtures/server/guc_folder_tree.json from the real mailbox. Read-only.

    python qa/record_server_shapes.py            # uses your saved gucmail login

It makes one folder-tree request (FindFolder, deep traversal) and records only standard
folder names and folder classes. Folders you created are renamed "Folder 1", "Folder 2", ...
No messages, subjects, addresses or identifiers are read or written. Run it only when you
mean to touch the live account.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from gucmail.bootstrap import build_context  # noqa: E402

OUTPUT = REPO / "tests" / "fixtures" / "server" / "guc_folder_tree.json"

# Names Exchange creates itself; anything else is the user's own folder and gets renamed.
STANDARD = {
    "Top of Information Store", "Inbox", "Drafts", "Sent Items", "Deleted Items",
    "Junk Email", "Outbox", "Archive", "Calendar", "Contacts", "Recipient Cache",
    "PersonMetadata", "Tasks", "Notes", "Journal", "Conversation Action Settings",
    "Quick Step Settings", "Conversation History", "RSS Feeds",
    "Social Activity Notifications", "ExternalContacts", "Files", "Yammer Root", "Feeds",
    "Inbound", "Outbound", "Sync Issues", "Conflicts", "Local Failures", "Server Failures",
    "{06967759-274D-40B2-A3EB-D7F9E73727D7}",
}


def main() -> int:
    context = build_context()
    try:
        account = context.gateway(interactive=True).account
        root = account.msg_folder_root
        renamed: dict[str, str] = {}

        def describe(folder) -> dict:
            name = folder.name if folder.name in STANDARD else renamed.setdefault(
                folder.id, f"Folder {len(renamed) + 1}"
            )
            node = {"name": name, "class": folder.folder_class}
            children = [describe(child) for child in folder.children]
            if children:
                node["children"] = children
            return node

        tree = {
            "about": json.loads(OUTPUT.read_text(encoding="utf-8"))["about"],
            "root": describe(root),
        }
        OUTPUT.write_text(json.dumps(tree, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {OUTPUT.relative_to(REPO)} ({len(renamed)} personal folders renamed).")
        print("Message counts are not recorded; add test messages to the fixture by hand.")
        return 0
    finally:
        context.close()


if __name__ == "__main__":
    raise SystemExit(main())
