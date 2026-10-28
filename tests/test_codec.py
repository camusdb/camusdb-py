import datetime as dt
import decimal
import uuid

import pytest

import camusdb
from camusdb import ColumnType, ObjectId, Typed
from camusdb._codec import (
    date_to_ticks,
    datetime_to_ticks,
    decode_cell,
    decode_row,
    encode_parameters,
    encode_value,
    read_schema,
    ticks_to_date,
    ticks_to_datetime,
)

UTC = dt.timezone.utc


class TestEncode:
    def test_names_get_the_placeholder_sigil(self):
        encoded = encode_parameters({"year": 1984, "@name": "T-800"})
        assert encoded == {
            "@year": {"type": ColumnType.INT64, "longValue": 1984},
            "@name": {"type": ColumnType.STRING, "strValue": "T-800"},
        }

    def test_the_same_name_twice_is_refused(self):
        with pytest.raises(camusdb.ProgrammingError, match="more than once"):
            encode_parameters({"year": 1, "@year": 2})

    def test_a_sequence_is_refused(self):
        with pytest.raises(camusdb.ProgrammingError, match="mapping"):
            encode_parameters([1, 2])  # type: ignore[arg-type]

    def test_empty_parameters_send_nothing(self):
        assert encode_parameters({}) is None
        assert encode_parameters(None) is None

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, {"type": 0}),
            (True, {"type": 4, "boolValue": True}),
            (-(2**63), {"type": 2, "longValue": -(2**63)}),
            (2.5, {"type": 5, "floatValue": 2.5}),
            (decimal.Decimal("19.990"), {"type": 12, "strValue": "19.990"}),
            (decimal.Decimal("1E+3"), {"type": 12, "strValue": "1000"}),
            ("hé", {"type": 3, "strValue": "hé"}),
            (b"\xde\xad", {"type": 7, "bytesValue": "3q0="}),
            (
                uuid.UUID("0191d3a4-5b6c-7d8e-9f00-112233445566"),
                {"type": 11, "strValue": "0191d3a4-5b6c-7d8e-9f00-112233445566"},
            ),
            (
                ObjectId("6AE24C3A5C7D131D88E2ED42"),
                {"type": 1, "strValue": "6ae24c3a5c7d131d88e2ed42"},
            ),
        ],
    )
    def test_types_come_from_python_types(self, value, expected):
        assert encode_value("@p", value) == expected

    def test_bool_is_not_an_int(self):
        assert encode_value("@p", False)["type"] == ColumnType.BOOL

    def test_an_int_outside_int64_is_refused(self):
        with pytest.raises(camusdb.DataError, match="INT64"):
            encode_value("@p", 2**63)

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_a_float_that_json_cannot_hold_is_refused(self, value):
        with pytest.raises(camusdb.DataError):
            encode_value("@p", value)

    def test_datetime_is_sent_as_utc_ticks(self):
        aware = dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
        naive_utc = dt.datetime(2026, 5, 1, 10, 0)
        assert encode_value("@p", aware) == encode_value("@p", naive_utc)
        assert encode_value("@p", aware)["type"] == ColumnType.DATETIME

    def test_date_is_sent_as_midnight_ticks(self):
        assert encode_value("@p", dt.date(2026, 5, 1)) == {
            "type": ColumnType.DATE,
            "longValue": datetime_to_ticks(dt.datetime(2026, 5, 1, tzinfo=UTC)),
        }

    def test_array_element_type_is_inferred(self):
        assert encode_value("@p", [1, None, 3]) == {
            "type": ColumnType.ARRAY,
            "arrayElementType": ColumnType.INT64,
            "arrayValues": [
                {"type": 2, "longValue": 1},
                {"type": 0},
                {"type": 2, "longValue": 3},
            ],
        }

    def test_a_decimal_array_travels_as_float64(self):
        encoded = encode_value("@p", [decimal.Decimal("1.5")])
        assert encoded["arrayElementType"] == ColumnType.FLOAT64
        assert encoded["arrayValues"] == [{"type": 5, "floatValue": 1.5}]

    def test_an_all_null_array_needs_a_type(self):
        with pytest.raises(camusdb.ProgrammingError, match="element type"):
            encode_value("@p", [None])
        typed = encode_value("@p", Typed([None], ColumnType.ARRAY, element_type=ColumnType.STRING))
        assert typed["arrayElementType"] == ColumnType.STRING

    def test_an_empty_array_has_no_element_type(self):
        assert encode_value("@p", []) == {
            "type": ColumnType.ARRAY,
            "arrayElementType": ColumnType.NULL,
            "arrayValues": [],
        }

    def test_typed_values(self):
        assert encode_value("@p", Typed(0.5, ColumnType.FLOAT32)) == {"type": 6, "floatValue": 0.5}
        assert encode_value("@p", Typed("12345678901234567890.5", ColumnType.NUMERIC)) == {
            "type": 12,
            "strValue": "12345678901234567890.5",
        }
        assert encode_value("@p", Typed("6ae24c3a5c7d131d88e2ed42", ColumnType.ID))["type"] == 1
        assert encode_value("@p", Typed("2026-05-01", ColumnType.DATE))["type"] == 8

    def test_a_typed_value_that_does_not_convert_is_a_data_error(self):
        with pytest.raises(camusdb.DataError, match="ID"):
            encode_value("@p", Typed("not-an-oid", ColumnType.ID))
        with pytest.raises(camusdb.DataError, match="whole number"):
            encode_value("@p", Typed(1.5, ColumnType.INT64))

    def test_an_unknown_python_type_is_refused(self):
        with pytest.raises(camusdb.ProgrammingError, match="Typed"):
            encode_value("@p", object())


