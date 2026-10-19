// SMART on FHIR EHR launch helpers (ADR-002 item 14): PKCE and the per-tab launch record.
//
// The PKCE verifier and the state stay in sessionStorage (this tab only) between the redirect to the EHR's
// authorization server and the redirect back. The server keeps the hash of the state and the S256 challenge, so a
// code is only exchanged by the tab that started the launch.

export const LAUNCH_KEY = 'df:smart-launch';

const base64url = (bytes) => btoa(String.fromCharCode(...new Uint8Array(bytes)))
  .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export const makeVerifier = () => {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return base64url(bytes); // 43 characters
};

export const challengeFor = async (verifier) =>
  base64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));

export const saveLaunch = (record) => sessionStorage.setItem(LAUNCH_KEY, JSON.stringify(record));

// The record for this state, used once. A mismatched or missing state returns null.
export const takeLaunch = (state) => {
  let record = null;
  try {
    record = JSON.parse(sessionStorage.getItem(LAUNCH_KEY) || 'null');
  } catch {
    record = null;
  }
  sessionStorage.removeItem(LAUNCH_KEY);
  return record && state && record.state === state ? record : null;
};

// Where to go once signed in: the workflow, else the patient, else the patient list with the reason.
export const landingPath = (launch) => {
  if (launch?.workflow_id) return `/workflows/${launch.workflow_id}`;
  if (launch?.patient_id) return `/patients/${launch.patient_id}`;
  return '/patients';
};
