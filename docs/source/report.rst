Sanitising collection reports
=============================

Header collection reports often replay synthetic requests.  A field name can
therefore appear normally in one record and with the HPACK ``never indexed``
representation in another.  The two must not be collapsed into one value.

``hpack.clean_records`` is a cleaning export built on
:class:`~hpack.NeverIndexedHeaderTuple` and the decoded header tuples.  It
keeps the distinction and withholds restricted values:

.. code-block:: python

    from hpack import (
        NeverIndexedHeaderTuple,
        ReportPolicy,
        clean_records,
    )

    report = clean_records(
        {
            "request-1": [("cookie", "")],
            "request-2": [NeverIndexedHeaderTuple("cookie", "session=secret")],
        },
        policy=ReportPolicy(allowed_aggregates={"count", "empty_count"}),
    )

    print(report.fields[0].value)   # ValueState.EMPTY
    print(report.fields[1].value)   # ValueState.REDACTED
    print(report.duplicates[0].name)  # "cookie"

    print(report.statistics_json())

``EMPTY`` and ``REDACTED`` are distinct markers.  An ordinary empty field is
reported as ``EMPTY``; a restricted field is always ``REDACTED``, even when
its original value was empty.  Both occur together in
:attr:`CleanedReport.duplicates`.

Restricted occurrences expose only the name, the record, the field position
and the occurrence ordinal.  Any aggregate quantities must be enabled
explicitly with :class:`ReportPolicy`; the default is none.  The serialised
statistics record deliberately omits detail records, so it contains no header
values, and :class:`SanitizationError` summaries contain structural details
only.
