"""Synthetic ChatStorage.sqlite (spec §7: fixtures are synthetic). Invented
numbers (+1555010…), invented group JIDs, invented @lid ids (999…), invented names.

Shape, so the assertions elsewhere have one place to read:

  sessions  8: alice direct, one group, one @lid direct, status@broadcast,
               a second row for alice's JID (duplicate, no messages),
               a legacy-MX direct chat, an unnormalizable 1-digit JID,
               a JID with no '@' at all
  messages 14: covers from_me, empty text, a group message via ZGROUPMEMBER,
               a senderless group inbound, Ben's own group message with no
               ZGROUPMEMBER, a LID chat message, a duplicate stanza id, real
               media, the §5.3 media trap, a vcard, a message in a skipped
               session, and a group-only sender (attributed via ZGROUPMEMBER,
               with ZFROMJID deliberately set to the group's own JID, as the
               real store does) who has no direct chat of their own
  members   5: an admin who also messages, a named member, an inactive member,
               a @lid member, and a group-only sender with no direct chat
  profile push names 4 (ZWAPROFILEPUSHNAME; one blank). Message ZPUSHNAMEs are
               base64 protobuf blobs, as on the real store (spec 2026-10-14 §1).
  address book (build_contacts, ContactsV2.sqlite) 2: the @lid member mapped to
               a phone, and the group-only sender keyed by both JIDs
"""

import sqlite3
from pathlib import Path

EPOCH_2001 = 978307200

DDL = """
CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, ZSESSIONTYPE INTEGER,
  ZGROUPINFO INTEGER, ZLASTMESSAGEDATE TIMESTAMP, ZCONTACTJID VARCHAR, ZPARTNERNAME VARCHAR);
CREATE TABLE ZWAGROUPINFO (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER,
  ZCREATIONDATE TIMESTAMP);
CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZCHATSESSION INTEGER, ZISADMIN INTEGER,
  ZISACTIVE INTEGER, ZCONTACTNAME VARCHAR, ZMEMBERJID VARCHAR);
CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, ZMESSAGE INTEGER, ZMEDIALOCALPATH VARCHAR,
  ZMEDIAURL VARCHAR, ZTITLE VARCHAR, ZVCARDSTRING VARCHAR);
CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZGROUPEVENTTYPE INTEGER, ZISFROMME INTEGER,
  ZMESSAGETYPE INTEGER, ZCHATSESSION INTEGER, ZGROUPMEMBER INTEGER, ZMEDIAITEM INTEGER,
  ZMESSAGEDATE TIMESTAMP, ZPUSHNAME VARCHAR, ZSTANZAID VARCHAR, ZTEXT VARCHAR,
  ZFROMJID VARCHAR);
CREATE TABLE ZWAPROFILEPUSHNAME (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZJID VARCHAR, ZPUSHNAME VARCHAR);
"""

ALICE_JID = "15550100001@s.whatsapp.net"
GROUP_JID = "10000000001-1500000000@g.us"
LID_JID = "99900000000001@lid"
# Legacy Mexican mobile form: 52 + 1 + 10 digits (§6.4). Must normalize to
# +525555555555 by stripping the 1.
MX_JID = "5215555555555@s.whatsapp.net"
MEMBER_2_JID = "15550100002@s.whatsapp.net"
MEMBER_3_JID = "15550100003@s.whatsapp.net"
MEMBER_LID_JID = "99900000000002@lid"
# A group-only sender with no direct chat of their own (§4.1's second clause).
MEMBER_4_JID = "15550100004@s.whatsapp.net"
# The address book maps the @lid member to this phone JID (spec 2026-10-14 §5.3).
MEMBER_LID_PHONE_JID = "15550100005@s.whatsapp.net"
# Frank's LID, so the address book can key him both ways.
MEMBER_4_LID_JID = "99900000000004@lid"

CONTACTS_DDL = """
CREATE TABLE ZWAADDRESSBOOKCONTACT (Z_PK INTEGER PRIMARY KEY, Z_ENT INTEGER, Z_OPT INTEGER,
  ZFULLNAME VARCHAR, ZLID VARCHAR, ZPHONENUMBER VARCHAR, ZWHATSAPPID VARCHAR);
"""


def wa_ts(unix_seconds: int) -> float:
    """ZMESSAGEDATE is seconds (not nanoseconds) since 2001-01-01 (spec §3)."""
    return float(unix_seconds - EPOCH_2001)


T0 = 1_750_000_000  # 2025-06-15, comfortably inside the store's real range


