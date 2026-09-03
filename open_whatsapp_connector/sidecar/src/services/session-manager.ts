import {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  fetchLatestWaWebVersion,
  isJidBroadcast,
  makeCacheableSignalKeyStore,
  makeWASocket,
  useMultiFileAuthState,
  type GroupMetadata,
  type WASocket,
  type ConnectionState,
  type WAMessage,
  type AnyMessageContent,
  proto,
  generateWAMessageFromContent,
  normalizeMessageContent,
} from "@whiskeysockets/baileys";
import { isJidGroup } from "@whiskeysockets/baileys";
import * as QRCode from "qrcode";
import NodeCache from "node-cache";
import pino from "pino";
import { randomUUID } from "node:crypto";
import path from "node:path";
import fs from "node:fs";

const logger = pino({ level: process.env.LOG_LEVEL || "info" });

// Browser-fingerprint fallback chain. `browser[1]` maps to the DeviceProps
// `platformType` in the WhatsApp device-registration handshake. WhatsApp
// periodically rejects a given platformType server-side (e.g. the 2026-06-29
// change that started rejecting DESKTOP with mass 428-before-QR, Baileys
// #2671). We start with the currently-accepted fingerprint and, if a fresh
// connect keeps getting closed before any QR/connect, rotate to the next one
// on each reconnect until one is accepted. Order = most-likely-to-work first.
const BROWSER_FALLBACKS = [
  Browsers.macOS("Safari"),
  Browsers.macOS("Chrome"),
  Browsers.macOS("Desktop"),
];

export interface SessionInfo {
  sessionId: string;
  socket: WASocket;
  status: "connecting" | "qr_pending" | "connected" | "disconnected" | "logged_out";
  qrBase64?: string;
  phoneNumber?: string;
  odooBaseUrl?: string;
  callbackUrl?: string;
  webhookSecret?: string;
  sendReadReceipts: boolean;
  /** When true, mark this number online so WhatsApp streams contact presence. */
  shareOnlinePresence: boolean;
  /** Opt-in: capture contacts' Status (Stories) broadcasts into Odoo. */
  captureContactStatuses?: boolean;
  /** When true, opening a captured status in Odoo sends a WhatsApp view receipt. */
  statusMarkSeen?: boolean;
  /** Opt-in: log Statuses (Stories) the user posts from their phone into My Status. */
  syncOwnStatuses?: boolean;
  reconnectAttempts: number;
  shouldReconnect: boolean;
  /**
   * Phase D — pairing-code window. Baileys binds the issued 8-character code
   * to the socket's ephemeral keypair; if we auto-reconnect on a 408 (QR-refs
   * timeout), the new socket gets fresh keys and the user-typed code becomes
   * invalid. While this flag is true we suppress auto-reconnect and the
   * "WhatsApp logged out" auth-dir wipe so the user has a chance to type the
   * code on their phone. Cleared on successful 'open' (registered).
   */
  pairingInProgress?: boolean;
  /**
   * Browser-fingerprint fallback (2026-06-30). Index into BROWSER_FALLBACKS for
   * the fingerprint this socket was created with. If a fresh connect is closed
   * before any QR/connect (handshake rejected — e.g. WhatsApp rejecting a
   * platformType), the reconnect advances this index to try the next one. Once
   * a QR is shown or the socket opens, `browserLocked` pins the working one.
   */
  browserIndex?: number;
  browserLocked?: boolean;
}

interface InboundMessage {
  id?: string;
  from: string;
  sender_name?: string;
  timestamp: number;
  type: string;
  text?: { body: string };
  image?: { base64: string; mimetype: string; caption?: string };
  video?: { base64: string; mimetype: string; caption?: string };
  audio?: { base64: string; mimetype: string };
  document?: { base64: string; mimetype: string; filename?: string };
  location?: { latitude: number; longitude: number; name?: string; address?: string };
  reaction?: { message_id: string; emoji: string };
  context?: { message_id: string };
  chat_type: "direct" | "group";
  group_subject?: string;
  // Chat JID — for groups this is the group JID (`...@g.us`), for DMs it
  // equals the sender JID. Odoo uses this to find/create the discuss
  // channel so a group reply lands in the group thread (not in the
  // sender's personal channel).
  chat_jid?: string;
  // Sender JID for groups (the participant). For DMs equals chat_jid.
  participant?: string;
  // True when the account itself sent this message (from the phone / another
  // linked device, or the echo of a message Odoo sent). Odoo records these as
  // outbound and de-dupes its own echoes by message id. (#own-device-sync)
  from_me?: boolean;
}

const MAX_RECONNECT_ATTEMPTS = 5;
const RECONNECT_BASE_DELAY_MS = 2000;
const CREDS_FILENAME = "creds.json";
const CREDS_BACKUP_FILENAME = "creds.backup.json";

const credsSaveQueues = new Map<string, Promise<void>>();

function resolveCredsPath(authDir: string): string {
  return path.join(authDir, CREDS_FILENAME);
}

function resolveCredsBackupPath(authDir: string): string {
  return path.join(authDir, CREDS_BACKUP_FILENAME);
}

function maybeRestoreCredsFromBackup(authDir: string) {
  const credsPath = resolveCredsPath(authDir);
  const backupPath = resolveCredsBackupPath(authDir);
  if (!fs.existsSync(backupPath)) {
    return;
  }
  try {
    if (!fs.existsSync(credsPath)) {
      fs.copyFileSync(backupPath, credsPath);
      return;
    }
    const raw = fs.readFileSync(credsPath, "utf8");
    JSON.parse(raw);
  } catch {
    try {
      fs.copyFileSync(backupPath, credsPath);
    } catch {}
  }
}

async function safeSaveCreds(authDir: string, saveCreds: () => Promise<void>) {
  const credsPath = resolveCredsPath(authDir);
  const backupPath = resolveCredsBackupPath(authDir);
  try {
    if (fs.existsSync(credsPath)) {
      const raw = fs.readFileSync(credsPath, "utf8");
      JSON.parse(raw);
      fs.copyFileSync(credsPath, backupPath);
    }
  } catch {}

  try {
    await saveCreds();
  } catch (err) {
    logger.warn({ err, authDir }, "Failed saving WhatsApp credentials");
  }
}

function enqueueSaveCreds(authDir: string, saveCreds: () => Promise<void>) {
  const queue = credsSaveQueues.get(authDir) ?? Promise.resolve();
  const next = queue
    .then(() => safeSaveCreds(authDir, saveCreds))
    .catch((err) => {
      logger.warn({ err, authDir }, "WhatsApp credentials save queue error");
    });
  credsSaveQueues.set(authDir, next);
}

export class SessionManager {
  private sessions = new Map<string, SessionInfo>();
  private sessionsDir: string;
  private recentMessages = new Map<string, number>();
  private groupMetaCache = new Map<string, { subject?: string; expires: number }>();
  /** Maps LID JIDs to phone number JIDs (e.g. "262280801472734@lid" -> "919876543210@s.whatsapp.net") */
  private lidToPhoneCache = new Map<string, string>();
  /**
   * Phase A1 — sent-message store keyed by `${remoteJid}|${id}`. Baileys' `getMessage`
   * callback queries this when WhatsApp asks for a message to be re-encrypted for a
   * recipient whose initial decryption failed. Without it, the message is silently
   * lost. LRU-trimmed at MESSAGE_STORE_LIMIT entries per process.
   */
  private messageStore = new Map<string, proto.IMessage>();
  /**
   * Phase A3 — per-account group metadata cache. Baileys' `cachedGroupMetadata`
   * callback consults this on every group send to avoid an N+1 of `groupMetadata`
   * RPCs. Refreshed via the `groups.update` and `group-participants.update` events.
   */
  private groupMetadataCache = new Map<string, Map<string, GroupMetadata>>();
  /**
   * Phase B3 — JIDs we've called `presenceSubscribe` for, keyed by accountId.
   * WhatsApp only pushes presence.update for subscribed contacts, so we track
   * subscriptions to avoid re-calling on every inbound. Cleared on disconnect
   * so a fresh socket starts subscribing from scratch.
   */
  private subscribedPresence = new Map<string, Set<string>>();
  /**
   * Phase A4 — persistent retry counter shared across sessions. Survives a session
   * recreate within one process so decryption-retry storms don't loop after a soft
   * reconnect.
   */
  private msgRetryCounterCache = new NodeCache();
  private readonly GROUP_META_TTL_MS = 5 * 60 * 1000;
  private readonly DEDUPE_TTL_MS = 60 * 1000;
  private readonly MESSAGE_STORE_LIMIT = 1000;
  /**
   * History import — account IDs currently opened in history-sync mode. Normal
   * connects stay `syncFullHistory:false` (no flooding); only an explicit
   * history resync flips this on for that session, and it reverts once the
   * sync completes (`isLatest`). The captured chats/contacts/messages are
   * buffered to `sessions/<id>/history.json` and drained by Odoo in pages.
   */
  private historyMode = new Set<string>();
  /** Per-account history-sync watchdogs (idle = chunks stopped, hard = absolute
   * cap). Guarantee the sync concludes — and historyMode is cleared — even if
   * WhatsApp never sends a final isLatest chunk, so a future reconnect can never
   * silently re-enable full history sync ("no flooding on normal connects"). */
  private historyTimers = new Map<string, { idle?: ReturnType<typeof setTimeout>; hard?: ReturnType<typeof setTimeout> }>();
  private readonly HISTORY_MSG_LIMIT = 5000;
  private readonly HISTORY_IDLE_MS = 15000;   // 15s with no new chunk ⇒ sync done
  private readonly HISTORY_HARD_MS = 600000;  // 10-min absolute cap (idle ends it sooner)

  constructor(sessionsDir?: string) {
    this.sessionsDir = sessionsDir || path.resolve(process.cwd(), "sessions");
    if (!fs.existsSync(this.sessionsDir)) {
      fs.mkdirSync(this.sessionsDir, { recursive: true });
    }
    // Cleanup old dedup entries periodically
    setInterval(() => this.cleanupDedup(), 30_000);
  }

  private cleanupDedup() {
    const now = Date.now();
    for (const [key, ts] of this.recentMessages) {
      if (now - ts > this.DEDUPE_TTL_MS) {
        this.recentMessages.delete(key);
      }
    }
  }

  /** Phase A1: cache a message we just sent (or received) so getMessage can retrieve
   * it later if WhatsApp asks for a retry. Insertion-order LRU. */
  private storeMessage(remoteJid: string | null | undefined, id: string | null | undefined, msg: proto.IMessage | null | undefined) {
    if (!remoteJid || !id || !msg) return;
    const key = `${remoteJid}|${id}`;
    // Re-insert to refresh LRU position.
    this.messageStore.delete(key);
    this.messageStore.set(key, msg);
    if (this.messageStore.size > this.MESSAGE_STORE_LIMIT) {
      const oldest = this.messageStore.keys().next().value as string | undefined;
      if (oldest) this.messageStore.delete(oldest);
    }
  }

  /** Phase A1: getMessage callback for makeWASocket. */
  private getStoredMessage(remoteJid: string, id: string): proto.IMessage | undefined {
    return this.messageStore.get(`${remoteJid}|${id}`);
  }

  /** Phase A3: per-account cachedGroupMetadata callback for makeWASocket. */
  private getCachedGroupMetadata(accountId: string, jid: string): GroupMetadata | undefined {
    return this.groupMetadataCache.get(accountId)?.get(jid);
  }

  /** Phase A3: populate or refresh a single group's metadata in the per-account cache. */
  private cacheGroupMetadata(accountId: string, meta: GroupMetadata) {
    let bucket = this.groupMetadataCache.get(accountId);
    if (!bucket) {
      bucket = new Map();
      this.groupMetadataCache.set(accountId, bucket);
    }
    bucket.set(meta.id, meta);
  }

  private isDuplicate(key: string): boolean {
    if (this.recentMessages.has(key)) return true;
    this.recentMessages.set(key, Date.now());
    return false;
  }

  private isCurrentSession(accountId: string, sessionId: string, sock: WASocket): boolean {
    const current = this.sessions.get(accountId);
    return Boolean(current && current.sessionId === sessionId && current.socket === sock);
  }

  /**
   * Resolve a LID JID (e.g. "262280801472734@lid") to a phone JID (e.g. "917888433643@s.whatsapp.net").
   * Uses in-memory cache first, then falls back to reading the reverse mapping file from auth state.
   */
  private async resolveLidToPhone(accountId: string, lidJid: string): Promise<string | null> {
    // Check in-memory cache first
    const cached = this.lidToPhoneCache.get(lidJid);
    if (cached) return cached;

    // Extract the LID number from "262280801472734@lid". Multi-device LIDs
    // arrive as "<lid>:<deviceId>@lid" (e.g. "262280801472734:47@lid"); the
    // reverse-mapping file is keyed on the bare LID without the device
    // suffix, so strip it before the lookup.
    let lidNumber = lidJid.split("@")[0];
    if (!lidNumber) return null;
    if (lidNumber.includes(":")) {
      lidNumber = lidNumber.split(":")[0];
    }

    // Read the reverse mapping file from auth state
    const reverseFile = path.join(this.sessionsDir, accountId, `lid-mapping-${lidNumber}_reverse.json`);
    try {
      if (fs.existsSync(reverseFile)) {
        const raw = fs.readFileSync(reverseFile, "utf8");
        const phoneNumber = JSON.parse(raw);
        if (phoneNumber && typeof phoneNumber === "string") {
          const phoneJid = `${phoneNumber}@s.whatsapp.net`;
          this.lidToPhoneCache.set(lidJid, phoneJid);
          logger.info({ lid: lidJid, phone: phoneJid }, "Resolved LID to phone from auth state");
          return phoneJid;
        }
      }
    } catch (err) {
      logger.debug({ err, lidJid }, "Failed to read LID reverse mapping file");
    }

    return null;
  }

  // ── History import: capture-at-connect buffer ────────────────────────
  private historyPath(accountId: string): string {
    return path.join(this.sessionsDir, accountId, "history.json");
  }

  private loadHistory(accountId: string): {
    contacts: any[]; chats: any[]; messages: any[];
    progress: number; isLatest: boolean; finished: boolean; truncated: boolean;
  } {
    try {
      const h = JSON.parse(fs.readFileSync(this.historyPath(accountId), "utf8"));
      return {
        contacts: Array.isArray(h.contacts) ? h.contacts : [],
        chats: Array.isArray(h.chats) ? h.chats : [],
        messages: Array.isArray(h.messages) ? h.messages : [],
        progress: typeof h.progress === "number" ? h.progress : 0,
        isLatest: Boolean(h.isLatest),
        finished: Boolean(h.finished),
        truncated: Boolean(h.truncated),
      };
    } catch {
      return { contacts: [], chats: [], messages: [], progress: 0, isLatest: false, finished: false, truncated: false };
    }
  }

  private saveHistory(accountId: string, buf: any) {
    try {
      const dir = path.join(this.sessionsDir, accountId);
      if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(this.historyPath(accountId), JSON.stringify(buf));
    } catch (err) {
      logger.warn({ err, accountId }, "Failed to persist WhatsApp history buffer");
    }
  }

  private resetHistory(accountId: string) {
    this.saveHistory(accountId, {
      contacts: [], chats: [], messages: [], progress: 0,
      isLatest: false, finished: false, truncated: false,
    });
  }

