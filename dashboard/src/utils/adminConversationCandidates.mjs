/**
 * Resolve account identities only from existing private conversations.
 * Group creators and transport/session IDs must never become administrator IDs.
 */
export function adminConversationCandidates(infos = []) {
  const accounts = new Map();
  for (const info of Array.isArray(infos) ? infos : []) {
    if (!info || typeof info.umo !== "string") continue;
    const [platform, type, ...parts] = info.umo.split(":");
    if (!platform || !["friendmessage", "friend", "private", "privatemessage"].includes(type?.toLowerCase())) continue;
    if (info.platform && info.platform !== platform) continue;
    if (info.message_type && (typeof info.message_type !== "string" || info.message_type.toLowerCase() !== type.toLowerCase())) continue;

    let id = typeof info.creator_sender_id === "string" ? info.creator_sender_id : "";
    if (platform.startsWith("wangshangliao_")) {
      const match = parts.join(":").match(/^[1-9][0-9]*\/private\/([1-9][0-9]*)\/[A-Za-z0-9_-]+={0,2}$/);
      if (!match || (id && id !== match[1])) continue;
      id = match[1];
    }
    if (!id || id.length > 255 || /[\s\p{C}]/u.test(id)) continue;

    const names = [info.user_alias, info.auto_name]
      .filter(value => typeof value === "string" && value !== id && value !== info.umo)
      .map(value => value.replace(/[\p{C}]/gu, "").trim().slice(0, 255))
      .filter(Boolean);
    const account = accounts.get(id) || { id, name: "", names: [], platforms: [] };
    if (!account.name && names.length) account.name = names[0];
    account.names = [...new Set([...account.names, ...names])];
    account.platforms = [...new Set([...account.platforms, platform])];
    accounts.set(id, account);
  }
  return [...accounts.values()].sort((a, b) =>
    Number(!a.name) - Number(!b.name)
    || a.name.localeCompare(b.name, "zh-CN") || a.id.localeCompare(b.id),
  );
}
