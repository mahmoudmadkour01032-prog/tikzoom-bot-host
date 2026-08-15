<?php
/**
 * Reference webhook bot template (PHP).
 *
 * Run via:  php -S 127.0.0.1:$PORT -t .  (the platform handles the runner)
 *
 * Environment variables provided by the platform:
 *   BOT_TOKEN     — your Telegram token
 *   PORT          — local TCP port to bind on
 *   WEBHOOK_PATH  — path the platform forwards updates to (default /webhook)
 */

$token = getenv('BOT_TOKEN') ?: '';
$path  = getenv('WEBHOOK_PATH') ?: '/webhook';

$reqPath = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH);

if ($reqPath !== $path) {
    http_response_code(404);
    echo json_encode(['ok' => false, 'error' => 'not found']);
    return;
}

$body = file_get_contents('php://input');
$update = json_decode($body, true);

if (isset($update['message']['text'])) {
    $chatId = $update['message']['chat']['id'];
    $url = "https://api.telegram.org/bot{$token}/sendMessage";
    $payload = http_build_query([
        'chat_id' => $chatId,
        'text'    => '👋 أهلاً من بوت PHP المُستضاف على TikZoom.',
    ]);
    $ctx = stream_context_create([
        'http' => ['method' => 'POST', 'header' => 'Content-Type: application/x-www-form-urlencoded',
                   'content' => $payload, 'timeout' => 10]
    ]);
    @file_get_contents($url, false, $ctx);
}

header('Content-Type: application/json');
echo json_encode(['ok' => true]);
