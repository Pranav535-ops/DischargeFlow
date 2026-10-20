import React from 'react';
import { NavLink } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { 
  Users, 
  BarChart3, 
  Settings,
  Activity,
  LogOut,
  CheckSquare,
  LayoutDashboard,
  DollarSign,
  Bot,
  Sparkles,
  ClipboardList,
  CalendarCheck,
  BedDouble
} from 'lucide-react';
import { Button } from '../components/ui/button';

const Sidebar = ({ user }) => {
  const { logout } = useAuth();

  const navItems = [
    { id: 'home',      to: '/dashboard', label: 'Home',         icon: LayoutDashboard, roles: ['physician', 'nurse', 'pharmacist', 'admin'] },
    { id: 'patients',  to: '/patients',  label: 'Patients',     icon: Users,           roles: ['physician', 'nurse', 'pharmacist', 'admin'] },
    { id: 'plans',     to: '/plans',     label: 'Plan Review',  icon: CalendarCheck,   roles: ['physician', 'nurse', 'pharmacist', 'admin'] },
    { id: 'capacity',  to: '/capacity',  label: 'Capacity',     icon: BedDouble,       roles: ['physician', 'nurse', 'admin'] },
    { id: 'tasks',     to: '/tasks',     label: 'My Tasks',     icon: CheckSquare,     roles: ['physician', 'nurse', 'pharmacist', 'admin'] },
    { id: 'insurance', to: '/insurance', label: 'Insurance',    icon: DollarSign,      roles: ['physician', 'nurse', 'pharmacist', 'admin'] },
    { id: 'chatbot',   to: '/assistant', label: 'AI Assistant', icon: Bot,             roles: ['physician', 'nurse', 'pharmacist', 'admin'] },
    { id: 'census',    to: '/census',    label: 'Census',       icon: ClipboardList,   roles: ['physician', 'nurse', 'pharmacist', 'admin'] },
    { id: 'analytics', to: '/analytics', label: 'Analytics',    icon: BarChart3,       roles: ['physician', 'nurse', 'admin'] },
    { id: 'insights',  to: '/insights',  label: 'AI Insights',  icon: Sparkles,        roles: ['admin'] },
    { id: 'admin',     to: '/admin',     label: 'Admin',        icon: Settings,        roles: ['admin'] },
  ];

  const filteredNavItems = navItems.filter(item => 
    item.roles.includes(user?.role || 'nurse')
  );

  const getRoleBadgeColor = (role) => {
    const colors = {
      physician: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
      nurse: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
      pharmacist: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
      admin: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
    };
    return colors[role] || colors.nurse;
  };

  return (
    <div className="w-64 bg-zinc-900 border-r border-zinc-800 flex flex-col print:hidden">
      {/* Logo */}
      <div className="p-6 border-b border-zinc-800">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-lg bg-blue-600 flex items-center justify-center">
            <Activity className="w-6 h-6 text-white" strokeWidth={1.5} />
          </div>
          <div>
            <h1 className="text-lg font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
              DischargeFlow
            </h1>
          </div>
        </div>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-4">
        <ul className="space-y-1">
          {filteredNavItems.map((item) => (
            <li key={item.id}>
              <NavLink
                to={item.to}
                data-testid={`nav-${item.id}`}
                // Patient and workflow pages belong to the Patients section.
                className={({ isActive }) => {
                  const active = isActive || (item.id === 'patients' && /^\/(patients|workflows)\//.test(window.location.pathname));
                  return `w-full flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-medium transition-colors ${
                    active ? 'bg-zinc-800 text-zinc-50' : 'text-zinc-400 hover:bg-zinc-800/50 hover:text-zinc-300'
                  }`;
                }}
              >
                <item.icon className="w-5 h-5" strokeWidth={1.5} />
                {item.label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>

      {/* User section */}
      <div className="p-4 border-t border-zinc-800">
        <div className="flex items-center gap-3 mb-4">
          {user?.picture ? (
            <img 
              src={user.picture} 
              alt={user.name} 
              className="w-10 h-10 rounded-full object-cover"
            />
          ) : (
            <div className="w-10 h-10 rounded-full bg-zinc-800 flex items-center justify-center">
              <span className="text-zinc-400 text-sm font-medium">
                {user?.name?.charAt(0) || 'U'}
              </span>
            </div>
          )}
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-zinc-200 truncate">{user?.name}</p>
            <span className={`inline-block mt-1 text-xs px-2 py-0.5 rounded border capitalize ${getRoleBadgeColor(user?.role)}`}>
              {user?.role || 'User'}
            </span>
          </div>
        </div>
        <Button
          data-testid="logout-btn"
          onClick={logout}
          variant="ghost"
          className="w-full justify-start text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800"
        >
          <LogOut className="w-4 h-4 mr-2" strokeWidth={1.5} />
          Sign out
        </Button>
      </div>
    </div>
  );
};

export default Sidebar;
