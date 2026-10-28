import pytest

import camusdb
from camusdb import IsolationLevel, Locking, codes
from camusdb._config import build_config, parse_connection_string, redact


class TestConnectionString:
    def test_keys_ignore_case_and_space(self):
        assert parse_connection_string(" Endpoint = http://a:1 ; DATABASE=test;") == {
            "endpoint": "http://a:1",
            "database": "test",
        }

    def test_quotes_keep_semicolons_and_doubled_quotes(self):
        pairs = parse_connection_string("Password='pa;ss''word';User=\"a b\"")
        assert pairs == {"password": "pa;ss'word", "user": "a b"}

    def test_a_repeated_key_is_an_error(self):
        with pytest.raises(camusdb.ProgrammingError, match="repeated"):
            parse_connection_string("User=a;user=b")

    def test_an_unclosed_quote_is_an_error(self):
        with pytest.raises(camusdb.ProgrammingError, match="closing quote"):
            parse_connection_string("Password='abc")

    def test_redact_masks_secrets(self):
        text = redact("Endpoint=http://a:1;Database=d;User=u;Password=secret;AccessToken=xyz789")
        assert "secret" not in text
        assert "xyz789" not in text
        assert "user=u" in text


class TestBuildConfig:
    def test_the_dotnet_string(self):
        config = build_config(
            "Endpoint=http://localhost:5095,http://localhost:5096/;Database=test;Timeout=30;"
            "IsolationLevel=ReadCommitted;Locking=Optimistic;TransactionMode=ReadOnly;"
            "MaxAutoPrepare=64",
            {},
        )
        assert config.endpoints == ("http://localhost:5095", "http://localhost:5096")
        assert config.database == "test"
        assert config.timeout == 30
        assert config.isolation_level is IsolationLevel.READ_COMMITTED
        assert config.locking is Locking.OPTIMISTIC
        assert config.read_only is True
        assert config.autocommit is False

    def test_keywords_win_over_the_string(self):
        config = build_config(
            "Endpoint=http://a:1;Database=one", {"database": "two", "endpoint": None}
        )
        assert config.database == "two"
        assert config.endpoints == ("http://a:1",)

    def test_endpoint_list(self):
        config = build_config(None, {"endpoint": ["http://a:1", "http://b:2"], "database": "d"})
        assert config.endpoints == ("http://a:1", "http://b:2")

    def test_grpc_is_not_supported(self):
        with pytest.raises(camusdb.NotSupportedError):
            build_config("Endpoint=http://a:1;Database=d;Protocol=grpc", {})

    def test_an_unknown_key_is_an_error(self):
        with pytest.raises(camusdb.ProgrammingError, match="unknown key"):
            build_config("Endpoint=http://a:1;Database=d;Colour=blue", {})

    def test_a_bad_endpoint_is_an_error(self):
        with pytest.raises(camusdb.ProgrammingError, match="not an endpoint"):
            build_config(None, {"endpoint": "localhost:5095", "database": "d"})

    def test_a_database_is_required(self):
        with pytest.raises(camusdb.ProgrammingError, match="database"):
            build_config("Endpoint=http://a:1", {})

    @pytest.mark.parametrize(
        "endpoint", ["http://localhost:1", "http://127.0.0.1:1", "http://[::1]:1", "https://db:1"]
    )
    def test_credentials_are_allowed_over_tls_or_loopback(self, endpoint):
        build_config(None, {"endpoint": endpoint, "database": "d", "user": "u", "password": "p"})

    def test_credentials_in_clear_text_to_a_remote_host_are_refused(self):
        with pytest.raises(camusdb.InterfaceError) as info:
            build_config(None, {"endpoint": "http://db:1", "database": "d", "access_token": "t"})
        assert info.value.code == codes.INSECURE_TRANSPORT

    def test_the_insecure_override(self):
        build_config(
            None,
            {
                "endpoint": "http://db:1",
                "database": "d",
                "user": "u",
                "password": "p",
                "allow_insecure_credentials": True,
            },
        )

    def test_the_password_is_not_in_the_repr(self):
        config = build_config(
            None,
            {"endpoint": "http://localhost:1", "database": "d", "user": "u", "password": "hunter2"},
        )
        assert "hunter2" not in repr(config)

    def test_an_unknown_keyword_is_a_type_error(self):
        with pytest.raises(TypeError):
            build_config(None, {"endpoint": "http://a:1", "database": "d", "colour": "blue"})
