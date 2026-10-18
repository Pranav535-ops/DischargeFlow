import React from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

const auth = vi.hoisted(() => ({ current: {} }));
vi.mock('../context/AuthContext', () => ({ useAuth: () => auth.current }));

const { ProtectedRoute } = await import('../App');

const renderGate = () => render(
  <MemoryRouter>
    <ProtectedRoute><p>patient data</p></ProtectedRoute>
  </MemoryRouter>,
);

beforeEach(() => {
  auth.current = { loading: false, logout: vi.fn(), checkAuth: vi.fn().mockResolvedValue(null) };
});

describe('ProtectedRoute for accounts awaiting approval', () => {
  it('shows the waiting screen instead of the app for a pending account', async () => {
    auth.current.user = { role: 'pending', email: 'new.person@hospital.example' };
    renderGate();
    expect(screen.getByTestId('pending-approval')).toHaveTextContent('new.person@hospital.example');
    expect(screen.queryByText('patient data')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /check again/i }));
    expect(auth.current.checkAuth).toHaveBeenCalled();
    await userEvent.click(screen.getByRole('button', { name: /sign out/i }));
    expect(auth.current.logout).toHaveBeenCalled();
  });

  it('renders the app for an approved role', () => {
    auth.current.user = { role: 'nurse', email: 'nurse@hospital.example' };
    renderGate();
    expect(screen.getByText('patient data')).toBeInTheDocument();
    expect(screen.queryByTestId('pending-approval')).not.toBeInTheDocument();
  });
});