def build_store(tmp_path: Path) -> Path:
    path = tmp_path / "ChatStorage.sqlite"
    db = sqlite3.connect(path)
    db.executescript(DDL)

    db.executemany(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZSESSIONTYPE, ZGROUPINFO, ZLASTMESSAGEDATE,"
        " ZCONTACTJID, ZPARTNERNAME) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 0, None, wa_ts(T0 + 900), ALICE_JID, "Alice Example"),
            (2, 1, 1, wa_ts(T0 + 600), GROUP_JID, "Soccer Carpool"),
            (3, 0, None, wa_ts(T0 + 700), LID_JID, "Bob Example"),
            (4, 2, None, None, "status@broadcast", None),
            (5, 0, None, None, ALICE_JID, "Alice Example"),  # duplicate JID, no messages
            (6, 0, None, wa_ts(T0 + 800), MX_JID, "Carmen Example"),
            (7, 0, None, None, "5@s.whatsapp.net", None),  # unnormalizable
            (8, 0, None, None, "garbage", None),  # no '@'
        ],
    )
    db.execute(
        "INSERT INTO ZWAGROUPINFO (Z_PK, ZCHATSESSION, ZCREATIONDATE) VALUES (1, 2, ?)",
        (wa_ts(T0 - 86_400),),
    )
    db.executemany(
        "INSERT INTO ZWAGROUPMEMBER (Z_PK, ZCHATSESSION, ZISADMIN, ZISACTIVE, ZCONTACTNAME,"
        " ZMEMBERJID) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 2, 1, 1, "Alice Example", ALICE_JID),
            (2, 2, 0, 1, "Dana Example", MEMBER_2_JID),
            (3, 2, 0, 0, None, MEMBER_3_JID),  # inactive, never posts
            (4, 2, 0, 1, "Eve Example", MEMBER_LID_JID),
            (5, 2, 0, 1, "Frank Example", MEMBER_4_JID),  # group-only sender
        ],
    )
    db.executemany(
        "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZMEDIALOCALPATH, ZMEDIAURL, ZTITLE,"
        " ZVCARDSTRING) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (1, 9, "Media/invented.jpg", None, None, None),
            # The §5.3 trap: a media row with no real content, on a plain-text message.
            (2, 10, None, None, None, None),
            (3, 13, None, None, "Contact", "BEGIN:VCARD\nFN:Frank Example\nEND:VCARD"),
        ],
    )
    db.executemany(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZGROUPEVENTTYPE, ZISFROMME, ZMESSAGETYPE, ZCHATSESSION,"
        " ZGROUPMEMBER, ZMEDIAITEM, ZMESSAGEDATE, ZPUSHNAME, ZSTANZAID, ZTEXT, ZFROMJID)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            # ZGROUPEVENTTYPE is non-zero nearly everywhere and means nothing (§5.3 item 2).
            (1, 7, 1, 0, 1, None, None, wa_ts(T0 + 100), None, "s1", "Hi", None),
            (2, 7, 0, 0, 1, None, None, wa_ts(T0 + 200), "IAA=", "s2", "Hello", ALICE_JID),
            (3, 7, 0, 0, 1, None, None, wa_ts(T0 + 300), "IAA=", "s3", "", ALICE_JID),
            (4, 12, 0, 0, 2, 1, None, wa_ts(T0 + 400), "IAA=", "s4", "In the group", ALICE_JID),
            # Senderless group inbound: dropped and counted (§5.4).
            (5, 12, 0, 0, 2, None, None, wa_ts(T0 + 500), "IAA=", "s5", "Who?", MEMBER_2_JID),
            # Ben's own group message, also without ZGROUPMEMBER: kept.
            (6, 12, 1, 0, 2, None, None, wa_ts(T0 + 600), None, "s6", "On my way", None),
            (7, 7, 0, 0, 3, None, None, wa_ts(T0 + 700), "IAA=", "s7", "From a LID chat", None),
            # Duplicate (chat_jid, stanza_id) with row 2: collapsed, counted (§4.4).
            (8, 7, 0, 0, 1, None, None, wa_ts(T0 + 250), "IAA=", "s2", "Hello again", ALICE_JID),
            (9, 7, 0, 1, 1, None, 1, wa_ts(T0 + 850), "IAA=", "s9", None, ALICE_JID),
            (10, 7, 0, 0, 1, None, 2, wa_ts(T0 + 860), "IAA=", "s10", "Text only", ALICE_JID),
            (11, 7, 0, 0, 6, None, None, wa_ts(T0 + 800), "IAA=", "s11", "Hola", MX_JID),
            # In a skipped (status) session: never stored.
            (12, 0, 0, 0, 4, None, None, wa_ts(T0 + 870), None, "s12", "A status", None),
            (13, 7, 0, 4, 1, None, 3, wa_ts(T0 + 900), "IAA=", "s13", None, ALICE_JID),
            # Group-only sender, attributed via ZGROUPMEMBER: on the real store
            # ZFROMJID on a group row holds the group's own JID, never the
            # sender's, so it's set to GROUP_JID here too — the resulting
            # handle's jid must come from ZMEMBERJID (via ZGROUPMEMBER), not
            # this column (§4.1, §5.4).
            (
                14,
                7,
                0,
                0,
                2,
                5,
                None,
                wa_ts(T0 + 650),
                "CJaiyNAGIAA=",
                "s14",
                "Group only sender",
                GROUP_JID,
            ),
        ],
    )
    db.executemany(
        "INSERT INTO ZWAPROFILEPUSHNAME (Z_PK, ZJID, ZPUSHNAME) VALUES (?, ?, ?)",
        [
            (1, ALICE_JID, "Alice Push"),  # loses to ZPARTNERNAME
            (2, MEMBER_4_JID, "Frank Push"),
            (3, MEMBER_LID_JID, "Eve Push"),
            (4, MEMBER_2_JID, "   "),  # blank: filtered out by the read
        ],
    )
    db.commit()
    db.close()
    return path


def build_contacts(tmp_path: Path, rows: list[tuple] | None = None) -> Path:
    """Synthetic ContactsV2.sqlite beside ChatStorage.sqlite. rows are
    (ZLID, ZWHATSAPPID, ZFULLNAME)."""
    path = tmp_path / "ContactsV2.sqlite"
    db = sqlite3.connect(path)
    db.executescript(CONTACTS_DDL)
    if rows is None:
        rows = [
            (MEMBER_LID_JID, MEMBER_LID_PHONE_JID, "Eve Saved"),
            (MEMBER_4_LID_JID, MEMBER_4_JID, "Frank Saved"),
        ]
    db.executemany(
        "INSERT INTO ZWAADDRESSBOOKCONTACT (ZLID, ZWHATSAPPID, ZFULLNAME) VALUES (?, ?, ?)", rows
    )
    db.commit()
    db.close()
    return path
