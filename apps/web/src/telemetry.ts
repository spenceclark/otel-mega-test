// Browser telemetry: one TracerProvider, one LoggerProvider, initialized once at module scope
// (imported first by main.tsx, before createRoot). No credentials of any kind live here:
// both exporters post same-origin to the nginx gateway, which forwards to the collector.
//
// Context manager: StackContextManager, deliberately. It is synchronous: context.with() sets the
// active context for the duration of the callback and restores it when the callback returns,
// which for an async callback means at its first `await`. The fetch instrumentation reads
// context.active() synchronously when fetch() is called, so starting fetch inside ui.ask's
// callback is enough to parent the CLIENT span. Zone.js would not help: Vite emits native
// async/await (ES2017+), which zone.js cannot patch, so ambient context is lost after `await`
// either way. AskPage records that fact as `demo.context_after_await` and re-enters explicitly.
import { W3CTraceContextPropagator } from '@opentelemetry/core';
import { OTLPLogExporter } from '@opentelemetry/exporter-logs-otlp-proto';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-proto';
import { registerInstrumentations } from '@opentelemetry/instrumentation';
import { FetchInstrumentation } from '@opentelemetry/instrumentation-fetch';
import { resourceFromAttributes } from '@opentelemetry/resources';
import { BatchLogRecordProcessor, LoggerProvider } from '@opentelemetry/sdk-logs';
import {
    BatchSpanProcessor,
    StackContextManager,
    WebTracerProvider,
    type SpanProcessor,
} from '@opentelemetry/sdk-trace-web';
import { ATTR_SERVICE_NAME, ATTR_SERVICE_VERSION } from '@opentelemetry/semantic-conventions';

function init() {
    const version: string = import.meta.env.VITE_SERVICE_VERSION || 'dev';
    const resource = resourceFromAttributes({
        [ATTR_SERVICE_NAME]: 'trace-demo-web',
        [ATTR_SERVICE_VERSION]: version,
        'service.namespace': 'trace-continuity-demo',
        'deployment.environment.name': import.meta.env.VITE_DEPLOYMENT_ENVIRONMENT || 'local',
    });

    // The fetch instrumentation ends its CLIENT span in a setTimeout(300 ms) after the response
    // body completes (it waits for PerformanceResourceTiming). A flush straight after the
    // interaction would miss that span, so count open spans and flush once none are left.
    let openSpans = 0;
    const onIdle: (() => void)[] = [];
    const openSpanCounter: SpanProcessor = {
        onStart: () => {
            openSpans++;
        },
        onEnd: () => {
            if (--openSpans === 0) onIdle.splice(0).forEach((resolve) => resolve());
        },
        forceFlush: () => Promise.resolve(),
        shutdown: () => Promise.resolve(),
    };
    const allSpansEnded = () =>
        openSpans === 0 ? Promise.resolve() : new Promise<void>((resolve) => onIdle.push(resolve));

    const tracerProvider = new WebTracerProvider({
        resource,
        spanProcessors: [
            new BatchSpanProcessor(new OTLPTraceExporter({ url: '/telemetry/v1/traces' })),
            openSpanCounter,
        ],
    });
    // Explicit propagator: register()'s default would add W3C baggage, which this demo keeps off.
    tracerProvider.register({
        contextManager: new StackContextManager(),
        propagator: new W3CTraceContextPropagator(),
    });

    registerInstrumentations({
        tracerProvider,
        instrumentations: [
            new FetchInstrumentation({
                // /telemetry/: the OTLP exporter already bypasses the patch (fetch.__original); second guard.
                // /api/config: page-start read the API does not instrument, so its span would be a childless root.
                ignoreUrls: [/\/telemetry\//, /\/api\/config$/],
                // Same-origin requests get traceparent by default. Cross-origin targets must be listed
                // here explicitly and narrowly; the baseline has none.
                propagateTraceHeaderCorsUrls: [],
            }),
        ],
    });

    const loggerProvider = new LoggerProvider({
        resource,
        processors: [new BatchLogRecordProcessor({ exporter: new OTLPLogExporter({ url: '/telemetry/v1/logs' }) })],
    });

    return {
        tracer: tracerProvider.getTracer('trace-demo-web', version),
        logger: loggerProvider.getLogger('trace-demo-web', version),
        /** Waits for open spans (max 2 s), then force-flushes both providers. Resolves true if both exports succeeded. */
        async flushTelemetry(): Promise<boolean> {
            await Promise.race([allSpansEnded(), new Promise((resolve) => setTimeout(resolve, 2000))]);
            const results = await Promise.allSettled([tracerProvider.forceFlush(), loggerProvider.forceFlush()]);
            return results.every((r) => r.status === 'fulfilled');
        },
    };
}

// Module scope already runs once per page; the global guard also covers Vite HMR re-evaluating this module.
const holder = globalThis as typeof globalThis & { __traceDemoTelemetry?: ReturnType<typeof init> };
export const { tracer, logger, flushTelemetry } = (holder.__traceDemoTelemetry ??= init());
