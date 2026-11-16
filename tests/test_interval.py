"""The interval support: the ISO-8601 text of Interval, the timedelta conversions, and parameters.

The server sends an interval as its ISO-8601 text in a STRING column, and reads a parameter as text
with CAST(@p AS interval). No server is necessary. The cases follow TestIntervalValues in the .NET
driver.
"""

import datetime as dt

import pytest

import camusdb
from camusdb import ColumnType, Interval, Typed
from camusdb._codec import decode_cell, encode_value

HOUR = 3_600_000_000
MINUTE = 60_000_000


class TestText:
    # Each pair is the server's text for the parts, checked on a live server.
    @pytest.mark.parametrize(
        ("text", "months", "days", "micros"),
        [
            ("PT0S", 0, 0, 0),
            ("P1DT2H", 0, 1, 2 * HOUR),
            ("P1DT15H", 0, 1, 15 * HOUR),
            ("P-7D", 0, -7, 0),
            ("P43Y9M27D", 525, 27, 0),
            ("P1Y2M3DT4H5M6.5S", 14, 3, 4 * HOUR + 5 * MINUTE + 6_500_000),
            ("P-1DT-12H", 0, -1, -12 * HOUR),
            ("PT-1H-30M-5.25S", 0, 0, -(HOUR + 30 * MINUTE + 5_250_000)),
            ("P1DT2H3M4.123457S", 0, 1, 2 * HOUR + 3 * MINUTE + 4_123_457),
            ("PT-0.000001S", 0, 0, -1),
            ("P-1Y-2M", -14, 0, 0),
        ],
    )
    def test_the_server_text_round_trips(self, text, months, days, micros):
        value = Interval.parse(text)
        assert value == Interval(months, days, micros)
        assert str(value) == text

    @pytest.mark.parametrize(
        ("text", "months", "days", "micros"),
        [
            ("P2W", 0, 14, 0),
            ("p1dt2h", 0, 1, 2 * HOUR),
            (" PT1.0000005S ", 0, 0, 1_000_000),
            ("PT1.0000015S", 0, 0, 1_000_002),
            ("P+1D", 0, 1, 0),
        ],
    )
    def test_parse_reads_other_iso_forms(self, text, months, days, micros):
        assert Interval.parse(text) == Interval(months, days, micros)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "P",
            "PT",
            "P1DT",
            "1 day",
            "01:00:00",
            "P1.5D",
            "P1H",
            "PT1D",
            "P1DT2H3",
            "PTT1H",
            "P.D",
            "P+D",
            "P1e3D",
            "PInfinityD",
        ],
    )
    def test_parse_refuses_text_that_is_not_an_iso_interval(self, text):
        with pytest.raises(ValueError, match="not an ISO-8601 interval"):
            Interval.parse(text)

    def test_parse_refuses_a_part_out_of_range(self):
        with pytest.raises(OverflowError):
            Interval.parse("P9999999999D")
        with pytest.raises(OverflowError):
            Interval.parse("PT9999999999999999999S")
        with pytest.raises(TypeError):
            Interval.parse(None)  # type: ignore[arg-type]

    def test_the_parts_are_checked(self):
        with pytest.raises(OverflowError):
            Interval(2**31, 0, 0)
        with pytest.raises(OverflowError):
            Interval(0, 0, 2**63)
        with pytest.raises(TypeError):
            Interval(0, 1.5, 0)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            Interval(True, 0, 0)

    def test_equality_compares_the_three_parts(self):
        # On the server INTERVAL 'P1M' = INTERVAL 'P30D' is true. Here the parts differ.
        assert Interval(1, 0, 0) != Interval(0, 30, 0)
        assert Interval(1, 2, 3) == Interval.parse("P1M2DT0.000003S")
        assert hash(Interval(1, 2, 3)) == hash(Interval(1, 2, 3))


class TestTimedelta:
    def test_a_timedelta_keeps_its_days_in_the_days_part(self):
        span = dt.timedelta(days=1, hours=2, minutes=3, seconds=4, milliseconds=500)

        interval = Interval.from_timedelta(span)

        assert interval == Interval(0, 1, 2 * HOUR + 3 * MINUTE + 4_500_000)
        assert str(interval) == "P1DT2H3M4.5S"
        assert interval.to_timedelta() == span

    def test_a_negative_timedelta_gives_both_parts_its_sign(self):
        negative = -dt.timedelta(hours=36)
        assert str(Interval.from_timedelta(negative)) == "P-1DT-12H"
        assert Interval.parse("P-1DT-12H").to_timedelta() == negative
        assert str(Interval.from_timedelta(-dt.timedelta(microseconds=1))) == "PT-0.000001S"

    def test_to_timedelta_refuses_a_month_part(self):
        with pytest.raises(ValueError, match="month"):
            Interval.parse("P1M").to_timedelta()

    def test_to_timedelta_refuses_a_value_out_of_range(self):
        with pytest.raises(OverflowError):
            Interval(0, 2**31 - 1, 2**63 - 1).to_timedelta()


class TestParameters:
    def test_a_timedelta_binds_as_interval_text(self):
        assert encode_value("@p", dt.timedelta(days=1, hours=2)) == {
            "type": ColumnType.STRING,
            "strValue": "P1DT2H",
        }
        assert encode_value("@p", dt.timedelta(0))["strValue"] == "PT0S"
        assert encode_value("@p", dt.timedelta(microseconds=1))["strValue"] == "PT0.000001S"

    def test_a_typed_string_timedelta_binds_as_interval_text(self):
        value = encode_value("@p", Typed(dt.timedelta(minutes=90), ColumnType.STRING))
        assert value == {"type": ColumnType.STRING, "strValue": "PT1H30M"}

    def test_an_interval_binds_as_its_text(self):
        assert encode_value("@p", Interval(14, 3, 0)) == {
            "type": ColumnType.STRING,
            "strValue": "P1Y2M3D",
        }

    def test_an_array_of_timedelta_needs_an_explicit_element_type(self):
        spans = [dt.timedelta(hours=1), dt.timedelta(days=1)]

        with pytest.raises(camusdb.ProgrammingError, match="interval") as excinfo:
            encode_value("@p", spans)
        assert excinfo.value.code == "CADB0400"

        array = encode_value("@p", Typed(spans, ColumnType.ARRAY, element_type=ColumnType.STRING))
        assert array["arrayElementType"] == ColumnType.STRING
        assert [e["strValue"] for e in array["arrayValues"]] == ["PT1H", "P1D"]

    def test_a_null_interval_binds_as_null(self):
        assert encode_value("@p", Typed(None, ColumnType.STRING)) == {"type": ColumnType.NULL}


def test_a_result_interval_reads_as_text():
    # The cell of an interval is a STRING. Interval.parse reads it.
    cell = decode_cell("P1DT2H3M4.123457S", ColumnType.STRING)
    assert cell == "P1DT2H3M4.123457S"
    assert Interval.parse(cell).to_timedelta() == dt.timedelta(
        days=1, hours=2, minutes=3, seconds=4, microseconds=123_457
    )
