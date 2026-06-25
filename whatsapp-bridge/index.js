// autoagents WhatsApp bridge — Baileys (unofficial WhatsApp Web, multi-device).
//
// Holds a persistent WhatsApp connection, persists auth creds to GCS so restarts
// don't require re-pairing, forwards inbound messages to the gateway, and exposes
// /send for outbound. QR pairing served at /qr (token-gated).
//
// Env:
//   PORT                  (default 8080)
//   GCS_BUCKET            bucket for auth creds + inbound media
//   WA_AUTH_PREFIX        GCS prefix for creds (default wa-auth/)
//   GATEWAY_INBOUND_URL   POST target for inbound messages
//   WA_SECRET             shared secret (X-WA-Secret header; also ?token= for /qr)
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

let sock = null;
let connected = false;
let lastQrPng = null; // data: URL of the current pairing QR

// ---------- GCS-backed auth persistence ----------
async function restoreAuth() {
  mkdirSync(AUTH_DIR, { recursive: true });
  const [files] = await bucket.getFiles({ prefix: AUTH_PREFIX });
  for (const f of files) {
    const name = f.name.slice(AUTH_PREFIX.length);
    if (!name) continue;
    await f.download({ destination: path.join(AUTH_DIR, name) });
  }
  console.log(`restored ${files.length} auth file(s) from gs://${BUCKET}/${AUTH_PREFIX}`);
}

// Baileys fires creds.update very frequently (every pre-key). Debounce the
// whole-dir backup and upload sequentially with simple (non-resumable) uploads
// so we don't flood the 1GB e2-micro / GCS with parallel requests.
let backupTimer = null;
let backingUp = false;

function scheduleBackup() {
  if (backupTimer || backingUp) return;
  backupTimer = setTimeout(runBackup, 8000);
}

async function runBackup() {
  backupTimer = null;
  backingUp = true;
  try {
    const names = await fs.readdir(AUTH_DIR);
    for (const n of names) {
      await bucket.upload(path.join(AUTH_DIR, n), {
        destination: AUTH_PREFIX + n,
        resumable: false,
      });
    }
  } catch (e) {
    console.error("auth backup failed", e.message);
  }
  backingUp = false;
}

async function clearAuth() {
  await fs.rm(AUTH_DIR, { recursive: true, force: true }).catch(() => {});
  const [files] = await bucket.getFiles({ prefix: AUTH_PREFIX });
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

async function extractMedia(m) {
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

// ---------- WhatsApp socket ----------
async function start() {
  await restoreAuth();
  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    browser: Browsers.appropriate("autoagents"),
    markOnlineOnConnect: false,
    syncFullHistory: false,
  });

  sock.ev.on("creds.update", async () => {
    await saveCreds();
    scheduleBackup();
  });

  sock.ev.on("connection.update", async (u) => {
    const { connection, lastDisconnect, qr } = u;
    if (qr) {
      lastQrPng = await qrcode.toDataURL(qr);
      connected = false;
      console.log("QR ready — open /qr to scan");
    }
    if (connection === "open") {
      connected = true;
      lastQrPng = null;
      console.log("WhatsApp connected");
    }
    if (connection === "close") {
      connected = false;
      const code = lastDisconnect?.error?.output?.statusCode;
      if (code === DisconnectReason.loggedOut) {
        console.log("logged out — clearing creds; re-pair via /qr");
        await clearAuth();
        setTimeout(start, 2000);
      } else {
        console.log(`connection closed (${code}); reconnecting`);
        setTimeout(start, 3000);
      }
    }
  });

  sock.ev.on("messages.upsert", async ({ messages, type }) => {
    if (type !== "notify") return;
    for (const m of messages) {
      if (!m.message || m.key.fromMe) continue;
      const jid = m.key.remoteJid || "";
      if (jid.endsWith("@g.us")) continue; // DMs only for v1
      const from = jid.replace("@s.whatsapp.net", "");
      const text = extractText(m.message);
      const media = await extractMedia(m);
      console.log(`inbound from ${from}: ${text.slice(0, 60)}${media ? " [media]" : ""}`);
      await postInbound({ from, text, media, name: m.pushName || "" });
    }
  });
}

// ---------- HTTP server ----------
const app = express();
app.use(express.json({ limit: "1mb" }));

function authed(req) {
  return WA_SECRET && req.get("X-WA-Secret") === WA_SECRET;
}

app.get("/health", (_req, res) => res.json({ status: "ok", connected }));

app.get("/qr", async (req, res) => {
  if (WA_SECRET && req.query.token !== WA_SECRET) return res.status(401).send("unauthorized");
  if (connected) return res.send("<h2>WhatsApp already linked ✅</h2>");
  if (!lastQrPng) return res.send("<h2>No QR yet — wait a few seconds and refresh.</h2>");
  res.send(
    `<html><body style="text-align:center;font-family:sans-serif">
     <h2>Scan with WhatsApp → Linked devices → Link a device</h2>
     <img src="${lastQrPng}" style="width:320px"/>
     <p>Page auto-refreshes.</p>
     <script>setTimeout(()=>location.reload(),8000)</script>
     </body></html>`,
  );
});

app.post("/send", async (req, res) => {
  if (!authed(req)) return res.status(401).json({ error: "unauthorized" });
  if (!connected || !sock) return res.status(503).json({ error: "not connected" });
  const { to, text } = req.body || {};
  if (!to || !text) return res.status(400).json({ error: "to and text required" });
  const jid = String(to).includes("@")
    ? String(to)
    : String(to).replace(/\D/g, "") + "@s.whatsapp.net";
  try {
    const r = await sock.sendMessage(jid, { text: String(text) });
    res.json({ ok: true, id: r?.key?.id || "" });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message });
  }
});

app.listen(PORT, () => console.log(`bridge http on :${PORT}`));
start().catch((e) => {
  console.error("start failed", e);
  process.exit(1);
});
