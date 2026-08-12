"""A redacted row is not enough: the FILE must not carry the credential either."""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew import snapshot_redact as redact

KEY = "AKIAIOSFODNN7EXAMPLE"


def _stage(tmp_path: Path) -> Path:
    stage = tmp_path / "bundle"
    stage.mkdir()
    (stage / "MANIFEST.json").write_text(json.dumps({"components": {}}), encoding="utf-8")
    return stage


def _realistic_db(path: Path) -> None:
    """Several rows, only one carrying the secret -- the shape a real memory store has.

    A single-row fixture hides the defect: rewriting the only cell happens to overwrite
    the same bytes. With neighbours present the old cell content stays in the page's
    unused space.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with redact.sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE semantic(id INTEGER PRIMARY KEY, key TEXT, value TEXT)")
        conn.executemany(
            "INSERT INTO semantic(key, value) VALUES(?, ?)",
            [
                ("project.terrace", "Sydney property platform"),
                ("aws.prod", f"account key {KEY}"),
                ("pref.lang", "Chinese for discussion, English for code"),
            ],
        )
        conn.commit()
    conn.close()


class TestTheRedactedFileCarriesNoCredential:
    def test_the_plaintext_is_gone_from_the_bytes_not_just_the_rows(
        self, tmp_path: Path
    ) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        _realistic_db(db)
        assert KEY.encode("ascii") in db.read_bytes(), "fixture did not plant the secret"

        redact.redact_bundle_for_egress(stage)

        assert KEY.encode("ascii") not in db.read_bytes(), (
            "every row reads redacted but the credential is still greppable in the file "
            "that leaves the host"
        )

    def test_the_rows_and_the_database_survive_the_rebuild(self, tmp_path: Path) -> None:
        stage = _stage(tmp_path)
        db = stage / "memory.db"
        _realistic_db(db)

        redact.redact_bundle_for_egress(stage)

        with redact.sqlite3.connect(str(db)) as conn:
            assert conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok"
            rows = dict(conn.execute("SELECT key, value FROM semantic").fetchall())
        conn.close()
        assert rows["project.terrace"] == "Sydney property platform"
        assert rows["pref.lang"] == "Chinese for discussion, English for code"
        assert KEY not in rows["aws.prod"]
        assert "REDACTED" in rows["aws.prod"]

    def test_an_untouched_database_is_not_rewritten(self, tmp_path: Path) -> None:
        """Nothing to redact means nothing to rebuild -- the file should be byte-identical."""
        stage = _stage(tmp_path)
        db = stage / "clean.db"
        with redact.sqlite3.connect(str(db)) as conn:
            conn.execute("CREATE TABLE t(x TEXT)")
            conn.execute("INSERT INTO t(x) VALUES('nothing secret here')")
            conn.commit()
        conn.close()
        before = db.read_bytes()

        redact.redact_bundle_for_egress(stage)

        assert db.read_bytes() == before

    def test_the_rebuild_runs_after_the_content_is_clean(self) -> None:
        import inspect

        src = inspect.getsource(redact._redact_database)
        assert 'conn.execute("VACUUM")' in src
        assert src.index("UPDATE") < src.index('conn.execute("VACUUM")'), (
            "rebuilding before the rows are cleaned would preserve the old bytes"
        )
