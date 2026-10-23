export type BlacklistMember = { id: string; name: string; conflict?: boolean };
export type BlacklistMode = "add" | "remove";
export type BlacklistEntry = BlacklistMember & {
  peer: string;
  kicks?: number;
  phase: string;
  status: string;
  cycle: number;
};
export type BlacklistScope = {
  account: string;
  group_id: string;
  tenant_version: number;
};
export type BlacklistPreview = {
  token: string;
  version: number;
} & ({
  member: BlacklistMember;
  mode: BlacklistMode;
  mute_minutes?: number;
  summary?: string;
} | {
  mode: "settings";
  mute_minutes: number;
});

export interface MemberBlacklistApi {
  (action: "member_blacklist", payload: BlacklistScope & {
    operation: "read";
  }): Promise<{ version: number; members: BlacklistEntry[]; mute_minutes: number }>;
  (action: "member_blacklist", payload: BlacklistScope & {
    operation: "search"; mode: BlacklistMode; query: string;
  }): Promise<{ selection: string; version: number; members: BlacklistMember[] }>;
  (action: "member_blacklist", payload: BlacklistScope & {
    operation: "preview"; selection: string; index: number;
  }): Promise<BlacklistPreview & { mode: BlacklistMode }>;
  (action: "member_blacklist", payload: BlacklistScope & {
    operation: "settings_preview"; minutes: number;
  }): Promise<BlacklistPreview & { mode: "settings" }>;
  (action: "member_blacklist", payload: BlacklistScope & {
    operation: "commit"; token: string;
  }): Promise<
    { saved: true; member: string; mode: BlacklistMode }
    | { saved: true; mode: "settings"; mute_minutes: number }
  >;
}
