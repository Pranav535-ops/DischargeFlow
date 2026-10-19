// SMART on FHIR EHR launch (ADR-002 items 14 and 15) against a fake EHR on localhost: authorization server, JWKS
// and FHIR server in one small Node HTTP server. The browser follows the real redirects; the backend makes the real
// token exchange and FHIR reads (http to localhost is allowed outside production). Not a real EHR sandbox.
import http from 'node:http';
import crypto from 'node:crypto';
import { test, expect } from '@playwright/test';
import { signInAs } from './helpers';

const PORT = 4199;
const EHR = `http://localhost:${PORT}`;
const ISS = `${EHR}/fhir`;
const CLIENT_ID = 'dischargeflow-e2e';
const REDIRECT = 'http://localhost:4173/launch';
const EHR_TOKEN = `ehr-token-${crypto.randomUUID()}`;
const MRN = `E2E${Date.now()}`;
const PHYSICIAN_EMAIL = 'e2e.physician@dischargeflow.test'; // the dev login's physician

const { privateKey, publicKey } = crypto.generateKeyPairSync('rsa', { modulusLength: 2048 });
const b64url = (buf) => Buffer.from(buf).toString('base64url');
const signJwt = (claims) => {
  const head = b64url(JSON.stringify({ alg: 'RS256', typ: 'JWT', kid: 'e2e-1' }));
  const body = b64url(JSON.stringify(claims));
  return `${head}.${body}.${b64url(crypto.sign('sha256', Buffer.from(`${head}.${body}`), privateKey))}`;
};

const codes = new Map();
const json = (res, status, body) => {
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
};
const bundle = (resources) => ({ resourceType: 'Bundle', type: 'searchset', entry: resources.map(resource => ({ resource })) });

const server = http.createServer((req, res) => {
  const url = new URL(req.url, EHR);
  if (url.pathname === '/fhir/.well-known/smart-configuration') {
    return json(res, 200, { issuer: ISS, jwks_uri: `${EHR}/jwks`, authorization_endpoint: `${EHR}/authorize`,
      token_endpoint: `${EHR}/token`, code_challenge_methods_supported: ['S256'] });
  }
  if (url.pathname === '/jwks') {
    return json(res, 200, { keys: [{ ...publicKey.export({ format: 'jwk' }), kid: 'e2e-1', alg: 'RS256', use: 'sig' }] });
  }
  if (url.pathname === '/authorize') {
    // The EHR user is already signed in to the EHR: approve and send the browser back.
    const code = crypto.randomUUID();
    codes.set(code, Object.fromEntries(url.searchParams));
    res.writeHead(302, { Location: `${url.searchParams.get('redirect_uri')}?code=${code}&state=${url.searchParams.get('state')}` });
    return res.end();
  }
  if (url.pathname === '/token' && req.method === 'POST') {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      const form = Object.fromEntries(new URLSearchParams(raw));
      const issued = codes.get(form.code);
      codes.delete(form.code);
      const challenge = b64url(crypto.createHash('sha256').update(form.code_verifier || '').digest());
      if (!issued || issued.code_challenge !== challenge || form.client_id !== CLIENT_ID) {
        return json(res, 400, { error: 'invalid_grant' });
      }
      const now = Math.floor(Date.now() / 1000);
      return json(res, 200, { access_token: EHR_TOKEN, token_type: 'Bearer', expires_in: 600, scope: issued.scope,
        patient: 'ehr-pat-1', encounter: 'ehr-enc-1',
        id_token: signJwt({ iss: ISS, aud: CLIENT_ID, sub: 'prac-1', iat: now, exp: now + 300, nonce: issued.nonce,
          fhirUser: 'Practitioner/prac-1' }) });
    });
    return undefined;
  }
  if (url.pathname.startsWith('/fhir/')) {
    if (req.headers.authorization !== `Bearer ${EHR_TOKEN}`) return json(res, 401, { resourceType: 'OperationOutcome' });
    const resource = url.pathname.slice('/fhir/'.length);
    if (resource === 'Practitioner/prac-1') {
      return json(res, 200, { resourceType: 'Practitioner', name: [{ text: 'E2E Physician' }],
        telecom: [{ system: 'email', value: PHYSICIAN_EMAIL }] });
    }
    if (resource === 'Patient/ehr-pat-1') {
      return json(res, 200, { resourceType: 'Patient', identifier: [{ system: 'urn:e2e:mrn', value: MRN }] });
    }
    if (resource === 'Encounter/ehr-enc-1') return json(res, 200, { resourceType: 'Encounter', identifier: [] });
    if (resource === 'MedicationRequest') {
      return json(res, 200, bundle([{ resourceType: 'MedicationRequest', status: 'active',
        medicationCodeableConcept: { text: 'Lisinopril 10 MG Oral Tablet' }, dosageInstruction: [{ text: '10 mg daily' }] }]));
    }
    if (resource === 'Condition') {
      return json(res, 200, bundle([{ resourceType: 'Condition', code: { text: 'Type 2 diabetes mellitus' } }]));
    }
    if (resource === 'AllergyIntolerance') {
      return json(res, 200, bundle([{ resourceType: 'AllergyIntolerance', code: { text: 'Penicillin' },
        reaction: [{ manifestation: [{ text: 'Hives' }] }] }]));
    }
  }
  return json(res, 404, {});
});