class TestTicks:
    def test_round_trip(self):
        value = dt.datetime(2026, 10, 28, 19, 25, 37, 708785, tzinfo=UTC)
        assert ticks_to_datetime(datetime_to_ticks(value)) == value

    def test_known_values(self):
        # 2025-04-02T00:00:00Z in .NET ticks, as the server stored it.
        assert ticks_to_date(638_791_488_000_000_000) == dt.date(2025, 4, 2)
        assert date_to_ticks(dt.date(2025, 4, 2)) == 638_791_488_000_000_000
        assert datetime_to_ticks(dt.datetime(1970, 1, 1, tzinfo=UTC)) == 621_355_968_000_000_000


class TestDecode:
    def test_a_row_is_decoded_against_its_schema(self):
        schema = read_schema(
            [
                {"name": "id", "type": 1},
                {"name": "u", "type": 11},
                {"name": "n", "type": 12},
                {"name": "f", "type": 6},
                {"name": "d", "type": 8},
                {"name": "dt", "type": 9},
                {"name": "b", "type": 7},
                {"name": "a", "type": 10, "elementType": 2},
                {"name": "s", "type": 3},
                {"name": "i", "type": 2},
                {"name": "bo", "type": 4},
                {"name": "ad", "type": 10, "elementType": 8},
            ]
        )
        row = decode_row(
            schema,
            [
                "6ae24c3a5c7d131d88e2ed42",
                [113104168522907022, -6989567783092333210],
                "12345678901234567890.123456789",
                1.1,
                638800000000000000,
                638800123456789012,
                "3q0=",
                [1, None],
                "hé",
                -9007199254740993,
                True,
                [638800000000000000],
            ],
        )
        assert row == (
            "6ae24c3a5c7d131d88e2ed42",
            uuid.UUID("0191d3a4-5b6c-7d8e-9f00-112233445566"),
            decimal.Decimal("12345678901234567890.123456789"),
            pytest.approx(1.1, rel=1e-7),
            ticks_to_date(638800000000000000),
            ticks_to_datetime(638800123456789012),
            b"\xde\xad",
            [1, None],
            "hé",
            -9007199254740993,
            True,
            [ticks_to_date(638800000000000000)],
        )

    def test_float32_reads_back_as_the_stored_single(self):
        value = decode_cell(1.1, ColumnType.FLOAT32)
        assert value != 1.1
        assert value == pytest.approx(1.1, rel=1e-7)

    def test_a_float64_zero_is_a_float(self):
        assert isinstance(decode_cell(0, ColumnType.FLOAT64), float)

    def test_a_missing_position_is_none(self):
        schema = read_schema([{"name": "a", "type": 2}, {"name": "b", "type": 2}])
        assert decode_row(schema, [1]) == (1, None)

    def test_a_uuid_mistagged_as_string_is_recovered(self):
        assert decode_cell([0, 1], ColumnType.STRING) == uuid.UUID(int=1)

    def test_numeric_from_a_json_number(self):
        assert decode_cell(0, ColumnType.NUMERIC) == decimal.Decimal(0)

    def test_an_unknown_type_is_decoded_by_token(self):
        schema = read_schema([{"name": "x", "type": 99}])
        assert decode_row(schema, [7]) == (7,)

    def test_array_without_element_type_uses_tokens(self):
        assert decode_cell([1, "a", True], ColumnType.ARRAY) == [1, "a", True]
