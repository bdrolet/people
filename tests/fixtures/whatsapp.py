"""Synthetic ChatStorage.sqlite (spec §7: fixtures are synthetic). Invented
numbers (+1555010…), invented group JIDs, invented names.

Shape, so the assertions elsewhere have one place to read:

  sessions  8: alice direct, one group, one @lid direct, status@broadcast,
               a second row for alice's JID (duplicate, no messages),
               a legacy-MX direct chat, an unnormalizable 1-digit JID,
               a JID with no '@' at all
  messages 13: covers from_me, empty text, a group message via ZGROUPMEMBER,
               a senderless group inbound, Ben's own group message with no
               ZGROUPMEMBER, a LID chat message, a duplicate stanza id, real
               media, the §5.3 media trap, a vcard, and a message in a skipped
               session
  members   4: an admin who also messages, a named member, an inactive member,
               and a @lid member
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
"""

ALICE_JID = "15550100001@s.whatsapp.net"
GROUP_JID = "10000000001-1500000000@g.us"
LID_JID = "19709655306273@lid"
# Legacy Mexican mobile form: 52 + 1 + 10 digits (§6.4). Must normalize to
# +525555555555 by stripping the 1.
MX_JID = "5215555555555@s.whatsapp.net"
MEMBER_2_JID = "15550100002@s.whatsapp.net"
MEMBER_3_JID = "15550100003@s.whatsapp.net"
MEMBER_LID_JID = "19709655306274@lid"


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
            (2, 7, 0, 0, 1, None, None, wa_ts(T0 + 200), "Alice", "s2", "Hello", ALICE_JID),
            (3, 7, 0, 0, 1, None, None, wa_ts(T0 + 300), "Alice", "s3", "", ALICE_JID),
            (4, 12, 0, 0, 2, 1, None, wa_ts(T0 + 400), "Alice", "s4", "In the group", ALICE_JID),
            # Senderless group inbound: dropped and counted (§5.4).
            (5, 12, 0, 0, 2, None, None, wa_ts(T0 + 500), "Ghost", "s5", "Who?", MEMBER_2_JID),
            # Ben's own group message, also without ZGROUPMEMBER: kept.
            (6, 12, 1, 0, 2, None, None, wa_ts(T0 + 600), None, "s6", "On my way", None),
            (7, 7, 0, 0, 3, None, None, wa_ts(T0 + 700), "Bob", "s7", "From a LID chat", None),
            # Duplicate (chat_jid, stanza_id) with row 2: collapsed, counted (§4.4).
            (8, 7, 0, 0, 1, None, None, wa_ts(T0 + 250), "Alice", "s2", "Hello again", ALICE_JID),
            (9, 7, 0, 1, 1, None, 1, wa_ts(T0 + 850), "Alice", "s9", None, ALICE_JID),
            (10, 7, 0, 0, 1, None, 2, wa_ts(T0 + 860), "Alice", "s10", "Text only", ALICE_JID),
            (11, 7, 0, 0, 6, None, None, wa_ts(T0 + 800), "Carmen", "s11", "Hola", MX_JID),
            # In a skipped (status) session: never stored.
            (12, 0, 0, 0, 4, None, None, wa_ts(T0 + 870), None, "s12", "A status", None),
            (13, 7, 0, 4, 1, None, 3, wa_ts(T0 + 900), "Alice", "s13", None, ALICE_JID),
        ],
    )
    db.commit()
    db.close()
    return path
