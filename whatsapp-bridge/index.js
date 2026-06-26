// autoagents WhatsApp bridge — Baileys, MULTI-SESSION (one socket per tenant).
//
// Each tenant links their own dedicated WhatsApp number by scanning a QR. The
// bridge holds one Baileys socket per tenant, persists per-tenant auth creds to
// GCS so restarts don't require re-pairing, forwards inbound (tagged with the
// tenant) to the gateway, and exposes a tenant-scoped HTTP API for pairing and
// sending. The account boundary IS the tenant boundary, so routing is trivial.
//
// Env:
//   PORT                  (default 8080)
//   GCS_BUCKET            bucket for auth creds + inbound media
//   WA_AUTH_PREFIX        GCS prefix for creds (default wa-auth/); per tenant: wa-auth/<tenant>/
//   GATEWAY_INBOUND_URL   POST target for inbound messages
//   WA_SECRET             shared secret (X-WA-Secret header on every endpoint)
//   AUTH_DIR              local creds dir (default /data/auth); per tenant: <AUTH_DIR>/<tenant>
import { mkdirSync, promises as fs } from "node:fs";
import path from "node:path";

import { Storage } from "@google-cloud/storage";
import makeWASocket, {
  Browsers,
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from "baileys";
import express from "express";
import pino from "pino";
import qrcode from "qrcode";

const PORT = parseInt(process.env.PORT || "8080", 10);
const BUCKET = process.env.GCS_BUCKET || "autoagents-500500-attachments";
const AUTH_PREFIX = process.env.WA_AUTH_PREFIX || "wa-auth/";
const GATEWAY_INBOUND_URL = process.env.GATEWAY_INBOUND_URL || "";
const WA_SECRET = process.env.WA_SECRET || "";
const AUTH_DIR = process.env.AUTH_DIR || "/data/auth";

const logger = pino({ level: "silent" });
const storage = new Storage();
const bucket = storage.bucket(BUCKET);

// tenantId -> { sock, connected, connecting, lastQr, number, saveCreds, backupTimer, backingUp }
const sessions = new Map();

const digits = (j) => String(j || "").split("@")[0].split(":")[0].replace(/\D/g, "");
const tenantPrefix = (t) => `${AUTH_PREFIX}${t}/`;
const tenantDir = (t) => path.join(AUTH_DIR, t);

// ---------- GCS-backed per-tenant auth persistence ----------
async function restoreAuth(tenant) {
  const dir = tenantDir(tenant);
  mkdirSync(dir, { recursive: true });
  const [files] = await bucket.getFiles({ prefix: tenantPrefix(tenant) });
  for (const f of files) {
    const name = f.name.slice(tenantPrefix(tenant).length);
    if (!name) continue;
    await f.download({ destination: path.join(dir, name) });
  }
  console.log(`[${tenant}] restored ${files.length} auth file(s)`);
}

// Baileys fires creds.update very frequently; debounce the per-tenant whole-dir
// backup and upload sequentially (non-resumable) so we don't flood the VM / GCS.
function scheduleBackup(tenant) {
  const s = sessions.get(tenant);
  if (!s || s.backupTimer || s.backingUp) return;
  s.backupTimer = setTimeout(() => runBackup(tenant), 8000);
}

async function runBackup(tenant) {
  const s = sessions.get(tenant);
  if (!s) return;
  s.backupTimer = null;
  s.backingUp = true;
  try {
    const names = await fs.readdir(tenantDir(tenant));
    for (const n of names) {
      await bucket.upload(path.join(tenantDir(tenant), n), {
        destination: tenantPrefix(tenant) + n,
        resumable: false,
      });
    }
  } catch (e) {
    console.error(`[${tenant}] auth backup failed`, e.message);
  }
  s.backingUp = false;
}

async function clearAuth(tenant) {
  await fs.rm(tenantDir(tenant), { recursive: true, force: true }).catch(() => {});
  const [files] = await bucket.getFiles({ prefix: tenantPrefix(tenant) });
  await Promise.all(files.map((f) => f.delete().catch(() => {})));
}

// ---------- inbound forwarding ----------
async function postInbound(payload) {
  if (!GATEWAY_INBOUND_URL) return;
  try {
    const r = await fetch(GATEWAY_INBOUND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-WA-Secret": WA_SECRET },
      body: JSON.stringify(payload),
    });
    if (!r.ok) console.error("gateway inbound", r.status, (await r.text()).slice(0, 200));
  } catch (e) {
    console.error("gateway inbound error", e.message);
  }
}