  private clearHistoryTimers(accountId: string) {
    const t = this.historyTimers.get(accountId);
    if (t) {
      if (t.idle) clearTimeout(t.idle);
      if (t.hard) clearTimeout(t.hard);
      this.historyTimers.delete(accountId);
    }
  }

  /** Conclude the history sync: leave history mode (so future reconnects don't
   * re-flood), cancel watchdogs, and persist `finished` so the importer's
   * `done` resolves even when WhatsApp never sent a final isLatest chunk. */
  private markHistoryFinished(accountId: string, truncated = false) {
    this.clearHistoryTimers(accountId);
    this.historyMode.delete(accountId);
    const buf = this.loadHistory(accountId);
    if (!buf.finished || (truncated && !buf.truncated)) {
      buf.finished = true;
      if (truncated) buf.truncated = true;
      this.saveHistory(accountId, buf);
      if (truncated) {
        logger.warn({ accountId, messages: buf.messages.length },
          "WhatsApp history sync hit the cap/timeout — imported recent history only");
      }
    }
  }

  /** (Re)arm the idle watchdog — fires when chunks stop arriving. Preserves the
   * hard-cap timer. */
  private armHistoryIdle(accountId: string) {
    const t = this.historyTimers.get(accountId) || {};
    if (t.idle) clearTimeout(t.idle);
    t.idle = setTimeout(() => this.markHistoryFinished(accountId), this.HISTORY_IDLE_MS);
    this.historyTimers.set(accountId, t);
  }

  /** Append one messaging-history.set chunk into the per-session buffer. */
  private captureHistory(accountId: string, h: any) {
    // Capture ONLY while a resync is actively in progress. historyMode is set
    // by resyncHistory and cleared the moment the sync concludes (finished /
    // idle / cap) OR the session is logged out / replaced — so a late chunk on
    // a torn-down socket can never append, re-persist, or resurrect a wiped
    // session dir. (The isCurrentSession guard on the listener doesn't cover
    // logout, which leaves the same socket in place.)
    if (!this.historyMode.has(accountId)) return;
    const buf = this.loadHistory(accountId);
    if (buf.finished) return; // belt-and-suspenders: ignore any late chunk
    if (Array.isArray(h?.contacts)) buf.contacts.push(...h.contacts);
    if (Array.isArray(h?.chats)) buf.chats.push(...h.chats);
    if (Array.isArray(h?.messages) && buf.messages.length < this.HISTORY_MSG_LIMIT) {
      buf.messages.push(...h.messages.slice(0, this.HISTORY_MSG_LIMIT - buf.messages.length));
    }
    if (typeof h?.progress === "number") buf.progress = h.progress;
    if (h?.isLatest) buf.isLatest = true;
    const capped = buf.messages.length >= this.HISTORY_MSG_LIMIT;
    this.saveHistory(accountId, buf);
    // Conclude on the final chunk or when we've captured enough; otherwise keep
    // extending the idle watchdog so a stream that ENDS WITHOUT isLatest still
    // terminates (the hard-cap timer set in resyncHistory is the backstop).
    if (h?.isLatest) {
      this.markHistoryFinished(accountId);          // natural, complete finish
    } else if (capped) {
      this.markHistoryFinished(accountId, true);    // hit HISTORY_MSG_LIMIT — truncated
    } else if (this.historyMode.has(accountId)) {
      this.armHistoryIdle(accountId);
    }
  }

  getHistoryStatus(accountId: string) {
    const buf = this.loadHistory(accountId);
    const session = this.sessions.get(accountId);
    const total = buf.contacts.length + buf.chats.length + buf.messages.length;
    return {
      available: total > 0 || buf.finished,
      syncComplete: buf.finished,
      truncated: buf.truncated,
      syncing: this.historyMode.has(accountId) && !buf.finished,
      connected: session?.status === "connected",
      progress: buf.finished ? 100 : buf.progress,
      counts: { contacts: buf.contacts.length, chats: buf.chats.length, messages: buf.messages.length },
    };
  }

  async readHistoryPage(accountId: string, cursor: number, limit: number) {
    const buf = this.loadHistory(accountId);
    const start = Math.max(0, cursor | 0);
    const lim = Math.min(Math.max(1, limit | 0), 500);
    const slice = buf.messages.slice(start, start + lim);
    const nextCursor = start + slice.length;
    const onFirst = start === 0;

    // Resolve @lid identifiers to phone JIDs so imported conversations match
    // the LIVE ones. WhatsApp delivers history keyed by privacy LIDs, but live
    // inbound resolves the SAME contact to a phone number — without this the
    // same person lands in two separate Odoo channels (history under the LID,
    // live messages under the phone). Best-effort: an unmapped LID is left
    // as-is so the conversation still imports (just anonymously).
    const cache = new Map<string, string>();
    const resolveJid = async (jid: string): Promise<string> => {
      if (!jid || !jid.endsWith("@lid")) return jid;
      if (cache.has(jid)) return cache.get(jid)!;
      const phone = (await this.resolveLidToPhone(accountId, jid)) || jid;
      cache.set(jid, phone);
      return phone;
    };
    const mapId = async (o: any) => {
      const cur = o?.id || o?.jid || "";
      const next = await resolveJid(cur);
      return next && next !== cur ? { ...o, id: next } : o;
    };
    const contacts = onFirst ? await Promise.all((buf.contacts || []).map(mapId)) : [];
    const chats = onFirst ? await Promise.all((buf.chats || []).map(mapId)) : [];
    const messages = await Promise.all(slice.map(async (m: any) => {
      const rj = (m?.key || {}).remoteJid || "";
      const next = await resolveJid(rj);
      return next && next !== rj ? { ...m, key: { ...m.key, remoteJid: next } } : m;
    }));

    // contacts + chats are small; deliver them all on the first page only.
    return {
      contacts,
      chats,
      messages,
      nextCursor,
      // `finished` (not just isLatest) so a stream that ended without a final
      // chunk still lets the importer complete instead of looping forever.
      done: buf.finished && nextCursor >= buf.messages.length,
    };
  }

  /** Reconnect this session with history streaming on (no QR re-scan — saved
   * creds reconnect the socket). Resets the buffer and arms watchdogs so the
   * sync ALWAYS terminates even if WhatsApp never sends a final chunk. */
  /** Arm history capture for the NEXT pairing of this account WITHOUT re-linking.
   * Call this BEFORE createSession (Connect) so the fresh pairing streams the
   * bulk history (createSession reads syncFullHistory from historyMode). This is
   * the single-scan path for a not-yet-connected account; an already-connected
   * account must re-link instead (resyncHistory). The hard-cap watchdog is armed
   * on 'connected', so a delay between arming and scanning is fine. */
  armHistory(accountId: string): { armed: boolean } {
    this.resetHistory(accountId);
    this.clearHistoryTimers(accountId);
    this.historyMode.add(accountId);
    return { armed: true };
  }

  async resyncHistory(accountId: string): Promise<{ started: boolean; status?: string; qr_base64?: string; error?: string }> {
    if (this.historyMode.has(accountId)) {
      return { started: false, error: "A history sync is already in progress for this account." };
    }
    const authDir = path.join(this.sessionsDir, accountId);
    const configPath = path.join(authDir, "config.json");
    if (!fs.existsSync(configPath)) {
      return { started: false, error: "No saved session — connect this WhatsApp account first." };
    }
    let cfg: any = {};
    try { cfg = JSON.parse(fs.readFileSync(configPath, "utf8")); } catch {}
    this.resetHistory(accountId);
    // WhatsApp only streams the bulk message history at the INITIAL device-link,
    // never on a reconnect of an already-linked device. So to import history we
    // fully RE-LINK: drop the current socket, wipe the saved credentials, and
    // start a FRESH pairing with syncFullHistory on (set via historyMode below).
    // The user re-scans the QR and the history streams in at that pairing.
    // Use socket.end() (NOT logout()) so the loggedOut close-handler doesn't fire
    // and clear historyMode / wipe state out from under the new session.
    const existing = this.sessions.get(accountId);
    if (existing) {
      existing.shouldReconnect = false;
      try { existing.socket.end(undefined); } catch {}
      this.sessions.delete(accountId);
      await new Promise<void>((r) => setTimeout(r, 500));
    }
    // Invalidate the saved credentials (incl. creds.backup.json, else
    // createSession's maybeRestoreCredsFromBackup would silently restore the old
    // login and reconnect with no fresh history). Keep config.json — createSession
    // rewrites it anyway from the cfg we just read.
    try {
      for (const f of fs.readdirSync(authDir)) {
        if (f !== "config.json") fs.rmSync(path.join(authDir, f), { recursive: true, force: true });
      }
    } catch {}
    this.clearHistoryTimers(accountId);
    this.historyMode.add(accountId);
    // The hard-cap watchdog is armed on 'connected' (in connection.update), NOT
    // here — the user may take a while to scan the new QR, and arming it now
    // could conclude an empty buffer before the history even starts streaming.
    const result = await this.createSession(
      accountId,
      cfg.odoo_base_url || "",
      cfg.webhook_secret || "",
      cfg.callback_url || undefined,
      cfg.send_read_receipts !== false,
      Boolean(cfg.share_online_presence),
      0,
      Boolean(cfg.capture_contact_statuses),
      Boolean(cfg.status_mark_seen),
      Boolean(cfg.sync_own_statuses)
    );
    return { started: true, status: result.status, qr_base64: result.qr_base64 };
  }

