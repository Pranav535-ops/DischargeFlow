// The app's security headers live in ../vercel.json (production, and `vite preview` for E2E) and in
// security-headers.conf (the Docker/nginx image). This keeps the copies equal and the policy's key properties fixed.
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const frontend = path.resolve(__dirname, '../..');
const vercel = JSON.parse(fs.readFileSync(path.join(frontend, '../vercel.json'), 'utf8'));
const rule = vercel.headers.find((h) => h.source === '/((?!api/).*)');
const vercelHeaders = Object.fromEntries(rule.headers.map(({ key, value }) => [key, value]));

const nginxHeaders = Object.fromEntries(
  [...fs.readFileSync(path.join(frontend, 'security-headers.conf'), 'utf8')
    .matchAll(/^add_header ([\w-]+) "([^"]*)" always;$/gm)].map((m) => [m[1], m[2]]),
);

const csp = Object.fromEntries(vercelHeaders['Content-Security-Policy'].split(';')
  .map((d) => d.trim().split(/\s+/)).map(([name, ...sources]) => [name, sources]));

describe('SPA security headers', () => {
  it('are the same for Vercel and nginx', () => {
    expect(nginxHeaders).toEqual(vercelHeaders);
  });

  it('forbid framing', () => {
    expect(vercelHeaders['X-Frame-Options']).toBe('DENY');
    expect(csp['frame-ancestors']).toEqual(["'none'"]);
  });

  it('allow scripts and API calls from the app origin only', () => {
    expect(csp['script-src']).toEqual(["'self'"]);
    expect(csp['connect-src']).toEqual(["'self'"]);
    expect(csp['object-src']).toEqual(["'none'"]);
    expect(csp['base-uri']).toEqual(["'self'"]);
  });

  it('send nosniff, a referrer policy and a permissions policy', () => {
    expect(vercelHeaders['X-Content-Type-Options']).toBe('nosniff');
    expect(vercelHeaders['Referrer-Policy']).toBe('strict-origin-when-cross-origin');
    expect(vercelHeaders['Permissions-Policy']).toMatch(/camera=\(\)/);
  });
});
