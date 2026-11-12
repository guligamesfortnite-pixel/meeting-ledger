// Phantom's tiny backend: counts what players really type (so rarity is real)
// and keeps leaderboards. Everything is append-only: one blob per answer or
// score, so two players writing at the same moment can never overwrite each other.
import { getStore } from "@netlify/blobs";
import type { Config, Context } from "@netlify/functions";

const ID = /^[a-z0-9]{1,64}$/;
const NAME = /^[a-z0-9]{1,10}$/;
const BOARD = /^(d-\d{4}-\d{2}-\d{2}|vs-(10|15|20)-[a-z0-9]{4,8})$/;
const json = (data: unknown, status = 200) =>
  Response.json(data, { status, headers: { "cache-control": "no-store" } });

export default async (req: Request, context: Context) => {
  const store = getStore({ name: "phantom", consistency: "strong" });
  const url = new URL(req.url);
  const route = context.params.route;

  if (req.method === "GET" && route === "stats") {
    // keys look like a/<question>/<answer>/<uuid>
    const { blobs } = await store.list({ prefix: "a/" });
    const counts: Record<string, Record<string, number>> = {};
    for (const { key } of blobs) {
      const [, q, a] = key.split("/");
      (counts[q] ??= {})[a] = (counts[q][a] ?? 0) + 1;
    }
    return json({ counts });
  }

  if (req.method === "GET" && route === "board") {
    const id = url.searchParams.get("id") ?? "";
    if (!BOARD.test(id)) return json({ error: "bad board" }, 400);
    // keys look like s/<board>/<name>/<score>; a player's best score counts
    const { blobs } = await store.list({ prefix: `s/${id}/` });
    const scores: Record<string, number> = {};
    for (const { key } of blobs) {
      const [, , name, score] = key.split("/");
      scores[name] = Math.max(scores[name] ?? 0, Number(score));
    }
    return json({ scores });
  }

  if (req.method === "POST") {
    let body: Record<string, unknown>;
    try {
      body = await req.json();
    } catch {
      return json({ error: "bad json" }, 400);
    }
    if (route === "answer") {
      const { q, a } = body;
      if (typeof q !== "string" || typeof a !== "string" || !ID.test(q) || !ID.test(a))
        return json({ error: "bad answer" }, 400);
      await store.set(`a/${q}/${a}/${crypto.randomUUID()}`, "1");
      return json({ ok: true });
    }
    if (route === "score") {
      const { board, name, score } = body;
      if (
        typeof board !== "string" || !BOARD.test(board) ||
        typeof name !== "string" || !NAME.test(name) ||
        typeof score !== "number" || !Number.isInteger(score) || score < 0 || score > 200000
      )
        return json({ error: "bad score" }, 400);
      await store.set(`s/${board}/${name}/${score}`, "1");
      return json({ ok: true });
    }
  }
  return json({ error: "not found" }, 404);
};

export const config: Config = { path: "/api/:route" };