test.beforeAll(async () => { await new Promise((resolve) => server.listen(PORT, resolve)); });
test.afterAll(async () => { await new Promise((resolve) => server.close(resolve)); });

test('launch from the EHR opens the patient with the EHR lists, without a second sign-in', async ({ page }) => {
  // Admin: register the EHR, and admit the patient through the ADT feed so the MRN is known.
  await page.context().clearCookies();
  const admin = await (await page.request.post('/api/auth/dev-login', { data: { role: 'admin' } })).json();
  const adminHeaders = { Authorization: `Bearer ${admin.token}` };
  const issuer = await page.request.post('/api/smart/issuers', { headers: adminHeaders, data: {
    iss: ISS, name: 'E2E EHR', client_id: CLIENT_ID, redirect_uri: REDIRECT, mrn_system: 'urn:e2e:mrn', mrn_authority: 'E2E' } });
  expect([200, 409]).toContain(issuer.status()); // 409: registered by an earlier run on a reused database
  const client = await (await page.request.post('/api/integrations/clients', { headers: adminHeaders,
    data: { name: 'E2E interface engine' } })).json();
  const pv1 = ['PV1', '1', 'I', '^E2E-1', ...Array(41).fill('')].join('|');
  const a01 = [`MSH|^~\\&|EHR|HOSP|DISCHARGEFLOW|HOSP|20261019100000||ADT^A01^ADT_A01|${MRN}|P|2.5.1`,
    'EVN|A01|20261019095500', `PID|1||${MRN}^^^E2E^MR||Launch^Ellis||19600101|F`, pv1].join('\r');
  const ack = await page.request.post('/api/hl7/adt', { data: a01,
    headers: { 'X-API-Key': client.api_key, 'Content-Type': 'x-application/hl7-v2+er7' } });
  expect(await ack.text()).toContain('MSA|AA');

  // The physician starts the discharge (signed in normally), then signs out of DischargeFlow entirely.
  const physician = await (await page.request.post('/api/auth/dev-login', { data: { role: 'physician' } })).json();
  const patients = await (await page.request.get('/api/patients', {
    headers: { Authorization: `Bearer ${physician.token}` } })).json();
  const patient = patients.find((p) => p.name === 'Ellis Launch');
  expect(patient).toBeTruthy();
  const started = await page.request.post(`/api/workflows/initiate/${patient.patient_id}`, {
    headers: { Authorization: `Bearer ${physician.token}` } });
  const workflowId = (await started.json()).workflow.workflow_id;
  await page.context().clearCookies();

  // The EHR launches DischargeFlow: no DischargeFlow sign-in page, straight to the workflow.
  await page.goto(`/launch?iss=${encodeURIComponent(ISS)}&launch=opaque-e2e`);
  await expect(page).toHaveURL(new RegExp(`/workflows/${workflowId}$`));
  await expect(page.getByTestId('logout-btn')).toBeVisible();
  const context = page.getByTestId('ehr-context');
  await expect(context.getByTestId('ehr-medications')).toContainText('Lisinopril 10 MG Oral Tablet');
  await expect(context.getByTestId('ehr-problems')).toContainText('Type 2 diabetes mellitus');
  await expect(context.getByTestId('ehr-allergies')).toContainText('Penicillin');
  await expect(page.getByTestId('ehr-summary-note')).toContainText('1 active medications');
  // The EHR's token never reached the browser.
  expect(await page.content()).not.toContain(EHR_TOKEN);
});

test('a launch from an EHR that is not registered is refused', async ({ page }) => {
  await signInAs(page, 'nurse');
  await page.context().clearCookies();
  await page.goto('/launch?iss=https%3A%2F%2Funknown.example%2Ffhir&launch=x');
  await expect(page.getByTestId('smart-launch-error')).toContainText('not allowed');
});
