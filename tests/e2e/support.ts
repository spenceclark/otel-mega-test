import { expect, type Page } from '@playwright/test';
import { copyFileSync, mkdirSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';

export const PROMPT = 'Can I get the demo lantern delivered to DEMO-NORTH? CANARY-PROMPT-91c4';

export type Run = {
    run_id: string;
    trace_id: string;
    outcome: 'ok' | 'error';
    error_code: string | null;
    telemetry_export: string | null; // 'ok' | 'failed': did the browser's final forceFlush succeed
    started_at: string;
    finished_at: string;
};

/** Loads the app and fills the fixed request, without submitting. */
export async function prepare(page: Page): Promise<void> {
    await page.goto('/');
    await page.getByTestId('message').fill(PROMPT);
    await page.getByTestId('product').selectOption('DEMO-LANTERN');
    await page.getByTestId('zone').selectOption('DEMO-NORTH');
}

/** Submits, waits for answer or error, then for the browser telemetry flush, and reads the ids. */
export async function submit(page: Page): Promise<Run> {
    const started_at = new Date().toISOString();
    await page.getByTestId('submit').click();
    const answer = page.getByTestId('answer');
    const error = page.getByTestId('error');
    await expect(answer.or(error)).toBeVisible({ timeout: 60_000 });
    const flushed = page.getByTestId('telemetry-flushed');
    await expect(flushed).toHaveAttribute('data-telemetry-flushed', 'true');
    const finished_at = new Date().toISOString();
    const ok = await answer.isVisible();
    return {
        run_id: (await page.getByTestId('run-id').textContent()) ?? '',
        trace_id: (await page.getByTestId('trace-id').textContent()) ?? '',
        outcome: ok ? 'ok' : 'error',
        error_code: ok ? null : await error.getAttribute('data-code'),
        telemetry_export: await flushed.getAttribute('data-telemetry-export'),
        started_at,
        finished_at,
    };
}

export async function scenario(page: Page): Promise<string> {
    return process.env.SCENARIO ?? (await page.getByTestId('scenario').textContent()) ?? 'unknown';
}

/** Writes `${EVIDENCE_DIR}/runs/<name>` and copies it to `runs/<latest>`. */
export function writeManifest(name: string, manifest: object, latest: string): string {
    const dir = join(resolve(process.env.EVIDENCE_DIR ?? '../../evidence'), 'runs');
    mkdirSync(dir, { recursive: true });
    const file = join(dir, name.replace(/[^\w.-]/g, '_')); // name may contain a server-supplied run_id
    writeFileSync(file, JSON.stringify(manifest, null, 2) + '\n');
    copyFileSync(file, join(dir, latest));
    return file;
}

export const utcStamp = () => new Date().toISOString().replace(/[:.]/g, '-');
