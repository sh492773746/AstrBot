<script setup lang="ts">
import OpenService from "../../plugin-pages/wangshangliao/OpenService.vue";
import type {
  BlacklistEntry, BlacklistMember, BlacklistMode, BlacklistPreview,
} from "../../plugin-pages/wangshangliao/memberBlacklistControl";
import "../../plugin-pages/wangshangliao/style.css";

type Payload = Record<string, unknown>;
const conflictMessage = "名单未修改：该成员已在本群另一份名单中，请先移除另一名单，再重新添加。黑白名单不能同时添加，历史记录不会自动删除。";
const calls: Array<{ action: string; payload: Payload }> = [];
const candidates: BlacklistMember[] = [
  { id: "3", name: "同名成员" }, { id: "4", name: "同名成员" },
];
const lists: Record<string, BlacklistEntry[]> = {
  "5": [
    { id: "8", peer: "1008", name: "已离群成员", kicks: 3, phase: "mute", status: "unknown", cycle: 1 },
    { id: "9", peer: "1009", name: "正在处理的成员", kicks: 1, phase: "mute", status: "pending", cycle: 1 },
    { id: "10", peer: "1010", name: "执行出错的成员", kicks: 0, phase: "mute", status: "error", cycle: 1 },
    { id: "11", peer: "1011", name: "用于检查长名称换行的黑名单成员".repeat(3), kicks: 2, phase: "mute", status: "active", cycle: 1 },
    { id: "16", peer: "1016", name: "同名黑名单成员", phase: "mute", status: "idle", cycle: 1 },
    { id: "17", peer: "1017", name: "同名黑名单成员", phase: "mute", status: "idle", cycle: 1 },
  ],
  "6": [],
};
const whitelist: Record<string, Array<BlacklistMember & { source?: string; count?: number }>> = {
  "5": [
    { id: "12", name: "" },
    { id: "13", name: "同名名单成员" },
    { id: "14", name: "同名名单成员", source: "daily_top30", count: 101 },
    { id: "11", name: "历史双名单成员" },
  ],
  "6": [],
};
const nativeCandidates: BlacklistMember[] = [
  ...candidates, ...lists["5"].filter(member => member.id !== "8"),
  ...whitelist["5"].filter(member => !["11", "12"].includes(member.id)),
];
const groups = ["5", "6"].map(group_id => ({
  account: "1", group_id, name: `离线测试群${group_id}`, active: true, present: true,
  version: 1, epoch: 1, paused: 0, admission: "verified",
  native_role: "GROUP_ROLE_ADMIN", native_capability: { state: "available" },
  rules: { advertising: true, flood: true, keywords: [], custom_rules: [] }, managers: ["2"],
}));
const selections = new Map<string, { action: string; group: string; mode: BlacklistMode; candidates: BlacklistMember[] }>();
const muteMinutes: Record<string, number> = { "5": 41760, "6": 120 };
const versions: Record<string, number> = { "5": 1, "6": 1 };
const whitelistVersions: Record<string, number> = { "5": 1, "6": 1 };
const drafts = new Map<string, BlacklistPreview & { action: string; group: string }>();
const fixture = {
  calls, hold: "", fail: "", error: "", muteMinutes,
  waiting: [] as Array<{ resolve: () => void; reject: () => void }>,
  release(fail = false) {
    const next = fixture.waiting.shift();
    if (!next) throw new Error("No pending fixture response");
    if (fail) next.reject(); else next.resolve();
  },
};
(window as unknown as { blacklistFixture: typeof fixture }).blacklistFixture = fixture;

