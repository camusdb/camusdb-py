import pytest

import camusdb
from camusdb import codes
from camusdb._protocol import parse_response, sanitize
from camusdb.errors import error_for_code


@pytest.mark.parametrize(
    ("code", "cls"),
    [
        ("CADB0011", camusdb.ProgrammingError),
        ("CADB0300", camusdb.IntegrityError),
        ("CADB0303", camusdb.IntegrityError),
        ("CADB0402", camusdb.IntegrityError),
        ("CADB0302", camusdb.DataError),
        ("CADB0406", camusdb.ProgrammingError),
        ("CADB0502", camusdb.SerializationFailure),
        ("CADB0509", camusdb.OperationalError),
        ("CADB0516", camusdb.OperationalError),
        ("CADB0517", camusdb.ProgrammingError),
        ("CADB0533", camusdb.NotSupportedError),
        ("CADB0540", camusdb.InternalError),
        ("CA0000", camusdb.InternalError),
        ("CADB0000", camusdb.DatabaseError),
        ("CADB0899", camusdb.DatabaseError),
    ],
)
def test_codes_map_to_pep249_classes(code, cls):
    exc = error_for_code(code, "message")
    assert type(exc) is cls
    assert exc.code == code
    assert exc.message == "message"
    assert str(exc) == f"{code}: message"


def test_every_error_is_a_camusdb_error():
    assert issubclass(camusdb.SerializationFailure, camusdb.OperationalError)
    assert issubclass(camusdb.OperationalError, camusdb.DatabaseError)
    assert issubclass(camusdb.InterfaceError, camusdb.Error)


def test_a_failed_body_raises_its_code():
    content = b'{"status":"failed","code":"CADB0011","message":"Table \'x\' doesn\'t exist"}'
    with pytest.raises(camusdb.ProgrammingError) as info:
        parse_response(500, content)
    assert info.value.code == codes.TABLE_DOESNT_EXIST


def test_a_failure_without_json():
    with pytest.raises(camusdb.DatabaseError, match="HTTP 502"):
        parse_response(502, b"<html>bad gateway</html>")


def test_sanitize_masks_secrets_and_control_characters():
    text = sanitize('login failed: password="hunter2", next\nline Bearer abc.def')
    assert "hunter2" not in text
    assert "abc.def" not in text
    assert "\n" not in text


def test_sanitize_limits_the_length():
    assert len(sanitize("x" * 5000)) <= 2049


def test_is_retryable():
    assert camusdb.is_retryable(error_for_code("CADB0502"))
    assert camusdb.is_retryable(error_for_code("CADB0400", "create failed: MustRetry"))
    assert not camusdb.is_retryable(error_for_code("CADB0509", "MustRetry"))
    assert not camusdb.is_retryable(error_for_code("CADB0011"))
    assert not camusdb.is_retryable(ValueError())

    try:
        try:
            raise error_for_code("CADB0504")
        except camusdb.Error as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert camusdb.is_retryable(outer)