const MEDIA_TYPES = ["imageMessage", "videoMessage", "audioMessage", "documentMessage"];

async function extractMedia(m, sock) {
  const msg = m.message || {};
  for (const mt of MEDIA_TYPES) {
    if (!msg[mt]) continue;
    try {
      const buf = await downloadMediaMessage(
        m,
        "buffer",
        {},
        { logger, reuploadRequest: sock.updateMediaMessage },
      );
      const mime = msg[mt].mimetype || "application/octet-stream";
      const ext = (mime.split("/")[1] || "bin").split(";")[0];
      const dest = `wa-inbound/${new Date().toISOString().slice(0, 10)}/${Date.now()}.${ext}`;
      await bucket.file(dest).save(buf, { contentType: mime });
      return { uri: `gs://${BUCKET}/${dest}`, type: mime };
    } catch (e) {
      console.error("media download failed", e.message);
    }
    break;
  }
  return null;
}

function extractText(msg) {
  return (
    msg.conversation ||
    msg.extendedTextMessage?.text ||
    msg.imageMessage?.caption ||
    msg.videoMessage?.caption ||
    msg.documentMessage?.caption ||
    ""
  );
}

// ---------- per-tenant WhatsApp socket ----------
// Guarded entry: won't create a duplicate while one is connected/connecting.
async function start(tenant) {
  const ex = sessions.get(tenant);
  if (ex && (ex.connected || ex.connecting)) return ex;
  return _connect(tenant);
}

async function _connect(tenant) {
  await restoreAuth(tenant);
  const { state, saveCreds } = await useMultiFileAuthState(tenantDir(tenant));
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    logger,
    browser: Browsers.appropriate("autoagents"),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });

  const s = {
    sock,
    connected: false,
    connecting: true,
    lastQr: null,
    number: "",
    saveCreds,
  };
  sessions.set(tenant, s);

  sock.ev.on("creds.update", async () => {
    await saveCreds();
    scheduleBackup(tenant);
  });

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      s.lastQr = await qrcode.toDataURL(qr);
      console.log(`[${tenant}] QR ready`);
    }
    if (connection === "open") {
      s.connected = true;
      s.connecting = false;
      s.lastQr = null;
      s.number = digits(sock.user?.id || "");
      console.log(`[${tenant}] connected as ${s.number}`);
    }
    if (connection === "close") {
      s.connected = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        console.log(`[${tenant}] logged out — clearing creds`);
        s.connecting = false;
        await clearAuth(tenant);
        sessions.delete(tenant);
      } else {
        console.log(`[${tenant}] connection closed (${code}); reconnecting`);
        s.connecting = true; // keep guarded while we retry
        setTimeout(() => _connect(tenant), 3000);
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      if (!m.message || m.key.fromMe) continue;
      const jid = m.key.remoteJid || "";
      if (jid.endsWith("@g.us")) continue; // DMs only
      // Resolve both the LID (…@lid) and the real phone (…@s.whatsapp.net) so the
      // gateway can identify the owner vs a third-party contact on this socket.
      const jidAlt = m.key.remoteJidAlt || "";
      let lid = "";
      let pn = "";
      for (const j of [jid, jidAlt]) {
        if (j.endsWith("@lid")) lid = digits(j);
        else if (j.endsWith("@s.whatsapp.net")) pn = digits(j);
      }
      if (!pn && lid) {
        try {
          const mapped = await sock.signalRepository?.lidMapping?.getPNForLID?.(
            jid.endsWith("@lid") ? jid : jidAlt,
          );
          if (mapped) pn = digits(mapped);
        } catch {
          /* mapping unavailable */
        }
      }
      const from = jid.replace("@s.whatsapp.net", "");
      const text = extractText(m.message);
      const media = await extractMedia(m, sock);
      console.log(
        `[${tenant}] inbound from ${from} (pn:${pn || "-"} lid:${lid || "-"}): ${text.slice(0, 50)}${media ? " [media]" : ""}`,
      );
      await postInbound({ tenant, from, pn, lid, text, media, name: m.pushName || "" });
    }
  });

  return s;
}