const bridge = {
  async apiGet() { throw new Error("Fixture GET not allowed"); },
  async apiPost(endpoint: string, request: { action: string; payload: Payload }) {
    if (endpoint !== "open-service") throw new Error("Fixture endpoint not allowed");
    const { action, payload: p } = request;
    calls.push({ action, payload: structuredClone(p) });
    if (action === "status") return {
      mode: "integrated", settings: { paused: 0, global_limit: 100 }, groups,
      accounts: [{ account: "1", nickname: "离线机器人" }],
      ai_profiles: [], providers: [], queue_health: [], audit: [], admins: [],
    };
    if (action === "tenant_members") return {
      members: [{ id: "2", name: "离线管理", role: "GROUP_ROLE_OWNER" }],
      native_capability: { state: "available" },
    };
    if (action === "business_rules") {
      if (p.operation !== "read") throw new Error("Business rule writes not allowed");
      return { version: 0, config: { keywords: [], ads: [] }, schedules: [] };
    }
    if (action === "page_schema") return { fields: { properties: {} } };
    if (action !== "member_blacklist" && action !== "member_whitelist") throw new Error(`Unexpected action: ${action}`);
    if (p.account !== "1" || !["5", "6"].includes(String(p.group_id)) || p.tenant_version !== 1
        || ["id", "uid", "member", "peer"].some(key => key in p)) throw new Error("Invalid fixture scope");
    const group = String(p.group_id);
    const operation = String(p.operation);
    const isBlacklist = action === "member_blacklist";
    const savedLists: Record<string, BlacklistMember[]> = isBlacklist ? lists : whitelist;
    const otherLists = isBlacklist ? whitelist : lists;
    const revision = isBlacklist ? versions : whitelistVersions;
    if (fixture.fail === operation) {
      fixture.fail = "";
      const error = fixture.error || "timeout";
      fixture.error = "";
      throw new Error(error);
    }
    let response: unknown;
    if (operation === "read") response = {
      version: revision[group],
      members: structuredClone(savedLists[group]).map(member => ({
        ...member, conflict: otherLists[group].some(other => other.id === member.id),
      })),
      ...(isBlacklist ? { mute_minutes: muteMinutes[group] } : {}),
    };
    else if (operation === "search") {
      const mode = p.mode as BlacklistMode;
      const available = group === "5" ? nativeCandidates : [];
      const matches = mode === "remove"
        ? savedLists[group].filter(m => m.id === p.query || m.name.includes(String(p.query)))
        : available.filter((member, index) => member.name.includes(String(p.query))
          && available.findIndex(row => row.id === member.id) === index
          && !savedLists[group].some(row => row.id === member.id));
      const selection = `selection-${calls.length}`;
      selections.set(selection, { action, group, mode, candidates: matches });
      response = { selection: matches.length ? selection : "", members: matches, version: revision[group] };
    } else if (operation === "preview") {
      const selected = selections.get(String(p.selection));
      const member = selected?.candidates[Number(p.index) - 1];
      if (!selected || selected.action !== action || selected.group !== group || !member) throw new Error("blacklist_changed");
      if (selected.mode === "add" && otherLists[group].some(row => row.id === member.id)) throw new Error(conflictMessage);
      const token = `token-${calls.length}`;
      drafts.set(token, { token, version: revision[group], action, group, mode: selected.mode, member });
      response = {
        token, version: revision[group], mode: selected.mode, member,
        ...(isBlacklist ? { mute_minutes: muteMinutes[group] } : {}),
      };
    } else if (operation === "settings_preview" && isBlacklist) {
      if (!Number.isInteger(p.minutes) || Number(p.minutes) < 1 || Number(p.minutes) > 41760) {
        throw new Error("blacklist_minutes");
      }
      const token = `settings-${calls.length}`;
      const draft = {
        token, version: versions[group], mode: "settings" as const, mute_minutes: Number(p.minutes),
      };
      drafts.set(token, { ...draft, action, group });
      response = draft;
    } else if (operation === "commit") {
      const draft = drafts.get(String(p.token));
      drafts.delete(String(p.token));
      if (!draft || draft.action !== action || draft.group !== group || draft.version !== revision[group]) throw new Error("blacklist_changed");
      if (draft.mode === "add" && otherLists[group].some(row => row.id === draft.member.id)) throw new Error(conflictMessage);
      revision[group]++;
      if (draft.mode === "settings") {
        muteMinutes[group] = draft.mute_minutes;
        response = { saved: true, mode: "settings", mute_minutes: muteMinutes[group] };
      } else {
        if (draft.mode === "add") {
          if (isBlacklist) lists[group].push({
            ...draft.member, peer: `100${draft.member.id}`, kicks: 0, phase: "mute", status: "idle", cycle: 1,
          });
          else whitelist[group].push(draft.member);
        } else savedLists[group] = savedLists[group].filter(m => m.id !== draft.member.id);
        response = { saved: true, member: draft.member.name, mode: draft.mode };
      }
    } else throw new Error("Unexpected fixture operation");
    if (fixture.hold === operation) {
      fixture.hold = "";
      await new Promise<void>((resolve, reject) => fixture.waiting.push({
        resolve, reject: () => reject(new Error("timeout")),
      }));
    }
    return response;
  },
};
</script>

<template>
  <main><OpenService :bridge="bridge" view="rules" /></main>
</template>
