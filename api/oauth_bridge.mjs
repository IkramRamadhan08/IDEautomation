import process from "node:process";

async function readStdin() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8").trim();
  return raw ? JSON.parse(raw) : {};
}

function out(obj) {
  process.stdout.write(JSON.stringify(obj));
}

function fail(message, extra = {}) {
  process.stderr.write(typeof message === "string" ? message : JSON.stringify(message));
  process.exitCode = 1;
}

async function openaiJson({ model, system, user }) {
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) throw new Error("OPENAI_API_KEY is not set");

  const resp = await fetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      input: [
        { role: "system", content: [{ type: "input_text", text: system }] },
        { role: "user", content: [{ type: "input_text", text: user }] },
      ],
      text: { format: { type: "text" } },
    }),
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    return { text: "", error_message: data?.error?.message || `OpenAI error ${resp.status}` };
  }

  const text = data?.output_text || "";
  return { text };
}

async function anthropicJson({ model, system, user }) {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) throw new Error("ANTHROPIC_API_KEY is not set");

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model,
      max_tokens: 4000,
      system,
      messages: [{ role: "user", content: user }],
    }),
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    return { text: "", error_message: data?.error?.message || `Anthropic error ${resp.status}` };
  }

  const text = Array.isArray(data?.content)
    ? data.content.filter((item) => item?.type === "text").map((item) => item.text || "").join("\n")
    : "";
  return { text };
}

async function openrouterJson({ model, system, user }) {
  const apiKey = process.env.OPENROUTER_API_KEY;
  if (!apiKey) throw new Error("OPENROUTER_API_KEY is not set");

  const resp = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiKey}`,
    },
    body: JSON.stringify({
      model,
      messages: [
        { role: "system", content: system },
        { role: "user", content: user },
      ],
    }),
  });

  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    return { text: "", error_message: data?.error?.message || `OpenRouter error ${resp.status}` };
  }

  const text = data?.choices?.[0]?.message?.content || "";
  return { text };
}

async function listModels({ provider }) {
  if (provider === "openai" || provider === "openai-codex") {
    return {
      models: [
        "gpt-5.4-mini",
        "gpt-4o-mini",
        "gpt-4.1-mini",
        "o4-mini",
        "gpt-5.4",
        "gpt-5-mini",
        "gpt-4.1",
        "gpt-4o",
        "o3-mini",
        "o3",
      ],
    };
  }
  if (provider === "anthropic") {
    return {
      models: [
        "claude-3-5-haiku-latest",
        "claude-3-5-sonnet-latest",
        "claude-3-7-sonnet-latest",
        "claude-sonnet-4-0",
        "claude-opus-4-0",
      ],
    };
  }
  if (provider === "openrouter") {
    return {
      models: [
        "openai/gpt-5.4-mini",
        "openai/gpt-4o-mini",
        "openai/gpt-4.1-mini",
        "google/gemini-2.5-flash",
        "anthropic/claude-3.5-haiku",
        "openai/gpt-5.4",
        "openai/gpt-4.1",
        "anthropic/claude-sonnet-4",
        "anthropic/claude-3.7-sonnet",
        "google/gemini-2.5-pro",
        "meta-llama/llama-3.3-70b-instruct",
        "deepseek/deepseek-chat-v3-0324",
        "deepseek/deepseek-r1",
      ],
    };
  }
  return { models: [] };
}

async function main() {
  const command = process.argv[2];
  const payload = await readStdin();

  if (command === "models") {
    out(await listModels(payload));
    return;
  }
  if (command === "openai-byok-json") {
    out(await openaiJson(payload));
    return;
  }
  if (command === "anthropic-byok-json") {
    out(await anthropicJson(payload));
    return;
  }
  if (command === "openrouter-byok-json") {
    out(await openrouterJson(payload));
    return;
  }

  throw new Error(`Unknown bridge command: ${command}`);
}

main().catch((err) => fail(err?.message || String(err)));
