# Retrieval evaluation report

- Run at: 2026-10-12T11:52:44+00:00 (git 3767562)
- Dataset: version 1, 13 documents, 51 queries, sha256 95e54901485e
- Embedding provider: bge-m3 (BAAI/bge-m3 @ 5617a9f61b028005a4858fdac845db406aefb181, device cpu)
- Chunks indexed: 60 (target 120 tokens, overlap 20, min 20)
- Search: BM25 k1=1.2 b=0.75, RRF k=60, fusion depth 50
- Host: Linux-6.17.0-1022-azure-x86_64-with-glibc2.39, Python 3.12.3, 4 CPUs; latency over 3 runs per query

## Quality by query language

| Mode | Slice | Queries | Recall@5 | Recall@10 | Precision@5 | MRR@10 | nDCG@10 |
|---|---|---:|---:|---:|---:|---:|---:|
| keyword | all | 47 | 0.819 | 0.830 | 0.187 | 0.841 | 0.802 |
| keyword | en | 19 | 0.789 | 0.816 | 0.179 | 0.800 | 0.755 |
| keyword | ja | 18 | 0.750 | 0.750 | 0.178 | 0.824 | 0.761 |
| keyword | mixed | 10 | 1.000 | 1.000 | 0.220 | 0.950 | 0.963 |
| semantic | all | 47 | 0.979 | 0.989 | 0.238 | 0.934 | 0.927 |
| semantic | en | 19 | 0.974 | 1.000 | 0.232 | 0.899 | 0.904 |
| semantic | ja | 18 | 0.972 | 0.972 | 0.256 | 0.963 | 0.930 |
| semantic | mixed | 10 | 1.000 | 1.000 | 0.220 | 0.950 | 0.963 |
| hybrid | all | 47 | 0.894 | 0.968 | 0.217 | 0.878 | 0.874 |
| hybrid | en | 19 | 0.868 | 0.947 | 0.211 | 0.813 | 0.815 |
| hybrid | ja | 18 | 0.861 | 0.972 | 0.222 | 0.907 | 0.888 |
| hybrid | mixed | 10 | 1.000 | 1.000 | 0.220 | 0.950 | 0.963 |

## Quality by query type

| Query type | Queries | keyword nDCG@10 | semantic nDCG@10 | hybrid nDCG@10 | keyword Recall@10 | semantic Recall@10 | hybrid Recall@10 |
|---|---:|---:|---:|---:|---:|---:|---:|
| cross_language | 15 | 0.548 | 0.903 | 0.731 | 0.533 | 0.967 | 0.900 |
| distractor | 6 | 0.444 | 0.737 | 0.607 | 0.667 | 1.000 | 1.000 |
| exact_keyword | 23 | 0.951 | 0.938 | 0.944 | 0.978 | 1.000 | 1.000 |
| ja_no_spaces | 17 | 0.747 | 0.926 | 0.881 | 0.735 | 0.971 | 0.971 |
| mixed | 10 | 0.963 | 0.963 | 0.963 | 1.000 | 1.000 | 1.000 |
| multi_relevant | 8 | 0.573 | 0.952 | 0.852 | 0.500 | 0.938 | 0.938 |
| paraphrase | 15 | 0.663 | 0.937 | 0.816 | 0.700 | 0.967 | 0.900 |

## Queries with no relevant passage

The ideal answer is an empty list. Semantic search always returns its nearest
neighbours unless min_similarity is set, so it cannot return nothing here.

| Mode | Queries | Empty results | Mean results returned |
|---|---:|---:|---:|
| keyword | 4 | 1 | 1.2 |
| semantic | 4 | 0 | 10.0 |
| hybrid | 4 | 0 | 10.0 |

## Fusion settings

Semantic and keyword candidates fetched once per query and re-fused offline with
every combination of RRF k, fusion depth and keyword weight (the application uses
weight 1). Best ten by nDCG@10, the single methods, and the current default. With
63 combinations tried on 51 queries, small differences
between the top rows are not meaningful.

| Fusion | k | Depth | Keyword weight | nDCG@10 | Recall@10 | MRR@10 | nDCG en | nDCG ja | nDCG mixed |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| semantic only | - | - | - | 0.927 | 0.989 | 0.934 | 0.904 | 0.930 | 0.963 |
| keyword only | - | - | - | 0.802 | 0.830 | 0.841 | 0.755 | 0.761 | 0.963 |
| rrf | 1 | 10 | 0.25 | 0.924 | 0.989 | 0.936 | 0.904 | 0.924 | 0.963 |
| rrf | 1 | 20 | 0.25 | 0.924 | 0.989 | 0.936 | 0.904 | 0.924 | 0.963 |
| rrf | 1 | 50 | 0.25 | 0.924 | 0.989 | 0.936 | 0.904 | 0.924 | 0.963 |
| rrf | 1 | 10 | 0.5 | 0.921 | 0.989 | 0.926 | 0.911 | 0.907 | 0.963 |
| rrf | 1 | 20 | 0.5 | 0.921 | 0.989 | 0.926 | 0.911 | 0.907 | 0.963 |
| rrf | 1 | 50 | 0.5 | 0.921 | 0.989 | 0.926 | 0.911 | 0.907 | 0.963 |
| rrf | 5 | 10 | 0.25 | 0.919 | 0.989 | 0.926 | 0.904 | 0.911 | 0.963 |
| rrf | 5 | 20 | 0.25 | 0.919 | 0.989 | 0.926 | 0.904 | 0.911 | 0.963 |
| rrf | 5 | 50 | 0.25 | 0.919 | 0.989 | 0.926 | 0.904 | 0.911 | 0.963 |
| rrf | 5 | 10 | 0.5 | 0.902 | 0.989 | 0.906 | 0.870 | 0.903 | 0.963 |
| rrf (default) | 60 | 50 | 1.0 | 0.874 | 0.968 | 0.878 | 0.815 | 0.888 | 0.963 |

