from datetime import UTC, datetime, timedelta, timezone

from clients import hubspot


def test_hs_date_naive_is_utc_midnight():
    assert hubspot._hs_date(datetime(2026, 9, 3, 15, 30)) == str(
        int(datetime(2026, 9, 3, tzinfo=UTC).timestamp() * 1000)
    )


def test_hs_date_aware_non_utc_uses_utc_day():
    est = timezone(timedelta(hours=-5))
    # 2026-09-03 22:00 EST == 2026-09-04 03:00 UTC → UTC midnight of the 4th
    assert hubspot._hs_date(datetime(2026, 9, 3, 22, 0, tzinfo=est)) == str(
        int(datetime(2026, 9, 4, tzinfo=UTC).timestamp() * 1000)
    )


def test_as_utc_naive_and_aware():
    assert hubspot._as_utc(datetime(2026, 1, 1)).tzinfo == timezone.utc
    est = timezone(timedelta(hours=-5))
    assert hubspot._as_utc(datetime(2026, 1, 1, 0, 0, tzinfo=est)).hour == 5


def test_split_name():
    assert hubspot._split_name(None) == ("", "")
    assert hubspot._split_name("  ") == ("", "")
    assert hubspot._split_name("Ada") == ("Ada", "")
    assert hubspot._split_name("Ada Lovelace King") == ("Ada", "Lovelace King")
