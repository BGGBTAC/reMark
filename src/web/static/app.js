/* reMark Web — small client helpers for subscription + push UI. */

async function urlB64ToUint8Array(base64) {
  const padding = "=".repeat((4 - (base64.length % 4)) % 4);
  const str = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(str);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

async function subscribeToPush() {
  const vapidResp = await fetch("/vapid-public-key");
  const { key } = await vapidResp.json();
  if (!key) {
    alert("VAPID keys not configured. Set web.vapid_public_key in config.yaml.");
    return;
  }

  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: await urlB64ToUint8Array(key),
  });

  await fetch("/webpush/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(sub),
  });
  alert("Notifications enabled.");
}

window.remarkSubscribe = subscribeToPush;
