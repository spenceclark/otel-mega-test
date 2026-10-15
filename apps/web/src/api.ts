// Shapes mirror contracts/ask.schema.json and contracts/report.schema.json.
export const PRODUCTS = ['DEMO-LANTERN', 'DEMO-KETTLE', 'DEMO-TENT'] as const;
export const ZONES = ['DEMO-NORTH', 'DEMO-SOUTH', 'DEMO-EAST'] as const;

export type AskRequest = {
    message: string;
    product_code: (typeof PRODUCTS)[number];
    destination_zone: (typeof ZONES)[number];
};

export type AvailabilityReport = {
    product_code: string;
    destination_zone: string;
    available: boolean;
    quantity_on_hand: number;
    warehouse: string;
    shipping: {
        destination_zone: string;
        carrier: string;
        price_cents: number;
        currency: string;
        eta_days: number;
        reference: string;
    };
    summary: string;
};

export type AskResponse = {
    run_id: string;
    answer: string;
    evidence: AvailabilityReport;
    trace_id: string;
    scenario: string;
    mode: string;
};

export type AskError = { run_id: string; trace_id: string; error: string; detail: string };

export type AskResult =
    | { outcome: 'ok'; response: AskResponse }
    | { outcome: 'error'; error: AskError }
    | { outcome: 'canceled' };

export type DemoConfig = { scenario: string; mode: string };

const unknownConfig: DemoConfig = { scenario: 'unknown', mode: 'unknown' };

/** Loaded once at page start. */
export const configPromise: Promise<DemoConfig> = fetch('/api/config')
    .then((res) => (res.ok ? (res.json() as Promise<DemoConfig>) : unknownConfig))
    .catch(() => unknownConfig);

/**
 * Calls fetch() synchronously (before its first await), so the caller's active context at call
 * time parents the fetch instrumentation's CLIENT span. Never throws.
 */
export async function postAsk(request: AskRequest, signal: AbortSignal): Promise<AskResult> {
    let status = 0;
    try {
        const res = await fetch('/api/ask', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-Demo-Canary': 'CANARY-HEADER-e620' },
            body: JSON.stringify(request),
            signal,
        });
        status = res.status;
        const body = await res.json();
        if (res.ok) return { outcome: 'ok', response: body as AskResponse };
        if (typeof body?.error === 'string') return { outcome: 'error', error: body as AskError };
        throw new Error('unexpected error body');
    } catch {
        if (signal.aborted) return { outcome: 'canceled' };
        const error = status ? `http_${status}` : 'network_error';
        const detail = status ? 'response was not an AskError body' : 'request failed before a response';
        return { outcome: 'error', error: { run_id: '', trace_id: '', error, detail } };
    }
}
