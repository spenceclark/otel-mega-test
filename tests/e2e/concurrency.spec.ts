import { expect, test } from '@playwright/test';
import { prepare, scenario, submit, utcStamp, writeManifest } from './support';

const COUNT = 20;

test(`${COUNT} simultaneous submissions get ${COUNT} distinct traces`, async ({ browser, baseURL }) => {
    const contexts = await Promise.all(Array.from({ length: COUNT }, () => browser.newContext({ baseURL })));
    const pages = await Promise.all(contexts.map((c) => c.newPage()));
    await Promise.all(pages.map(prepare)); // load and fill everything first...
    const started_at = new Date().toISOString();
    const runs = await Promise.all(pages.map(submit)); // ...then click all at once
    const finished_at = new Date().toISOString();

    const distinct = new Set(runs.map((r) => r.trace_id).filter(Boolean)).size;
    const file = writeManifest(
        `concurrency-${utcStamp()}.json`,
        {
            schema_version: 1,
            kind: 'concurrency',
            scenario: await scenario(pages[0]),
            base_url: baseURL,
            started_at,
            finished_at,
            distinct_trace_ids: distinct,
            runs: runs.map(({ run_id, trace_id, outcome, error_code, telemetry_export }) => ({
                run_id,
                trace_id,
                outcome,
                error_code,
                telemetry_export,
            })),
        },
        'latest-concurrency.json',
    );
    console.log(`manifest: ${file}`);
    await Promise.all(contexts.map((c) => c.close()));

    expect(runs.map((r) => r.outcome)).toEqual(Array(COUNT).fill('ok'));
    expect(distinct).toBe(COUNT);
});
