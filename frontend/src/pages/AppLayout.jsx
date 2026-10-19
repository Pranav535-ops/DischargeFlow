import React from 'react';
import { Outlet } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import Sidebar from '../components/Sidebar';
import Header from '../components/Header';
import DowntimeBanner from '../components/DowntimeBanner';
import { useNotificationStream } from '@/lib/queries';

// The signed-in shell: navigation, header and the routed screen.
const AppLayout = () => {
  const { user } = useAuth();
  useNotificationStream();
  return (
    <div className="min-h-screen bg-zinc-950 flex print:block print:bg-white">
      <Sidebar user={user} />
      <div className="flex-1 flex flex-col min-w-0">
        <Header user={user} />
        <DowntimeBanner />
        <main className="flex-1 p-6 overflow-auto print:p-0 print:overflow-visible">
          <Outlet />
        </main>
      </div>
    </div>
  );
};

export default AppLayout;
