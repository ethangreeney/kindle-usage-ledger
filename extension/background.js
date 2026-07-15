const CLAUDE_USAGE_URL =
  "https://claude.ai/api/organizations/YOUR-ORG-ID/usage"; // find your org id in any claude.ai request URL (DevTools network tab)
const DASHBOARD_URL = "http://127.0.0.1:8790/api/claude";
const DASHBOARD_TOKEN = "change-me"; // must match DASHBOARD_ACCESS_TOKEN on the server
const ALARM_NAME = "sync-claude-usage";

async function waitForTab(tabId) {
  const existing = await chrome.tabs.get(tabId);
  if (existing.status === "complete") return existing;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      chrome.tabs.onUpdated.removeListener(listener);
      reject(new Error("Claude did not finish loading"));
    }, 20000);
    function listener(updatedId, changeInfo, tab) {
      if (updatedId !== tabId || changeInfo.status !== "complete") return;
      clearTimeout(timeout);
      chrome.tabs.onUpdated.removeListener(listener);
      resolve(tab);
    }
    chrome.tabs.onUpdated.addListener(listener);
  });
}

async function getClaudeTab() {
  const tabs = await chrome.tabs.query({ url: "https://claude.ai/*" });
  let tab = tabs.find((candidate) => candidate.status === "complete") || tabs[0];
  if (!tab) {
    tab = await chrome.tabs.create({
      url: "https://claude.ai/new",
      active: false,
      pinned: true,
    });
  }
  return waitForTab(tab.id);
}

async function readUsage(tabId) {
  const [execution] = await chrome.scripting.executeScript({
    target: { tabId },
    world: "MAIN",
    func: async (url) => {
      const response = await fetch(url, {
        credentials: "include",
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Claude returned ${response.status}`);
      return response.json();
    },
    args: [CLAUDE_USAGE_URL],
  });
  if (!execution || !execution.result) throw new Error("Claude usage was unavailable");
  return execution.result;
}

async function syncUsage() {
  try {
    const tab = await getClaudeTab();
    const usage = await readUsage(tab.id);
    const response = await fetch(DASHBOARD_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "access-token": DASHBOARD_TOKEN,
      },
      body: JSON.stringify(usage),
    });
    if (!response.ok) throw new Error(`Dashboard returned ${response.status}`);
    await chrome.action.setBadgeBackgroundColor({ color: "#1F7A1F" });
    await chrome.action.setBadgeText({ text: "✓" });
  } catch (error) {
    console.error("Claude Usage Bridge:", error);
    await chrome.action.setBadgeBackgroundColor({ color: "#9F1D1D" });
    await chrome.action.setBadgeText({ text: "!" });
  }
}

function start() {
  chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
  syncUsage();
}

chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) syncUsage();
});
