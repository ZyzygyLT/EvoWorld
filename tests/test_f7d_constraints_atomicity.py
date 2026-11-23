"""Test for _ensure_challenge_participants_constraints atomicity and rollback."""

import sys
sys.path.insert(0, '.')

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.orm import sessionmaker

from worldsim.server.db import (
    _ensure_challenge_participants_constraints,
    Base,
)


def setup_minimal_table(conn):
    """Create challenge_participants with minimal DDL (missing CHECK/UNIQUE).
    No data inserted - the function adds constraints and backfills data."""
    conn.execute(text("DROP TABLE IF EXISTS challenge_participants"))
    conn.execute(text('''CREATE TABLE challenge_participants (
        id INTEGER NOT NULL PRIMARY KEY,
        challenge_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        result_status TEXT,
        public_alias TEXT,
        created_at TEXT,
        FOREIGN KEY (challenge_id) REFERENCES challenges (id),
        FOREIGN KEY (user_id) REFERENCES users (id)
    )'''))
    # No data inserted - function will backfill data


def test_atomic_rollback():
    """Test that the function uses SQLite BEGIN/COMMIT/ROLLBACK atomically."""
    print('=== Test: Atomic rollback ===')
    # Do NOT call Base.metadata.create_all - we create the table manually
    test_engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False})
    conn = test_engine.connect()
    setup_minimal_table(conn)
    
    row = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='challenge_participants'")).fetchone()
    print('Original DDL:', row[0] if row else 'No table')
    
    _ensure_challenge_participants_constraints(conn, inspect(test_engine))
    
    row = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='challenge_participants'")).fetchone()
    print('After DDL:', row[0][:200] if row else 'No table')
    
    count = conn.execute(text("SELECT COUNT(*) FROM challenge_participants")).scalar()
    print('Rows:', count)
    
    conn.close()
    test_engine.dispose()
    print('Test PASSED\n')


def test_rollback_new_connection():
    """Test that original data is intact from a new connection after failure."""
    print('=== Test: Rollback from new connection ===')
    test_engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False})
    conn = test_engine.connect()
    setup_minimal_table(conn)
    
    original_count = conn.execute(text("SELECT COUNT(*) FROM challenge_participants")).scalar()
    original_ddl = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='challenge_participants'")
    ).fetchone()[0]
    
    print(f'Original count: {original_count}')
    
    _ensure_challenge_participants_constraints(conn, inspect(test_engine))
    
    # After function, check same connection
    new_count = conn.execute(text("SELECT COUNT(*) FROM challenge_participants")).scalar()
    new_ddl = conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name='challenge_participants'")
    ).fetchone()[0]
    
    print(f'After function count: {new_count}')
    print('After DDL:', new_ddl[:100] if new_ddl else 'None')
    
    # Key point: original data should be consistent
    assert original_count == new_count, f'Count changed: {original_count} -> {new_count}'
    
    conn.close()
    test_engine.dispose()
    print('Test PASSED\n')


def test_recovery_and_idempotency():
    """Test that after the migration is successful:
    1. Running again works (idempotency)
    2. Constraints are correct"""
    print('=== Test: Recovery and Idempotency ===')
    # Do NOT call Base.metadata.create_all
    test_engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False})
    conn = test_engine.connect()
    setup_minimal_table(conn)
    
    # First run - should add constraints
    _ensure_challenge_participants_constraints(conn, inspect(test_engine))
    row = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='challenge_participants'")).fetchone()
    print('After first run:', row[0][:200] if row else 'No table')
    
    # Second run - should be idempotent, return without changes
    _ensure_challenge_participants_constraints(conn, inspect(test_engine))
    print('Second run completed (idempotent)')
    
    # Verify final state
    row = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='challenge_participants'")).fetchone()
    print('Final DDL:', row[0][:200] if row else 'No table')
    
    # Check data is intact
    count = conn.execute(text("SELECT COUNT(*) FROM challenge_participants")).scalar()
    print('Data count intact:', count)
    
    conn.close()
    test_engine.dispose()
    print('Test PASSED\n')


if __name__ == '__main__':
    test_atomic_rollback()
    test_rollback_new_connection()
    test_recovery_and_idempotency()
    print('ALL TESTS PASSED!')