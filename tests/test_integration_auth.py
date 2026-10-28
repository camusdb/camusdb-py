"""Authentication against a server started with CAMUSDB_AUTH_ENABLED=true.

Set CAMUSDB_TEST_AUTH_ENDPOINT, CAMUSDB_TEST_AUTH_USER and CAMUSDB_TEST_AUTH_PASSWORD (a superuser).
"""

import os
import uuid

import pytest

import camusdb
from camusdb import codes
from camusdb._auth import TOKENS

ENDPOINT = os.environ.get("CAMUSDB_TEST_AUTH_ENDPOINT")
USER = os.environ.get("CAMUSDB_TEST_AUTH_USER", "admin")
PASSWORD = os.environ.get("CAMUSDB_TEST_AUTH_PASSWORD", "")

pytestmark = pytest.mark.skipif(not ENDPOINT, reason="set CAMUSDB_TEST_AUTH_ENDPOINT to run")


# One database for the module, and the token cache kept between tests: the server permits 20 logins
# for each account each minute, and every login the tests do not need brings a run nearer to CADB0518.
@pytest.fixture(scope="module")
def database():
    TOKENS._tokens.clear()
    name = "auth_" + uuid.uuid4().hex[:10]
    with camusdb.connect(
        endpoint=ENDPOINT, database=name, user=USER, password=PASSWORD, autocommit=True
    ) as admin:
        admin.create_database()
    yield name
    with camusdb.connect(
        endpoint=ENDPOINT, database=name, user=USER, password=PASSWORD, autocommit=True
    ) as admin:
        admin.drop_database()


def test_statements_carry_the_token(database):
    with camusdb.connect(
        endpoint=ENDPOINT, database=database, user=USER, password=PASSWORD
    ) as conn:
        conn.execute("CREATE TABLE t (id OID PRIMARY KEY NOT NULL, n INT64)")
        conn.execute("INSERT INTO t (id, n) VALUES (GEN_ID(), 1)")
        conn.commit()
        assert conn.execute("SELECT n FROM t").fetchall() == [(1,)]
        assert conn.access_token


def test_no_credentials_are_refused(database):
    with (
        camusdb.connect(endpoint=ENDPOINT, database=database, autocommit=True) as conn,
        pytest.raises(camusdb.OperationalError) as info,
    ):
        conn.execute("SELECT 1")
    assert info.value.code == codes.AUTHENTICATION_FAILED


def test_a_wrong_password_is_refused(database):
    with (
        camusdb.connect(
            endpoint=ENDPOINT, database=database, user=USER, password="wrong", autocommit=True
        ) as conn,
        pytest.raises(camusdb.OperationalError) as info,
    ):
        conn.execute("SELECT 1")
    assert info.value.code == codes.AUTHENTICATION_FAILED


def test_explicit_login_and_logout(database):
    with camusdb.connect(endpoint=ENDPOINT, database=database, autocommit=True) as conn:
        token = conn.login(USER, PASSWORD)
        assert conn.access_token == token
        assert conn.execute("SELECT 1").fetchone() == (1,)
        conn.logout()
        # The credentials stay, so the next statement logs in again.
        assert conn.execute("SELECT 1").fetchone() == (1,)
        assert conn.access_token != token


def test_a_revoked_token_is_renewed_transparently(database):
    with camusdb.connect(
        endpoint=ENDPOINT, database=database, user=USER, password=PASSWORD, autocommit=True
    ) as conn:
        conn.execute("SELECT 1")
        token = conn.access_token
        # Revoke the token behind the driver's back: the store still holds it.
        with camusdb.connect(
            endpoint=ENDPOINT, database=database, access_token=token, autocommit=True
        ) as other:
            other._transport.logout()
        assert conn._transport.auth.cached().value == token
        assert conn.execute("SELECT 1").fetchone() == (1,)
        assert conn.access_token != token


def test_a_static_token(database):
    with camusdb.connect(endpoint=ENDPOINT, database=database, autocommit=True) as conn:
        token = conn.login(USER, PASSWORD)
    TOKENS._tokens.clear()
    with camusdb.connect(
        endpoint=ENDPOINT, database=database, access_token=token, autocommit=True
    ) as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_create_user_with_a_bound_password(database):
    with camusdb.connect(
        endpoint=ENDPOINT, database=database, user=USER, password=PASSWORD, autocommit=True
    ) as conn:
        name = "u" + uuid.uuid4().hex[:8]
        conn.execute(f"CREATE USER {name} IDENTIFIED BY @password", {"password": "app-secret-1"})
        conn.execute(f"GRANT SELECT ON {database}.* TO {name}")
    with camusdb.connect(
        endpoint=ENDPOINT, database=database, user=name, password="app-secret-1", autocommit=True
    ) as app:
        assert app.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(camusdb.ProgrammingError) as info:
            app.execute("CREATE TABLE nope (id OID PRIMARY KEY NOT NULL)")
        assert info.value.code == codes.INSUFFICIENT_PRIVILEGE
