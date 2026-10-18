import React from 'react';
import { Link } from 'react-router-dom';
import { AlertTriangle, Printer } from 'lucide-react';
import { useDowntimeStatus } from '@/lib/queries';

// Shown on every screen while DischargeFlow is read-only (ADR-002 item 6). Write controls disable themselves via
// useReadOnly(); anything that still tries to write gets a 503 that says the same thing.
const DowntimeBanner = () => {
  const { data: status } = useDowntimeStatus();
  if (!status?.read_only) return null;
  const period = status.period;
  return (
    <div
      role="alert"
      data-testid="downtime-banner"
      className="print:hidden border-b border-amber-500/40 bg-amber-500/15 px-6 py-3 text-sm text-amber-200 flex flex-wrap items-center gap-x-4 gap-y-2"
    >
      <span className="flex items-center gap-2 font-medium text-amber-100">
        <AlertTriangle className="w-4 h-4" />
        Downtime: DischargeFlow is read-only
      </span>
      <span>
        {period?.reason ? `${period.reason}. ` : ''}
        {period?.started_at ? `Since ${new Date(period.started_at).toLocaleString()}. ` : ''}
        {status.forced && !period ? 'Set by the deployment configuration. ' : ''}
        Record work on the paper downtime forms; it is entered afterwards with the time it was done.
      </span>
      <Link to="/census" className="ml-auto flex items-center gap-1 underline hover:text-amber-100">
        <Printer className="w-4 h-4" /> Print census
      </Link>
    </div>
  );
};

export default DowntimeBanner;
