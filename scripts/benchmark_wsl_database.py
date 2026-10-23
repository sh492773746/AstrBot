"""Compare message query plans on temporary synthetic data, never live databases."""

import argparse
import json
import sqlite3
import statistics
import time
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory


def benchmark(rows: int, repeats: int) -> dict:
    """Measure warm-cache query latency with and without the two inbox indexes.

    Args:
        rows: Number of synthetic rows; must be between 1000 and 500000.
        repeats: Measured reads per phase; must be between 1 and 50.

    Returns:
        Plans, median milliseconds and matching result counts, without payloads.

    Raises:
        ValueError: If fixture limits are invalid.
        AssertionError: If indexing changes query results.
    """
    if not 1000 <= rows <= 500000 or not 1 <= repeats <= 50:
        raise ValueError("Use 1000..500000 rows and 1..50 repetitions")
    queries = {
        "ranking": (
            "SELECT message,payload,state FROM inbox WHERE account=? AND team=? AND received_at>=?",
            ("a", "g", rows - 1000),
        ),
        "context": (
            "SELECT message,payload FROM inbox WHERE account=? AND team=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
            ("a", "g", rows, 20),
        ),
    }
    report = {
        "synthetic": True,
        "sqlite_version": sqlite3.sqlite_version,
        "rows": rows,
        "repeats": repeats,
        "queries": {},
    }
    with TemporaryDirectory(prefix="wsl-db-benchmark-") as directory:
        with closing(
            sqlite3.connect(Path(directory) / "fixture.sqlite3", cached_statements=0)
        ) as db:
            db.executescript(
                "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
                "CREATE TABLE inbox(account TEXT,team TEXT,message TEXT,payload TEXT,state TEXT,received_at REAL,PRIMARY KEY(account,team,message));"
            )
            db.executemany(
                "INSERT INTO inbox VALUES('a','g',?,'{\"text\":\"synthetic fixture\"}','processed',?)",
                ((str(rows - n), n) for n in range(rows)),
            )
            db.commit()
            original = {}
            for phase in ("before", "after"):
                if phase == "after":
                    db.executescript(
                        "CREATE INDEX inbox_ranking_window ON inbox(account,team,received_at);"
                        "CREATE INDEX inbox_conversation ON inbox(account,team);"
                    )
                for name, (query, params) in queries.items():
                    result = db.execute(query, params).fetchall()
                    comparable = sorted(result) if name == "ranking" else result
                    if phase == "before":
                        original[name] = comparable
                    else:
                        assert comparable == original[name], name
                    durations = []
                    for _ in range(repeats):
                        started = time.perf_counter()
                        db.execute(query, params).fetchall()
                        durations.append((time.perf_counter() - started) * 1000)
                    report["queries"].setdefault(name, {})[phase] = {
                        "median_ms": round(statistics.median(durations), 3),
                        "result_rows": len(result),
                        "plan": [
                            row[3]
                            for row in db.execute("EXPLAIN QUERY PLAN " + query, params)
                        ],
                    }
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100000)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.rows, args.repeats), indent=2))
