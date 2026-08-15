// Reference webhook bot template (Node.js, no external deps).
//
// The platform sets:
//   BOT_TOKEN, PORT, WEBHOOK_PATH

const http  = require("node:http");
const https = require("node:https");
const { URL } = require("node:url");

const TOKEN = process.env.BOT_TOKEN || "";
const PORT  = parseInt(process.env.PORT || "5000", 10);
const PATH  = process.env.WEBHOOK_PATH || "/webhook";

function tgPost(method, body) {
  return new Promise((resolve, reject) => {
    const data = new URLSearchParams(body).toString();
    const req = https.request(
      `https://api.telegram.org/bot${TOKEN}/${method}`,
      { method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" } },
      res => {
        let buf = "";
        res.on("data", c => buf += c);
        res.on("end", () => resolve(buf));
      }
    );
    req.on("error", reject);
    req.write(data);
    req.end();
  });
}

async function handleUpdate(update) {
  const msg = update && update.message;
  if (msg && msg.text) {
    await tgPost("sendMessage", {
      chat_id: msg.chat.id,
      text: "👋 أهلاً! بوت Node.js مُستضاف على TikZoom.",
    });
  }
}

const server = http.createServer((req, res) => {
  if (req.method === "POST" && req.url === PATH) {
    let body = "";
    req.on("data", c => body += c);
    req.on("end", async () => {
      try { await handleUpdate(JSON.parse(body)); } catch (e) { /* ignore */ }
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: true }));
    });
    return;
  }
  res.writeHead(404); res.end("not found");
});

server.listen(PORT, "127.0.0.1", () => console.log(`bot listening on ${PORT}${PATH}`));
