import assert from "node:assert/strict";
import test from "node:test";
import { adminConversationCandidates } from "../src/utils/adminConversationCandidates.mjs";

function wsl(uid, name, extra = {}) {
  return {
    umo: `wangshangliao_bot:FriendMessage:20000002/private/${uid}/MTIz`,
    creator_sender_id: uid,
    auto_name: name,
    ...extra,
  };
}

test("uses the business UID, never the bot, UMO or NIM transport identity", () => {
  const [account] = adminConversationCandidates([wsl("23691273", "testfork")]);
  assert.equal(account.id, "23691273");
  assert.equal(account.name, "testfork");
  assert.deepEqual(account.platforms, ["wangshangliao_bot"]);
});

test("rejects groups even when they have a named creator", () => {
  assert.deepEqual(adminConversationCandidates([
    wsl("23691273", "Group", { umo: "wangshangliao_bot:GroupMessage:20000002/1143980" }),
    { umo: "telegram_bot:GroupMessage:group", creator_sender_id: "123", auto_name: "A group" },
  ]), []);
});

test("rejects contradictory, malformed and missing account evidence", () => {
  assert.deepEqual(adminConversationCandidates([
    wsl("23691273", "Wrong", { creator_sender_id: "123" }),
    wsl("23691273", "Wrong", { platform: "other_bot" }),
    wsl("23691273", "Wrong", { message_type: "GroupMessage" }),
    wsl("23691273", "Wrong", { umo: "wangshangliao_bot:FriendMessage:1143980" }),
    { umo: "telegram_bot:FriendMessage:abc", creator_sender_id: "" },
    { umo: "telegram_bot:FriendMessage:abc", creator_sender_id: "user\nid" },
    null,
  ]), []);
});

test("deduplicates an account across private conversations and preserves aliases", () => {
  const result = adminConversationCandidates([
    wsl("23691273", "23691273"),
    wsl("23691273", "testfork", { user_alias: "测试用户" }),
    wsl("23691273", "testfork", { umo: "wangshangliao_other:FriendMessage:12/private/23691273/MTIz" }),
  ]);
  assert.equal(result.length, 1);
  assert.equal(result[0].name, "测试用户");
  assert.deepEqual(result[0].names, ["测试用户", "testfork"]);
  assert.equal(result[0].platforms.length, 2);
});

test("supports native private identity without an alias but does not invent a name", () => {
  const [result] = adminConversationCandidates([wsl("23691273", "", { creator_sender_id: "" })]);
  assert.equal(result.id, "23691273");
  assert.equal(result.name, "");
});

test("preserves distinct IDs with identical display names", () => {
  const result = adminConversationCandidates([wsl("2", "同名"), wsl("3", "同名")]);
  assert.equal(result.length, 2);
  assert.deepEqual(result.map(account => account.id), ["2", "3"]);
});

test("uses verified creator identity for other private platforms", () => {
  const [account] = adminConversationCandidates([{
    umo: "telegram_bot:FriendMessage:opaque-session",
    creator_sender_id: "456",
    auto_name: "<b>Not HTML</b>\u202e",
  }]);
  assert.equal(account.id, "456");
  assert.equal(account.name, "<b>Not HTML</b>");
});

test("handles invalid response collections without changing any administrator values", () => {
  assert.deepEqual(adminConversationCandidates(null), []);
  assert.deepEqual(adminConversationCandidates({}), []);
});
