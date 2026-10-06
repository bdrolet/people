import pytest

from services import labels

SYSTEM = labels.SYSTEM_GROUP_TYPE
USER = "USER_CONTACT_GROUP"

BY_RN = {
    "contactGroups/myContacts": {
        "name": "myContacts",
        "formattedName": "My Contacts",
        "groupType": SYSTEM,
    },
    "contactGroups/family": {"name": "family", "formattedName": "Family", "groupType": SYSTEM},
    "contactGroups/starred": {"name": "starred", "formattedName": "Starred", "groupType": SYSTEM},
    "contactGroups/inbox1": {"name": "Inbox", "formattedName": "Inbox", "groupType": USER},
    "contactGroups/climb": {"name": "Climbing", "formattedName": "Climbing", "groupType": USER},
    "contactGroups/inv": {"name": "investor", "formattedName": "investor", "groupType": USER},
}


def test_user_groups_drops_system_and_reserved():
    assert labels.user_groups(BY_RN, "Inbox") == {
        "contactGroups/climb": "Climbing",
        "contactGroups/inv": "investor",
    }


def test_user_groups_reserved_match_ignores_case():
    assert "contactGroups/inbox1" not in labels.user_groups(BY_RN, "inbox")


def test_membership_rns_returns_every_group_in_order_without_duplicates():
    person = {
        "memberships": [
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/climb"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/inv"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/climb"}},
            {"domainMembership": {"inViewerDomain": True}},
        ]
    }
    assert labels.membership_rns(person) == ["contactGroups/climb", "contactGroups/inv"]


def test_membership_rns_handles_no_memberships():
    assert labels.membership_rns({}) == []


def test_normalize_change_strips_and_collapses_duplicates():
    add, remove = labels.normalize_change([" Climbing ", "climbing", "investor"], [])
    assert add == ["Climbing", "investor"] and remove == []


@pytest.mark.parametrize("bad", ["", "   "])
def test_normalize_change_rejects_blank(bad):
    with pytest.raises(labels.LabelError, match="blank"):
        labels.normalize_change([bad], [])
    with pytest.raises(labels.LabelError, match="blank"):
        labels.normalize_change([], [bad])


def test_normalize_change_rejects_add_and_remove_overlap():
    with pytest.raises(labels.LabelError, match="both"):
        labels.normalize_change(["Climbing"], ["CLIMBING"])


def test_match_name_case_insensitive():
    assert labels.match_name({"g/1": "Climbing"}, "climbing") == "g/1"


def test_match_name_exact_case_breaks_a_tie():
    assert labels.match_name({"g/1": "VIP", "g/2": "vip"}, "vip") == "g/2"


def test_match_name_ambiguous_without_exact():
    with pytest.raises(labels.AmbiguousLabel):
        labels.match_name({"g/1": "VIP", "g/2": "vip"}, "Vip")


def test_match_name_none_when_absent():
    assert labels.match_name({"g/1": "Climbing"}, "investor") is None


def test_resolve_finds_a_user_group():
    assert labels.resolve(BY_RN, "CLIMBING", "Inbox") == "contactGroups/climb"


def test_resolve_none_for_a_new_name():
    assert labels.resolve(BY_RN, "Book Club", "Inbox") is None


def test_resolve_rejects_the_reserved_group():
    with pytest.raises(labels.LabelError, match="reserved"):
        labels.resolve(BY_RN, "inbox", "Inbox")


@pytest.mark.parametrize("name", ["starred", "Starred", "My Contacts", "myContacts"])
def test_resolve_rejects_a_system_name_with_no_user_group(name):
    with pytest.raises(labels.LabelError, match="built-in"):
        labels.resolve(BY_RN, name, "Inbox")


def test_resolve_prefers_a_user_group_over_a_system_name():
    # Review Focus 1: a user "Family" label coexists with the built-in family group.
    by_rn = {
        **BY_RN,
        "contactGroups/fam1": {"name": "Family", "formattedName": "Family", "groupType": USER},
    }
    assert labels.resolve(by_rn, "family", "Inbox") == "contactGroups/fam1"


def test_ambiguous_is_a_label_error():
    assert issubclass(labels.AmbiguousLabel, labels.LabelError)
