# API Integration Guide

## Authentication 認証

API の認証には OAuth 2.0 の client credentials flow を使用する。access token の有効期限は1時間で、期限が切れたら新しい token を取得する。

## Rate limit

各 API key には毎分600リクエストの rate limit がある。上限を超えると HTTP 429 が返されるので、Retry-After ヘッダーの秒数だけ待ってから再試行すること。

## Pagination

一覧系の endpoint は cursor-based pagination を採用している。レスポンスの next_cursor を次のリクエストに渡すと、続きのページを取得できる。

## Webhook

Webhook の送信元を検証するため、すべてのリクエストには HMAC-SHA256 の署名が付与される。署名が一致しないリクエストは破棄すること。

## Versioning

破壊的な変更は新しいメジャーバージョンでのみ行い、旧バージョンは少なくとも12か月間サポートする。
