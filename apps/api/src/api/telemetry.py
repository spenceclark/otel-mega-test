"""One TracerProvider + one LoggerProvider per process, exported over OTLP/HTTP protobuf.

Endpoint/protocol come from the standard OTEL_EXPORTER_OTLP_* variables, the sampler from OTEL_TRACES_SAMPLER
(SDK default parentbased_always_on), resource attributes from OTEL_RESOURCE_ATTRIBUTES.
"""

import logging
import os

from opentelemetry import propagate, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# The package ships no py.typed; the handler's signature is annotated in source.
from opentelemetry.instrumentation.logging.handler import LoggingHandler  # pyright: ignore[reportMissingTypeStubs]
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_PROPAGATORS = {"tracecontext": TraceContextTextMapPropagator, "baggage": W3CBaggagePropagator}
_providers: tuple[TracerProvider, LoggerProvider] | None = None


def setup(
    service_name: str, *, span_exporter: SpanExporter | None = None, log_exporter: LogRecordExporter | None = None
) -> None:
    """Install the process-wide providers once; later calls are no-ops.

    Passing exporters (tests) switches to synchronous processors so assertions need no flush.
    """
    global _providers
    if _providers is not None:
        return

    # opentelemetry.propagate composes its global propagator at import time, before this runs; recompose it
    # here so OTEL_PROPAGATORS is honoured and an unset variable means tracecontext only (baggage off).
    names = os.environ.get("OTEL_PROPAGATORS", "tracecontext").split(",")
    propagate.set_global_textmap(CompositePropagator([_PROPAGATORS[name.strip()]() for name in names]))

    # Resource.create lets explicit attributes override OTEL_SERVICE_NAME, so read the env first.
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter()) if span_exporter is None else SimpleSpanProcessor(span_exporter)
    )
    trace.set_tracer_provider(tracer_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter()) if log_exporter is None else SimpleLogRecordProcessor(log_exporter)
    )
    set_logger_provider(logger_provider)

    # Bridge stdlib logging: records emitted inside a span carry its trace/span ids. Only our own `api.*`
    # loggers are raised to INFO; libraries stay at WARNING so their INFO lines (request URLs etc.) stay local.
    logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))
    logging.getLogger("api").setLevel(logging.INFO)

    _providers = tracer_provider, logger_provider


def shutdown() -> None:
    """Flush and stop both providers."""
    if _providers is not None:
        for provider in _providers:
            provider.shutdown()
