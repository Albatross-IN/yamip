import { Router, type Request, type Response } from "express";
import type { SessionManager } from "../services/session-manager.js";

/**
 * History-import endpoints (capture-at-connect model). `:id` is the session
 * key (account id, e.g. "odoo17ee_41").
 *
 *  POST /session/:id/history/resync  → reconnect with history streaming on
 *  GET  /session/:id/history/status  → buffer status (available, progress, counts)
 *  GET  /session/:id/history?cursor=&limit= → drain one page of buffered history
 */
export function createHistoryRoutes(manager: SessionManager): Router {
  const router = Router();

  router.post("/session/:id/history/arm", (req: Request, res: Response) => {
    try {
      res.json(manager.armHistory(req.params.id));
    } catch (err: any) {
      res.status(500).json({ error: err.message });
    }
  });

  router.post("/session/:id/history/resync", async (req: Request, res: Response) => {
    try {
      const result = await manager.resyncHistory(req.params.id);
      if (!result.started) {
        res.status(409).json({ error: result.error || "Could not start history sync" });
        return;
      }
      res.json(result);
    } catch (err: any) {
      res.status(500).json({ error: err.message });
    }
  });

  router.get("/session/:id/history/status", (req: Request, res: Response) => {
    try {
      res.json(manager.getHistoryStatus(req.params.id));
    } catch (err: any) {
      res.status(500).json({ error: err.message });
    }
  });

  router.get("/session/:id/history", async (req: Request, res: Response) => {
    try {
      const cursor = parseInt(String(req.query.cursor ?? "0"), 10) || 0;
      const limit = parseInt(String(req.query.limit ?? "200"), 10) || 200;
      res.json(await manager.readHistoryPage(req.params.id, cursor, limit));
    } catch (err: any) {
      res.status(500).json({ error: err.message });
    }
  });

  return router;
}