## Latency

Wall time of SearchService.search in process (query analysis, query embedding,
database queries, snippets); HTTP overhead is not included.

| Mode | Samples | p50 ms | p95 ms |
|---|---:|---:|---:|
| keyword | 153 | 5.1 | 13.4 |
| semantic | 153 | 185.4 | 200.2 |
| hybrid | 153 | 186.7 | 198.3 |

## Per query

Rank of the first chunk containing a judged passage (- = not in the top 10; for
no-relevant queries, the number of results returned).

| Query | Lang | Text | keyword | semantic | hybrid |
|---|---|---|---:|---:|---:|
| en-01 | en | point-in-time recovery | 2 | 2 | 2 |
| en-02 | en | how long are database snapshots kept | 1 | 1 | 1 |
| en-03 | en | SEV1 postmortem deadline | 1 | 1 | 1 |
| en-04 | en | who keeps the outage response organized | 1 | 1 | 1 |
| en-05 | en | per diem | 1 | 1 | 1 |
| en-06 | en | do I need to keep the receipt for a cheap taxi ride | 1 | 1 | 1 |
| en-07 | en | backup restore procedure for the production database | 5 | 3 | 3 |
| en-08 | en | how often do we practise recovering a database | 2 | 1 | 1 |
| en-09 | en | hardware security keys | 1 | 1 | 1 |
| en-10 | en | what to do if my laptop is lost | 1 | 1 | 1 |
| en-11 | en | minimum password length | 1 | 1 | 1 |
| en-12 | en | internet allowance for working from home | - | 1 | 9 |
| en-13 | en | how many days a week can I work remotely | - | 1 | - |
| en-14 | en | quarterly earnings forecast | 1 results | 10 results | 10 results |
| en-15 | en | Kubernetes pod autoscaling thresholds | 0 results | 10 results | 10 results |
| en-16 | en | status page update frequency | 1 | 4 | 2 |
| en-17 | en | canary deployment error threshold | 1 | 1 | 1 |
| en-18 | en | API rate limit | 1 | 1 | 1 |
| en-19 | en | deadline for installing operating system updates | 1 | 1 | 1 |
| en-20 | en | how to verify webhook signatures | 1 | 1 | 1 |
| en-21 | en | "incident commander" | 1 | 1 | 1 |
| ja-01 | ja | 在宅勤務の申請期限 | 1 | 1 | 1 |
| ja-02 | ja | 通信費手当 | 1 | 1 | 1 |
| ja-03 | ja | 家で仕事をするときに公共のWi-Fiを使ってもいいですか | 1 | 1 | 1 |
| ja-04 | ja | パスワードの最低文字数 | 1 | 1 | 1 |
| ja-05 | ja | パソコンをなくしたときの連絡先 | - | 1 | 6 |
| ja-06 | ja | 障害発生時の第一報 | 1 | 1 | 1 |
| ja-07 | ja | 障害の振り返りはいつまでに行うか | 3 | 1 | 1 |
| ja-08 | ja | 領収書が必要な金額 | 1 | 1 | 1 |
| ja-09 | ja | 経費精算の締め切り | 2 | 1 | 1 |
| ja-10 | ja | タクシー代は精算できますか | 1 | 1 | 1 |
| ja-11 | ja | データベースのバックアップの保存期間 | - | 3 | 6 |
| ja-12 | ja | 不審なメールを受け取ったらどうする | 1 | 1 | 1 |
| ja-13 | ja | 退職者のアカウント無効化 | 1 | 1 | 1 |
| ja-14 | ja | 社員食堂の今週のメニュー | 3 results | 10 results | 10 results |
| ja-15 | ja | 停電時のサーバー対策 | 1 | 1 | 1 |
| ja-16 | ja | 本番データを変更する作業のルール | 1 | 1 | 1 |
| ja-17 | ja | 地震のときエレベーターを使ってよいか | 1 | 1 | 1 |
| ja-18 | ja | 出張の日当はいくら | 1 | 1 | 1 |
| ja-19 | ja | 「振り返り会」 | 1 | 1 | 1 |
| mx-01 | mixed | helm rollback の手順 | 1 | 1 | 1 |
| mx-02 | mixed | access token の有効期限 | 1 | 1 | 1 |
| mx-03 | mixed | HTTP 429 が返ってきたらどうする | 1 | 1 | 1 |
| mx-04 | mixed | values.yaml を変更するときのレビュー | 1 | 1 | 1 |
| mx-05 | mixed | 未完成の機能を隠したまま deploy する方法 | 1 | 1 | 1 |
| mx-06 | mixed | pagination の next_cursor | 1 | 1 | 1 |
| mx-07 | mixed | MFA は必須ですか | 1 | 1 | 1 |
| mx-08 | mixed | PostgreSQL の restore drill の頻度 | 1 | 1 | 1 |
| mx-09 | mixed | API の破壊的変更と versioning | 1 | 1 | 1 |
| mx-10 | mixed | Slack bot の設定方法 | 1 results | 10 results | 10 results |
| mx-11 | mixed | SEV1 のときの status page 更新 | 2 | 2 | 2 |
