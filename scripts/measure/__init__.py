"""Measurements over artifacts too large to read.

    from measure import Measured, Unmeasured, Refused
    from measure.dumpdb import open_default

Nothing here ever returns a bare number. See result.py for why.
"""
from measure.result import (  # noqa: F401
    Measurement, Measured, Unmeasured, Refused, Report, UnmeasuredError,
)
