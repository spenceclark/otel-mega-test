import { expect, test } from '@playwright/test';
import { prepare, scenario, submit, utcStamp, writeManifest } from './support';

test('single submission writes a run manifest', async ({ page, baseURL }) => {
    await prepare(page);
    const run = await submit(page);
    const { telemetry_export, ...ids } = run;

    const file = writeManifest(
        `${run.run_id || `no-run-id-${utcStamp()}`}.json`,
        { schema_version: 1, kind: 'single', ...ids, scenario: await scenario(page), base_url: baseURL, telemetry_export },
        'latest.json',
    );
    console.log(`RUN_ID=${run.run_id} TRACE_ID=${run.trace_id}`);
    console.log(`manifest: ${file}`);

    // Passes on either outcome (expected-failure experiments still need a manifest) unless pinned.
    if (process.env.EXPECT_OUTCOME) expect(run.outcome).toBe(process.env.EXPECT_OUTCOME);
});