  async createSession(
    accountId: string,
    odooBaseUrl: string,
    webhookSecret: string,
    callbackUrl?: string,
    sendReadReceipts: boolean = true,
    shareOnlinePresence: boolean = false,
    browserIndex: number = 0,
    captureContactStatuses: boolean = false,
    statusMarkSeen: boolean = false,
    syncOwnStatuses: boolean = false,
  ): Promise<{ status: string; qr_base64?: string }> {
    // Close existing session if any
    const existing = this.sessions.get(accountId);
    if (existing?.status === "connected") {
      // Update mutable config in-place so a re-create that's really a no-op
      // still picks up the latest send_read_receipts / online-presence toggle.
      existing.sendReadReceipts = sendReadReceipts;
      existing.captureContactStatuses = captureContactStatuses;
      existing.statusMarkSeen = statusMarkSeen;
      existing.syncOwnStatuses = syncOwnStatuses;
      if (existing.shareOnlinePresence !== shareOnlinePresence) {
        existing.shareOnlinePresence = shareOnlinePresence;
        try {
          await existing.socket.sendPresenceUpdate(
            shareOnlinePresence ? "available" : "unavailable");
        } catch (err) {
          logger.error({ err, accountId }, "Failed to apply online-presence on re-create");
        }
      }
      return { status: "connected" };
    }
    if (existing) {
      existing.shouldReconnect = false;
      try { existing.socket.end(undefined); } catch {}
      // Brief pause to let old socket fully close before creating new one
      await new Promise<void>((r) => setTimeout(r, 500));
    }

    const authDir = path.join(this.sessionsDir, accountId);
    if (!fs.existsSync(authDir)) {
      fs.mkdirSync(authDir, { recursive: true });
    }

    // Persist Odoo config for auto-restore on sidecar restart
    const configPath = path.join(authDir, "config.json");
    fs.writeFileSync(
      configPath,
      JSON.stringify(
        {
          odoo_base_url: odooBaseUrl,
          callback_url: callbackUrl || null,
          webhook_secret: webhookSecret,
          send_read_receipts: sendReadReceipts,
          share_online_presence: shareOnlinePresence,
          capture_contact_statuses: captureContactStatuses,
          status_mark_seen: statusMarkSeen,
          sync_own_statuses: syncOwnStatuses,
        },
        null,
        2
      )
    );

    maybeRestoreCredsFromBackup(authDir);

    const baileysLogger = pino({ level: "warn" }) as any;
    const { state, saveCreds } = await useMultiFileAuthState(authDir);
    // Prefer WA Web's live `sw.js` revision (most current, matches what real
    // browsers send). Fall back to the Baileys-maintained Defaults if WA Web
    // is unreachable so a transient network blip can't block reconnect.
    let version: [number, number, number];
    try {
      const live = await fetchLatestWaWebVersion();
      version = live.version;
      if (live.isLatest) {
        logger.info({ accountId, version }, "pinned to live WA Web client revision");
      } else {
        logger.warn({ accountId, version, error: (live as any).error }, "WA Web version probe failed, using Baileys default");
      }
    } catch (err) {
      const fallback = await fetchLatestBaileysVersion();
      version = fallback.version;
      logger.warn({ accountId, err, version }, "fetchLatestWaWebVersion threw, using fetchLatestBaileysVersion");
    }

    const sock = makeWASocket({
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, baileysLogger),
      },
      version,
      logger: baileysLogger,
      // Phase E1 — upstream-recommended fingerprint reduces bot-flag risk.
      // 2026-06-30: WhatsApp pushed a server-side change that rejects the
      // device-registration handshake for some platformTypes (mass "428
      // Connection Terminated before QR" across all Baileys 7.x, issue #2671).
      // We rotate through BROWSER_FALLBACKS (Safari -> Chrome -> Desktop) on
      // repeated pre-QR closes; the (re)connect caller picks the index.
      browser: BROWSER_FALLBACKS[browserIndex % BROWSER_FALLBACKS.length],
      // Default OFF (no flooding). An explicit history resync (resyncHistory)
      // flips this on for the session, then reverts once the sync completes.
      syncFullHistory: this.historyMode.has(accountId),
      markOnlineOnConnect: false,
      retryRequestDelayMs: 250,
      // Phase E2 — generate rich URL previews for outbound messages.
      generateHighQualityLinkPreview: true,
      // Phase A2 — drop status-broadcast spam at the source so a fresh QR scan
      // doesn't flood Odoo with stranger updates. `syncFullHistory: false` above
      // already keeps the history pull short; this filter is the safety net.
      // Drop broadcast JIDs (status + broadcast lists) at the source — EXCEPT
      // status@broadcast when this account opted in to capturing contacts'
      // Stories. Uses the create-time flag; toggling capture on takes effect on
      // the next (re)connect. (#capture-statuses)
      shouldIgnoreJid: (jid: string) =>
        isJidBroadcast(jid) &&
        !((captureContactStatuses || syncOwnStatuses) && jid === "status@broadcast"),
      // Phase A4 — share retry counters across session recreates so storms don't loop.
      msgRetryCounterCache: this.msgRetryCounterCache,
      // Phase A1 — return the original message body so WhatsApp can re-encrypt
      // and retry delivery if the recipient's first decryption attempt failed.
      getMessage: async (key) => {
        if (key.remoteJid && key.id) {
          return this.getStoredMessage(key.remoteJid, key.id);
        }
        return undefined;
      },
      // Phase A3 — pre-cached group metadata avoids one RPC per group send.
      cachedGroupMetadata: async (jid: string) => {
        return this.getCachedGroupMetadata(accountId, jid);
      },
    });

    const sessionInfo: SessionInfo = {
      sessionId: randomUUID(),
      socket: sock,
      status: "connecting",
      odooBaseUrl,
      callbackUrl,
      webhookSecret,
      sendReadReceipts,
      shareOnlinePresence,
      captureContactStatuses,
      statusMarkSeen,
      syncOwnStatuses,
      reconnectAttempts: 0,
      shouldReconnect: true,
      browserIndex,
      browserLocked: false,
    };
    this.sessions.set(accountId, sessionInfo);
    logger.info(
      { accountId, browserIndex, browser: BROWSER_FALLBACKS[browserIndex % BROWSER_FALLBACKS.length][1] },
      "Using browser fingerprint",
    );
    void this.forwardConnectionState(accountId, "connecting");

    // QR code and connection handling
    sock.ev.on("connection.update", async (update: Partial<ConnectionState>) => {
      if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) {
        return;
      }
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // A QR means WhatsApp accepted the handshake with this fingerprint —
        // lock it so reconnects don't keep rotating.
        sessionInfo.browserLocked = true;
        try {
          sessionInfo.qrBase64 = await QRCode.toDataURL(qr);
          sessionInfo.status = "qr_pending";
          void this.forwardConnectionState(accountId, "qr_pending", { qr_base64: sessionInfo.qrBase64 || null });
        } catch (err) {
          logger.error({ err }, "Failed to generate QR code");
        }
      }

      if (connection === "open") {
        if (!sessionInfo.shouldReconnect && !sessionInfo.pairingInProgress) {
          return;
        }
        sessionInfo.status = "connected";
        sessionInfo.browserLocked = true;
        sessionInfo.qrBase64 = undefined;
        sessionInfo.reconnectAttempts = 0;
        sessionInfo.phoneNumber = sock.user?.id?.split(":")[0] || undefined;
        // Phase D — pairing succeeded. Clear the flag and restore normal
        // reconnect behaviour for the regular connected lifecycle.
        if (sessionInfo.pairingInProgress) {
          logger.info({ accountId, phone: sessionInfo.phoneNumber }, "Pairing-code login succeeded");
          sessionInfo.pairingInProgress = false;
          sessionInfo.shouldReconnect = true;
        }
        logger.info({ accountId, phone: sessionInfo.phoneNumber }, "WhatsApp connected");
        void this.forwardConnectionState(accountId, "connected", { qr_base64: null, phone_number: sessionInfo.phoneNumber || null });
        // History (re-)link: now that we're connected (the user may have taken a
        // while to scan the new QR), start the hard-cap backstop so the sync
        // always concludes even if the stream never ends. captureHistory arms the
        // shorter idle watchdog once the first chunk arrives.
        if (this.historyMode.has(accountId)) {
          const histBuf = this.loadHistory(accountId);
          if (!histBuf.finished) {
            const timers = this.historyTimers.get(accountId) || {};
            if (!timers.hard) {
              timers.hard = setTimeout(
                () => this.markHistoryFinished(accountId, true), this.HISTORY_HARD_MS);
              this.historyTimers.set(accountId, timers);
            }
          }
        }
        // Opt-in: mark this number online so WhatsApp streams contacts' presence
        // back (default markOnlineOnConnect=false keeps us invisible otherwise).
        if (sessionInfo.shareOnlinePresence) {
          try {
            await sock.sendPresenceUpdate("available");
          } catch (err) {
            logger.error({ err, accountId }, "Failed to mark online on connect");
          }
        }
      }

      if (connection === "close") {
        // Presence subscriptions don't survive a socket close — clear so the
        // next socket re-subscribes when traffic resumes.
        this.subscribedPresence.delete(accountId);
        const statusCode = (lastDisconnect?.error as any)?.output?.statusCode;
        const errorMessage = (lastDisconnect?.error as any)?.message || "unknown";
        logger.info({ accountId, statusCode, errorMessage, pairing: !!sessionInfo.pairingInProgress }, "Connection closed");
        // Phase D — when a pairing-code attempt is in flight, the 408
        // QR-refs-ended close is normal: the user just hasn't typed the
        // code yet. Don't auto-reconnect (would invalidate the issued
        // code) and don't wipe the auth dir (no creds to lose anyway).
        if (sessionInfo.pairingInProgress) {
          sessionInfo.shouldReconnect = false;
          sessionInfo.status = "disconnected";
          logger.info({ accountId, statusCode }, "Pairing window closed without registration; not auto-reconnecting");
          void this.forwardConnectionState(accountId, "disconnected", { last_error: "pairing_window_closed" });
          return;
        }
        if (statusCode === DisconnectReason.loggedOut) {
          sessionInfo.shouldReconnect = false;
          sessionInfo.status = "logged_out";
          // Abort any in-flight history sync: clear the flag (so a later QR
          // re-scan does NOT silently re-enable full-history streaming) and
          // cancel the watchdogs (so an orphan timer can't resurrect the auth
          // dir we wipe just below).
          this.clearHistoryTimers(accountId);
          this.historyMode.delete(accountId);
          logger.info({ accountId }, "WhatsApp logged out");
          void this.forwardConnectionState(accountId, "logged_out", { qr_base64: null, last_error: errorMessage });
          // Clean auth state
          try {
            fs.rmSync(authDir, { recursive: true, force: true });
          } catch {}
        } else if (statusCode === 440 /* conflict:replaced */) {
          // Another session took over — do NOT reconnect
          sessionInfo.shouldReconnect = false;
          sessionInfo.status = "disconnected";
          this.clearHistoryTimers(accountId);
          this.historyMode.delete(accountId);
          logger.warn({ accountId, statusCode }, "Session replaced by another client, not reconnecting");
          void this.forwardConnectionState(accountId, "disconnected", { last_error: `replaced (${statusCode})` });
        } else if (
          statusCode === DisconnectReason.timedOut &&
          sessionInfo.browserLocked &&
          !sock.authState?.creds?.registered
        ) {
          // A QR was shown for a brand-new (never-linked) account but nobody
          // scanned it, so WhatsApp ended the QR refs (statusCode 408). This is
          // NOT a dropped connection — auto-reconnecting here just burns the
          // whole reconnect budget re-requesting fresh QRs (~6 cycles / ~18 min)
          // and pointlessly re-hits WhatsApp's device-registration endpoint,
          // which raises bot-flag risk on a number that isn't even linked yet.
          // Stop after this single QR cycle and wait for the user to click
          // Connect again. The two paths that SHOULD retry stay untouched:
          //  - 428 pre-QR handshake reject keeps browserLocked=false -> still
          //    rotates browser fingerprints and reconnects;
          //  - a real network drop on a linked account has creds.registered=true
          //    -> still reconnects.
          sessionInfo.shouldReconnect = false;
          sessionInfo.status = "disconnected";
          logger.info({ accountId }, "QR expired without scan; not auto-reconnecting (awaiting manual Connect)");
          void this.forwardConnectionState(accountId, "disconnected", { qr_base64: null, last_error: "qr_expired" });
        } else {
          // restartRequired (515) fires right after a successful QR scan / pair:
          // Baileys wants a socket restart with the freshly-saved creds. This is
          // NOT a disconnect — reporting 'disconnected' collapses the QR view and
          // stops the client polling, so when the reconnect opens as 'connected'
          // nothing is watching and the user has to click Connect a second time.
          // Report a transient 'connecting' so the view stays mounted + polling
          // and flips straight to Connected on reconnect. (#connect_flow_515)
          const isRestart = statusCode === DisconnectReason.restartRequired;
          sessionInfo.status = isRestart ? "connecting" : "disconnected";
          void this.forwardConnectionState(
            accountId,
            isRestart ? "connecting" : "disconnected",
            isRestart ? {} : { last_error: `${errorMessage} (${statusCode || "n/a"})` },
          );
          // Auto-reconnect with exponential backoff
          // Preserve attempt counter: store it before createSession resets it
          const currentAttempt = sessionInfo.reconnectAttempts;
          if (sessionInfo.shouldReconnect && currentAttempt < MAX_RECONNECT_ATTEMPTS) {
            const delay = RECONNECT_BASE_DELAY_MS * Math.pow(2, currentAttempt);
            const nextAttempt = currentAttempt + 1;
            // Browser-fingerprint fallback: if this socket closed before it ever
            // showed a QR or connected (handshake rejected — e.g. WhatsApp
            // rejecting a platformType), advance to the next fingerprint. Once a
            // QR/connect set browserLocked, keep the working one.
            const curBrowserIndex = sessionInfo.browserIndex ?? 0;
            const nextBrowserIndex = sessionInfo.browserLocked ? curBrowserIndex : curBrowserIndex + 1;
            if (!sessionInfo.browserLocked) {
              logger.info(
                {
                  accountId,
                  from: BROWSER_FALLBACKS[curBrowserIndex % BROWSER_FALLBACKS.length][1],
                  to: BROWSER_FALLBACKS[nextBrowserIndex % BROWSER_FALLBACKS.length][1],
                },
                "Pre-QR close — rotating browser fingerprint",
              );
            }
            logger.info({ accountId, attempt: nextAttempt, delay }, "Reconnecting...");
            setTimeout(async () => {
              if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock) || !sessionInfo.shouldReconnect) {
                return;
              }
              try {
                await this.createSession(
                  accountId, odooBaseUrl, webhookSecret,
                  sessionInfo.callbackUrl, sessionInfo.sendReadReceipts,
                  sessionInfo.shareOnlinePresence, nextBrowserIndex,
                  sessionInfo.captureContactStatuses, sessionInfo.statusMarkSeen,
                  sessionInfo.syncOwnStatuses,
                );
                // Carry forward reconnect counter so it doesn't reset to 0
                const newSession = this.sessions.get(accountId);
                if (newSession && newSession.status !== "connected") {
                  newSession.reconnectAttempts = nextAttempt;
                }
              } catch (err) {
                logger.error({ err, accountId }, "Reconnect failed");
              }
            }, delay);
          } else {
            logger.error({ accountId, attempts: currentAttempt }, "Max reconnect attempts reached");
            // Retries exhausted — settle on a terminal 'disconnected' so a
            // transient 'connecting' (e.g. a restartRequired that then failed to
            // reconnect) can't leave the UI spinning forever. (#connect_flow_515)
            sessionInfo.status = "disconnected";
            void this.forwardConnectionState(accountId, "disconnected", { last_error: "max_reconnect_attempts" });
          }
        }
      }
    });

    // Save credentials on update
    sock.ev.on("creds.update", () => enqueueSaveCreds(authDir, saveCreds));

    // History import — buffer the chats/contacts/messages WhatsApp streams
    // after a history-enabled (re)connect. No-op for normal connects since
    // syncFullHistory is off and WhatsApp sends little/nothing.
    sock.ev.on("messaging-history.set", (item) => {
      // Only buffer chunks from the CURRENT socket — a late chunk from a prior
      // socket (e.g. during a resync teardown) must not pollute or prematurely
      // finish the new sync's buffer. Matches the other event handlers.
      if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) return;
      try {
        this.captureHistory(accountId, item);
      } catch (err) {
        logger.warn({ err, accountId }, "Failed to capture WhatsApp history chunk");
      }
    });

    // Handle incoming messages
    sock.ev.on("messages.upsert", async (upsert) => {
      logger.info({ accountId, type: upsert.type, count: upsert.messages?.length }, "messages.upsert event received");
      if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) {
        logger.warn({ accountId }, "Ignoring messages.upsert — stale session");
        return;
      }
      // Process both "notify" (v6 style) and "append" (v7 style) messages
      // For "append", only process recent messages to avoid history flood
      const isAppend = upsert.type === "append";
      const isNotify = upsert.type === "notify";
      if (!isNotify && !isAppend) return;

      for (const msg of upsert.messages) {
        // For "append" type, skip old messages (only process within last 60s)
        if (isAppend) {
          const msgTs = Number(msg.messageTimestamp) || 0;
          const nowSec = Math.floor(Date.now() / 1000);
          if (nowSec - msgTs > 60) {
            logger.debug({ accountId, id: msg.key?.id, age: nowSec - msgTs }, "Skipping old append message");
            continue;
          }
        }
        logger.info({ accountId, from: msg.key?.remoteJid, id: msg.key?.id, fromMe: msg.key?.fromMe, type: upsert.type }, "Processing inbound message");
        // Phase A1 — keep the message body around in case Baileys' getMessage
        // is asked for it during a retry-decrypt cycle. Cheap to do here; the
        // store is bounded.
        if (msg.message) {
          this.storeMessage(msg.key?.remoteJid, msg.key?.id, msg.message);
        }
        await this.handleInboundMessage(accountId, msg, sock);
      }
    });

    // Forward delivery/read receipts to Odoo
    sock.ev.on("messages.update", async (updates) => {
      if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) {
        return;
      }
      const statuses: { id: string; status: string }[] = [];
      for (const u of updates) {
        const msgId = u.key?.id;
        const status = (u.update as any)?.status;
        if (!msgId || status === undefined || status === null) continue;
        // Baileys MessageStatus: 0=ERROR, 1=PENDING, 2=SERVER_ACK,
        // 3=DELIVERY_ACK, 4=READ, 5=PLAYED. Only forward states the
        // Odoo controller knows how to translate.
        let mapped: string | undefined;
        switch (Number(status)) {
          case 0: mapped = "failed"; break;
          case 2: mapped = "sent"; break;
          case 3: mapped = "delivered"; break;
          case 4: mapped = "read"; break;
          case 5: mapped = "read"; break;
          default: continue;
        }
        statuses.push({ id: msgId, status: mapped });
      }
      if (statuses.length === 0) return;
      try {
        const session = this.sessions.get(accountId);
        if (!session?.odooBaseUrl || !session?.webhookSecret) return;
        const incomingUrl = session.callbackUrl
          || `${session.odooBaseUrl}/open_whatsapp_connector/webhook/incoming`;
        const statusUrl = incomingUrl.replace("/webhook/incoming", "/webhook/status");
        const dbName = new URL(statusUrl).searchParams.get("db");
        const headers: Record<string, string> = { "Content-Type": "application/json" };
        if (dbName) headers["X-Odoo-Database"] = dbName;
        const payload = {
          jsonrpc: "2.0",
          params: {
            account_id: accountId,
            secret: session.webhookSecret,
            statuses,
          },
        };
        logger.info({ accountId, count: statuses.length }, "Forwarding status updates to Odoo");
        const response = await fetch(statusUrl, {
          method: "POST",
          headers,
          body: JSON.stringify(payload),
        });
        if (!response.ok) {
          const body = await response.text();
          logger.error({ status: response.status, accountId, body: body.substring(0, 500) }, "Failed to forward status updates");
        }
      } catch (err) {
        logger.error({ err, accountId }, "Failed to forward status updates");
      }
    });

    // Track LID→phone mappings from contacts updates
    sock.ev.on("contacts.update", (updates) => {
      for (const contact of updates) {
        if (contact.id?.endsWith("@lid") && (contact as any).phoneNumber) {
          const phoneJid = (contact as any).phoneNumber;
          this.lidToPhoneCache.set(contact.id, phoneJid);
          logger.debug({ lid: contact.id, phone: phoneJid }, "LID→phone mapping cached");
        }
      }
    });

    // Also track from contacts.upsert if available
    sock.ev.on("contacts.upsert" as any, (contacts: any[]) => {
      for (const contact of contacts) {
        if (contact.id?.endsWith("@lid") && contact.phoneNumber) {
          this.lidToPhoneCache.set(contact.id, contact.phoneNumber);
        }
      }
    });

    // Phase 21: incoming voice/video call events. Baileys exposes a `call`
    // event with status transitions (offer→ringing→timeout/reject/accept).
    // We can't carry audio, but we forward each terminal status to Odoo so
    // the user has a full audit log.
    sock.ev.on("call" as any, async (calls: any[]) => {
      if (!Array.isArray(calls) || calls.length === 0) return;
      for (const call of calls) {
        try {
          await this.forwardCallEvent(accountId, call);
        } catch (err) {
          logger.error({ err, accountId, call }, "Failed to forward call event");
        }
      }
    });

    // Phase B1: group metadata changes — keep our cache fresh AND forward to Odoo.
    sock.ev.on("groups.upsert" as any, async (metas: GroupMetadata[]) => {
      if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) return;
      for (const meta of metas || []) {
        this.cacheGroupMetadata(accountId, meta);
        try {
          await this.forwardGroupMetaEvent(accountId, "upsert", meta);
        } catch (err) {
          logger.error({ err, accountId, jid: meta?.id }, "Failed to forward groups.upsert");
        }
      }
    });
    sock.ev.on("groups.update" as any, async (updates: Partial<GroupMetadata>[]) => {
      if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) return;
      for (const u of updates || []) {
        if (!u?.id) continue;
        const bucket = this.groupMetadataCache.get(accountId);
        const existing = bucket?.get(u.id);
        if (existing) {
          this.cacheGroupMetadata(accountId, { ...existing, ...u } as GroupMetadata);
        }
        try {
          await this.forwardGroupMetaEvent(accountId, "update", u as GroupMetadata);
        } catch (err) {
          logger.error({ err, accountId, jid: u?.id }, "Failed to forward groups.update");
        }
      }
    });

    // Phase B2: group-participants change — refresh our cache AND log to Odoo.
    sock.ev.on(
      "group-participants.update" as any,
      async (event: { id: string; participants: string[]; action: string; author?: string }) => {
        if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) return;
        // Invalidate the cached entry; next group-send will refetch via groupMetadata().
        this.groupMetadataCache.get(accountId)?.delete(event.id);
        try {
          await this.forwardGroupParticipantsEvent(accountId, event);
        } catch (err) {
          logger.error({ err, accountId, event }, "Failed to forward group-participants.update");
        }
      },
    );

    // Phase B3: presence updates — only forward when an Odoo agent is actively
    // viewing the channel (rate-limited at the Odoo end). For now we forward
    // every event; Odoo can choose to surface it or not.
    sock.ev.on("presence.update" as any, async (event: { id: string; presences: Record<string, any> }) => {
      if (!this.isCurrentSession(accountId, sessionInfo.sessionId, sock)) return;
      try {
        await this.forwardPresenceEvent(accountId, event);
      } catch (err) {
        logger.error({ err, accountId, jid: event?.id }, "Failed to forward presence.update");
      }
    });

    // Wait briefly for QR or connection
    await new Promise<void>((resolve) => setTimeout(resolve, 2000));

    return {
      status: sessionInfo.status,
      qr_base64: sessionInfo.qrBase64,
    };
  }

  private async handleInboundMessage(accountId: string, msg: WAMessage, sock: WASocket) {
    const session = this.sessions.get(accountId);
    if (!session?.odooBaseUrl || !session?.webhookSecret) return;

    let remoteJid = msg.key?.remoteJid;
    if (!remoteJid) return;
    // Status broadcasts (contacts' Stories). Capture them into Odoo when the
    // account opted in — otherwise drop as before. Never capture our OWN status
    // echo (fromMe): we already recorded it when it was posted.
    if (remoteJid.endsWith("@status") || remoteJid.endsWith("@broadcast")) {
      if (msg.key?.fromMe) {
        // A Status the user posted from their phone / another linked device.
        // Log it into My Status when opted in (deduped in Odoo against statuses
        // posted THROUGH Odoo, which already created their own row). (#own-status)
        if (session.syncOwnStatuses) {
          this.handleOwnStatus(accountId, msg, sock).catch((err) =>
            logger.warn({ err, accountId }, "Failed to sync own status"));
        }
      } else if (session.captureContactStatuses) {
        this.handleStatusUpdate(accountId, msg, sock).catch((err) =>
          logger.warn({ err, accountId }, "Failed to capture contact status"));
      }
      return;
    }
    // Own messages (fromMe) are messages YOU sent from the phone / another
    // linked device, OR the echo of a message Odoo itself sent. Forward them
    // (tagged from_me) so phone-sent replies appear in Odoo Discuss; Odoo
    // records them as outbound and de-dupes its own echoes by message id.
    // (#own-device-sync)
    const fromMe = !!msg.key?.fromMe;

    const msgId = msg.key?.id;
    if (msgId) {
      const dedupeKey = `${accountId}:${remoteJid}:${msgId}`;
      if (this.isDuplicate(dedupeKey)) return;
    }

    const isGroup = isJidGroup(remoteJid);
    let senderJid = isGroup ? msg.key?.participant : remoteJid;

    // Resolve LID JIDs to phone numbers
    if (senderJid?.endsWith("@lid")) {
      const resolved = await this.resolveLidToPhone(accountId, senderJid);
      if (resolved) {
        senderJid = resolved;
        if (!isGroup) remoteJid = resolved;
      } else {
        logger.warn({ accountId, lid: senderJid, msgId }, "Could not resolve LID to phone number, forwarding with LID");
      }
    }

    const from = senderJid?.split("@")[0] || "";

    // Phase B3 — auto-subscribe so future presence.update events fire
    // for this chat. Cheap and idempotent.
    if (remoteJid && !remoteJid.endsWith("@status") && !remoteJid.endsWith("@broadcast")) {
      this.subscribePresence(accountId, remoteJid).catch(() => {});
    }

    // Extract message content. Unwrap Baileys content envelopes first —
    // self/device-synced, disappearing (ephemeral), view-once and
    // document-with-caption messages nest their real payload one level deep
    // (deviceSentMessage/ephemeralMessage/viewOnceMessage*/documentWithCaption).
    // Without this, conversation/extendedTextMessage/captions read undefined and
    // the message reaches Odoo body-less, surfacing as a "[text message]"
    // placeholder. normalizeMessageContent strips all those wrappers.
    const rawMessage = msg.message;
    if (!rawMessage) return;
    const message = normalizeMessageContent(rawMessage) || rawMessage;

    let inbound: InboundMessage = {
      id: msgId || undefined,
      from,
      sender_name: msg.pushName || undefined,
      timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
      type: "text",
      chat_type: isGroup ? "group" : "direct",
      // For groups, route the inbound to the GROUP discuss channel
      // (chat_jid = remoteJid `@g.us`), not the sender's personal channel.
      chat_jid: remoteJid,
      participant: senderJid || undefined,
      from_me: fromMe,
    };

    // Text messages
    const textBody =
      message.conversation ||
      message.extendedTextMessage?.text ||
      message.imageMessage?.caption ||
      message.videoMessage?.caption ||
      message.documentMessage?.caption;

    if (textBody) {
      inbound.type = "text";
      inbound.text = { body: textBody };
    }

    // Image
    if (message.imageMessage) {
      inbound.type = "image";
      try {
        const buffer = await this.downloadMedia(msg, sock);
        if (buffer) {
          inbound.image = {
            base64: buffer.toString("base64"),
            mimetype: message.imageMessage.mimetype || "image/jpeg",
            caption: message.imageMessage.caption || undefined,
          };
        }
      } catch (err) {
        logger.error({ err }, "Failed to download image");
      }
    }

    // Video
    if (message.videoMessage) {
      inbound.type = "video";
      try {
        const buffer = await this.downloadMedia(msg, sock);
        if (buffer) {
          inbound.video = {
            base64: buffer.toString("base64"),
            mimetype: message.videoMessage.mimetype || "video/mp4",
            caption: message.videoMessage.caption || undefined,
          };
        }
      } catch (err) {
        logger.error({ err }, "Failed to download video");
      }
    }

    // Audio
    if (message.audioMessage) {
      inbound.type = "audio";
      try {
        const buffer = await this.downloadMedia(msg, sock);
        if (buffer) {
          inbound.audio = {
            base64: buffer.toString("base64"),
            mimetype: message.audioMessage.mimetype || "audio/ogg",
          };
        }
      } catch (err) {
        logger.error({ err }, "Failed to download audio");
      }
    }

    // Document
    if (message.documentMessage) {
      inbound.type = "document";
      try {
        const buffer = await this.downloadMedia(msg, sock);
        if (buffer) {
          inbound.document = {
            base64: buffer.toString("base64"),
            mimetype: message.documentMessage.mimetype || "application/octet-stream",
            filename: message.documentMessage.fileName || undefined,
          };
        }
      } catch (err) {
        logger.error({ err }, "Failed to download document");
      }
    }

    // Sticker
    if (message.stickerMessage) {
      inbound.type = "sticker";
      try {
        const buffer = await this.downloadMedia(msg, sock);
        if (buffer) {
          (inbound as any).sticker = {
            base64: buffer.toString("base64"),
            mimetype: message.stickerMessage.mimetype || "image/webp",
          };
        }
      } catch (err) {
        logger.error({ err }, "Failed to download sticker");
      }
    }

    // Contact card
    if (message.contactMessage) {
      inbound.type = "contacts";
      (inbound as any).contacts = [{
        displayName: message.contactMessage.displayName || "",
        vcard: message.contactMessage.vcard || "",
      }];
    }
    if (message.contactsArrayMessage) {
      inbound.type = "contacts";
      (inbound as any).contacts = (message.contactsArrayMessage.contacts || []).map((c: any) => ({
        displayName: c.displayName || "",
        vcard: c.vcard || "",
      }));
    }

    // Location
    if (message.locationMessage) {
      inbound.type = "location";
      inbound.location = {
        latitude: message.locationMessage.degreesLatitude || 0,
        longitude: message.locationMessage.degreesLongitude || 0,
        name: message.locationMessage.name || undefined,
        address: message.locationMessage.address || undefined,
      };
    }

    // Reaction
    if (message.reactionMessage) {
      inbound.type = "reaction";
      inbound.reaction = {
        message_id: message.reactionMessage.key?.id || "",
        emoji: message.reactionMessage.text || "",
      };
    }

    // Button response
    if (message.buttonsResponseMessage) {
      inbound.type = "button_response";
      inbound.text = {
        body: message.buttonsResponseMessage.selectedDisplayText || message.buttonsResponseMessage.selectedButtonId || "",
      };
      (inbound as any).button_response = {
        button_id: message.buttonsResponseMessage.selectedButtonId || "",
        button_text: message.buttonsResponseMessage.selectedDisplayText || "",
      };
    }

    // List response
    if (message.listResponseMessage) {
      inbound.type = "list_response";
      inbound.text = {
        body: message.listResponseMessage.title || message.listResponseMessage.singleSelectReply?.selectedRowId || "",
      };
      (inbound as any).list_response = {
        row_id: message.listResponseMessage.singleSelectReply?.selectedRowId || "",
        row_title: message.listResponseMessage.title || "",
        row_description: message.listResponseMessage.description || "",
      };
    }

    // Reply context
    const contextInfo =
      message.extendedTextMessage?.contextInfo ||
      message.imageMessage?.contextInfo ||
      message.videoMessage?.contextInfo;
    if (contextInfo?.stanzaId) {
      inbound.context = { message_id: contextInfo.stanzaId };
      // When a customer replies to a Status/story (or any quoted message that
      // carries a photo/video), surface the referenced media so the agent can
      // see WHAT the customer is asking about (e.g. "how much is this?"). The
      // quoted media only exists inside the WhatsApp message proto, so download
      // it best-effort here; never block the inbound message on a failure
      // (status media is ephemeral and can expire).
      const quoted: any = contextInfo.quotedMessage;
      const quotedMedia = quoted?.imageMessage || quoted?.videoMessage;
      if (quoted && quotedMedia) {
        try {
          const { downloadMediaMessage } = await import("@whiskeysockets/baileys");
          const fakeMsg: any = {
            key: {
              remoteJid: contextInfo.participant || remoteJid,
              id: contextInfo.stanzaId,
              fromMe: false,
            },
            message: quoted,
          };
          const buf = await downloadMediaMessage(fakeMsg, "buffer", {});
          if (buf && (buf as Buffer).length) {
            (inbound.context as any).quoted_media = {
              type: quoted.imageMessage ? "image" : "video",
              mimetype:
                quotedMedia.mimetype ||
                (quoted.imageMessage ? "image/jpeg" : "video/mp4"),
              base64: (buf as Buffer).toString("base64"),
              caption: quotedMedia.caption || "",
            };
          }
        } catch (e) {
          logger.warn(
            { err: String(e), accountId },
            "quoted-media (status reply) download failed",
          );
        }
      }
    }

    // Group metadata
    if (isGroup) {
      const rJid = remoteJid!;
      const cached = this.groupMetaCache.get(rJid);
      if (cached && cached.expires > Date.now()) {
        inbound.group_subject = cached.subject;
      } else {
        try {
          const meta = await sock.groupMetadata(rJid);
          const entry = { subject: meta.subject, expires: Date.now() + this.GROUP_META_TTL_MS };
          this.groupMetaCache.set(rJid, entry);
          inbound.group_subject = meta.subject;
        } catch {}
      }
    }

    // Send read receipt — gated by session config (Phase 4). Never for our
    // own (fromMe) messages — there is nothing of ours to mark read.
    if (!fromMe && session.sendReadReceipts !== false) {
      try {
        await sock.readMessages([{ remoteJid: remoteJid!, id: msgId!, participant: isGroup ? (senderJid || undefined) : undefined, fromMe: false }]);
      } catch {}
    }

    // Own-device noise filter: a fromMe sync event with no usable content
    // (e.g. a protocol/placeholder message, or a media message whose download
    // failed) would create an empty/misleading outbound bubble in Odoo. Only
    // forward own messages that carry real content. (Own reactions are synced
    // separately — excluded here so a bare reaction isn't forwarded.)
    // (#own-device-sync)
    if (fromMe) {
      const hasContent =
        inbound.text?.body ||
        inbound.image || inbound.video || inbound.audio ||
        inbound.document || (inbound as any).sticker ||
        (inbound as any).contacts || inbound.location;
      if (!hasContent) {
        logger.debug({ accountId, id: msgId, type: inbound.type }, "Skipping empty own (fromMe) message");
        return;
      }
    }

    // Forward to Odoo
    try {
      const webhookUrl = session.callbackUrl || `${session.odooBaseUrl}/open_whatsapp_connector/webhook/incoming`;
      logger.info({ accountId, webhookUrl, from, type: inbound.type, textBody: inbound.text?.body?.substring(0, 50) }, "Forwarding message to Odoo");
      const payload = {
        jsonrpc: "2.0",
        params: {
          account_id: accountId,
          secret: session.webhookSecret,
          messages: [inbound],
        },
      };
      // Extract db name from callback URL query params for X-Odoo-Database header
      const webhookUrlObj = new URL(webhookUrl);
      const dbName = webhookUrlObj.searchParams.get("db");
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (dbName) {
        headers["X-Odoo-Database"] = dbName;
      }
      const response = await fetch(webhookUrl, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });
      const responseText = await response.text();
      if (!response.ok) {
        logger.error({ status: response.status, accountId, body: responseText.substring(0, 500) }, "Failed to forward message to Odoo");
      } else {
        logger.info({ status: response.status, accountId, body: responseText.substring(0, 200) }, "Message forwarded to Odoo successfully");
      }
    } catch (err) {
      logger.error({ err, accountId }, "Failed to forward message to Odoo");
    }
  }

  // Phase 21: forward a single Baileys `call` event to Odoo's
  // /webhook/call endpoint. Idempotent on (account, call_id) at the Odoo side.
  private async forwardCallEvent(accountId: string, call: any): Promise<void> {
    const session = this.sessions.get(accountId);
    if (!session?.odooBaseUrl || !session?.webhookSecret) return;

    // Baileys 7.0.0-rc10+ exposes the caller's actual phone number on
    // the offer event via `callerPn` (sourced from the `caller_pn` XML
    // attribute). Prefer it — it's the only candidate guaranteed to be a
    // real phone, never a LID. See Baileys PR #2190.
    // Baileys 7.x sometimes returns callerPn already as a full JID
    // (`+917888433643@s.whatsapp.net`) and sometimes just digits — strip
    // both `+` and any `@...` suffix before re-attaching the canonical one,
    // otherwise we end up with `917888433643@s.whatsapp.net@s.whatsapp.net`
    // which breaks downstream routing.
    const callerPnRaw =
      typeof call.callerPn === "string" && call.callerPn.length > 0
        ? call.callerPn
        : "";
    const callerPnDigits = callerPnRaw
      .replace(/^\+/, "")
      .split("@")[0]
      .split(":")[0];
    let phoneJid = callerPnDigits ? `${callerPnDigits}@s.whatsapp.net` : "";

    // Fallback for events that don't carry callerPn (older library versions
    // or non-offer transitions): pick the first non-LID JID from the legacy
    // candidate chain. When the caller is multi-device, Baileys may set
    // `from` to a LID (`<lid>@lid`) which is an anonymized device handle.
    // The real phone JID may also live in `chatId` / `peerJid`.
    if (!phoneJid) {
      const fromCandidates: string[] = [
        call.from,
        call.chatId,
        call.peerJid,
      ].filter((j: any) => typeof j === "string" && j.length > 0);
      phoneJid =
        fromCandidates.find((j) => !j.endsWith("@lid")) ||
        fromCandidates[0] ||
        "";
    }

    // If we still ended up with a LID (every candidate was @lid and
    // callerPn was absent), try the explicit LID→phone resolver before
    // giving up. Cheap on cache hit, slightly more expensive on disk read.
    if (phoneJid && phoneJid.endsWith("@lid")) {
      const resolved = await this.resolveLidToPhone(accountId, phoneJid);
      if (resolved) {
        logger.info(
          { accountId, lid: phoneJid, resolved },
          "forwardCallEvent: resolved LID to phone JID",
        );
        phoneJid = resolved;
      } else {
        logger.warn(
          { accountId, lid: phoneJid },
          "forwardCallEvent: every candidate is @lid and no LID→phone mapping is known; storing the LID",
        );
      }
    }

    const callPayload = {
      id: call.id,
      from: phoneJid,
      from_lid: call.from && call.from.endsWith("@lid") ? call.from : null,
      chat_id: call.chatId || null,
      status: call.status,
      isVideo: !!call.isVideo,
      isGroup: !!call.isGroup,
      date: call.date,
    };

    const incomingUrl =
      session.callbackUrl ||
      `${session.odooBaseUrl}/open_whatsapp_connector/webhook/incoming`;
    const callUrl = incomingUrl.replace("/webhook/incoming", "/webhook/call");

    const payload = {
      jsonrpc: "2.0",
      params: {
        secret: session.webhookSecret,
        calls: [callPayload],
      },
    };

    const urlObj = new URL(callUrl);
    const dbName = urlObj.searchParams.get("db");
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (dbName) headers["X-Odoo-Database"] = dbName;

    try {
      const response = await fetch(callUrl, {
        method: "POST",
        headers,
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const text = await response.text();
        logger.error(
          { accountId, status: response.status, body: text.substring(0, 300) },
          "Failed to forward call event",
        );
      } else {
        logger.info(
          { accountId, callId: call.id, status: call.status, isVideo: !!call.isVideo },
          "Call event forwarded",
        );
      }
    } catch (err) {
      logger.error({ err, accountId }, "Call webhook POST failed");
    }
  }

  /** Phase B — generic webhook POST helper used by group / presence forwarders. */
  private async postWebhook(
    accountId: string,
    relativePath: string,
    body: Record<string, any>,
  ): Promise<void> {
    const session = this.sessions.get(accountId);
    if (!session?.odooBaseUrl || !session?.webhookSecret) return;
    const incomingUrl =
      session.callbackUrl ||
      `${session.odooBaseUrl}/open_whatsapp_connector/webhook/incoming`;
    // relativePath is the FULL module path (e.g.
    // "/open_whatsapp_connector/webhook/connection"), so replace the full
    // incoming path — not just "/webhook/incoming" — otherwise the
    // "/open_whatsapp_connector" prefix is duplicated and Odoo 404s, which
    // silently kills the connection/group/presence push (the post-scan
    // status never auto-flips). The "?db=" query is preserved either way.
    const url = incomingUrl.replace(
      "/open_whatsapp_connector/webhook/incoming",
      relativePath,
    );
    const payload = {
      jsonrpc: "2.0",
      params: { secret: session.webhookSecret, ...body },
    };
    const urlObj = new URL(url);
    const dbName = urlObj.searchParams.get("db");
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (dbName) headers["X-Odoo-Database"] = dbName;
    try {
      const response = await fetch(url, { method: "POST", headers, body: JSON.stringify(payload) });
      if (!response.ok) {
        const text = await response.text();
        logger.error(
          { accountId, url: relativePath, status: response.status, body: text.substring(0, 300) },
          "Webhook POST failed",
        );
      }
    } catch (err) {
      logger.error({ err, accountId, url: relativePath }, "Webhook POST threw");
    }
  }

  /** Push WhatsApp connection-state changes to Odoo so the account form
   * reflects qr_pending / connected within ms (vs up to 5 min waiting
   * for _cron_heartbeat). Uses the same postWebhook auth path. */
  private async forwardConnectionState(
    accountId: string,
    state:
      | "connecting"
      | "qr_pending"
      | "connected"
      | "disconnected"
      | "logged_out",
    extras: {
      qr_base64?: string | null;
      phone_number?: string | null;
      last_error?: string | null;
    } = {},
  ): Promise<void> {
    const body: Record<string, any> = { account_id: accountId, state };
    if (extras.qr_base64 !== undefined) body.qr_base64 = extras.qr_base64;
    if (extras.phone_number) body.phone_number = extras.phone_number;
    if (extras.last_error) body.last_error = extras.last_error;
    await this.postWebhook(
      accountId,
      "/open_whatsapp_connector/webhook/connection",
      body,
    );
  }

  /** Phase B1 — forward groups.upsert / groups.update to /webhook/group_meta. */
  private async forwardGroupMetaEvent(
    accountId: string,
    kind: "upsert" | "update",
    meta: GroupMetadata,
  ): Promise<void> {
    if (!meta?.id) return;
    await this.postWebhook(accountId, "/open_whatsapp_connector/webhook/group_meta", {
      kind,
      group: {
        id: meta.id,
        subject: meta.subject || null,
        subjectOwner: meta.subjectOwner || null,
        subjectTime: meta.subjectTime || null,
        desc: (meta as any).desc || null,
        descOwner: (meta as any).descOwner || null,
        creation: meta.creation || null,
        owner: meta.owner || null,
        announce: !!meta.announce,
        restrict: !!meta.restrict,
        size: meta.size || null,
      },
    });
  }

  /** Phase B2 — forward group-participants.update to /webhook/group_participants. */
  private async forwardGroupParticipantsEvent(
    accountId: string,
    event: { id: string; participants: string[]; action: string; author?: string },
  ): Promise<void> {
    if (!event?.id || !event?.action) return;
    await this.postWebhook(
      accountId,
      "/open_whatsapp_connector/webhook/group_participants",
      {
        group_jid: event.id,
        action: event.action,
        participants: event.participants || [],
        author: event.author || null,
      },
    );
  }

  /** Phase B3 — forward presence.update to /webhook/presence. */
  private async forwardPresenceEvent(
    accountId: string,
    event: { id: string; presences: Record<string, any> },
  ): Promise<void> {
    if (!event?.id || !event?.presences) return;
    // Flatten presences to an array of {participant, lastKnownPresence, lastSeen}.
    const flat = Object.entries(event.presences).map(([participant, p]) => ({
      participant,
      lastKnownPresence: (p as any)?.lastKnownPresence || null,
      lastSeen: (p as any)?.lastSeen || null,
    }));
    await this.postWebhook(
      accountId,
      "/open_whatsapp_connector/webhook/presence",
      { chat_jid: event.id, presences: flat },
    );
  }

  private async downloadMedia(msg: WAMessage, sock: WASocket): Promise<Buffer | null> {
    const { downloadMediaMessage } = await import("@whiskeysockets/baileys");
    // First try the normal direct-fetch path against mmg.whatsapp.net.
    try {
      const buffer = await downloadMediaMessage(msg, "buffer", {});
      return buffer as Buffer;
    } catch (err: any) {
      logger.warn(
        { err, jid: msg.key?.remoteJid, id: msg.key?.id },
        "Direct media download failed; attempting reuploadRequest fallback",
      );
    }
    // Fallback: explicitly ask WhatsApp to re-upload the media over our
    // existing WebSocket, then download the refreshed message. We must call
    // updateMediaMessage DIRECTLY rather than pass it as downloadMediaMessage's
    // `reuploadRequest` ctx: Baileys only invokes that ctx when the CDN error
    // carries an HTTP status of 404/410 (REUPLOAD_REQUIRED_STATUS). A TCP-level
    // firewall block to mmg.whatsapp.net throws a raw ECONNREFUSED/ETIMEDOUT
    // with no .status, so the ctx path is inert for exactly the advertised
    // firewall/VPN scenario. Calling updateMediaMessage by hand bypasses that
    // gate and refreshes the media URLs over the live socket.
    try {
      const refreshed = await sock.updateMediaMessage(msg);
      const buffer = await downloadMediaMessage(refreshed, "buffer", {});
      if (buffer) {
        logger.info(
          { jid: msg.key?.remoteJid, id: msg.key?.id, bytes: (buffer as Buffer).length },
          "Media download succeeded via explicit updateMediaMessage re-upload",
        );
      }
      return buffer as Buffer;
    } catch (err) {
      logger.error(
        { err, jid: msg.key?.remoteJid, id: msg.key?.id },
        "Media download failed (both direct fetch and updateMediaMessage re-upload)",
      );
      return null;
    }
  }

  getSession(accountId: string): {
    status: string;
    qr_base64?: string;
    phone_number?: string;
  } | null {
    const session = this.sessions.get(accountId);
    if (!session) return null;
    return {
      status: session.status,
      qr_base64: session.qrBase64,
      phone_number: session.phoneNumber,
    };
  }

  /** Phase 4: live-update mutable session config (e.g. send_read_receipts). */
  updateSessionConfig(
    accountId: string,
    patch: {
      sendReadReceipts?: boolean;
      shareOnlinePresence?: boolean;
      captureContactStatuses?: boolean;
      statusMarkSeen?: boolean;
      syncOwnStatuses?: boolean;
    }
  ): { ok: boolean } {
    const session = this.sessions.get(accountId);
    if (!session) return { ok: false };
    if (typeof patch.syncOwnStatuses === "boolean") {
      // Like captureContactStatuses, the broadcast filter is applied on the
      // next reconnect; persist so a restart honours it from socket creation.
      session.syncOwnStatuses = patch.syncOwnStatuses;
      this.persistConfigField(accountId, "sync_own_statuses", patch.syncOwnStatuses);
    }
    if (typeof patch.captureContactStatuses === "boolean") {
      // Takes full effect (broadcast filter) on the next reconnect; persist so
      // a restart honours it and applies the filter from socket creation.
      session.captureContactStatuses = patch.captureContactStatuses;
      this.persistConfigField(accountId, "capture_contact_statuses", patch.captureContactStatuses);
    }
    if (typeof patch.statusMarkSeen === "boolean") {
      session.statusMarkSeen = patch.statusMarkSeen;
      this.persistConfigField(accountId, "status_mark_seen", patch.statusMarkSeen);
    }
    if (typeof patch.sendReadReceipts === "boolean") {
      session.sendReadReceipts = patch.sendReadReceipts;
      // Persist so a sidecar restart restores the chosen read-receipt mode.
      // (Symmetric with share_online_presence below — this used to be
      // in-memory only, so a restart silently reverted a stealth choice.)
      this.persistConfigField(accountId, "send_read_receipts", patch.sendReadReceipts);
    }
    if (typeof patch.shareOnlinePresence === "boolean") {
      session.shareOnlinePresence = patch.shareOnlinePresence;
      // Persist so a sidecar restart restores the chosen presence mode.
      this.persistConfigField(accountId, "share_online_presence", patch.shareOnlinePresence);
      if (session.status === "connected") {
        session.socket
          .sendPresenceUpdate(patch.shareOnlinePresence ? "available" : "unavailable")
          .catch((err) =>
            logger.error({ err, accountId }, "Failed to apply online-presence toggle"));
      }
    }
    return { ok: true };
  }

  /** Patch a single key in a session's persisted config.json (best-effort). */
  private persistConfigField(accountId: string, key: string, value: unknown): void {
    try {
      const configPath = path.join(this.sessionsDir, accountId, "config.json");
      if (!fs.existsSync(configPath)) return;
      const cfg = JSON.parse(fs.readFileSync(configPath, "utf8"));
      cfg[key] = value;
      fs.writeFileSync(configPath, JSON.stringify(cfg, null, 2));
    } catch (err) {
      logger.error({ err, accountId, key }, "Failed to persist config field");
    }
  }

  async disconnect(accountId: string): Promise<boolean> {
    const session = this.sessions.get(accountId);
    if (!session) return false;
    try {
      session.shouldReconnect = false;
      session.reconnectAttempts = MAX_RECONNECT_ATTEMPTS; // Prevent auto-reconnect
      session.socket.ws?.close();
      session.status = "disconnected";
      return true;
    } catch {
      return false;
    }
  }

  async logout(accountId: string): Promise<boolean> {
    // Robust logout: succeed and fully clean up even when the socket is already
    // down (disconnected / flapping / no in-memory session). Unlinking the
    // device is best-effort; wiping the saved credentials + reporting
    // logged_out always happens, so the account never gets stuck "linked".
    const session = this.sessions.get(accountId);
    if (session) {
      session.shouldReconnect = false;
      session.reconnectAttempts = MAX_RECONNECT_ATTEMPTS;
      try { await session.socket.logout(); } catch {}   // best-effort unlink
      try { session.socket.end(undefined); } catch {}
      session.status = "logged_out";
      this.sessions.delete(accountId);
    }
    // Cancel any in-flight history sync and wipe the auth state regardless.
    this.clearHistoryTimers(accountId);
    this.historyMode.delete(accountId);
    try {
      const authDir = path.join(this.sessionsDir, accountId);
      fs.rmSync(authDir, { recursive: true, force: true });
    } catch {}
    void this.forwardConnectionState(accountId, "logged_out", { qr_base64: null });
    return true;
  }

  async sendText(
    accountId: string,
    to: string,
    body: string,
    replyTo?: string,
    options?: { mentionAll?: boolean; mentions?: string[] },
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    const content: AnyMessageContent = { text: body };
    if (replyTo) {
      (content as any).quoted = { key: { remoteJid: jid, id: replyTo } };
    }
    if (options?.mentionAll && jid.endsWith("@g.us")) {
      // Baileys has NO `mentionAll` content flag — it only pings JIDs listed
      // in `mentions`. The old `content.mentionAll = true` was silently
      // ignored. Expand to every current participant so @all actually pings
      // the whole group.
      try {
        const meta = await session.socket.groupMetadata(jid);
        const all = (meta.participants || [])
          .map((p: any) => p.id)
          .filter(Boolean);
        if (all.length) {
          (content as any).mentions = all;
        }
      } catch (e) {
        logger.warn({ err: String(e), jid }, "mentionAll: groupMetadata fetch failed");
      }
    }
    if (options?.mentions && options.mentions.length) {
      // Explicit mentions win / merge over the @all expansion.
      const existing = ((content as any).mentions as string[]) || [];
      (content as any).mentions = Array.from(new Set([...existing, ...options.mentions]));
    }
    const result = await session.socket.sendMessage(jid, content);
    return { message_id: result?.key?.id || "" };
  }

  async editMessage(
    accountId: string,
    to: string,
    messageId: string,
    newText: string,
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    // WhatsApp edits are sent as a normal message that references the original
    // message key via the `edit` field. The server enforces the ~15-minute edit
    // window and rejects older edits.
    const key = { remoteJid: jid, fromMe: true, id: messageId };
    const result = await session.socket.sendMessage(jid, {
      text: newText,
      edit: key,
    } as any);
    return { message_id: result?.key?.id || "" };
  }

  async sendMedia(
    accountId: string,
    to: string,
    mediaBase64: string,
    mimetype: string,
    filename?: string,
    caption?: string,
    options?: { ptt?: boolean; gifPlayback?: boolean }
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    const buffer = Buffer.from(mediaBase64, "base64");
    const ptt = !!options?.ptt;
    const gifPlayback = !!options?.gifPlayback;

    let content: AnyMessageContent;
    if (mimetype.startsWith("image/") && !gifPlayback) {
      content = { image: buffer, caption: caption || undefined, mimetype };
    } else if (mimetype.startsWith("video/") || gifPlayback) {
      content = {
        video: buffer,
        caption: caption || undefined,
        mimetype: mimetype.startsWith("video/") ? mimetype : "video/mp4",
        gifPlayback,
      } as AnyMessageContent;
    } else if (mimetype.startsWith("audio/")) {
      content = {
        audio: buffer,
        mimetype,
        ptt: ptt || mimetype.includes("ogg"),
      };
    } else {
      // Phase H bug-fix — documents must forward the caption too, otherwise
      // typing "Hi! See attached" alongside a PDF arrives as just the PDF
      // with no body. Baileys' documentMessage proto supports `caption` and
      // standard WhatsApp clients render it below the file preview.
      content = {
        document: buffer,
        mimetype,
        fileName: filename || "document",
        caption: caption || undefined,
      } as AnyMessageContent;
    }

    const result = await session.socket.sendMessage(jid, content);
    return { message_id: result?.key?.id || "" };
  }

  async sendAlbum(
    accountId: string,
    to: string,
    items: Array<{
      media_base64: string;
      mimetype: string;
      caption?: string;
    }>
  ): Promise<{ parent_message_id: string; child_message_ids: string[] }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    if (!Array.isArray(items) || items.length < 2) {
      throw new Error("album requires at least 2 media items");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;

    const expectedImageCount = items.filter((m) => m.mimetype.startsWith("image/")).length;
    const expectedVideoCount = items.filter((m) => m.mimetype.startsWith("video/")).length;
    if (expectedImageCount + expectedVideoCount !== items.length) {
      throw new Error("album items must all be image/* or video/*");
    }

    const parent = await session.socket.sendMessage(jid, {
      album: { expectedImageCount, expectedVideoCount },
    } as any);
    const parentKey = parent?.key;
    if (!parentKey) throw new Error("album parent send returned no key");

    const childIds: string[] = [];
    for (const item of items) {
      const buffer = Buffer.from(item.media_base64, "base64");
      const childContent: any = item.mimetype.startsWith("image/")
        ? { image: buffer, mimetype: item.mimetype, caption: item.caption || undefined }
        : {
            video: buffer,
            mimetype: item.mimetype.startsWith("video/") ? item.mimetype : "video/mp4",
            caption: item.caption || undefined,
          };
      childContent.albumParentKey = parentKey;
      const child = await session.socket.sendMessage(jid, childContent);
      if (child?.key?.id) childIds.push(child.key.id);
    }
    return {
      parent_message_id: parentKey.id || "",
      child_message_ids: childIds,
    };
  }

  async sendPoll(
    accountId: string,
    to: string,
    name: string,
    options: string[],
    selectableCount: number = 1
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    const result = await session.socket.sendMessage(jid, {
      poll: {
        name,
        values: options,
        selectableCount,
      },
    } as AnyMessageContent);
    return { message_id: result?.key?.id || "" };
  }

  async sendContact(
    accountId: string,
    to: string,
    contacts: Array<{ display_name?: string; vcard: string }>
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    const result = await session.socket.sendMessage(jid, {
      contacts: {
        displayName: contacts[0]?.display_name || "Contact",
        contacts: contacts.map((c) => ({
          displayName: c.display_name,
          vcard: c.vcard,
        })),
      },
    } as AnyMessageContent);
    return { message_id: result?.key?.id || "" };
  }

  async sendLocation(
    accountId: string,
    to: string,
    latitude: number,
    longitude: number,
    name?: string,
    address?: string
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    const result = await session.socket.sendMessage(jid, {
      location: {
        degreesLatitude: latitude,
        degreesLongitude: longitude,
        name,
        address,
      },
    });
    return { message_id: result?.key?.id || "" };
  }

  async sendReaction(
    accountId: string,
    chatJid: string,
    messageId: string,
    emoji: string,
    fromMe: boolean,
    targetParticipant?: string,
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = chatJid.includes("@") ? chatJid : `${chatJid}@s.whatsapp.net`;
    const key: any = { remoteJid: jid, id: messageId, fromMe };
    if (targetParticipant) key.participant = targetParticipant;
    const result = await session.socket.sendMessage(jid, {
      react: { text: emoji, key },
    });
    return { message_id: result?.key?.id || "" };
  }

  async sendButtons(
    accountId: string,
    to: string,
    body: string,
    buttons: Array<{ id: string; text: string }>,
    header?: { type: string; text?: string; media_base64?: string; mimetype?: string },
    footer?: string
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    // NB: sendMessage() routes through generateWAMessageContent, whose first
    // dispatch branch is `text` — so a flat {text, buttons} object was always
    // converted to a plain extendedTextMessage and the buttons were dropped.
    // The correct path is to hand-build a proto ButtonsMessage and relay it
    // with generateWAMessageFromContent + relayMessage. Proto field names:
    // contentText (body), footerText (string), text (text-header). Note:
    // modern WhatsApp clients may not RENDER interactive buttons from a
    // non-official (Baileys) sender — hence the text fallback below.
    // Modern interactive path. WhatsApp deprecated the legacy buttonsMessage
    // (~2023) and current clients silently drop it (relayed, never rendered,
    // no error). The path that still renders is
    // interactiveMessage.nativeFlowMessage (quick_reply buttons), wrapped in a
    // viewOnceMessage envelope carrying messageContextInfo.deviceListMetadata —
    // the technique community forks use to re-enable interactive on
    // QR/unofficial sessions. NOTE: WhatsApp still gates delivery of interactive
    // to official Business-API senders, so on a QR session this is best-effort
    // and the numbered text below is the reliable path. We try this first and
    // fall back to a numbered text list only when the relay itself errors.
    const headerObj =
      header && header.type === "text" && header.text
        ? { title: header.text, hasMediaAttachment: false }
        : undefined;
    const nativeButtons = buttons.map((b, i) => ({
      name: "quick_reply",
      buttonParamsJson: JSON.stringify({
        display_text: b.text,
        id: b.id || `btn_${i}`,
      }),
    }));
    try {
      const userJid = session.socket.user?.id || "";
      const waMsg = generateWAMessageFromContent(
        jid,
        {
          viewOnceMessage: {
            message: {
              messageContextInfo: {
                deviceListMetadata: {},
                deviceListMetadataVersion: 2,
              },
              interactiveMessage: {
                body: { text: body },
                footer: footer ? { text: footer } : undefined,
                header: headerObj,
                nativeFlowMessage: {
                  buttons: nativeButtons,
                  messageParamsJson: "",
                },
              },
            },
          },
        } as any,
        { userJid } as any,
      );
      await session.socket.relayMessage(jid, waMsg.message!, {
        messageId: waMsg.key.id!,
      });
      return { message_id: waMsg.key.id || "" };
    } catch (e) {
      logger.warn(
        { err: String(e) },
        "sendButtons: interactive relay failed; falling back to numbered text",
      );
      const lines = [body, "", ...buttons.map((b, i) => `${i + 1}. ${b.text}`)];
      if (footer) lines.push("", footer);
      const result = await session.socket.sendMessage(jid, { text: lines.join("\n") });
      return { message_id: result?.key?.id || "" };
    }
  }

  async sendList(
    accountId: string,
    to: string,
    body: string,
    buttonText: string,
    sections: Array<{
      title: string;
      rows: Array<{ id: string; title: string; description?: string }>;
    }>,
    footer?: string
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    // As with sendButtons, a flat {text, sections} object is dropped to a plain
    // extendedTextMessage by generateWAMessageContent (no listMessage dispatch
    // branch). Build a proto ListMessage and relay it. Modern WhatsApp may not
    // render lists from a Baileys sender → text fallback below.
    // Modern interactive path. The legacy listMessage is no longer rendered by
    // current WhatsApp clients, so we use interactiveMessage.nativeFlowMessage
    // with a single_select button (the tappable list/dropdown), wrapped in the
    // viewOnceMessage + deviceListMetadata envelope so it renders on
    // QR/unofficial sessions. Fall back to a numbered text menu on relay error.
    const singleSelectParams = {
      title: buttonText,
      sections: sections.map((s) => ({
        title: s.title,
        rows: s.rows.map((r) => ({
          header: "",
          title: r.title,
          description: r.description || "",
          id: r.id,
        })),
      })),
    };
    try {
      const userJid = session.socket.user?.id || "";
      const waMsg = generateWAMessageFromContent(
        jid,
        {
          viewOnceMessage: {
            message: {
              messageContextInfo: {
                deviceListMetadata: {},
                deviceListMetadataVersion: 2,
              },
              interactiveMessage: {
                body: { text: body },
                footer: footer ? { text: footer } : undefined,
                nativeFlowMessage: {
                  buttons: [
                    {
                      name: "single_select",
                      buttonParamsJson: JSON.stringify(singleSelectParams),
                    },
                  ],
                  messageParamsJson: "",
                },
              },
            },
          },
        } as any,
        { userJid } as any,
      );
      await session.socket.relayMessage(jid, waMsg.message!, {
        messageId: waMsg.key.id!,
      });
      return { message_id: waMsg.key.id || "" };
    } catch (e) {
      logger.warn(
        { err: String(e) },
        "sendList: interactive relay failed; falling back to text menu",
      );
      const lines = [body, ""];
      for (const s of sections) {
        if (s.title) lines.push(`*${s.title}*`);
        s.rows.forEach((r, i) =>
          lines.push(`${i + 1}. ${r.title}${r.description ? ` — ${r.description}` : ""}`),
        );
      }
      if (footer) lines.push("", footer);
      const result = await session.socket.sendMessage(jid, { text: lines.join("\n") });
      return { message_id: result?.key?.id || "" };
    }
  }

  async getGroups(accountId: string): Promise<Array<{ jid: string; subject: string; participants: number }>> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      throw new Error("Session not connected");
    }
    const groups = await session.socket.groupFetchAllParticipating();
    return Object.values(groups).map((g) => ({
      jid: g.id,
      subject: g.subject,
      participants: g.participants?.length || 0,
    }));
  }

  // ── Phase C1 — group participant administration ───────────────────────
  async groupParticipantsUpdate(
    accountId: string,
    groupJid: string,
    action: "add" | "remove" | "promote" | "demote",
    jids: string[],
  ): Promise<{ jid: string; status: string }[]> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    const normalized = jids.map((j) => (j.includes("@") ? j : `${j}@s.whatsapp.net`));
    const result = await session.socket.groupParticipantsUpdate(groupJid, normalized, action);
    return (result as any[]).map((r) => ({ jid: r.jid, status: String(r.status || "ok") }));
  }

  async updateMemberLabel(
    accountId: string,
    groupJid: string,
    label: string,
  ): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    const sock: any = session.socket;
    if (typeof sock.relayMessage !== "function") {
      throw new Error("relayMessage not exposed on this Baileys version");
    }
    const trimmed = (label || "").slice(0, 30);
    const protocolMessage = {
      protocolMessage: {
        type: proto.Message.ProtocolMessage.Type.GROUP_MEMBER_LABEL_CHANGE,
        memberLabel: {
          label: trimmed,
          labelTimestamp: Math.floor(Date.now() / 1000),
        },
      },
    };
    await sock.relayMessage(groupJid, protocolMessage, {
      additionalNodes: [
        {
          tag: "meta",
          attrs: { tag_reason: "user_update", appdata: "member_tag" },
        },
      ],
    });
    return { ok: true };
  }

  // ── Phase F — full group management (create / leave / invite / settings) ─

  private toJidArray(participants: string[]): string[] {
    return (participants || [])
      .map((p) => (p || "").trim().replace(/^\+/, ""))
      .filter((p) => p.length > 0)
      .map((p) => (p.includes("@") ? p : `${p}@s.whatsapp.net`));
  }

  private static stripInvitePrefix(s: string): string {
    return (s || "").trim().replace(/^https?:\/\/chat\.whatsapp\.com\//i, "");
  }

  async groupCreate(
    accountId: string,
    subject: string,
    participants: string[],
  ): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const cleanSubject = (subject || "").slice(0, 100);
    if (!cleanSubject) throw new Error("subject is required");
    const jids = this.toJidArray(participants);
    if (jids.length === 0) throw new Error("at least one participant is required");
    const result = await (session.socket as any).groupCreate(cleanSubject, jids);
    return result;
  }

  async groupLeave(accountId: string, groupJid: string): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    await (session.socket as any).groupLeave(groupJid);
    return { ok: true };
  }

  async groupUpdateSubject(
    accountId: string,
    groupJid: string,
    subject: string,
  ): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    await (session.socket as any).groupUpdateSubject(groupJid, (subject || "").slice(0, 100));
    return { ok: true };
  }

  async groupUpdateDescription(
    accountId: string,
    groupJid: string,
    description: string,
  ): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    const trimmed = (description || "").slice(0, 512);
    await (session.socket as any).groupUpdateDescription(groupJid, trimmed || undefined);
    return { ok: true };
  }

  async groupSettingUpdate(
    accountId: string,
    groupJid: string,
    setting: string,
  ): Promise<{ ok: true }> {
    const valid = ["announcement", "not_announcement", "locked", "unlocked"];
    if (!valid.includes(setting)) {
      throw new Error(`setting must be one of ${valid.join("|")}`);
    }
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    await (session.socket as any).groupSettingUpdate(groupJid, setting as any);
    return { ok: true };
  }

  async groupInviteCode(
    accountId: string,
    groupJid: string,
  ): Promise<{ code: string | null }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    const code = await (session.socket as any).groupInviteCode(groupJid);
    return { code: code || null };
  }

  async groupRevokeInvite(
    accountId: string,
    groupJid: string,
  ): Promise<{ code: string | null }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    const code = await (session.socket as any).groupRevokeInvite(groupJid);
    return { code: code || null };
  }

  async groupAcceptInvite(
    accountId: string,
    code: string,
  ): Promise<{ jid: string | null }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const cleaned = SessionManager.stripInvitePrefix(code);
    if (!cleaned) throw new Error("invite code is required");
    const jid = await (session.socket as any).groupAcceptInvite(cleaned);
    return { jid: jid || null };
  }

  async groupGetInviteInfo(accountId: string, code: string): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const cleaned = SessionManager.stripInvitePrefix(code);
    if (!cleaned) throw new Error("invite code is required");
    return await (session.socket as any).groupGetInviteInfo(cleaned);
  }

  async groupFetchAllParticipating(
    accountId: string,
  ): Promise<{ groups: any[] }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const dict = await (session.socket as any).groupFetchAllParticipating();
    const groups = Object.entries(dict || {}).map(([jid, meta]) => ({
      ...((meta as any) || {}),
      id: (meta as any)?.id || jid,
    }));
    return { groups };
  }

  // ── Phase G — group profile picture, forward, reactions, chatModify, sticker, live location, communities, newsletters, events, V4 invites ─

  async updateGroupProfilePicture(
    accountId: string,
    groupJid: string,
    imageBase64: string | null,
  ): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!groupJid.endsWith("@g.us")) throw new Error("Not a group JID");
    const sock: any = session.socket;
    if (!imageBase64) {
      await sock.removeProfilePicture(groupJid);
    } else {
      const buf = Buffer.from(imageBase64, "base64");
      await sock.updateProfilePicture(groupJid, buf);
    }
    return { ok: true };
  }

  async forwardMessage(
    accountId: string,
    fromJid: string,
    msgId: string,
    toJid: string,
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!fromJid || !msgId) throw new Error("from_jid and msg_id are required");
    const stored = this.getStoredMessage(fromJid, msgId);
    if (!stored) {
      throw new Error(
        `Source message not in cache (${fromJid}|${msgId}); cache only holds messages seen since the sidecar last started.`,
      );
    }
    const fullMessage: any = {
      key: { remoteJid: fromJid, id: msgId, fromMe: false },
      message: stored,
    };
    const target = toJid.includes("@") ? toJid : `${toJid}@s.whatsapp.net`;
    const result = await (session.socket as any).sendMessage(target, {
      forward: fullMessage,
      force: true,
    });
    return { message_id: result?.key?.id || "" };
  }

  async sendStatusBroadcast(
    accountId: string,
    type: "text" | "image" | "video",
    text?: string,
    mediaBase64?: string,
    mimetype?: string,
    recipients?: string[],
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");

    // statusJidList must be non-empty for the broadcast to be visible to
    // anyone other than the sender. Normalise whatever Python passes (may
    // include `+` prefix or no `@suffix`) to the canonical
    // `<digits>@s.whatsapp.net` shape Baileys expects.
    const jidList = (recipients || [])
      .map((j) => (j || "").replace(/^\+/, "").trim())
      .filter(Boolean)
      .map((j) => (j.includes("@") ? j : `${j}@s.whatsapp.net`));

    let content: AnyMessageContent;
    if (type === "text") {
      content = { text: text || "" } as AnyMessageContent;
    } else if (type === "image") {
      if (!mediaBase64) throw new Error("media_base64 required for image status");
      content = {
        image: Buffer.from(mediaBase64, "base64"),
        mimetype: mimetype || "image/jpeg",
        caption: text || undefined,
      } as AnyMessageContent;
    } else {
      if (!mediaBase64) throw new Error("media_base64 required for video status");
      content = {
        video: Buffer.from(mediaBase64, "base64"),
        mimetype: mimetype || "video/mp4",
        caption: text || undefined,
      } as AnyMessageContent;
    }

    const result = await (session.socket as any).sendMessage(
      "status@broadcast",
      content,
      { statusJidList: jidList },
    );
    return { message_id: result?.key?.id || "" };
  }

  // ── Contacts' Status (Stories) capture + interactions ─────────────
  /** Capture one contact's Status broadcast → download media → forward to Odoo. */
  private async handleStatusUpdate(accountId: string, msg: WAMessage, sock: WASocket) {
    const session = this.sessions.get(accountId);
    if (!session) return;
    const msgId = msg.key?.id;
    if (!msgId) return;
    if (this.isDuplicate(`${accountId}:status:${msgId}`)) return;

    let poster = msg.key?.participant || "";
    if (poster.endsWith("@lid")) {
      const resolved = await this.resolveLidToPhone(accountId, poster);
      if (resolved) poster = resolved;
    }
    const from = poster.split("@")[0] || "";
    if (!from) return;

    const raw = msg.message;
    if (!raw) return;
    const message = normalizeMessageContent(raw) || raw;

    let mediaType: "text" | "image" | "video" = "text";
    const text =
      message.conversation ||
      message.extendedTextMessage?.text ||
      message.imageMessage?.caption ||
      message.videoMessage?.caption ||
      "";
    let attachmentB64: string | undefined;
    let mimetype: string | undefined;
    if (message.imageMessage || message.videoMessage) {
      mediaType = message.imageMessage ? "image" : "video";
      mimetype =
        message.imageMessage?.mimetype ||
        message.videoMessage?.mimetype ||
        (mediaType === "image" ? "image/jpeg" : "video/mp4");
      try {
        const buf = await this.downloadMedia(msg, sock);
        if (buf) attachmentB64 = buf.toString("base64");
      } catch (err) {
        logger.warn({ err, accountId, msgId }, "Status media download failed");
      }
    }

    await this.postWebhook(accountId, "/open_whatsapp_connector/webhook/story", {
      account_id: accountId,
      message_id: msgId,
      participant: poster,
      from,
      sender_name: msg.pushName || undefined,
      media_type: mediaType,
      text,
      attachment_b64: attachmentB64,
      mimetype,
      timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
    });
  }

  /**
   * A Status (Story) the user posted from their phone / another linked device.
   * Forward it (tagged from_me) so it appears under My Status in Odoo. Odoo
   * de-dupes against statuses posted THROUGH the connector by wa_message_id.
   * (#own-status)
   */
  private async handleOwnStatus(accountId: string, msg: WAMessage, sock: WASocket) {
    const session = this.sessions.get(accountId);
    if (!session) return;
    const msgId = msg.key?.id;
    if (!msgId) return;
    if (this.isDuplicate(`${accountId}:ownstatus:${msgId}`)) return;

    const raw = msg.message;
    if (!raw) return;
    const message = normalizeMessageContent(raw) || raw;

    let mediaType: "text" | "image" | "video" = "text";
    const text =
      message.conversation ||
      message.extendedTextMessage?.text ||
      message.imageMessage?.caption ||
      message.videoMessage?.caption ||
      "";
    let attachmentB64: string | undefined;
    let mimetype: string | undefined;
    if (message.imageMessage || message.videoMessage) {
      mediaType = message.imageMessage ? "image" : "video";
      mimetype =
        message.imageMessage?.mimetype ||
        message.videoMessage?.mimetype ||
        (mediaType === "image" ? "image/jpeg" : "video/mp4");
      try {
        const buf = await this.downloadMedia(msg, sock);
        if (buf) attachmentB64 = buf.toString("base64");
      } catch (err) {
        logger.warn({ err, accountId, msgId }, "Own-status media download failed");
      }
    }

    await this.postWebhook(accountId, "/open_whatsapp_connector/webhook/story", {
      account_id: accountId,
      from_me: true,
      message_id: msgId,
      media_type: mediaType,
      text,
      attachment_b64: attachmentB64,
      mimetype,
      timestamp: Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000),
    });
  }

  private normalizeStatusKey(statusKey: any): any {
    return {
      remoteJid: "status@broadcast",
      id: statusKey?.id,
      participant: statusKey?.participant,
      fromMe: false,
    };
  }

  /** Delete a status you posted (revoke it from contacts' phones). */
  async revokeStatus(accountId: string, messageId: string): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    await (session.socket as any).sendMessage("status@broadcast", {
      delete: { remoteJid: "status@broadcast", id: messageId, fromMe: true },
    });
    return { ok: true };
  }

  /** React with an emoji to a contact's status. */
  async statusReact(accountId: string, statusKey: any, emoji: string): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    await (session.socket as any).sendMessage("status@broadcast", {
      react: { text: emoji || "", key: this.normalizeStatusKey(statusKey) },
    });
    return { ok: true };
  }

  /** Reply to a contact's status — a DM to the poster. */
  async statusReply(accountId: string, to: string, text: string): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const jid = to.includes("@") ? to : `${to}@s.whatsapp.net`;
    const result = await (session.socket as any).sendMessage(jid, { text: text || "" });
    return { message_id: result?.key?.id || "" };
  }

  /** Send a WhatsApp view receipt for a contact's status. */
  async statusMarkSeen(accountId: string, statusKey: any): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    await (session.socket as any).readMessages([this.normalizeStatusKey(statusKey)]);
    return { ok: true };
  }

  async sendSticker(
    accountId: string,
    toJid: string,
    stickerBase64: string,
    mimetype = "image/webp",
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const jid = toJid.includes("@") ? toJid : `${toJid}@s.whatsapp.net`;
    const buf = Buffer.from(stickerBase64, "base64");
    const result = await (session.socket as any).sendMessage(jid, {
      sticker: buf,
      mimetype,
    });
    return { message_id: result?.key?.id || "" };
  }

  // Phase G bug-fix: real WhatsApp live location requires periodic position
  // updates which Baileys doesn't plumb for us. The previous sendLiveLocation
  // method silently demoted to a static location, mis-using sequenceNumber as
  // a duration value. Removed. Use the existing `sendLocation` (Phase 1) for
  // static-location sends.

  async chatModifyAction(
    accountId: string,
    targetJid: string,
    action: "archive" | "unarchive" | "pin" | "unpin" | "mute" | "unmute",
    muteEndTimestamp?: number,
  ): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const jid = targetJid.includes("@") ? targetJid : `${targetJid}@s.whatsapp.net`;
    const sock: any = session.socket;
    let mod: any;
    switch (action) {
      case "archive": mod = { archive: true, lastMessages: [] }; break;
      case "unarchive": mod = { archive: false, lastMessages: [] }; break;
      case "pin": mod = { pin: true }; break;
      case "unpin": mod = { pin: false }; break;
      case "mute": mod = { mute: muteEndTimestamp || 8 * 60 * 60 * 1000 }; break;
      case "unmute": mod = { mute: null }; break;
      default: throw new Error(`Unknown chatModify action: ${action}`);
    }
    await sock.chatModify(mod, jid);
    return { ok: true };
  }

  // Phase G bug-fix: sendNativeFlowSingleSelect was removed. The previous
  // implementation passed `{text, title, buttonText, sections}` to
  // sock.sendMessage, but Baileys' generateWAMessageContent has no handler
  // for that shape — it silently fell through to a plain text message,
  // never rendering as a Native Flow dropdown. Proper implementation needs
  // interactiveMessage.nativeFlowMessage proto encoding which is fragile
  // across WA Web revisions; the existing `list` template type covers the
  // section/rows menu use-case correctly.

  // ── Communities ────────────────────────────────────────────────────────
  async communityFetchAllParticipating(accountId: string): Promise<{ communities: any[] }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const dict = await (session.socket as any).communityFetchAllParticipating();
    const communities = Object.entries(dict || {}).map(([jid, meta]) => ({
      ...((meta as any) || {}),
      id: (meta as any)?.id || jid,
    }));
    return { communities };
  }

  async communityCreate(accountId: string, subject: string, body?: string): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    return await (session.socket as any).communityCreate((subject || "").slice(0, 100), body || "");
  }

  async communityCreateGroup(
    accountId: string,
    subject: string,
    participants: string[],
    parentCommunityJid: string,
  ): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!parentCommunityJid.endsWith("@g.us")) throw new Error("parentCommunityJid must end with @g.us");
    const jids = this.toJidArray(participants);
    return await (session.socket as any).communityCreateGroup(subject, jids, parentCommunityJid);
  }

  async communityLeave(accountId: string, communityJid: string): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!communityJid.endsWith("@g.us")) throw new Error("Not a community JID");
    await (session.socket as any).communityLeave(communityJid);
    return { ok: true };
  }

  async communityUpdateSubject(
    accountId: string,
    communityJid: string,
    subject: string,
  ): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!communityJid.endsWith("@g.us")) throw new Error("Not a community JID");
    await (session.socket as any).communityUpdateSubject(communityJid, (subject || "").slice(0, 100));
    return { ok: true };
  }

  async communityMetadata(accountId: string, communityJid: string): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!communityJid.endsWith("@g.us")) throw new Error("Not a community JID");
    return await (session.socket as any).communityMetadata(communityJid);
  }

  async communityFetchLinkedGroups(accountId: string, communityJid: string): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    if (!communityJid.endsWith("@g.us")) throw new Error("Not a community JID");
    const sock: any = session.socket;
    if (typeof sock.communityFetchLinkedGroups !== "function") return { groups: [] };
    const groups = await sock.communityFetchLinkedGroups(communityJid);
    return { groups: Array.isArray(groups) ? groups : [] };
  }

  // ── Newsletters ────────────────────────────────────────────────────────
  async newsletterCreate(accountId: string, name: string, description?: string): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    return await (session.socket as any).newsletterCreate(name, description || "");
  }

  async newsletterFollow(accountId: string, newsletterJid: string): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    await (session.socket as any).newsletterFollow(newsletterJid);
    return { ok: true };
  }

  async newsletterUnfollow(accountId: string, newsletterJid: string): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    await (session.socket as any).newsletterUnfollow(newsletterJid);
    return { ok: true };
  }

  async newsletterMetadata(accountId: string, newsletterJid: string): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const sock: any = session.socket;
    // Baileys newsletterMetadata(type, key) takes `key` as a STRING and
    // inserts it verbatim into the GraphQL payload — passing an object
    // produced a malformed request and broke subscribe-by-invite-link.
    return await sock.newsletterMetadata("jid", newsletterJid);
  }

  async newsletterMetadataByInvite(accountId: string, code: string): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const sock: any = session.socket;
    // key is a plain string (the invite code), not { invite: code }.
    return await sock.newsletterMetadata("invite", code);
  }

  async newsletterMute(accountId: string, newsletterJid: string, mute: boolean): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const sock: any = session.socket;
    if (mute) {
      await sock.newsletterMute(newsletterJid);
    } else {
      await sock.newsletterUnmute(newsletterJid);
    }
    return { ok: true };
  }

  async newsletterUpdate(
    accountId: string,
    newsletterJid: string,
    updates: { name?: string; description?: string; pictureBase64?: string | null },
  ): Promise<{ ok: true }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const sock: any = session.socket;
    if (updates.name !== undefined) await sock.newsletterUpdateName(newsletterJid, updates.name);
    if (updates.description !== undefined) await sock.newsletterUpdateDescription(newsletterJid, updates.description);
    if (updates.pictureBase64 !== undefined) {
      if (updates.pictureBase64 === null || updates.pictureBase64 === "") {
        await sock.newsletterRemovePicture(newsletterJid);
      } else {
        const buf = Buffer.from(updates.pictureBase64, "base64");
        await sock.newsletterUpdatePicture(newsletterJid, buf);
      }
    }
    return { ok: true };
  }

  async newsletterFetchMessages(
    accountId: string,
    newsletterJid: string,
    count = 100,
    since = 0,
    after = 0,
  ): Promise<{ messages: any[] }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const sock: any = session.socket;
    // Channel/newsletter post history is NOT part of the messaging-history.set
    // sync; it must be fetched explicitly. Some gateway builds don't expose it
    // — return empty rather than throw so the Odoo import stays best-effort.
    if (typeof sock.newsletterFetchMessages !== "function") {
      return { messages: [] };
    }
    // Channel-post history fetch: newsletterFetchMessages(jid, count, since, after).
    // The return shape varies across builds, so normalise to a plain array and
    // let Odoo read each post defensively.
    const res = await sock.newsletterFetchMessages(newsletterJid, count, since, after);
    const messages = Array.isArray(res) ? res : (res?.messages ?? []);
    return { messages };
  }

  // ── Events ─────────────────────────────────────────────────────────────
  async sendEvent(
    accountId: string,
    toJid: string,
    name: string,
    description: string,
    startUnixSeconds: number,
    location?: { name?: string; degreesLatitude?: number; degreesLongitude?: number },
  ): Promise<{ message_id: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const jid = toJid.includes("@") ? toJid : `${toJid}@s.whatsapp.net`;
    // Baileys (Utils/messages.js:410) does
    //   Math.floor(message.event.startDate.getTime() / 1000)
    // so we MUST pass a Date object, not the raw seconds value.
    const eventContent: any = {
      name: (name || "").slice(0, 100),
      description: (description || "").slice(0, 1024),
      startDate: new Date(startUnixSeconds * 1000),
    };
    if (location) eventContent.location = location;
    const result = await (session.socket as any).sendMessage(jid, { event: eventContent });
    return { message_id: result?.key?.id || "" };
  }

  // ── V4 invite (embedded chat invite-card → accept) ─────────────────────
  async groupAcceptInviteV4(
    accountId: string,
    sourceJid: string,
    inviteMessage: any,
  ): Promise<any> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    // Baileys auto-wraps a string into {remoteJid: ...}, but downstream IQ
    // builders read key.remoteJid raw — pass a properly suffixed JID so
    // device-routing and signal session lookup don't malform.
    const sjid = sourceJid && sourceJid.includes("@")
      ? sourceJid
      : `${sourceJid}@s.whatsapp.net`;
    return await (session.socket as any).groupAcceptInviteV4(sjid, inviteMessage);
  }

  // ── Phase C2 — block / unblock / fetch-blocklist ──────────────────────
  async updateBlockStatus(
    accountId: string,
    jid: string,
    block: boolean,
  ): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const target = jid.includes("@") ? jid : `${jid}@s.whatsapp.net`;
    await session.socket.updateBlockStatus(target, block ? "block" : "unblock");
    return { ok: true };
  }

  async fetchBlocklist(accountId: string): Promise<{ jids: string[] }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const list = await session.socket.fetchBlocklist();
    return { jids: (list || []).filter((j): j is string => typeof j === "string") };
  }

  // ── Phase C3 — business profile picture ───────────────────────────────
  async updateProfilePicture(
    accountId: string,
    imageBase64: string,
  ): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const buffer = Buffer.from(imageBase64, "base64");
    const me = session.socket.user?.id;
    if (!me) throw new Error("Session has no user JID yet");
    await session.socket.updateProfilePicture(me, buffer);
    return { ok: true };
  }

  async removeProfilePicture(accountId: string): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const me = session.socket.user?.id;
    if (!me) throw new Error("Session has no user JID yet");
    await session.socket.removeProfilePicture(me);
    return { ok: true };
  }

  // Fetch (download) a profile picture FROM WhatsApp for any JID — a contact
  // (<digits>@s.whatsapp.net), a group (@g.us) or a channel (@newsletter).
  // profilePictureUrl returns a short-lived signed CDN URL; we download the
  // bytes here so Odoo never has to reach WhatsApp's CDN and the URL can't
  // expire in transit. Returns nulls when the JID has no picture or privacy
  // hides it (profilePictureUrl throws item-not-found in that case).
  async fetchProfilePicture(
    accountId: string,
    jid: string,
    hiRes = true,
  ): Promise<{ url: string | null; image_base64: string | null; mimetype: string | null }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const picType = hiRes ? "image" : "preview";
    const sock = session.socket as any;
    const meDigits = (sock.user?.id || "").split(":")[0].split("@")[0];
    const jidDigits = jid.split("@")[0].split(":")[0];
    const isContactJid = jid.endsWith("@s.whatsapp.net") && jidDigits !== meDigits;
    // Library-internal picture queries attach a trusted-contact token for
    // contact JIDs; the current WhatsApp revision silently DROPS those
    // stanzas (they time out — observed live), so every contact photo came
    // back null while self/groups (token-less) worked. For contacts we send
    // the token-less form of the same IQ ourselves, retrying with the
    // peer's LID when the PN form reports no picture. (#pic-tokenless)
    const rawPictureQuery = async (target: string, tcToken?: any): Promise<string | undefined> => {
      const picture: any = { tag: "picture", attrs: { type: picType, query: "url" } };
      // Correctly-nested trusted-contact token (child of <picture>, with its
      // 't' timestamp) — WhatsApp only returns photos for contacts-only-privacy
      // peers when this rides along. Mirrors upstream PR #2607 (rc13 built the
      // node as a sibling with empty attrs, which WA drops → timeout).
      if (tcToken) picture.content = [tcToken];
      const result = await sock.query(
        {
          tag: "iq",
          attrs: { target, to: "@s.whatsapp.net", type: "get", xmlns: "w:profile:picture" },
          content: [picture],
        },
        15000,
      );
      const child = (result?.content || []).find((c: any) => c?.tag === "picture");
      return child?.attrs?.url;
    };
    // Read the stored trusted-contact token for a peer (kept under its LID) and
    // build the <tctoken> node — but only if present and not expired (28-day
    // rolling window, rc13 parity), so we never send a stale token (WA rejects
    // those). Returns undefined → caller falls back to the token-less query.
    const tcTokenNodeFor = async (contactJid: string): Promise<any | undefined> => {
      try {
        let storageJid = contactJid;
        if (contactJid.endsWith("@s.whatsapp.net")) {
          const lid = await sock.signalRepository?.lidMapping?.getLIDForPN?.(contactJid);
          if (lid) storageJid = `${String(lid).split("@")[0].split(":")[0]}@lid`;
        }
        const data = await sock.authState?.keys?.get?.("tctoken", [storageJid]);
        const entry = data?.[storageJid];
        if (!entry?.token?.length) return undefined;
        const ts = Number(entry.timestamp);
        if (!ts || Number.isNaN(ts)) return undefined;
        const BUCKET = 604800, BUCKETS = 4; // ~28-day rolling window
        const cutoff = (Math.floor(Math.floor(Date.now() / 1000) / BUCKET) - (BUCKETS - 1)) * BUCKET;
        if (ts < cutoff) return undefined;
        return { tag: "tctoken", attrs: { t: String(entry.timestamp) }, content: entry.token };
      } catch {
        return undefined;
      }
    };
    let url: string | undefined;
    if (isContactJid) {
      // Resolve the peer's LID lazily and memoize (used by several attempts).
      let lidUser: string | undefined | null = undefined;
      const resolveLid = async (): Promise<string> => {
        if (lidUser !== undefined) return lidUser || "";
        try {
          const lid = await sock.signalRepository?.lidMapping?.getLIDForPN?.(jid);
          lidUser = lid ? `${String(lid).split("@")[0].split(":")[0]}@lid` : null;
        } catch {
          lidUser = null;
        }
        return lidUser || "";
      };
      // 1) token-less PN form (proven path — most contacts have a public photo).
      try {
        url = await rawPictureQuery(jid);
      } catch (err) {
        logger.warn({ err, accountId, jid }, "picture query failed (pn form)");
      }
      // 2) token-less via the peer's LID.
      if (!url) {
        const lu = await resolveLid();
        if (lu) {
          try {
            url = await rawPictureQuery(lu);
          } catch (err) {
            logger.warn({ err, accountId, jid }, "picture query failed (lid form)");
          }
        }
      }
      // 3) tokened retry for contacts-only-privacy peers (PR #2607) — only when
      //    a valid token exists AND the token-less forms found nothing.
      if (!url) {
        const tcToken = await tcTokenNodeFor(jid);
        if (tcToken) {
          try {
            url = await rawPictureQuery(jid, tcToken);
          } catch (err) {
            logger.warn({ err, accountId, jid }, "picture query failed (pn+token)");
          }
          if (!url) {
            const lu = await resolveLid();
            if (lu) {
              try {
                url = await rawPictureQuery(lu, tcToken);
              } catch (err) {
                logger.warn({ err, accountId, jid }, "picture query failed (lid+token)");
              }
            }
          }
        }
      }
    } else {
      try {
        url = await sock.profilePictureUrl(jid, picType, 15000);
      } catch (err) {
        logger.warn({ err, accountId, jid }, "profilePictureUrl failed");
        url = undefined;
      }
    }
    if (!url) return { url: null, image_base64: null, mimetype: null };
    try {
      const response = await fetch(url);
      if (!response.ok) return { url, image_base64: null, mimetype: null };
      const buf = Buffer.from(await response.arrayBuffer());
      const mimetype = response.headers.get("content-type") || "image/jpeg";
      return { url, image_base64: buf.toString("base64"), mimetype };
    } catch {
      return { url, image_base64: null, mimetype: null };
    }
  }

  // ── Phase B3 — subscribe to a contact's presence so WhatsApp pushes
  // online/offline/typing updates. Idempotent: re-subscribing the same
  // JID is a no-op.
  async subscribePresence(accountId: string, jid: string): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") {
      return { ok: false };
    }
    const target = jid.includes("@") ? jid : `${jid}@s.whatsapp.net`;
    let set = this.subscribedPresence.get(accountId);
    if (!set) {
      set = new Set<string>();
      this.subscribedPresence.set(accountId, set);
    }
    if (set.has(target)) return { ok: true };
    try {
      await (session.socket as any).presenceSubscribe(target);
      set.add(target);
    } catch (err) {
      logger.error({ err, accountId, jid: target }, "presenceSubscribe failed");
      return { ok: false };
    }
    return { ok: true };
  }

  async subscribePresenceBulk(
    accountId: string,
    jids: string[],
  ): Promise<{ subscribed: number; total: number }> {
    let subscribed = 0;
    for (const jid of jids) {
      const r = await this.subscribePresence(accountId, jid);
      if (r.ok) subscribed++;
    }
    return { subscribed, total: jids.length };
  }

  // ── Phase C4 — send a presence indicator ('composing' / 'paused' / etc.) ─
  async sendPresenceUpdate(
    accountId: string,
    jid: string,
    state: "available" | "unavailable" | "composing" | "recording" | "paused",
  ): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const target = jid.includes("@") ? jid : `${jid}@s.whatsapp.net`;
    await session.socket.sendPresenceUpdate(state, target);
    return { ok: true };
  }

  // ── Phase C5 — star / unstar a message ────────────────────────────────
  async starMessage(
    accountId: string,
    jid: string,
    msgId: string,
    fromMe: boolean,
    star: boolean,
  ): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const target = jid.includes("@") ? jid : `${jid}@s.whatsapp.net`;
    await session.socket.chatModify(
      { star: { messages: [{ id: msgId, fromMe }], star } },
      target,
    );
    return { ok: true };
  }

  // ── Phase C6 — generate a WhatsApp call link ──────────────────────────
  async createCallLink(
    accountId: string,
    type: "audio" | "video",
  ): Promise<{ token: string; url: string }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const sock: any = session.socket;
    if (typeof sock.createCallLink !== "function") {
      throw new Error("createCallLink not supported by this Baileys version");
    }
    // WhatsApp does not always answer the createCallLink IQ on Baileys
    // 7.0.0-rc10 (same class of upstream flakiness as pair-code). The bare
    // await would hang ~60s on Baileys' internal query timeout — long past
    // Odoo's 30s HTTP read timeout — so the caller just sees a raw
    // ReadTimeout. Race it against a shorter timeout for a clean failure.
    const result = await Promise.race([
      sock.createCallLink({ type }),
      new Promise((_resolve, reject) =>
        setTimeout(
          () => reject(new Error(
            "WhatsApp did not respond to the call-link request (timed out). " +
            "This is a known Baileys rc10 limitation — try again, or use a " +
            "direct chat instead.")),
          18000,
        ),
      ),
    ]);
    const token = (result as any)?.token || result;
    return { token: String(token), url: `https://call.whatsapp.com/${type}/${token}` };
  }

  // ── Reject incoming WhatsApp call ─────────────────────────────────────
  async rejectCall(
    accountId: string,
    callId: string,
    callFrom: string,
  ): Promise<{ ok: boolean }> {
    const session = this.sessions.get(accountId);
    if (!session || session.status !== "connected") throw new Error("Session not connected");
    const sock: any = session.socket;
    if (typeof sock.rejectCall !== "function") {
      throw new Error("rejectCall not supported by this Baileys version");
    }
    if (!callId || !callFrom) {
      throw new Error("call_id and from are required");
    }
    await sock.rejectCall(callId, callFrom);
    return { ok: true };
  }

  // ── Phase D — pairing-code login (alternative to QR scan) ─────────────
  async requestPairingCode(
    accountId: string,
    phone: string,
  ): Promise<{ code: string }> {
    const session = this.sessions.get(accountId);
    if (!session) throw new Error("Session not found — create it first");
    if (session.socket.authState.creds.registered) {
      throw new Error("Account already registered; pairing code only valid before first login");
    }
    // Phone must be digits only, country code included, no `+`.
    const digits = phone.replace(/\D/g, "");
    if (digits.length < 8) throw new Error("Invalid phone number");
    // Mark pairing in flight so the connection-close handler knows to keep
    // the socket dead on a 408 instead of auto-reconnecting (which would
    // generate a fresh ephemeral keypair and invalidate this code).
    session.pairingInProgress = true;
    session.shouldReconnect = false;
    const code = await session.socket.requestPairingCode(digits);
    logger.info({ accountId, phone: digits, code }, "Pairing code issued; awaiting phone-side entry");
    return { code };
  }

  getHealthStatus(): { status: string; sessions: Record<string, string> } {
    const sessions: Record<string, string> = {};
    for (const [id, session] of this.sessions) {
      sessions[id] = session.status;
    }
    return { status: "ok", sessions };
  }

  /** Restore sessions from persisted auth state on startup */
  async restoreSessions() {
    if (!fs.existsSync(this.sessionsDir)) return;
    const dirs = fs
      .readdirSync(this.sessionsDir, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name);

    let restored = 0;
    for (const accountId of dirs) {
      const credsPath = path.join(this.sessionsDir, accountId, "creds.json");
      const configPath = path.join(this.sessionsDir, accountId, "config.json");

      if (!fs.existsSync(credsPath) || !fs.existsSync(configPath)) {
        continue;
      }

      try {
        const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
        if (!config.odoo_base_url || !config.webhook_secret) {
          logger.warn({ accountId }, "Skipping restore: incomplete config.json");
          continue;
        }

        logger.info({ accountId }, "Auto-restoring session");
        await this.createSession(
          accountId,
          config.odoo_base_url,
          config.webhook_secret,
          config.callback_url || undefined,
          config.send_read_receipts !== false,
          config.share_online_presence === true,
          0,
          Boolean(config.capture_contact_statuses),
          Boolean(config.status_mark_seen),
          Boolean(config.sync_own_statuses),
        );
        restored++;

        // Brief delay between restores to avoid overwhelming Baileys
        if (restored < dirs.length) {
          await new Promise<void>((r) => setTimeout(r, 1000));
        }
      } catch (err) {
        logger.error({ err, accountId }, "Failed to auto-restore session");
      }
    }

    if (restored > 0) {
      logger.info({ count: restored }, "Auto-restored sessions");
    }
  }
}
