/**
 * Keep readable account labels separate from stable instance and account IDs.
 */
export function getPlatformInstanceName(platform, runtime = {}) {
  if (platform?.type !== "wangshangliao") return platform?.id || "";
  const account = String(platform.account_id || "");
  const runtimeAccount = String(runtime?.account_id || runtime?.account || "");
  const nickname = runtimeAccount === account
    ? runtime?.nickname || platform.nickname
    : platform.nickname;
  const name = typeof nickname === "string"
    ? nickname.replace(/[\p{Cc}\p{Cf}]/gu, "").trim().slice(0, 128)
    : "";
  return name || account || platform.id || "";
}
