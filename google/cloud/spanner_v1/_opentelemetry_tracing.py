# Copyright 2020 Google LLC All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Manages OpenTelemetry trace creation and handling"""

from contextlib import contextmanager
import os

from google.cloud.spanner_v1 import SpannerClient
from google.cloud.spanner_v1 import gapic_version

try:
    from opentelemetry import trace
    from opentelemetry.trace.status import Status, StatusCode
    from opentelemetry.semconv.attributes.otel_attributes import (
        OTEL_SCOPE_NAME,
        OTEL_SCOPE_VERSION,
    )

    HAS_OPENTELEMETRY_INSTALLED = True
except ImportError:
    HAS_OPENTELEMETRY_INSTALLED = False

TRACER_NAME = "cloud.google.com/python/spanner"
TRACER_VERSION = gapic_version.__version__
extended_tracing_globally_disabled = (
    os.getenv("SPANNER_ENABLE_EXTENDED_TRACING", "").lower() == "false"
)


def get_tracer(tracer_provider=None):
    """
    get_tracer is a utility to unify and simplify retrieval of the tracer, without
    leaking implementation details given that retrieving a tracer requires providing
    the full qualified library name and version.
    When the tracer_provider is set, it'll retrieve the tracer from it, otherwise
    it'll fall back to the global tracer provider and use this library's specific semantics.
    """
    if not tracer_provider:
        # Acquire the global tracer provider.
        tracer_provider = trace.get_tracer_provider()

    return tracer_provider.get_tracer(TRACER_NAME, TRACER_VERSION)


def _make_tracer_and_span_attributes(
    session=None, extra_attributes=None, observability_options=None
):
    if not HAS_OPENTELEMETRY_INSTALLED:
        return None, None

    tracer_provider = None

    # By default enable_extended_tracing=True because in a bid to minimize
    # breaking changes and preserve legacy behavior, we are keeping it turned
    # on by default.
    enable_extended_tracing = True

    db_name = ""
    if session and getattr(session, "_database", None):
        db_name = session._database.name

    if isinstance(observability_options, dict):  # Avoid false positives with mock.Mock
        tracer_provider = observability_options.get("tracer_provider", None)
        enable_extended_tracing = observability_options.get(
            "enable_extended_tracing", enable_extended_tracing
        )
        db_name = observability_options.get("db_name", db_name)

    tracer = get_tracer(tracer_provider)

    # Set base attributes that we know for every trace created
    attributes = {
        "db.type": "spanner",
        "db.url": SpannerClient.DEFAULT_ENDPOINT,
        "db.instance": db_name,
        "net.host.name": SpannerClient.DEFAULT_ENDPOINT,
        OTEL_SCOPE_NAME: TRACER_NAME,
        OTEL_SCOPE_VERSION: TRACER_VERSION,
    }

    if extra_attributes:
        attributes.update(extra_attributes)

    if extended_tracing_globally_disabled:
        enable_extended_tracing = False

    if not enable_extended_tracing:
        attributes.pop("db.statement", False)
        attributes.pop("sql", False)
    else:
        # Otherwise there are places where the annotated sql was inserted
        # directly from the arguments as "sql", and transform those into "db.statement".
        db_statement = attributes.get("db.statement", None)
        if not db_statement:
            sql = attributes.get("sql", None)
            if sql:
                attributes = attributes.copy()
                attributes.pop("sql", False)
                attributes["db.statement"] = sql

    return tracer, attributes


def trace_call_end_lazily(
    name, session=None, extra_attributes=None, observability_options=None
):
    """
    trace_call_end_lazily is used in situations where you don't want a context managed
    span in a with statement to end as soon as a block exits. This is useful for example
    after a Database.batch or Database.snapshot but without a context manager.
    If you need to directly invoke tracing with a context manager, please invoke
    `trace_call` with which you can invoke
    ￼        `with trace_call(...) as span:`
    It is the caller's responsibility to explicitly invoke the returned ending function.
    """

    if not name:
        return None

    tracer, span_attributes = _make_tracer_and_span_attributes(
        session, extra_attributes, observability_options
    )
    if not tracer:
        return None

    span = tracer.start_span(
        name, kind=trace.SpanKind.CLIENT, attributes=span_attributes
    )
    ctx_manager = trace.use_span(span, end_on_exit=True, record_exception=True)
    ctx_manager.__enter__()

    def discard(exc_type=None, exc_value=None, exc_traceback=None):
        if not exc_type:
            span.set_status(Status(StatusCode.OK))

        ctx_manager.__exit__(exc_type, exc_value, exc_traceback)

    return discard


@contextmanager
def trace_call(name, session=None, extra_attributes=None, observability_options=None):
    """
    ￼   trace_call is used in situations where you need to end a span with a context manager
    ￼   or after a scope is exited. If you need to keep a span alive and lazily end it, please
    ￼   invoke `trace_call_end_lazily`.
    """
    if not name:
        yield None
        return

    tracer, span_attributes = _make_tracer_and_span_attributes(
        session, extra_attributes, observability_options
    )
    if not tracer:
        yield None
        return

    with tracer.start_as_current_span(
        name, kind=trace.SpanKind.CLIENT, attributes=span_attributes
    ) as span:
        try:
            yield span
        except Exception as error:
            span.set_status(Status(StatusCode.ERROR, str(error)))
            # OpenTelemetry-Python imposes invoking span.record_exception on __exit__
            # on any exception. We should file a bug later on with them to only
            # invoke .record_exception if not already invoked, hence we should not
            # invoke .record_exception on our own else we shall have 2 exceptions.
            raise
        else:
            if span._status.status_code == StatusCode.UNSET:
                # OpenTelemetry-Python only allows a status change
                # if the current code is UNSET or ERROR. At the end
                # of the generator's consumption, only set it to OK
                # it wasn't previously set otherwise
                span.set_status(Status(StatusCode.OK))


def set_span_status_error(span, error):
    if span:
        span.set_status(Status(StatusCode.ERROR, str(error)))


def set_span_status_ok(span):
    if span:
        span.set_status(Status(StatusCode.OK))


def get_current_span():
    if not HAS_OPENTELEMETRY_INSTALLED:
        return None
    return trace.get_current_span()


def add_span_event(span, event_name, event_attributes=None):
    if span:
        span.add_event(event_name, event_attributes)


def add_event_on_current_span(event_name, attributes=None, span=None):
    if not span:
        span = get_current_span()

    if span:
        span.add_event(event_name, attributes)


def record_span_exception_and_status(span, exc):
    if span:
        span.set_status(Status(StatusCode.ERROR, str(exc)))
        span.record_exception(exc)
