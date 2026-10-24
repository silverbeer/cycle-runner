/**
 * Scrum Master as a swamp behavior (SB-1386 spike).
 *
 * Wraps the three stages of the `scrum-master` CLI in cycle-runner. Each
 * method runs one stage and stores its JSON as swamp data, so a workflow can
 * chain collect -> analyze -> render and every intermediate is versioned.
 * The CLI stays the source of truth; nothing here re-implements a rule.
 *
 * @module
 */
import { z } from "npm:zod@4";

const GlobalArgsSchema = z.object({
  /** cycle-runner checkout, relative to the swamp repo root. */
  projectDir: z.string().default(".."),
  linearClientId: z.string().meta({ sensitive: true }),
  linearClientSecret: z.string().meta({ sensitive: true }),
});

type GlobalArgs = z.infer<typeof GlobalArgsSchema>;

/** The CLI owns these shapes; swamp stores them as-is. */
const JsonDocument = z.record(z.string(), z.unknown());

const RenderedSchema = z.object({
  format: z.enum(["terminal", "markdown", "json"]),
  text: z.string(),
});

interface Context {
  repoDir: string;
  globalArgs: GlobalArgs;
  logger: { info: (msg: string, props?: Record<string, unknown>) => void };
  writeResource: (
    specName: string,
    name: string,
    data: Record<string, unknown>,
  ) => Promise<{ name: string }>;
  readResource: (
    instanceName: string,
  ) => Promise<Record<string, unknown> | null>;
}

/** Run one `scrum-master` stage; throw with its stderr if it fails. */
async function scrumMaster(ctx: Context, args: string[]): Promise<void> {
  const cwd = `${ctx.repoDir}/${ctx.globalArgs.projectDir}`;
  const out = await new Deno.Command("uv", {
    args: ["run", "--quiet", "scrum-master", ...args],
    cwd,
    env: {
      LINEAR_CLIENT_ID: ctx.globalArgs.linearClientId,
      LINEAR_CLIENT_SECRET: ctx.globalArgs.linearClientSecret,
    },
    stdout: "piped",
    stderr: "piped",
  }).output();
  if (!out.success) {
    const err = new TextDecoder().decode(out.stderr).trim();
    throw new Error(`scrum-master ${args[0]} exited ${out.code}: ${err}`);
  }
}

/** Read the latest stored document, or fail naming the method to run first. */
async function latest(
  ctx: Context,
  instance: string,
  producer: string,
): Promise<Record<string, unknown>> {
  const doc = await ctx.readResource(instance);
  if (!doc) throw new Error(`no ${instance} data yet; run ${producer} first`);
  return doc;
}

/** Behavior definition for the Scrum Master pipeline. */
export const behavior = {
  type: "@silverbeer/scrum-master",
  version: "2026.10.24.1",
  globalArguments: GlobalArgsSchema,
  resources: {
    snapshot: {
      description: "Stage 1 output: Linear, GitHub and git state",
      schema: JsonDocument,
      lifetime: "infinite",
      garbageCollection: 10,
    },
    report: {
      description: "Stage 2 output: findings and top actions",
      schema: JsonDocument,
      lifetime: "infinite",
      garbageCollection: 10,
    },
    rendered: {
      description: "Stage 3 output: the report as text",
      schema: RenderedSchema,
      lifetime: "infinite",
      garbageCollection: 10,
    },
  },
  methods: {
    collect: {
      description: "Read Linear, GitHub and git into a snapshot",
      arguments: z.object({}),
      execute: async (_args: Record<string, never>, ctx: Context) => {
        const dir = await Deno.makeTempDir({ prefix: "scrum-" });
        try {
          await scrumMaster(ctx, ["collect", "-o", `${dir}/snapshot.json`]);
          const doc = JSON.parse(await Deno.readTextFile(`${dir}/snapshot.json`));
          return { dataHandles: [await ctx.writeResource("snapshot", "snapshot", doc)] };
        } finally {
          await Deno.remove(dir, { recursive: true });
        }
      },
    },
    analyze: {
      description: "Apply the rules to the latest snapshot",
      arguments: z.object({}),
      execute: async (_args: Record<string, never>, ctx: Context) => {
        const snapshot = await latest(ctx, "snapshot", "collect");
        const dir = await Deno.makeTempDir({ prefix: "scrum-" });
        try {
          await Deno.writeTextFile(`${dir}/snapshot.json`, JSON.stringify(snapshot));
          await scrumMaster(ctx, [
            "analyze", `${dir}/snapshot.json`, "-o", `${dir}/report.json`,
          ]);
          const doc = JSON.parse(await Deno.readTextFile(`${dir}/report.json`));
          return { dataHandles: [await ctx.writeResource("report", "report", doc)] };
        } finally {
          await Deno.remove(dir, { recursive: true });
        }
      },
    },
    render: {
      description: "Render the latest report as text",
      arguments: z.object({
        format: z.enum(["terminal", "markdown", "json"]).default("markdown"),
      }),
      execute: async (
        args: { format: "terminal" | "markdown" | "json" },
        ctx: Context,
      ) => {
        const report = await latest(ctx, "report", "analyze");
        const dir = await Deno.makeTempDir({ prefix: "scrum-" });
        try {
          await Deno.writeTextFile(`${dir}/report.json`, JSON.stringify(report));
          await scrumMaster(ctx, [
            "render", `${dir}/report.json`, "-f", args.format, "-o", `${dir}/out`,
          ]);
          const text = await Deno.readTextFile(`${dir}/out`);
          ctx.logger.info("rendered {format}, {chars} chars", {
            format: args.format,
            chars: text.length,
          });
          return {
            dataHandles: [
              await ctx.writeResource("rendered", "rendered", { format: args.format, text }),
            ],
          };
        } finally {
          await Deno.remove(dir, { recursive: true });
        }
      },
    },
  },
};