// Restore + start every tenant that already has creds in GCS.
async function startAll() {
  const [files] = await bucket.getFiles({ prefix: AUTH_PREFIX });
  const tenants = new Set();
  for (const f of files) {
    const rest = f.name.slice(AUTH_PREFIX.length); // "<tenant>/creds.json"
    if (!rest.includes("/")) continue; // ignore stray top-level files (no tenant dir)
    const t = rest.split("/")[0];
    if (t) tenants.add(t);
  }
  console.log(`restoring ${tenants.size} tenant session(s): ${[...tenants].join(", ") || "none"}`);
  for (const t of tenants) {
    try {
      await start(t);
    } catch (e) {
      console.error(`start ${t} failed`, e.message);
    }
  }
}

// ---------- HTTP server ----------
const app = express();
app.use(express.json({ limit: "1mb" }));

function authed(req) {
  return WA_SECRET && req.get("X-WA-Secret") === WA_SECRET;
}

app.get("/health", (_req, res) => res.json({ status: "ok", sessions: sessions.size }));

app.post("/sessions/:tenant/start", async (req, res) => {
  if (!authed(req)) return res.status(401).json({ error: "unauthorized" });
  try {
    const s = await start(req.params.tenant);
    res.json({ tenant: req.params.tenant, connected: s.connected, hasQr: !!s.lastQr });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get("/sessions/:tenant", (req, res) => {
  if (!authed(req)) return res.status(401).json({ error: "unauthorized" });
  const s = sessions.get(req.params.tenant);
  res.json({
    tenant: req.params.tenant,
    connected: !!s?.connected,
    number: s?.number || "",
    hasQr: !!s?.lastQr,
  });
});

app.get("/sessions/:tenant/qr", (req, res) => {
  if (!authed(req)) return res.status(401).json({ error: "unauthorized" });
  const s = sessions.get(req.params.tenant);
  if (!s) return res.json({ connected: false, qr: null, number: "" });
  res.json({ connected: s.connected, qr: s.connected ? null : s.lastQr, number: s.number || "" });
});

app.post("/sessions/:tenant/logout", async (req, res) => {
  if (!authed(req)) return res.status(401).json({ error: "unauthorized" });
  const t = req.params.tenant;
  const s = sessions.get(t);
  try {
    if (s?.sock) {
      try {
        await s.sock.logout();
      } catch {
        /* socket may already be down */
      }
    }
    await clearAuth(t);
    sessions.delete(t);
    res.json({ ok: true });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.post("/send", async (req, res) => {
  if (!authed(req)) return res.status(401).json({ error: "unauthorized" });
  const { tenant, to, text } = req.body || {};
  if (!tenant || !to || !text) {
    return res.status(400).json({ error: "tenant, to and text required" });
  }
  const s = sessions.get(tenant);
  if (!s || !s.connected || !s.sock) {
    return res.status(503).json({ error: "tenant session not connected" });
  }
  const jid = String(to).includes("@")
    ? String(to)
    : String(to).replace(/\D/g, "") + "@s.whatsapp.net";
  try {
    const r = await s.sock.sendMessage(jid, { text: String(text) });
    res.json({ ok: true, id: r?.key?.id || "" });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.listen(PORT, () => console.log(`bridge http on :${PORT}`));
startAll().catch((e) => console.error("startAll failed", e.message));
