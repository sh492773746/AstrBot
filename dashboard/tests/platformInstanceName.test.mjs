import assert from "node:assert/strict";
import test from "node:test";
import { getPlatformInstanceName } from "../src/utils/platformInstanceName.mjs";

const bot = { type: "wangshangliao", id: "stable-id", account_id: "123" };

test("uses the current account name without changing instance identity", () => {
  assert.equal(getPlatformInstanceName(bot, { account_id: "123", nickname: "Current Name" }), "Current Name");
  assert.deepEqual(bot, { type: "wangshangliao", id: "stable-id", account_id: "123" });
});

test("does not reuse another account's runtime name", () => {
  assert.equal(getPlatformInstanceName(bot, { account_id: "456", nickname: "Other Name" }), "123");
  assert.equal(getPlatformInstanceName({ ...bot, nickname: "Saved" }, { account_id: "456", nickname: "Other" }), "Saved");
});

test("unknown names fall back to account then instance and strip controls", () => {
  assert.equal(getPlatformInstanceName(bot), "123");
  assert.equal(getPlatformInstanceName({ ...bot, account_id: "" }), "stable-id");
  assert.equal(getPlatformInstanceName({ ...bot, nickname: "\u202e\u0000" }), "123");
  assert.equal(getPlatformInstanceName({ ...bot, nickname: "  Safe\u202e Name  " }), "Safe Name");
  assert.equal(getPlatformInstanceName({ ...bot, nickname: "x".repeat(200) }).length, 128);
});

test("other platforms retain their existing instance labels", () => {
  assert.equal(getPlatformInstanceName({ id: "telegram_instance", type: "telegram", nickname: "Ignore" }), "telegram_instance");
});
