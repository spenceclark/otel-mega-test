import { context, ROOT_CONTEXT, SpanKind, SpanStatusCode, trace } from '@opentelemetry/api';
import { SeverityNumber } from '@opentelemetry/api-logs';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { flushSync } from 'react-dom';
import { configPromise, postAsk, PRODUCTS, ZONES, type AskRequest, type AskResult, type DemoConfig } from './api';
import { flushTelemetry, logger, tracer } from './telemetry';

const DEFAULT_MESSAGE = 'Can I get the demo lantern delivered to DEMO-NORTH?';

export function AskPage() {
    const [message, setMessage] = useState(DEFAULT_MESSAGE);
    const [product, setProduct] = useState<AskRequest['product_code']>('DEMO-LANTERN');
    const [zone, setZone] = useState<AskRequest['destination_zone']>('DEMO-NORTH');
    const [config, setConfig] = useState<DemoConfig>();
    const [busy, setBusy] = useState(false);
    const [result, setResult] = useState<AskResult>();
    const [flush, setFlush] = useState<'ok' | 'failed'>();
    const abortRef = useRef<AbortController>(undefined);

    useEffect(() => {
        void configPromise.then(setConfig);
    }, []);

    async function submit(e: FormEvent) {
        e.preventDefault();
        const abort = new AbortController();
        abortRef.current = abort;
        setBusy(true);
        setResult(undefined);
        setFlush(undefined);

        const cfg = await configPromise; // a fast click must not record scenario "unknown"
        const attributes = { 'demo.scenario': cfg.scenario, 'demo.mode': cfg.mode };
        // ROOT_CONTEXT: the submission is its own trace, never a child of page load or a previous click.
        await tracer.startActiveSpan('ui.ask', { kind: SpanKind.INTERNAL, attributes }, ROOT_CONTEXT, async (span) => {
            try {
                // postAsk calls fetch() before its first await, i.e. while ui.ask is still active.
                const r = await postAsk({ message, product_code: product, destination_zone: zone }, abort.signal);
                // Evidence: is ui.ask still the ambient span after an await? (StackContextManager: false.)
                span.setAttribute('demo.context_after_await', trace.getSpan(context.active()) === span);

                const runId = r.outcome === 'ok' ? r.response.run_id : r.outcome === 'error' ? r.error.run_id : '';
                const outcomeAttrs = { 'demo.outcome': r.outcome, ...(runId ? { 'demo.run_id': runId } : {}) };
                span.setAttributes(outcomeAttrs);
                if (r.outcome === 'error') span.setStatus({ code: SpanStatusCode.ERROR, message: r.error.error });

                flushSync(() => setResult(r)); // rendered before ui.ask ends

                // Ambient context is gone after the await, so re-enter ui.ask explicitly for the log record.
                context.with(trace.setSpan(context.active(), span), () =>
                    logger.emit({
                        severityNumber: SeverityNumber.INFO,
                        severityText: 'INFO',
                        body: 'ui.ask completed',
                        attributes: outcomeAttrs,
                    }),
                );
            } finally {
                span.end();
            }
        });

        setFlush((await flushTelemetry()) ? 'ok' : 'failed');
        setBusy(false);
    }

    const ids =
        result?.outcome === 'ok' ? result.response : result?.outcome === 'error' ? result.error : undefined;

    return (
        <main>
            <h1>Trace continuity demo</h1>
            <p>
                Scenario <strong data-testid="scenario">{config?.scenario ?? '…'}</strong>, mode{' '}
                <strong>{config?.mode ?? '…'}</strong>
            </p>

            <form onSubmit={submit}>
                <label>
                    Message
                    <textarea
                        data-testid="message"
                        rows={3}
                        required
                        maxLength={2000}
                        value={message}
                        onChange={(e) => setMessage(e.target.value)}
                    />
                </label>
                <label>
                    Product
                    <select
                        data-testid="product"
                        value={product}
                        onChange={(e) => setProduct(e.target.value as AskRequest['product_code'])}
                    >
                        {PRODUCTS.map((p) => (
                            <option key={p}>{p}</option>
                        ))}
                    </select>
                </label>
                <label>
                    Destination zone
                    <select
                        data-testid="zone"
                        value={zone}
                        onChange={(e) => setZone(e.target.value as AskRequest['destination_zone'])}
                    >
                        {ZONES.map((z) => (
                            <option key={z}>{z}</option>
                        ))}
                    </select>
                </label>
                <div className="actions">
                    <button data-testid="submit" type="submit" disabled={busy}>
                        Submit
                    </button>
                    <button data-testid="cancel" type="button" disabled={!busy} onClick={() => abortRef.current?.abort()}>
                        Cancel
                    </button>
                </div>
            </form>

            {result?.outcome === 'ok' && (
                <section>
                    <p data-testid="answer">{result.response.answer}</p>
                    <table>
                        <tbody>
                            <tr>
                                <th>Stock</th>
                                <td>
                                    {result.response.evidence.quantity_on_hand}
                                    {result.response.evidence.available ? ' (available)' : ' (unavailable)'}
                                </td>
                            </tr>
                            <tr>
                                <th>Warehouse</th>
                                <td>{result.response.evidence.warehouse}</td>
                            </tr>
                            <tr>
                                <th>Carrier</th>
                                <td>{result.response.evidence.shipping.carrier}</td>
                            </tr>
                            <tr>
                                <th>Price</th>
                                <td>
                                    {(result.response.evidence.shipping.price_cents / 100).toFixed(2)}{' '}
                                    {result.response.evidence.shipping.currency}
                                </td>
                            </tr>
                            <tr>
                                <th>ETA</th>
                                <td>{result.response.evidence.shipping.eta_days} days</td>
                            </tr>
                        </tbody>
                    </table>
                </section>
            )}

            {result?.outcome === 'error' && (
                <section className="error" data-testid="error" data-code={result.error.error}>
                    <strong>{result.error.error}</strong>: {result.error.detail}
                </section>
            )}

            {result?.outcome === 'canceled' && <section>Canceled.</section>}

            {ids && (
                <section>
                    <div>
                        Run ID <code data-testid="run-id">{ids.run_id}</code> <CopyButton value={ids.run_id} />
                    </div>
                    <div>
                        Trace ID <code data-testid="trace-id">{ids.trace_id}</code> <CopyButton value={ids.trace_id} />
                    </div>
                </section>
            )}

            <output
                data-testid="telemetry-flushed"
                data-telemetry-flushed={String(flush !== undefined)}
                data-telemetry-export={flush}
                hidden
            />
        </main>
    );
}

function CopyButton({ value }: { value: string }) {
    // navigator.clipboard only exists in secure contexts (http://127.0.0.1 and localhost qualify).
    return (
        <button type="button" disabled={!value} onClick={() => void navigator.clipboard?.writeText(value)}>
            Copy
        </button>
    );
}
