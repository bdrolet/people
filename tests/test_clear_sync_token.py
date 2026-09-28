from scripts import clear_sync_token


class FakeConn:
    def __init__(self):
        self.calls = []
        self.committed = False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

        class C:
            def fetchone(self_inner):
                return {"sync_token": "old-token"}

        return C()

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_clears_the_token_and_reports_it(capsys):
    conn = FakeConn()
    clear_sync_token.run(lambda: conn)
    joined = " ".join(sql for sql, _ in conn.calls)
    assert "UPDATE sync_state" in joined or "INSERT INTO sync_state" in joined
    out = capsys.readouterr().out
    assert "cleared" in out.lower()
    assert "old-token" not in out  # a token is a credential-ish value
