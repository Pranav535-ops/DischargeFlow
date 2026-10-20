import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { api } from '@/lib/api';
import {
  LAST_PROVIDER_KEY, buildAuthorizeUrl, codeChallenge, preferredProvider, takeCallback,
} from '@/lib/sso';

const auth = vi.hoisted(() => ({ current: {} }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => auth.current }));
const { default: LoginPage } = await import('../pages/LoginPage');

const PROVIDER = {
  provider_id: 'idp_stmary', tenant_id: 'stmary', name: "St. Mary's", client_id: 'client-1',
  authorize_url: 'https://stmary.auth.example/oauth2/authorize', scopes: 'openid email profile',
};

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  auth.current = { loginWithEmergent: vi.fn(), loginWithSso: vi.fn() };
});
afterEach(() => vi.restoreAllMocks());

describe('PKCE redirect', () => {
  it('computes the RFC 7636 S256 challenge', async () => {
    // Appendix B of RFC 7636.
    expect(await codeChallenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'))
      .toBe('E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
  });

  it('builds the authorize URL and saves what the callback needs', async () => {
    const url = new URL(await buildAuthorizeUrl(PROVIDER, { origin: 'https://app.example' }));
    expect(url.origin + url.pathname).toBe(PROVIDER.authorize_url);
    const p = url.searchParams;
    expect(p.get('response_type')).toBe('code');
    expect(p.get('client_id')).toBe('client-1');
    expect(p.get('redirect_uri')).toBe('https://app.example/auth/sso/callback');
    expect(p.get('code_challenge_method')).toBe('S256');
    expect(p.get('prompt')).toBe('login');
    const saved = JSON.parse(sessionStorage.getItem('df:sso-pending'));
    expect(saved.state).toBe(p.get('state'));
    expect(saved.nonce).toBe(p.get('nonce'));
    expect(await codeChallenge(saved.verifier)).toBe(p.get('code_challenge'));
    expect(saved.verifier.length).toBeGreaterThanOrEqual(43);

    const { body } = takeCallback(`?code=abc&state=${saved.state}`);
    expect(body).toEqual({ provider_id: 'idp_stmary', code: 'abc', code_verifier: saved.verifier,
      redirect_uri: 'https://app.example/auth/sso/callback', nonce: saved.nonce });
    // Single use.
    expect(takeCallback(`?code=abc&state=${saved.state}`).error).toBeTruthy();
  });

  it('refuses a callback that answers another request', async () => {
    await buildAuthorizeUrl(PROVIDER, { origin: 'https://app.example' });
    expect(takeCallback('?code=abc&state=forged').error).toMatch(/did not match/);
    expect(takeCallback('?error=access_denied').error).toBe('access_denied');
  });

  it('prefers the hospital in the URL, then the last provider used', () => {
    const other = { ...PROVIDER, provider_id: 'idp_b', tenant_id: 'b' };
    expect(preferredProvider([PROVIDER], '')).toBe(PROVIDER);
    expect(preferredProvider([PROVIDER, other], '')).toBeNull();
    expect(preferredProvider([PROVIDER, other], '?hospital=b')).toBe(other);
    localStorage.setItem(LAST_PROVIDER_KEY, 'idp_b');
    expect(preferredProvider([PROVIDER, other], '')).toBe(other);
  });
});

describe('LoginPage', () => {
  it('offers hospital SSO and hides the demo login when the deployment has a provider', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: { providers: [PROVIDER], emergent: false } });
    render(<LoginPage />);
    const button = await screen.findByTestId('sso-login-idp_stmary');
    expect(button).toHaveTextContent("Sign in with St. Mary's");
    expect(screen.queryByTestId('google-login-btn')).not.toBeInTheDocument();
    await userEvent.click(button);
    expect(auth.current.loginWithSso).toHaveBeenCalledWith(PROVIDER);
  });

  it('keeps the demo login when no provider is configured', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: { providers: [], emergent: true } });
    render(<LoginPage />);
    await userEvent.click(await screen.findByTestId('google-login-btn'));
    expect(auth.current.loginWithEmergent).toHaveBeenCalled();
  });
});
