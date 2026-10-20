// Hospital single sign-on (ADR-002 item 2): the OpenID Connect authorization-code flow with PKCE.
//
// The browser generates a code verifier, a state and a nonce, keeps them in sessionStorage (this tab only) and
// redirects to the hospital's identity provider. The provider redirects back to /auth/sso/callback with a code; the
// backend redeems it with the verifier (POST /api/auth/oidc/callback), checks the ID token and its nonce, and opens
// a session. No token from the provider is ever handled by page scripts.
import { api } from '@/lib/api';

export const SSO_CALLBACK_PATH = '/auth/sso/callback';
const PENDING_KEY = 'df:sso-pending';
// The provider last used in this browser, so the lock screen re-enters through it without a picker.
export const LAST_PROVIDER_KEY = 'df:sso-provider';

const base64url = (bytes) =>
  btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');

export const randomString = (byteLength = 32) => {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return base64url(bytes);
};

export const codeChallenge = async (verifier) =>
  base64url(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)));

export const fetchAuthConfig = async () => (await api.get('/auth/config')).data;

// The authorization URL for `provider`, after saving what the callback needs. `prompt=login` asks the provider for
// a fresh sign-in even if it still has a session: on a shared workstation the previous person's provider session
// must not let the next person in (ADR-002 item 4).
export const buildAuthorizeUrl = async (provider, { origin = window.location.origin } = {}) => {
  const verifier = randomString(48);
  const state = randomString(24);
  const nonce = randomString(24);
  const redirectUri = origin + SSO_CALLBACK_PATH;
  sessionStorage.setItem(PENDING_KEY, JSON.stringify({
    state, nonce, verifier, provider_id: provider.provider_id, redirect_uri: redirectUri,
  }));
  const params = new URLSearchParams({
    response_type: 'code',
    client_id: provider.client_id,
    redirect_uri: redirectUri,
    scope: provider.scopes || 'openid email profile',
    state,
    nonce,
    code_challenge: await codeChallenge(verifier),
    code_challenge_method: 'S256',
    prompt: 'login',
  });
  return `${provider.authorize_url}?${params.toString()}`;
};

export const startSso = async (provider) => {
  try {
    localStorage.setItem(LAST_PROVIDER_KEY, provider.provider_id);
  } catch {
    // Storage blocked: the lock screen will offer the picker instead.
  }
  window.location.assign(await buildAuthorizeUrl(provider));
};

// The callback's request body, or an error message. The saved state is used once: a replayed callback URL fails.
export const takeCallback = (search) => {
  const params = new URLSearchParams(search);
  let pending = null;
  try {
    pending = JSON.parse(sessionStorage.getItem(PENDING_KEY) || 'null');
    sessionStorage.removeItem(PENDING_KEY);
  } catch {
    pending = null;
  }
  if (params.get('error')) return { error: params.get('error_description') || params.get('error') };
  const code = params.get('code');
  if (!code || !pending) return { error: 'The sign-in could not be completed. Please start again.' };
  // CSRF: the response must answer the request this tab made.
  if (params.get('state') !== pending.state) return { error: 'The sign-in response did not match this browser.' };
  return {
    body: {
      provider_id: pending.provider_id,
      code,
      code_verifier: pending.verifier,
      redirect_uri: pending.redirect_uri,
      nonce: pending.nonce,
    },
  };
};

// Which provider a sign-in button (or the lock screen) should use: `?hospital=<tenant>` narrows the list, then the
// one this browser used last, then the only one.
export const preferredProvider = (providers, search = window.location.search) => {
  const hospital = new URLSearchParams(search).get('hospital');
  const candidates = hospital ? providers.filter((p) => p.tenant_id === hospital) : providers;
  let last = null;
  try {
    last = localStorage.getItem(LAST_PROVIDER_KEY);
  } catch {
    last = null;
  }
  return candidates.find((p) => p.provider_id === last) || (candidates.length === 1 ? candidates[0] : null);
};
