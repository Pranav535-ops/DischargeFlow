import React, { useState } from 'react';
import { api, errorMessage } from '@/lib/api';
import { keys, queryClient, useUsers } from '@/lib/queries';
import { 
  Loader2,
  Users,
  Shield,
  Save,
  CheckCircle2,
  UserPlus,
  Clock
} from 'lucide-react';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '../components/ui/select';
import { toast } from 'sonner';
import { BreakGlassReview, UnitsCard, UserUnitsPicker } from './AdminUnits';
import AdminDowntime from './AdminDowntime';


const AdminView = () => {
  const { data: users = [], isLoading: loading } = useUsers();
  const [updatingUser, setUpdatingUser] = useState(null);
  const [roleChanges, setRoleChanges] = useState({});
  const [savedUsers, setSavedUsers] = useState({});
  const [invite, setInvite] = useState({ email: '', name: '', role: 'nurse' });
  const [inviting, setInviting] = useState(false);
  const pendingCount = users.filter(u => u.role === 'pending').length;

  const fetchUsers = () => queryClient.invalidateQueries({ queryKey: keys.users });

  const handleRoleChange = (userId, newRole) => {
    setRoleChanges({ ...roleChanges, [userId]: newRole });
    setSavedUsers({ ...savedUsers, [userId]: false });
  };

  const saveRoleChange = async (userId) => {
    const newRole = roleChanges[userId];
    if (!newRole) return;

    setUpdatingUser(userId);
    try {
      await api.put(`/users/${userId}/role`, { role: newRole });
      toast.success('Role updated', { description: `User role has been updated to ${newRole}.` });
      setSavedUsers({ ...savedUsers, [userId]: true });
      fetchUsers();
      setRoleChanges({ ...roleChanges, [userId]: undefined });
    } catch (error) {
      toast.error('Failed to update role', { description: errorMessage(error) });
    } finally {
      setUpdatingUser(null);
    }
  };

  // Invited people get this role on their first Google sign-in, even when self-registration is off.
  const sendInvite = async (e) => {
    e.preventDefault();
    setInviting(true);
    try {
      await api.post('/users', invite);
      toast.success('User invited', { description: `${invite.email} can now sign in as ${invite.role}.` });
      setInvite({ email: '', name: '', role: invite.role });
      await fetchUsers();
    } catch (error) {
      toast.error('Failed to invite user', { description: errorMessage(error) });
    } finally {
      setInviting(false);
    }
  };

  const getRoleBadgeColor = (role) => {
    const colors = {
      physician: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
      nurse: 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20',
      pharmacist: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
      admin: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
      pending: 'bg-zinc-500/10 text-zinc-300 border-zinc-500/30',
    };
    return colors[role] || colors.nurse;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="w-8 h-8 animate-spin text-blue-500" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-semibold text-zinc-50" style={{ fontFamily: 'Outfit, sans-serif' }}>
          Admin Panel
        </h1>
        <p className="text-sm text-zinc-400 mt-1">
          Manage users and system settings
        </p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4">
        {['physician', 'nurse', 'pharmacist', 'admin'].map((role) => (
          <div key={role} className="bg-zinc-900 border border-zinc-800 rounded-lg p-4">
            <div className="flex items-center gap-3">
              <div className={`w-10 h-10 rounded-lg flex items-center justify-center ${
                role === 'physician' ? 'bg-blue-500/10' :
                role === 'nurse' ? 'bg-emerald-500/10' :
                role === 'pharmacist' ? 'bg-amber-500/10' :
                'bg-rose-500/10'
              }`}>
                {role === 'admin' ? (
                  <Shield className={`w-5 h-5 ${
                    role === 'physician' ? 'text-blue-400' :
                    role === 'nurse' ? 'text-emerald-400' :
                    role === 'pharmacist' ? 'text-amber-400' :
                    'text-rose-400'
                  }`} strokeWidth={1.5} />
                ) : (
                  <Users className={`w-5 h-5 ${
                    role === 'physician' ? 'text-blue-400' :
                    role === 'nurse' ? 'text-emerald-400' :
                    'text-amber-400'
                  }`} strokeWidth={1.5} />
                )}
              </div>
              <div>
                <p className="text-2xl font-semibold text-zinc-50">
                  {users.filter(u => u.role === role).length}
                </p>
                <p className="text-xs text-zinc-500 capitalize">{role}s</p>
              </div>
            </div>
          </div>
        ))}
      </div>

      {pendingCount > 0 && (
        <div data-testid="pending-users-notice" className="flex items-center gap-3 bg-amber-500/10 border border-amber-500/20 rounded-lg p-4 text-sm text-amber-300">
          <Clock className="w-4 h-4" />
          {pendingCount} {pendingCount === 1 ? 'person is' : 'people are'} waiting for approval. Give them a role below;
          until then they cannot see any patient data.
        </div>
      )}

      {/* Invite */}
      <form onSubmit={sendInvite} className="bg-zinc-900 border border-zinc-800 rounded-lg p-4 flex flex-wrap items-end gap-3">
        <div className="flex-1 min-w-[220px]">
          <label htmlFor="invite-email" className="text-xs text-zinc-500">Invite by email</label>
          <Input id="invite-email" data-testid="invite-email" type="email" required value={invite.email}
                 onChange={(e) => setInvite({ ...invite, email: e.target.value })}
                 placeholder="name@hospital.org" className="bg-zinc-800 border-zinc-700 mt-1" />
        </div>
        <div className="min-w-[160px]">
          <label htmlFor="invite-name" className="text-xs text-zinc-500">Name (optional)</label>
          <Input id="invite-name" value={invite.name} onChange={(e) => setInvite({ ...invite, name: e.target.value })}
                 className="bg-zinc-800 border-zinc-700 mt-1" />
        </div>
        <Select value={invite.role} onValueChange={(role) => setInvite({ ...invite, role })}>
          <SelectTrigger data-testid="invite-role" className="w-36 bg-zinc-800 border-zinc-700">
            <SelectValue />
          </SelectTrigger>
          <SelectContent className="bg-zinc-800 border-zinc-700">
            <SelectItem value="physician">Physician</SelectItem>
            <SelectItem value="nurse">Nurse</SelectItem>
            <SelectItem value="pharmacist">Pharmacist</SelectItem>
            <SelectItem value="admin">Admin</SelectItem>
          </SelectContent>
        </Select>
        <Button type="submit" data-testid="invite-submit" disabled={inviting} className="bg-blue-600 hover:bg-blue-500">
          {inviting ? <Loader2 className="w-4 h-4 animate-spin" /> : <><UserPlus className="w-4 h-4 mr-1" />Invite</>}
        </Button>
      </form>

      <AdminDowntime />

      <UnitsCard />

      {/* Users Table */}
      <div className="bg-zinc-900 border border-zinc-800 rounded-lg overflow-hidden">
        <div className="p-4 border-b border-zinc-800">
          <h2 className="text-lg font-medium text-zinc-50">User Management</h2>
          <p className="text-xs text-zinc-500 mt-0.5">{users.length} total users</p>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full">
            <thead>
              <tr className="bg-zinc-800/50">
                <th className="text-left text-xs font-medium text-zinc-500 uppercase tracking-wider p-4">User</th>
                <th className="text-left text-xs font-medium text-zinc-500 uppercase tracking-wider p-4">Email</th>
                <th className="text-left text-xs font-medium text-zinc-500 uppercase tracking-wider p-4">Current Role</th>
                <th className="text-left text-xs font-medium text-zinc-500 uppercase tracking-wider p-4">Change Role</th>
                <th className="text-left text-xs font-medium text-zinc-500 uppercase tracking-wider p-4">Units</th>
                <th className="text-left text-xs font-medium text-zinc-500 uppercase tracking-wider p-4">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800">
              {users.map((u) => (
                <tr key={u.user_id} data-testid={`user-row-${u.user_id}`} className="hover:bg-zinc-800/30 transition-colors">
                  <td className="p-4">
                    <div className="flex items-center gap-3">
                      {u.picture ? (
                        <img src={u.picture} alt={u.name} className="w-8 h-8 rounded-full" />
                      ) : (
                        <div className="w-8 h-8 rounded-full bg-zinc-800 flex items-center justify-center text-zinc-400 text-sm">
                          {u.name?.charAt(0) || 'U'}
                        </div>
                      )}
                      <span className="text-zinc-200">{u.name}</span>
                    </div>
                  </td>
                  <td className="p-4 text-zinc-400 text-sm">{u.email}</td>
                  <td className="p-4">
                    <span className={`inline-flex items-center px-2.5 py-0.5 rounded text-xs font-medium border capitalize ${getRoleBadgeColor(u.role)}`}>
                      {u.role}
                    </span>
                  </td>
                  <td className="p-4">
                    <Select
                      value={roleChanges[u.user_id] || u.role}
                      onValueChange={(value) => handleRoleChange(u.user_id, value)}
                    >
                      <SelectTrigger 
                        data-testid={`role-select-${u.user_id}`}
                        className="w-36 bg-zinc-800 border-zinc-700"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent className="bg-zinc-800 border-zinc-700">
                        <SelectItem value="physician">Physician</SelectItem>
                        <SelectItem value="nurse">Nurse</SelectItem>
                        <SelectItem value="pharmacist">Pharmacist</SelectItem>
                        <SelectItem value="admin">Admin</SelectItem>
                        <SelectItem value="pending">Pending (no access)</SelectItem>
                      </SelectContent>
                    </Select>
                  </td>
                  <td className="p-4"><UserUnitsPicker user={u} /></td>
                  <td className="p-4">
                    {roleChanges[u.user_id] && roleChanges[u.user_id] !== u.role ? (
                      <Button
                        data-testid={`save-role-${u.user_id}`}
                        size="sm"
                        onClick={() => saveRoleChange(u.user_id)}
                        disabled={updatingUser === u.user_id}
                        className="bg-blue-600 hover:bg-blue-500"
                      >
                        {updatingUser === u.user_id ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          <>
                            <Save className="w-4 h-4 mr-1" />
                            Save
                          </>
                        )}
                      </Button>
                    ) : savedUsers[u.user_id] ? (
                      <span className="flex items-center gap-1 text-emerald-400 text-xs">
                        <CheckCircle2 className="w-4 h-4" />
                        Saved
                      </span>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <BreakGlassReview />
    </div>
  );
};

export default AdminView;
