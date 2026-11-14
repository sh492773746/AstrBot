export type WhitelistMember = {
  id: string; name: string; conflict?: boolean; source?: "daily_top30"; count?: number;
};
export type HistoryPeriod = "recent" | "today" | "yesterday";
export type HistoryMember = {
  id: string; name: string; count: number; matched_count: number; unavailable: number;
  messages: Array<{ message: string; stamp: number; text: string }>;
};
export type HistoryResponse = {
  version: number; members: HistoryMember[]; period: HistoryPeriod; query: string;
  from_date: string; to_date: string; timezone: string; updated_at: number; catching_up: boolean;
};
export type HistoryRequest = {
  account: string; group_id: string; tenant_version: number; operation: "history";
  member_ids: string[]; list_version: number; period: HistoryPeriod; query: string;
};
export interface WhitelistHistoryApi {
  (action: "member_whitelist", payload: HistoryRequest): Promise<HistoryResponse>;
}
