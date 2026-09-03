import express from "express";
import pino from "pino";
import { SessionManager } from "./services/session-manager.js";
import { createSessionRoutes } from "./routes/session.js";
import { createSendRoutes } from "./routes/send.js";
import { createHealthRoutes } from "./routes/health.js";
import { createWhatsappRoutes } from "./routes/whatsapp.js";
import { createHistoryRoutes } from "./routes/history.js";

const logger = pino({ level: process.env.LOG_LEVEL || "info" });
const PORT = parseInt(process.env.PORT || "3100", 10);
// One shared sidecar process serves every account (and DB), so its auth
// accepts an allow-LIST of keys (comma-separated in API_KEY), not just one.
// A single key stays exactly backward compatible ([k].includes(k) === match);
// an empty API_KEY leaves auth disabled as before.
const API_KEYS = (process.env.API_KEY || "")
  .split(",")
  .map((k) => k.trim())
  .filter(Boolean);
const SESSIONS_DIR = process.env.SESSIONS_DIR || undefined;

const app = express();
app.use(express.json({ limit: "75mb" })); // Large limit for media base64 (50 MB binary ~= 67 MB base64 + overhead)

// API key middleware
if (API_KEYS.length) {
  app.use((req, res, next) => {
    if (req.path === "/health") return next();
    const key = req.headers["x-api-key"];
    if (typeof key !== "string" || !API_KEYS.includes(key)) {
      res.status(401).json({ error: "Invalid API key" });
      return;
    }
    next();
  });
}

const manager = new SessionManager(SESSIONS_DIR);

app.use(createHealthRoutes(manager));
app.use(createSessionRoutes(manager));
app.use(createSendRoutes(manager));
app.use(createWhatsappRoutes(manager));
app.use(createHistoryRoutes(manager));

app.listen(PORT, async () => {
  logger.info({ port: PORT }, "WhatsApp sidecar started");
  await manager.restoreSessions();
});
